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
    CpuCoreEnergy,
    DramEnergy,
    GpuPower,
    WallPower,
    CStateResidency,
    PStateFrequency,
    WakeupCounter,
    ContextSwitchCounter,
    InterruptCounter,
    BenchmarkEvent,
    AiSpanEvent,
    Other,
}

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
    pub wattch_version: String,
    pub created_unix_ns: u64,
    pub sources: Vec<SourceDescriptor>,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}
