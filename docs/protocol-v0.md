# Wattch Protocol v0

## Purpose

Wattch Protocol v0 defines how unprivileged clients communicate with local measurement daemons.

The protocol must support:

```text
strict source metadata
compact hot-path samples
multiple source types
nested source topologies
deterministic testing
future privileged RAPL daemon
future pyJoules and JouleHunter clients
```

The protocol should not encode benchmark interpretation or energy-impact conclusions.

## Planes

Wattch uses two planes.

```text
control plane
  metadata, source descriptors, capabilities, session setup

data plane
  compact raw sample batches and event batches
```

The control plane can be verbose because it is sent rarely.

The data plane must stay compact because it is on the hot path.

## Transport

Starting transport:

```text
local Unix socket
```

Later transports may include TCP or named pipes, but the initial production boundary is a local daemon installed by an administrator.

## Wire format (v0.1)

Every message is one frame:

```text
magic            4 bytes   "WCH0"
wire major       u16 LE    0
wire minor       u16 LE    1
frame kind       u16 LE
flags            u16 LE    bit 0 reserved for future protobuf payloads
payload length   u32 LE    max 16 MiB
payload          JSON (v0.1)
```

Frame kinds and payloads:

```text
1   HelloRequest      client -> daemon   client_name, client_version, protocol_version
2   HelloResponse     daemon -> client   daemon_name, daemon_version, protocol_version,
                                         node_id, sources[]
3   StartCapture      client -> daemon   run_id, interval_ms, source_ids[] (empty = all)
4   StopCapture       client -> daemon   run_id
5   StartCaptureAck   daemon -> client   run_id, accepted_source_ids[]
6   StopCaptureAck    daemon -> client   run_id, batches_sent, samples_sent
10  RawSampleBatch    daemon -> client   run_id, node_id, batch_sequence_no, samples[]
11  EventBatch        reserved
255 Error             daemon -> client   code, message
```

Session flow:

```text
connect
  -> HelloRequest
  <- HelloResponse (includes all source descriptors)
  -> StartCapture
  <- StartCaptureAck
  <- RawSampleBatch ... (every interval_ms)
  -> StopCapture
  <- StopCaptureAck (after draining in-flight batches)
  (another StartCapture, or disconnect)
```

One capture per connection at a time. Protocol violations are answered with
an Error frame; an unknown source id rejects the StartCapture but keeps the
session alive.

Error codes:

```text
1   PROTOCOL_VIOLATION
2   UNSUPPORTED_VERSION
3   UNKNOWN_SOURCE_ID
4   CAPTURE_ALREADY_ACTIVE
5   NO_ACTIVE_CAPTURE
6   ACCESS_DENIED
100 INTERNAL
```

## Privilege model

The daemon may run with elevated privileges when required by a backend.

Clients should not need elevated privileges.

```text
rapl-wattchd
  privileged acquisition

wattch CLI / pyJoules / JouleHunter
  unprivileged clients
```

## Source descriptor

Every raw sample points to a `local_source_id`.

The meaning of that id comes from a `SourceDescriptor`.

Required descriptor fields:

```text
local_source_id
source_uid
node_id
backend
kind
unit
temporality
evidence_class
source_path
clock_domain
sampling_mode
```

Important optional fields:

```text
parent_source_uid
nominal_period_ns
max_value
resolution
labels
```

## Source hierarchy

Nested source topology is represented through `source_path` and `parent_source_uid`.

Example:

```text
cpu/package/0/energy
cpu/package/0/core/0/energy
cpu/package/0/core/1/energy
cpu/package/0/dram/energy
gpu/0/power
cpu/package/0/cstate/c10_residency
scheduler/wakeups
```

The daemon does not aggregate parent and child values.

The preprocessor or benchmark layer may later interpret relationships.

## Sample format

A raw sample contains only:

```text
source_id
sequence_no
monotonic_ns
value
```

No repeated strings.
No units.
No backend names.
No aggregation.
No benchmark semantics.

## Temporality

`CUMULATIVE` means the value usually needs delta computation.

Example:

```text
RAPL package energy counter
wakeup counter
context switch counter
```

`INSTANT` means the value is an instantaneous reading.

Example:

```text
GPU power in watts
wall power in watts
```

`GAUGE` means the value is a state-like measurement.

Example:

```text
C-state residency percent
CPU utilization percent
frequency
```

## Evidence classes

Evidence classes prevent overclaiming.

Examples:

```text
RAW_HARDWARE_COUNTER
  RAPL cumulative energy

RAW_SENSOR_READING
  GPU instantaneous power

OS_TELEMETRY
  C-state residency, wakeups, context switches

TOOL_DERIVED_ESTIMATE
  imported estimate from another tool

MODEL_DERIVED_ESTIMATE
  attribution model output

BENCHMARK_MARKER
  phase start/end, request span, token span
```

## Invariants

The following invariants must hold:

```text
source ids are unique inside a run
source uids are unique inside a run
samples only reference known source ids
sequence numbers are monotonic per source
batch sequence numbers are monotonic per stream
monotonic_ns uses the source clock domain
source descriptors are saved with raw traces
raw traces are replayable offline
```

## Non-goals

The protocol must not contain:

```text
baseline subtraction
CPU + GPU aggregation
joules/token computation
impact scoring
benchmark comparison
optimization advice
report text
```

Those belong to later layers.

## Development note

`proto/wattch/v0/wattch.proto` is the semantic schema draft.

JSON fixtures are acceptable during early development, but the production direction is:

```text
binary frame envelope
protobuf payload
immutable raw trace artifacts
offline replay as authoritative path
```
