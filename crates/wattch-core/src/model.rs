use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Distinguishes what kind of claim a value can support.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EvidenceClass {
    RawHardwareCounter,
    RawSensorReading,
    OsTelemetry,
    ToolDerivedEstimate,
    ModelDerivedEstimate,
    BenchmarkMarker,
}

/// How a numeric value should be interpreted over time.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Temporality {
    /// Monotonically increasing counter, usually requiring delta computation.
    Cumulative,
    /// Instantaneous reading, for example watts at a timestamp.
    Instant,
    /// Average value over a source-reported interval. The descriptor must
    /// identify a raw nanosecond duration source and its timestamp anchor.
    IntervalAverage,
    /// Already a delta over a known interval.
    Delta,
    /// State-like value, for example utilization or C-state residency percentage.
    Gauge,
    /// Event-like counter or marker.
    Event,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Unit {
    Joule,
    Watt,
    Milliwatt,
    Microjoule,
    Nanosecond,
    Percent,
    Counter,
    Hertz,
    Celsius,
    Byte,
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SamplingMode {
    Periodic,
    Event,
    Polled,
    Imported,
    Synthetic,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SourceKind {
    CpuPackageEnergy,
    /// A platform-level energy domain such as Intel RAPL PSys. This is not
    /// interchangeable with package energy or whole-machine wall energy.
    PlatformEnergy,
    CpuCoreEnergy,
    DramEnergy,
    CpuPower,
    GpuPower,
    AnePower,
    WallPower,
    CStateResidency,
    PStateFrequency,
    WakeupCounter,
    ContextSwitchCounter,
    InterruptCounter,
    /// Native duration associated with an interval-average sample frame.
    SampleIntervalDuration,
    BenchmarkEvent,
    AiSpanEvent,
    Other,
}

/// Descriptor label linking an `INTERVAL_AVERAGE` source to the raw source
/// that reports the duration of each averaging interval.
pub const INTERVAL_DURATION_SOURCE_UID_LABEL: &str = "wattch.interval_duration_source_uid";

/// Descriptor label declaring where an interval is anchored on the daemon
/// clock. Wattch currently supports intervals ending at daemon receipt time.
pub const INTERVAL_ANCHOR_LABEL: &str = "wattch.interval_anchor";

/// Canonical interval anchor for independently updating collectors whose
/// sample timestamp is the daemon's process-monotonic receipt timestamp.
pub const INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP: &str = "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP";

/// Optional descriptor label naming the offline reconstruction used to align
/// a native interval timeline with its daemon-clock receipt anchors.
pub const INTERVAL_TIMELINE_RECONSTRUCTION_LABEL: &str = "wattch.interval_timeline_reconstruction";

/// Wattch v1 reconstruction: cumulative native elapsed intervals are mapped
/// onto daemon receipt anchors with one quality-checked affine fit offline.
pub const INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1: &str =
    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1";

/// Wattch v2 reconstruction: the same affine native timeline as v1, with an
/// explicit bounded allowance for rare scheduler-late receipt anchors.
pub const INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V2: &str =
    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V2";

/// Metadata sent once per source. Samples refer to `local_source_id`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SourceDescriptor {
    pub local_source_id: u32,
    pub source_uid: String,
    pub node_id: String,

    pub backend: String,
    pub kind: SourceKind,
    pub unit: Unit,
    pub temporality: Temporality,
    pub evidence_class: EvidenceClass,

    /// Stable hierarchical path, for example `cpu/package/0/energy`.
    pub source_path: String,

    /// Optional parent for nested domains, for example package -> core/DRAM.
    pub parent_source_uid: Option<String>,

    pub clock_domain: String,
    pub sampling_mode: SamplingMode,
    pub nominal_period_ns: Option<u64>,
    pub max_value: Option<f64>,
    pub resolution: Option<f64>,

    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", content = "value", rename_all = "snake_case")]
pub enum RawValue {
    F64(f64),
    I64(i64),
    Bytes(Vec<u8>),
}

/// Hot-path sample. No strings. No metadata. No derived claims.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RawSample {
    pub source_id: u32,
    pub sequence_no: u64,
    pub monotonic_ns: u64,
    pub value: RawValue,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RawSampleBatch {
    pub run_id: String,
    pub node_id: String,
    pub batch_sequence_no: u64,
    pub samples: Vec<RawSample>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RunManifest {
    pub run_id: String,
    pub node_id: String,
    pub protocol_version: String,
    /// Opaque identity of the daemon-wide monotonic clock epoch. Empty only
    /// when reading a legacy v0.1 manifest that predates shared-clock ids.
    #[serde(default)]
    pub clock_instance_id: String,
    pub wattch_version: String,
    pub created_unix_ns: u64,
    pub sources: Vec<SourceDescriptor>,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}
