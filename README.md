# Wattch Core

Wattch Core is the protocol-first measurement spine for Wattch.

It provides the foundation for privileged energy acquisition daemons, unprivileged clients, deterministic test daemons, and future higher-level tools such as pyJoules and JouleHunter adapters.

## Identity

Wattch is a trace-first benchmark platform for reproducible energy and system-telemetry evaluation of software and AI workloads.

The core question is not only:

```text
How much energy did this program consume?
```

The stronger question is:

```text
What did this software version cause the system to do?
```

That includes boundary-specific system energy, runtime, wakeups, C-state
behavior, scheduling effects, idle recovery, and eventually AI-specific spans
such as requests, tokens, tool calls, and agent steps. It does not imply that
the daemon can isolate a process's private energy.

## Current phase

The production foundation is implemented end to end:

```text
strict protocol v0.2 over local Unix sockets (binary frames, JSON payloads,
daemon-stamped events, and v0.1 raw-trace readability)
clear daemon/client boundary
wattch-powerd: linux-rapl, Apple powermetrics, and synthetic power backends,
plus descriptor-preserving Linux OS-telemetry composition
wattch-testd deterministic daemon for protocol validation and golden tests
CLI capture/inspect/validate workflow with replayable trace artifacts
local MCP stdio adapter for agent-owned raw capture windows
pywattch Python client: updated pyJoules-compatible API
matched C0/C1/C2 experiment scheduling, raw trial artifacts, boundary-aware
offline energy outcomes, paired effects, and a control-only dynamic baseline
pyinstrument energy profiler (time-share attribution, clearly labelled)
energy tests: discovery + runner CLI + VSCode "Energy Tests" extension
```

The repository implements the measurement and analysis machinery; it does not
claim that the ranking-reversal hypothesis has already been established on real
hardware.

## Why the daemon boundary matters

Modern Linux RAPL access may require elevated privileges. Wattch should therefore separate privileged acquisition from unprivileged user tooling:

```text
root/admin side:
  wattch-powerd
    reads protected RAPL counters or powermetrics values
    exposes Wattch protocol over a local socket

user side:
  wattch CLI
  pyJoules Wattch backend
  JouleHunter Wattch frontend
  benchmark runners
  AI evaluation tools
```

The daemon is the privilege and acquisition boundary. The research measurement
boundary is the explicitly selected source domain. Clients should not need
root.

## What this repository contains

```text
crates/wattch-core
  shared domain primitives: source descriptors, units, evidence classes, samples, manifests, validation

crates/wattch-protocol
  frame envelope, typed v0.2 messages, v0.1 trace decoding, stream helpers

crates/wattch-daemon
  shared daemon runtime: socket server, session state machine, batching, sequence numbers

crates/wattch-powerd
  powermeter daemon: linux-rapl, Apple powermetrics, synthetic, and raw
  Linux OS telemetry through one descriptor-preserving composite backend

crates/wattch-testd
  deterministic daemon serving scenario files over the same protocol

crates/wattch-cli
  wattch sources / capture / run / inspect / validate against a live daemon

crates/wattch-mcp
  bounded MCP stdio tools for source discovery, stateful raw capture, and replay

clients/python
  pywattch: protocol client, pyJoules-compatible meter API, handlers,
  pyinstrument energy profiler, energy test discovery, experiment scheduling,
  immutable trial artifacts, and offline causal/model analysis primitives

editors/vscode-energy-tests
  VSCode extension: Energy Tests view and results tab

packaging/
  systemd unit for wattch-powerd

proto/wattch/v0/wattch.proto
  canonical protocol schema draft

docs/protocol-v0.md     protocol rules, wire format, invariants
docs/powerd.md          powermeter daemon install and privilege model
docs/energy-impact-study.md  boundary-specific causal study and falsification plan
docs/energy-tests.md    energy test conventions and semantics
docs/mcp.md             local agent registration, tools, and safety boundary
docs/architecture-start.md

examples/scenarios      deterministic multi-source scenarios
examples/energy-tests   demo energy test suite
examples/bench          benchmark specs used later by the benchmark layer
```

## Non-goals (still later layers)

```text
NVML acquisition
PowerAPI or HWPC Sensor adapters
impact scoring
HTML reports
cluster coordinator
per-process energy allocation
absolute meter-accuracy claims without external calibration
```

## Architecture rule

The acquisition daemon must stay dumb.

It may:

```text
list sources
describe source metadata
read raw values
timestamp samples
assign sequence numbers
batch samples
stamp benchmark events
stream or write raw traces
```

It must not:

```text
aggregate CPU + GPU
compute joules/token
subtract idle baselines
compare benchmark versions
infer energy impact
score quality
generate reports
```

Interpretation belongs to offline preprocessing, benchmarking, claim analysis, and reporting.

## Quick start

Daemon + CLI:

```bash
cargo build --workspace

# Private runtime directory: daemon and clients share this path. This also
# makes auto-discovery work on macOS and other hosts without XDG_RUNTIME_DIR.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$(mktemp -d)}"

# Privilege-free, cross-platform development daemon.
./target/debug/wattch-powerd --backend synthetic --os-telemetry off &

./target/debug/wattch sources
./target/debug/wattch capture --out runs --duration-ms 2000
./target/debug/wattch inspect --run runs/<run-id>

# Shell-free command capture for automation and agents. Child output is kept in
# the artifact, while --json emits one stable summary object.
./target/debug/wattch run \
  --out runs \
  --run-id example-command-r01 \
  --label model=example \
  --json \
  -- /usr/bin/true
```

`wattch run` writes `command.json`, `target.stdout.log`, and
`target.stderr.log` beside the ordinary manifest and raw/event traces. Its
daemon-stamped `TARGET_SPAWN`, `TARGET_EXEC`, and `TARGET_EXIT` markers bracket
the command with raw samples; interval-average sources retain a bounded native
period tail. A nonzero command still produces a finished artifact and is
returned as the CLI exit code. These are system/source measurements, not
per-process energy allocation.

Local coding-agent access uses the same unprivileged client/artifact boundary:

```bash
cargo build --release -p wattch-mcp
codex mcp add wattch -- \
  "$PWD/target/release/wattch-mcp" \
  --workspace-root "$PWD" \
  --runs-dir "$PWD/runs"
```

Use `--socket /absolute/path.sock` in the registered command when the daemon is
not in a trusted auto-discovery path. See [docs/mcp.md](docs/mcp.md) for the
tool contract. The MCP adapter returns raw descriptors and integrity summaries;
it does not compute or compare energy.

Real Linux acquisition composes RAPL with the raw OS signals that are readable
on the host:

```bash
sudo ./target/debug/wattch-powerd \
  --backend linux-rapl \
  --os-telemetry auto
```

On macOS, `--backend apple-powermetrics` exposes separate CPU/GPU/ANE
`TOOL_DERIVED_ESTIMATE` interval-average sources plus each plist frame's raw
`elapsed_ns`, and requires the privilege needed by `/usr/bin/powermetrics`.
Offline study analysis synchronizes the contiguous native-duration timeline to
all daemon receipt anchors, rejects excessive drift or receipt residuals, and
integrates exact interval/window overlap—never a trapezoid between receipt
points. It is a separate measurement boundary from RAPL and must be calibrated
on the actual host before empirical use;
deterministic parser and lifecycle tests are not a privileged live-meter
validation.

Python (updated pyJoules API):

```bash
cd clients/python && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,profiler]'
```

Add the `analysis` extra when publishing `features.parquet`:

```bash
cd clients/python && .venv/bin/pip install -e '.[analysis]'
```

```python
from pywattch import EnergyMeter

with EnergyMeter() as meter:
    workload()
print(meter.get_trace().total_joules)
```

Energy tests (and the VSCode "Energy Tests" extension on top of them):

```bash
python -m pywattch.energytest run --root examples/energy-tests --json
```

Deterministic daemon for protocol work and golden tests:

```bash
cargo run -p wattch-testd -- --scenario examples/scenarios/laptop_cpu_gpu_nested.json
```

Verification gate:

```bash
cargo fmt --all && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
clients/python/.venv/bin/python -m pytest clients/python/tests
cd editors/vscode-energy-tests && npm run build && npm test
```

### Development

Run `just --list` to see the available recipes (`build`, `test`, `test-rust`, `test-python`, `test-ext`, `lint`, `fmt`) — they wrap the commands above. CI runs `cargo fmt`, `cargo clippy`, and the Rust/Python/extension test suites on every push to `main` and every pull request.

## Layering

```text
pyJoules-style user API / JouleHunter-style profiler / energy tests / VSCode
  ↓
pywattch client (unprivileged)
  ↓
wattch-powerd local socket (privileged when the backend needs it)
  ↓
linux-rapl counters / Apple powermetrics estimates / raw Linux OS telemetry /
synthetic evidence / future backends
```

For system-impact research, the runner captures all relevant raw sources but
freezes one `MeasurementBoundary` as the energy outcome. RAPL package, RAPL
PSys, and Apple `powermetrics` therefore produce boundary-qualified results;
none is automatically called whole-machine electrical energy. See
[the energy-impact study](docs/energy-impact-study.md).

Future HWPC Sensor integration remains out of scope. It may later become a socket/import adapter, not a core dependency.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or http://www.apache.org/licenses/LICENSE-2.0)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or http://opensource.org/licenses/MIT)

at your option.
