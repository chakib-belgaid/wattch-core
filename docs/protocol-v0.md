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
privileged RAPL and powermetrics acquisition
pyJoules and JouleHunter clients
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

## Wire format (v0.2)

Every message is one frame:

```text
magic            4 bytes   "WCH0"
wire major       u16 LE    0
wire minor       u16 LE    2
frame kind       u16 LE
flags            u16 LE    bit 0 reserved for future protobuf payloads
payload length   u32 LE    max 16 MiB
payload          JSON (v0.2)
```

Frame kinds and payloads:

```text
1   HelloRequest      client -> daemon   client_name, client_version, protocol_version
2   HelloResponse     daemon -> client   daemon_name, daemon_version, protocol_version,
                                         node_id, clock_instance_id, sources[]
3   StartCapture      client -> daemon   run_id, interval_ms, source_ids[] (empty = all)
4   StopCapture       client -> daemon   run_id
5   StartCaptureAck   daemon -> client   run_id, accepted_source_ids[]
6   StopCaptureAck    daemon -> client   run_id, batches_sent, samples_sent
7   RecordEvent       client -> daemon   run_id, event_id, kind, labels{}
8   RecordEventAck    daemon -> client   event
10  RawSampleBatch    daemon -> client   run_id, node_id, batch_sequence_no, samples[]
11  EventBatch        daemon -> client   run_id, node_id, batch_sequence_no, events[]
255 Error             daemon -> client   code, message
```

The canonical schema keeps the implemented JSON field semantics: sampling
mode `EVENT`, `StartCapture {run_id, source_ids, interval_ms}`, a run-id-only
`StopCapture`, and numeric error codes. Removed draft-only protobuf fields are
reserved so their tags cannot be reassigned accidentally.

Session flow:

```text
connect
  -> HelloRequest
  <- HelloResponse (includes clock identity and all source descriptors)
  -> StartCapture
  <- StartCaptureAck
  <- RawSampleBatch ... (every interval_ms)
  -> RecordEvent
  <- EventBatch (contains the daemon-stamped event)
  <- RecordEventAck (contains the same event)
     ... additional samples and events ...
  -> StopCapture
  <- StopCaptureAck (after draining in-flight batches)
  (another StartCapture, or disconnect)
```

One capture per connection at a time. Protocol violations are answered with
an Error frame; an unknown source id rejects the StartCapture but keeps the
session alive. `RecordEvent` is valid only during the matching active capture.
The daemon writes its singleton `EventBatch` and matching acknowledgement
adjacently under the connection writer lock, although raw batches may appear
before or after that pair. A well-formed event outside capture receives
`NO_ACTIVE_CAPTURE`; a mismatched active `run_id` is a protocol violation; a
duplicate event id receives `INVALID_REQUEST`.

Error codes:

```text
1   PROTOCOL_VIOLATION
2   UNSUPPORTED_VERSION
3   UNKNOWN_SOURCE_ID
4   CAPTURE_ALREADY_ACTIVE
5   NO_ACTIVE_CAPTURE
6   ACCESS_DENIED
7   INVALID_REQUEST
8   RESOURCE_EXHAUSTED
9   TIMEOUT
100 INTERNAL
```

Control-plane bounds in v0.2:

```text
run_id                     non-empty, no control characters, max 256 UTF-8 bytes
interval_ms                at least 10 for the default daemon policy
source_ids                 unique and no larger than the advertised source set
event_id                   non-empty, unique in a capture, no control characters,
                           max 256 UTF-8 bytes
event kind                 non-empty, no control characters, max 256 UTF-8 bytes
event labels               at most 64 entries
event label key            non-empty, no control characters, max 128 UTF-8 bytes
event label value          no control characters, max 1024 UTF-8 bytes
one connection             one active capture at a time
```

Daemon implementations bound connections, active captures, handshake and
idle reads, capture lifetime, and writes. A limit failure is explicit rather
than silently clamping a request.

## Compatibility

Writers emit v0.2 frames. Readers accept v0.1 and v0.2 frame headers so old raw
traces remain replayable. A v0.1 `HelloResponse` decodes with an empty
`clock_instance_id`; v0.2-only event kinds under a v0.1 header are rejected.

Live socket readers retain a 16 MiB per-frame payload limit. Offline replay has
a separate, explicit migration path for v0.1 raw-trace batches up to 256 MiB
per frame. The larger bound is never used for daemon/client traffic, does not
apply to v0.2 traces, and still rejects unbounded legacy frames.

Trace readability is not the same as live protocol negotiation. A v0.2 daemon
accepts clients with the same `0.2` major/minor version (patch differences are
allowed); it does not negotiate a live v0.1 session. Legacy traces therefore
remain usable for raw evidence, but they cannot provide a daemon clock identity
or authoritative v0.2 event markers.

## Daemon-wide clock

`clock_instance_id` is an opaque identity for one daemon monotonic-clock epoch.
It is stable for the daemon lifetime and changes after a restart. Raw samples
and daemon-stamped events from the same connection use that clock.

For a synchronous backend, the sample instant is taken when its raw collection
call completes. A composite preserves the nearest instant supplied by each
child instead of stamping every child before sequential collection. An
independently updating backend may preserve its producer-receipt instant; the
daemon converts that process-monotonic instant into the same clock epoch.
Backend receipt time must not be described as a native hardware timestamp.

The client never supplies an authoritative timestamp in `RecordEvent`. The
daemon creates:

```text
Event {
  event_id
  run_id
  node_id
  monotonic_ns
  kind
  labels
}
```

Events with different non-empty clock identities must not be aligned by their
raw `monotonic_ns` values without an explicit synchronization anchor.

## Privilege model

The daemon may run with elevated privileges when required by a backend.

Clients should not need elevated privileges.

```text
wattch-powerd
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

For research, an offline `MeasurementBoundary` binds one primary source or an
explicitly disjoint source set to exact descriptor semantics and required
topology. The declaration is stored in trial metadata, not sent to the daemon:
it constrains interpretation without making acquisition policy-aware. See
[Daemon-Based System Energy Impact Study](energy-impact-study.md).

An explicitly disjoint set is not inferred from sibling topology. Each primary
descriptor must carry a shared `wattch.disjoint_group` label and a unique
`wattch.disjoint_member` label. Offline boundary signatures freeze both label
values, and validation rejects a later descriptor that changes either one.

## Sample format

Protocol v0.2 leaves the compact `RawSample` unchanged. A raw sample contains
only:

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

## Raw stream integrity

Clients and artifact readers bind every batch to the capture established by
the control plane:

```text
batch run_id equals the acknowledged run_id
batch node_id equals HelloResponse.node_id
each source_id is advertised and appears at most once per batch
batch and per-source sequence numbers always move forward
per-source monotonic timestamps never regress
StopCaptureAck identity and counts equal the stream actually observed
```

Forward sequence gaps remain valid raw evidence and are reported as quality
signals. Replay, reordering, metadata substitution, and cross-run trace
splicing are integrity failures. `sources.json` must exactly match the source
descriptors embedded in `manifest.json` before an artifact is inspected.

Event-stream integrity is checked independently:

```text
event batch run_id and node_id match the active capture
event run_id and node_id match its containing batch
event_id is unique within the capture
event batch sequence numbers move forward independently of raw batches
RecordEventAck.event equals the event emitted for that request
event monotonic_ns belongs to HelloResponse.clock_instance_id
```

The event and raw streams have separate batch sequence spaces. Comparing their
batch numbers has no meaning; align them only through daemon monotonic time.

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

`INTERVAL_AVERAGE` means the value is an average over a native interval, not
a point observation. Its descriptor must carry
`wattch.interval_duration_source_uid`, linking it to a
`SAMPLE_INTERVAL_DURATION` / `NANOSECOND` / `DELTA` raw source sampled in the
same frame.
`wattch.interval_anchor=ENDS_AT_DAEMON_RECEIPT_TIMESTAMP` declares that the
sample timestamp is the daemon-clock receipt anchor for that native interval.
Receipt delivery jitter means the native interval must not be reconstructed by
independently subtracting every duration from every receipt. New
`powermetrics` descriptors carry
`wattch.interval_timeline_reconstruction=AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V2`.
Offline consumers form one contiguous cumulative-native-duration timeline and
fit one quality-checked affine mapping from its interval ends to all daemon
receipt anchors. V2 preserves V1's two-percent clock-scale drift limit and
ordinary receipt-residual envelope. For long traces, it additionally permits
at most two percent of anchors outside that ordinary envelope. Its hard
envelope is the greater of the ordinary envelope and twenty percent of the
median native interval, capped at 50 ms. A trace exceeding either bound is
invalid. V1 remains
supported unchanged for existing descriptors and permits no such outliers.
Consumers integrate the constant interval average only over its mapped overlap
with an event window; they must not fill gaps, double count, alter raw receipt
timestamps, or trapezoidally interpolate it.

Existing traces whose descriptors say `INSTANT` retain their point-sample
meaning and remain readable. The new temporality changes newly acquired
`powermetrics` evidence; it does not reinterpret legacy descriptors.

Example:

```text
Apple powermetrics CPU/GPU/ANE interval-average power
Apple powermetrics frame elapsed_ns
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
  direct sensor readings from a backend that exposes them

OS_TELEMETRY
  C-state residency, wakeups, context switches

TOOL_DERIVED_ESTIMATE
  Apple powermetrics CPU/GPU/ANE interval-average power estimates and native
  elapsed interval metadata

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
