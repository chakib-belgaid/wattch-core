# Wattch Starting Architecture

## Positioning

Wattch Core starts as a protocol-first, daemon/client measurement foundation.

The immediate goal is not to prove the full energy-impact hypothesis.

The immediate goal is to prove that Wattch can provide a robust production-grade foundation for energy and system-telemetry capture:

```text
strict protocol
privileged daemon boundary
unprivileged client UX
deterministic validation
trace replayability
future-safe extension path
```

## Starting components

```text
wattch-core
  Domain primitives and validation.

wattch-protocol
  Frame envelope and protocol constants.

wattch-testd
  Deterministic daemon for protocol and trace validation.

wattch-cli
  Client for source discovery, capture, inspection, and validation.
```

## First vertical slice

The first useful slice is:

```text
wattch-testd validates a deterministic scenario
wattch-testd exposes source descriptors
wattch-testd generates repeatable raw sample batches
wattch-cli lists sources
wattch-cli captures raw batches into a run artifact
validation rejects malformed descriptors or unknown source ids
```

## Why deterministic first

Real hardware introduces noise, permissions, platform differences, clock issues, and backend-specific errors.

The deterministic daemon lets the project validate:

```text
protocol semantics
multi-source traces
nested topologies
sample batching
counter temporality
C-state-like gauges
wakeup-like counters
fault injection later
CLI artifact layout
```

before adding RAPL.

## Future RAPL daemon

The future RAPL backend should be exposed as a daemon:

```text
rapl-wattchd
  reads protected RAPL counters
  exposes Wattch protocol
```

Clients remain unprivileged:

```text
wattch CLI
pyJoules Wattch backend
JouleHunter Wattch frontend
AI benchmark harness
```

## pyJoules and JouleHunter direction

pyJoules should become a Python client UX over Wattch Core.

JouleHunter should become a Python profiling frontend that correlates code regions with Wattch trace sessions and markers.

They should not own low-level privileged RAPL access in the future architecture.

## HWPC Sensor direction

HWPC Sensor is out of scope for the starting phase.

Later it can be added as:

```text
socket adapter
trace import adapter
external evidence source
```

It should not shape the core protocol today.

## Out of scope now

```text
real RAPL backend
GPU backend
macOS backend
PowerAPI adapter
HWPC adapter
impact claim engine
benchmark statistics
AI benchmark runner
MCP server
report generator
cluster coordinator
```

## Acceptance criteria before RAPL

Do not implement real RAPL until the following are working:

```text
source descriptor model
source validation
frame envelope
deterministic scenario generation
multi-source deterministic batches
nested CPU-like topology
C-state-like gauge
wakeup-like cumulative counter
CLI can capture and write a raw artifact
basic golden tests
```
