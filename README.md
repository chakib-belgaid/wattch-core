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

That includes direct energy, runtime, wakeups, C-state behavior, scheduling effects, idle recovery, and eventually AI-specific spans such as requests, tokens, tool calls, and agent steps.

## Current phase

The production foundation is implemented end to end:

```text
strict protocol v0.1 over local Unix sockets (binary frames, JSON payloads)
clear daemon/client boundary
wattch-powerd powermeter daemon: linux-rapl (powercap) + synthetic backends
wattch-testd deterministic daemon for protocol validation and golden tests
CLI capture/inspect/validate workflow with replayable trace artifacts
pywattch Python client: updated pyJoules-compatible API
pyinstrument energy profiler (time-share attribution, clearly labelled)
energy tests: discovery + runner CLI + VSCode "Energy Tests" extension
```

## Why the daemon boundary matters

Modern Linux RAPL access may require elevated privileges. Wattch should therefore separate privileged acquisition from unprivileged user tooling:

```text
root/admin side:
  rapl-wattchd
    reads protected RAPL counters
    exposes Wattch protocol over a local socket

user side:
  wattch CLI
  pyJoules Wattch backend
  JouleHunter Wattch frontend
  benchmark runners
  AI evaluation tools
```

The daemon is the measurement boundary. The clients should not need root.

## What this repository contains

```text
crates/wattch-core
  shared domain primitives: source descriptors, units, evidence classes, samples, manifests, validation

crates/wattch-protocol
  frame envelope, typed v0.1 messages, stream read/write helpers

crates/wattch-daemon
  shared daemon runtime: socket server, session state machine, batching, sequence numbers

crates/wattch-powerd
  powermeter daemon: linux-rapl (powercap sysfs) and synthetic backends

crates/wattch-testd
  deterministic daemon serving scenario files over the same protocol

crates/wattch-cli
  wattch sources / capture / inspect / validate against a live daemon

clients/python
  pywattch: protocol client, pyJoules-compatible meter API, handlers,
  pyinstrument energy profiler, energy test discovery + runner CLI

editors/vscode-energy-tests
  VSCode extension: Energy Tests view and results tab

packaging/
  systemd unit for wattch-powerd

proto/wattch/v0/wattch.proto
  canonical protocol schema draft

docs/protocol-v0.md     protocol rules, wire format, invariants
docs/powerd.md          powermeter daemon install and privilege model
docs/energy-tests.md    energy test conventions and semantics
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
benchmark statistics
MCP server
HTML reports
cluster coordinator
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

# powermeter daemon (auto picks linux-rapl when available, else synthetic)
./target/debug/wattch-powerd --socket /tmp/wattch-powerd.sock &

./target/debug/wattch sources --socket /tmp/wattch-powerd.sock
./target/debug/wattch capture --socket /tmp/wattch-powerd.sock --out runs --duration-ms 2000
./target/debug/wattch inspect --run runs/<run-id>
```

Python (updated pyJoules API):

```bash
cd clients/python && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,profiler]'
```

```python
from pywattch import EnergyMeter

with EnergyMeter(socket_path="/tmp/wattch-powerd.sock") as meter:
    workload()
print(meter.get_trace().total_joules)
```

Energy tests (and the VSCode "Energy Tests" extension on top of them):

```bash
python -m pywattch.energytest run --root examples/energy-tests \
    --socket /tmp/wattch-powerd.sock --json
```

Deterministic daemon for protocol work and golden tests:

```bash
cargo run -p wattch-testd -- --scenario examples/scenarios/laptop_cpu_gpu_nested.json --socket /tmp/wattch-testd.sock
```

Verification gate:

```bash
cargo fmt --all && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
clients/python/.venv/bin/python -m pytest clients/python/tests
cd editors/vscode-energy-tests && npm run build && npm test
```

## Layering

```text
pyJoules-style user API / JouleHunter-style profiler / energy tests / VSCode
  ↓
pywattch client (unprivileged)
  ↓
wattch-powerd local socket (privileged when the backend needs it)
  ↓
raw counters: linux-rapl powercap, synthetic, future NVML/...
```

Future HWPC Sensor integration remains out of scope. It may later become a socket/import adapter, not a core dependency.

## License

TBD.
