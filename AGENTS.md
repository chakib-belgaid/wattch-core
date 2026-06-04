# AGENT.md — Wattch Core

## Project role

You are working on **Wattch Core**, the protocol-first measurement spine for Wattch.

The project is not a RAPL wrapper. It is a production-oriented foundation for reproducible energy and system-telemetry capture, with a strict daemon/client boundary and future support for pyJoules, JouleHunter, AI workload benchmarks, and agent-facing energy reports.

## Current project phase

The current phase is the **starting production foundation**.

The goal is to build:

```text
strict protocol
shared core model
deterministic measurement daemon
CLI capture/inspect/validate workflow
replayable trace artifacts
golden tests
```

The first implementation must prove the architecture without hardware noise.

Do **not** start with real RAPL, NVML, PowerAPI, HWPC Sensor, MCP, or impact scoring unless explicitly requested.

## Default target

```text
closed enough to show
not perfect
```

Prefer a small vertical slice that compiles and demonstrates the boundary over a large abstract design that cannot run.

## Hard constraints

### 1. The daemon must stay dumb

The daemon may:

```text
list sources
describe source capabilities
read raw values
timestamp samples
assign sequence numbers
batch samples
stream raw samples
write raw traces
record metadata
```

The daemon must not:

```text
aggregate energy sources
compute derived energy metrics
convert power to energy for benchmark claims
subtract idle baselines
compute joules/token
compare software versions
score impact
make optimization recommendations
generate reports
```

If you are about to add interpretation inside the daemon, stop and move it to a later preprocessing/benchmark/report layer.

### 2. Raw evidence comes first

Preserve raw values and strict metadata.

A sample without a source descriptor is invalid.

A source descriptor must say what the value means:

```text
source_uid
node_id
backend
kind
unit
temporality
evidence_class
clock_domain
sampling_mode
source_path
optional parent_source_uid
```

Do not store repeated strings in hot-path samples. Use source ids.

### 3. Acquisition and interpretation are separate

Keep these layers conceptually separate:

```text
acquisition
preprocessing
benchmark interpretation
claim / impact analysis
reporting
```

This repository currently focuses on acquisition, protocol, deterministic validation, and CLI basics.

### 4. Deterministic daemon first

`wattch-testd` is a first-class component, not a toy.

It must eventually simulate:

```text
multiple sources
nested RAPL-like topologies
CPU package / core / DRAM domains
GPU-like instant power
C-state residency
wakeup counters
counter wraps
trace gaps
source disappearance
clock drift
multi-node-like source metadata
```

The deterministic daemon is how we validate protocol behavior before adding privileged RAPL.

### 5. Privilege boundary is architectural

Future real RAPL support belongs behind a daemon boundary:

```text
rapl-wattchd runs with the required privilege
wattch CLI remains unprivileged
pyJoules remains unprivileged
JouleHunter remains unprivileged
```

Do not add designs that require normal Python users to read RAPL counters directly.

### 6. HWPC Sensor is out of scope for now

Do not integrate HWPC Sensor in the starting phase.

Future direction:

```text
HWPC Sensor socket/import adapter
```

Not:

```text
HWPC Sensor as Wattch core dependency
```

### 7. Avoid architecture theater

Do not add crates, abstractions, traits, or config systems just because they sound clean.

A boundary is justified only when it protects one of these:

```text
protocol stability
privilege separation
hot-path overhead
trace replayability
testability
future backend substitution
```

## Repository map

```text
crates/wattch-core
  Shared domain primitives only.
  No daemon logic. No CLI logic. No hardware backend logic.

crates/wattch-protocol
  Framing, protocol versioning, wire-level constants, protocol envelope utilities.
  Keep protocol compatibility explicit.

crates/wattch-testd
  Deterministic local daemon.
  Used for CLI development, protocol validation, golden tests, and simulated multi-source traces.

crates/wattch-cli
  User-facing CLI.
  Should talk to a daemon over a socket and write reproducible run artifacts.

proto/wattch/v0/wattch.proto
  Canonical schema draft for protocol semantics.
  Do not make incompatible changes casually.

docs/protocol-v0.md
  Protocol rules, invariants, and semantics.

docs/architecture-start.md
  Starting-phase design and scope boundaries.

examples/scenarios
  Deterministic source simulations.

examples/bench
  Benchmark specs for later benchmark-layer work.
```

## Development workflow

For non-trivial tasks:

1. Inspect the existing code and docs first.
2. State the smallest useful vertical slice.
3. Modify only the files needed for that slice.
4. Keep public APIs narrow.
5. Add or update tests when behavior changes.
6. Run formatting and tests before claiming completion.

Preferred commands:

```bash
cargo fmt --all
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
```

If the environment cannot run Rust tools, say so explicitly and still make the smallest coherent patch.

## Rust guidelines

Use stable Rust.

Prefer:

```text
clear domain types
enums for closed protocol concepts
strings/maps only for extension points
small modules
explicit errors
boring code
```

Avoid:

```text
macro-heavy design
large generic abstractions
async everywhere without need
global mutable state
background threads without lifecycle control
panics in library code
silent unit conversions
```

## Protocol rules

The protocol is versioned.

Every frame/message must be interpretable by version.

Hot-path samples should stay compact:

```text
source_id
sequence_no
monotonic_ns
value
```

Metadata belongs in source descriptors and manifests, not in every sample.

Do not introduce breaking protocol changes without updating:

```text
proto/wattch/v0/wattch.proto
docs/protocol-v0.md
examples/scenarios/*
validation tests
```

## Trace artifact rules

A run artifact should eventually look like:

```text
runs/<run-id>/
  manifest.json
  sources.json
  raw.trace.pb
  events.trace.pb
  normalized.trace.pb
  quality.json
  experiment.json
  claims.json
  report.md
```

In the starting phase, JSONL fixtures are acceptable for debugging, but do not treat JSONL as the production hot-path format.

Production direction:

```text
binary framed protocol
protobuf payloads
immutable raw traces
offline replay as authoritative path
```

## Testing priorities

Prioritize tests for:

```text
source descriptor validation
unique source ids
unit/temporality compatibility
frame encoding/decoding
sequence monotonicity
counter wrap scenarios
gap detection scenarios
deterministic daemon reproducibility
CLI artifact creation
```

Do not fake tests that only assert implementation details.

## Naming conventions

Use precise names:

```text
SourceDescriptor
RawSample
RawSampleBatch
EvidenceClass
Temporality
SourceKind
ClockDomain
RunManifest
CaptureSession
```

Avoid vague names:

```text
Data
Info
Thing
Manager
Processor
Analyzer
```

## Dependency policy

Before adding a dependency, ask whether it protects the foundation.

Acceptable early dependencies:

```text
serde
serde_json for readable fixtures only
thiserror
clap for CLI
tracing for daemon/client logs
```

Be cautious with:

```text
heavy async frameworks
large observability stacks
metrics frameworks
database clients
GUI/charting libraries
hardware-specific crates
```

Do not add PowerAPI/HWPC dependencies in this phase.

## What not to build yet

Do not implement these unless explicitly requested:

```text
real RAPL backend
NVIDIA/NVML backend
macOS powermetrics backend
HWPC Sensor adapter
PowerAPI adapter
pyJoules rewrite
JouleHunter rewrite
impact score
AI benchmark harness
MCP server
HTML report generator
cluster coordinator
web dashboard
```

Keep them visible in docs as future directions, not in the starting implementation.

## Acceptance criteria for the first vertical slice

The first serious slice is done when:

```text
wattch-testd can expose deterministic multi-source telemetry over a local socket
wattch-cli can list sources from wattch-testd
wattch-cli can capture a short raw trace artifact
source descriptors are saved with the trace
raw samples refer to source ids only
basic validation rejects malformed descriptors
there is at least one nested CPU/package/core/DRAM scenario
there is at least one GPU-like instant-power source
there is at least one C-state-like gauge source
there is at least one wakeup-like cumulative counter source
```

Do not move to real RAPL before this works.

## Commit style

Use small commits with direct messages:

```text
chore: initialize protocol-first workspace
feat(protocol): add source descriptor model
feat(testd): stream deterministic sample batches
feat(cli): capture raw trace from local daemon
test(core): validate source descriptor invariants
docs: define protocol v0 invariants
```

## Documentation style

Be explicit and practical.

Write docs that explain:

```text
what exists now
what is intentionally out of scope
what invariant must not be broken
what future extension the design protects
```

Do not oversell research claims. The research hypothesis is a bonus; the immediate goal is a robust, credible software architecture foundation.
