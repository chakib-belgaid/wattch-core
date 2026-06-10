# Wattch production foundation — design

Date: 2026-06-10
Status: approved for implementation (autonomous goal session)

## Goal

Make wattch-core production ready by delivering four subsystems on top of the
existing protocol-first skeleton:

1. A powermeter daemon (`wattch-powerd`) behind the existing daemon/client
   boundary, with a privileged Linux RAPL backend and an unprivileged
   synthetic backend for development on other platforms.
2. A new Python client (`pywattch`) that provides an updated
   pyJoules-compatible API (energy meter, decorator, context manager,
   handlers) backed by the Wattch daemon socket instead of direct RAPL reads.
3. A pyinstrument wrapper (`pywattch.profiler`) that profiles Python code and
   attributes measured energy across the call tree (JouleHunter-style).
4. A VSCode extension ("Energy Tests") that discovers energy tests, runs them
   through `pywattch`, and shows per-test energy results in a dedicated tab —
   the unit-test experience, but for joules.

All interpretation (deltas, joules, attribution) happens in clients. Daemons
stay dumb, per the architecture rule.

## Decomposition and build order

```text
A. protocol v0.1 wire messages + Unix socket transport (Rust)
B. wattch-testd serving deterministic scenarios over the socket
C. wattch-powerd with backend trait: linux-rapl (sysfs powercap) + synthetic
D. wattch CLI: sources / capture / inspect / validate against a live daemon
E. pywattch: protocol codec, client, pyJoules-compatible meter API, handlers
F. pywattch.profiler: pyinstrument wrapper with time-share energy attribution
G. pywattch.energytest: discovery + runner CLI emitting JSON
H. editors/vscode-energy-tests: tree view + results tab driven by (G)
```

Each stage is independently testable; later stages consume earlier ones only
through their public boundary (socket protocol, Python API, runner JSON).

## A. Protocol v0.1

The existing binary frame envelope (`WCH0` magic, 16-byte header) is kept.
Payloads are JSON-serialized protocol messages for v0.1; the frame `flags`
field reserves bit 0 (`FLAG_PAYLOAD_PROTOBUF`) so protobuf payloads can be
introduced later without breaking the envelope. This honours the "binary
framed protocol" production direction while keeping v0.1 debuggable.

Frame kinds (existing values unchanged, two added):

```text
HelloRequest      = 1   client -> daemon  {client_name, client_version, protocol_version}
HelloResponse     = 2   daemon -> client  {daemon_name, daemon_version, protocol_version,
                                           node_id, sources: [SourceDescriptor]}
StartCapture      = 3   client -> daemon  {run_id, interval_ms, source_ids (empty = all)}
StopCapture       = 4   client -> daemon  {run_id}
StartCaptureAck   = 5   daemon -> client  {run_id, accepted_source_ids}
StopCaptureAck    = 6   daemon -> client  {run_id, batches_sent, samples_sent}
RawSampleBatch    = 10  daemon -> client  RawSampleBatch (existing core model)
EventBatch        = 11  reserved
Error             = 255 daemon -> client  {code, message}
```

Session flow: connect → Hello exchange → optional StartCapture →
RawSampleBatch stream at `interval_ms` → StopCapture → StopCaptureAck →
either a new capture or disconnect. One capture per connection at a time.
Invariants from `docs/protocol-v0.md` (monotonic sequence numbers per source,
monotonic batch sequence per stream, samples reference known ids) are
enforced by daemon code and checked in tests.

Transport: Unix domain socket (`std::os::unix::net`), blocking I/O, one
thread per connection. No async runtime — connection counts are tiny.

New code: `wattch-protocol/src/messages.rs` (typed messages + JSON
encode/decode into frames), `read_frame`/`write_frame` helpers over
`Read`/`Write`.

## B/C. Daemons

A new `crates/wattch-daemon` library crate holds the shared serving runtime,
justified by backend substitution and testability (testd and powerd must
speak identical protocol):

```text
trait SampleBackend: Send {
    fn descriptors(&self) -> &[SourceDescriptor];
    fn sample(&mut self) -> Vec<(u32, RawValue)>;   // one reading per source
}
serve(listener, backend_factory, daemon_identity)
```

The runtime owns timestamps, sequence numbers, batching, and the session
state machine. Backends only read raw values — the dumb-daemon rule lives in
the trait shape.

- `wattch-testd` becomes a real daemon: `DeterministicBackend` replays the
  existing linear-behavior scenarios over the socket. `--dry-run` keeps the
  golden-fixture output.
- `wattch-powerd` (new crate) is the powermeter daemon:
  - `linux-rapl` backend reads `energy_uj`/`max_energy_range_uj`/`name` from
    a powercap root (default `/sys/class/powercap`, overridable via
    `--powercap-root` so it is unit-testable on any OS with a fake tree).
    Emits CUMULATIVE / MICROJOULE / RAW_HARDWARE_COUNTER descriptors with
    package → subdomain (core/uncore/dram) parent links and `max_value` for
    wrap handling.
  - `synthetic` backend for unprivileged development on macOS/CI.
  - Backend selection: `--backend auto|linux-rapl|synthetic` (auto picks
    linux-rapl when the powercap tree exists, otherwise synthetic with a
    warning).
  - Production bits: clap CLI, tracing logs, stale-socket cleanup, clean
    shutdown on SIGINT/SIGTERM, example systemd unit + install docs
    (`docs/powerd.md`) covering the privilege model (daemon as root or with
    read access to powercap; socket group-writable for users).

## D. CLI

`wattch sources --socket …` prints the descriptor table from HelloResponse.
`wattch capture --socket … --out runs/ --duration-ms … [--interval-ms …]`
writes a run artifact:

```text
runs/<run-id>/
  manifest.json   RunManifest (existing model)
  sources.json    descriptors
  raw.trace.wch   framed binary RawSampleBatch frames (replayable)
```

`wattch inspect --run runs/<run-id>` replays the artifact offline and prints
per-source sample counts, time span, sequence/gap checks.
`wattch validate --input <scenario|manifest>` runs core validation.

## E. pywattch (clients/python/pywattch)

Pure-stdlib protocol implementation (no Rust binding needed):

```text
pywattch/_frames.py     envelope encode/decode (mirrors WCH0 framing)
pywattch/_messages.py   protocol message dataclasses + JSON codec
pywattch/client.py      WattchClient: connect/hello/sources/start/stop,
                        read_batches iterator
pywattch/meter.py       updated pyJoules API
pywattch/handlers.py    PrintHandler, CSVHandler, callable handlers
```

Updated pyJoules-compatible surface (same concepts, daemon-backed):

```python
from pywattch import EnergyMeter, EnergyContext, measure_energy

meter = EnergyMeter(socket_path=...)        # devices = daemon sources
meter.start(tag="foo"); ...; meter.record(tag="bar"); ...; meter.stop()
trace = meter.get_trace()                   # samples: tag, duration, energy per domain

@measure_energy(handler=CSVHandler("out.csv"))
def foo(): ...

with EnergyContext(handler=PrintHandler(), tag="phase1") as ctx:
    ...
    ctx.record(tag="phase2")
```

Energy is computed client-side: CUMULATIVE sources → last-first delta with
wrap correction via `max_value`; unit normalization (µJ → J) via descriptor
unit. INSTANT power sources → trapezoidal integration to joules, reported
separately as estimates. This is interpretation, so it lives in the client.

Capture mechanics: `EnergyMeter.start` opens a client connection and a
background reader thread accumulating batches; `record`/`stop` snapshot
per-source readings at marker time (nearest sample at/after the marker).

Packaging: `pyproject.toml` (hatchling or setuptools), `pywattch` version
0.1.0, extras: `profiler` (pyinstrument), `dev` (pytest). Tests use a
pure-Python fake daemon speaking protocol v0.1 over a temp Unix socket, plus
an opt-in integration test against a cargo-built wattch-testd when available.

## F. pywattch.profiler

`EnergyProfiler` wraps `pyinstrument.Profiler` and an `EnergyMeter` session
over the same interval. On stop it walks pyinstrument's session frame tree
and attributes total measured joules to frames proportionally to self-time
(energy_share = self_time / total_time). Output:

```python
with EnergyProfiler(socket_path=...) as ep: work()
print(ep.render_text())   # call tree annotated with J and %
ep.to_dict()              # JSON-able: frames, time, attributed_joules
```

Attribution is clearly labelled MODEL_DERIVED_ESTIMATE in output — time-share
attribution is an estimate, not a hardware measurement.

## G. Energy tests

Convention (mirrors pytest): files matching `energy_test_*.py` or
`test_*_energy.py`; test callables named `energy_test_*` or decorated with
`@pywattch.energytest.energy_test(budget_joules=None, repeat=1)`.

Runner CLI:

```text
python -m pywattch.energytest discover --root <dir> --json
python -m pywattch.energytest run --root <dir> [--test FILE::NAME]
       [--socket …] [--repeat N] --json
```

Run output per test: status (passed / failed / error / over-budget), wall
time, joules per energy domain, total joules, repeat statistics (median),
captured exception text. A test passes if it raises nothing and is within
its optional joule budget — that budget is what makes energy tests
assertions rather than mere measurements.

## H. VSCode extension (editors/vscode-energy-tests)

TypeScript, compiled with tsc, no bundler. Contributions:

- Activity-bar container **Energy Tests** with a tree view of discovered
  tests (workspace-wide, via the `discover --json` runner; refresh command,
  file watcher on `**/energy_test*.py` and `**/*_energy.py`).
- Commands: Run All, Run Test, Refresh, Open Results.
- A webview panel tab titled **Energy Tests** showing a results table:
  test, status icon, wall time, total J, per-domain J, budget verdict —
  unit-test UX with energy columns. Re-running updates the panel in place.
- Settings: `energyTests.pythonPath`, `energyTests.socketPath`,
  `energyTests.rootDir`, `energyTests.repeat`.
- The extension only shells out to the runner and renders JSON; all
  measurement logic stays in Python. Runner-output parsing is covered by
  `node --test` unit tests; the extension host glue is kept thin.

## Error handling

- Daemon: protocol errors answered with Error frames, connection dropped on
  framing violations; SIGTERM closes the listener and unlinks the socket.
- Client: explicit exceptions (`WattchConnectionError`, `WattchProtocolError`);
  meter stop is idempotent; reader thread failures surface on `stop()`.
- Extension: daemon-unreachable and runner-crash states render as actionable
  messages in the results panel, never silent empty tables.

## Testing

- Rust: existing unit tests + message round-trips, daemon session integration
  tests over real Unix sockets (testd and powerd-synthetic), linux-rapl
  backend tests against a fake powercap tree, CLI artifact golden test,
  wrap/gap fixtures. `cargo fmt`, `clippy -D warnings`, `cargo test`.
- Python: pytest against the fake daemon; wrap correction, unit conversion,
  marker math, profiler attribution on a synthetic tree, energytest
  discovery/run including budget failures.
- Extension: `tsc --noEmit`-clean build plus `node --test` for runner JSON
  parsing/model code.

## Out of scope (unchanged)

NVML, PowerAPI/HWPC adapters, impact scoring, MCP server, HTML reports,
cluster coordination, protobuf payloads (envelope reserves the flag only).
