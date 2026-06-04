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

This repository starts after the MVP validation phase.

The goal of the first production foundation is:

```text
strict protocol
clear daemon/client boundary
deterministic energy and telemetry daemon
CLI capture/inspect/validate workflow
replayable trace artifacts
future-safe path for privileged RAPL acquisition
```

This phase does **not** start with real RAPL.

Real RAPL comes after the deterministic daemon validates the protocol and trace semantics.

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
  protocol framing primitives and wire-version constants

crates/wattch-testd
  deterministic daemon used to validate protocol, CLI, and trace handling before real hardware support

crates/wattch-cli
  user-facing command skeleton for source discovery, capture, inspection, and validation

proto/wattch/v0/wattch.proto
  canonical protocol schema draft

docs/protocol-v0.md
  protocol rules and invariants

docs/architecture-start.md
  starting-phase architecture

examples/scenarios
  deterministic multi-source scenarios

examples/bench
  benchmark specs used later by the benchmark layer
```

## Non-goals for the initial commit

The first commit intentionally does not implement:

```text
real RAPL acquisition
NVML acquisition
PowerAPI or HWPC Sensor adapters
impact scoring
benchmark statistics
MCP server
HTML reports
cluster coordinator
pyJoules rewrite
JouleHunter rewrite
```

Those are later layers.

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

This initial commit is a foundation skeleton. The intended development loop is:

```bash
cargo fmt --all
cargo test --workspace
cargo run -p wattch-testd -- --scenario examples/scenarios/laptop_cpu_gpu_nested.json --socket /tmp/wattch-testd.sock
cargo run -p wattch-cli -- sources --socket /tmp/wattch-testd.sock
```

The deterministic daemon is the first acceptance target. It should make it possible to test multi-source traces without privileged hardware access.

## Compatibility direction

Future pyJoules integration:

```text
pyJoules user API
  ↓
Wattch client
  ↓
rapl-wattchd local socket
  ↓
privileged RAPL read
```

Future JouleHunter integration:

```text
JouleHunter profiler
  ↓
Wattch capture session markers
  ↓
rapl-wattchd samples
  ↓
offline trace/profile correlation
```

Future HWPC Sensor integration is out of scope for now. It may later become a socket/import adapter, not a core dependency.

## License

TBD.
