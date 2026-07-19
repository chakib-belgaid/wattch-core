use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use wattch_core::{RawSample, RawSampleBatch, RawValue, SourceDescriptor};
use wattch_daemon::SampleBackend;
use wattch_protocol::messages::{validate_record_event, RecordEvent};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Scenario {
    pub scenario_id: String,
    pub run_id: String,
    pub node_id: String,
    pub tick_ns: u64,
    pub ticks: u64,
    pub sources: Vec<SyntheticSource>,
    /// Client-side event requests scheduled relative to observed raw batches.
    ///
    /// `wattch-testd` never emits these itself. A fixture-driving client sends
    /// the request after `record_after_tick`, so the ordinary daemon event
    /// path owns the authoritative timestamp. This can model a delayed marker
    /// without adding interpretation or a synthetic clock to the daemon.
    #[serde(default)]
    pub record_events: Vec<ScheduledRecordEvent>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ScheduledRecordEvent {
    pub event_id: String,
    pub kind: String,
    /// Tick at which the benchmark action conceptually occurred.
    pub intended_tick: u64,
    /// Raw batch tick after which the fixture-driving client sends the event.
    pub record_after_tick: u64,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
}

impl ScheduledRecordEvent {
    /// Build the protocol request a fixture-driving client sends during an
    /// active capture. No timestamp crosses this boundary.
    pub fn request(&self, run_id: impl Into<String>) -> RecordEvent {
        RecordEvent {
            run_id: run_id.into(),
            event_id: self.event_id.clone(),
            kind: self.kind.clone(),
            labels: self.labels.clone(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SyntheticSource {
    #[serde(flatten)]
    pub descriptor: SourceDescriptor,
    pub behavior: SyntheticBehavior,
    /// Controls whether a source is present at a given tick. This models raw
    /// acquisition faults (gaps and source disappearance), not interpreted
    /// quality results.
    #[serde(default)]
    pub emission: EmissionPattern,
    /// Deterministic timestamp perturbations used by dry-run/replay fixtures.
    /// The socket daemon owns timestamps, so these do not affect
    /// [`SampleBackend::sample`].
    #[serde(default)]
    pub timestamp: TimestampPattern,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LinearBehavior {
    pub start: f64,
    pub step_per_tick: f64,
}

impl LinearBehavior {
    pub fn value_at(&self, tick: u64) -> f64 {
        self.start + self.step_per_tick * tick as f64
    }
}

/// A scenario value generator.
///
/// The untagged linear variant intentionally preserves the original fixture
/// shape:
///
/// ```json
/// { "start": 1000.0, "step_per_tick": 0.42 }
/// ```
///
/// Richer behaviors use an explicit `type` tag so causal and quality fixtures
/// remain readable and deterministic.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum SyntheticBehavior {
    Scripted(ScriptedBehavior),
    Linear(LinearBehavior),
}

impl SyntheticBehavior {
    pub fn value_at(&self, tick: u64) -> f64 {
        match self {
            Self::Scripted(behavior) => behavior.value_at(tick),
            Self::Linear(behavior) => behavior.value_at(tick),
        }
    }

    fn validate(&self) -> Result<()> {
        match self {
            Self::Scripted(behavior) => behavior.validate(),
            Self::Linear(behavior) => {
                ensure_finite("start", behavior.start)?;
                ensure_finite("step_per_tick", behavior.step_per_tick)
            }
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ScriptedBehavior {
    Constant {
        value: f64,
    },
    /// Piecewise-linear values. The active segment is the last segment whose
    /// `start_tick` is not greater than the current tick. Segment starts are
    /// explicit so counters can remain continuous or deliberately jump.
    PiecewiseLinear {
        segments: Vec<LinearSegment>,
    },
    /// Explicit deterministic values. Without `repeat`, the final value is
    /// held after the sequence ends.
    Sequence {
        values: Vec<f64>,
        #[serde(default)]
        repeat: bool,
    },
    /// A cumulative counter in the half-open range `[0, modulus)`.
    WrappedCounter {
        start: f64,
        step_per_tick: f64,
        modulus: f64,
    },
}

impl ScriptedBehavior {
    fn value_at(&self, tick: u64) -> f64 {
        match self {
            Self::Constant { value } => *value,
            Self::PiecewiseLinear { segments } => {
                let Some(segment) = segments
                    .iter()
                    .rev()
                    .find(|segment| segment.start_tick <= tick)
                else {
                    // Scenario loading rejects this shape. Returning NaN keeps
                    // directly constructed invalid values from panicking in
                    // library code and makes the invalid evidence explicit.
                    return f64::NAN;
                };
                segment.start
                    + segment.step_per_tick * tick.saturating_sub(segment.start_tick) as f64
            }
            Self::Sequence { values, repeat } => {
                if values.is_empty() {
                    // Scenario loading rejects empty sequences; keep direct
                    // library construction non-panicking as well.
                    return f64::NAN;
                }
                let index = if *repeat {
                    (tick % values.len() as u64) as usize
                } else {
                    usize::try_from(tick)
                        .unwrap_or(usize::MAX)
                        .min(values.len() - 1)
                };
                values[index]
            }
            Self::WrappedCounter {
                start,
                step_per_tick,
                modulus,
            } => (start + step_per_tick * tick as f64).rem_euclid(*modulus),
        }
    }

    fn validate(&self) -> Result<()> {
        match self {
            Self::Constant { value } => ensure_finite("value", *value),
            Self::PiecewiseLinear { segments } => {
                let Some(first) = segments.first() else {
                    bail!("piecewise_linear behavior requires at least one segment");
                };
                if first.start_tick != 0 {
                    bail!("piecewise_linear behavior must start at tick 0");
                }

                let mut previous_tick = None;
                for segment in segments {
                    if previous_tick.is_some_and(|previous| segment.start_tick <= previous) {
                        bail!("piecewise_linear segment start ticks must be strictly increasing");
                    }
                    ensure_finite("segment start", segment.start)?;
                    ensure_finite("segment step_per_tick", segment.step_per_tick)?;
                    previous_tick = Some(segment.start_tick);
                }
                Ok(())
            }
            Self::Sequence { values, .. } => {
                if values.is_empty() {
                    bail!("sequence behavior requires at least one value");
                }
                for value in values {
                    ensure_finite("sequence value", *value)?;
                }
                Ok(())
            }
            Self::WrappedCounter {
                start,
                step_per_tick,
                modulus,
            } => {
                ensure_finite("start", *start)?;
                ensure_finite("step_per_tick", *step_per_tick)?;
                ensure_finite("modulus", *modulus)?;
                if *modulus <= 0.0 {
                    bail!("wrapped_counter modulus must be greater than zero");
                }
                Ok(())
            }
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LinearSegment {
    pub start_tick: u64,
    pub start: f64,
    pub step_per_tick: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EmissionPattern {
    /// First tick on which this source may emit.
    #[serde(default)]
    pub start_tick: u64,
    /// Last tick on which this source may emit, inclusive.
    pub stop_after_tick: Option<u64>,
    /// Ticks deliberately omitted even when they are otherwise eligible.
    #[serde(default)]
    pub skip_ticks: Vec<u64>,
    /// Emit once every N ticks relative to `start_tick`.
    #[serde(default = "one_u64")]
    pub every_n_ticks: u64,
}

impl EmissionPattern {
    pub fn emits_at(&self, tick: u64) -> bool {
        if tick < self.start_tick
            || self.stop_after_tick.is_some_and(|last| tick > last)
            || self.skip_ticks.contains(&tick)
        {
            return false;
        }

        (tick - self.start_tick) % self.every_n_ticks == 0
    }

    fn validate(&self) -> Result<()> {
        if self.every_n_ticks == 0 {
            bail!("emission every_n_ticks must be greater than zero");
        }
        if self
            .stop_after_tick
            .is_some_and(|last| last < self.start_tick)
        {
            bail!("emission stop_after_tick must not precede start_tick");
        }
        Ok(())
    }
}

impl Default for EmissionPattern {
    fn default() -> Self {
        Self {
            start_tick: 0,
            stop_after_tick: None,
            skip_ticks: Vec::new(),
            every_n_ticks: 1,
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct TimestampPattern {
    /// Constant signed shift from the scenario monotonic clock.
    #[serde(default)]
    pub offset_ns: i64,
    /// Signed deterministic drift added for every elapsed scenario tick.
    #[serde(default)]
    pub drift_ns_per_tick: i64,
    /// Optional repeating deterministic jitter sequence.
    #[serde(default)]
    pub jitter_ns: Vec<i64>,
}

impl TimestampPattern {
    pub fn timestamp_at(&self, tick: u64, tick_ns: u64) -> u64 {
        let jitter = if self.jitter_ns.is_empty() {
            0
        } else {
            self.jitter_ns[(tick % self.jitter_ns.len() as u64) as usize]
        };
        let timestamp = i128::from(tick) * i128::from(tick_ns)
            + i128::from(self.offset_ns)
            + i128::from(tick) * i128::from(self.drift_ns_per_tick)
            + i128::from(jitter);

        timestamp.clamp(0, i128::from(u64::MAX)) as u64
    }
}

impl SyntheticSource {
    fn sample_at(&self, tick: u64, tick_ns: u64) -> Option<RawSample> {
        self.emission.emits_at(tick).then(|| RawSample {
            source_id: self.descriptor.local_source_id,
            sequence_no: tick,
            monotonic_ns: self.timestamp.timestamp_at(tick, tick_ns),
            value: RawValue::F64(self.behavior.value_at(tick)),
        })
    }

    fn validate(&self) -> Result<()> {
        self.behavior.validate()?;
        self.emission.validate()
    }
}

impl Scenario {
    pub fn validate_behavior(&self) -> Result<()> {
        if self.tick_ns == 0 {
            bail!("scenario tick_ns must be greater than zero");
        }
        for source in &self.sources {
            source.validate().with_context(|| {
                format!(
                    "invalid behavior for source {}",
                    source.descriptor.source_uid
                )
            })?;
        }

        let mut event_ids = BTreeSet::new();
        for event in &self.record_events {
            if event.intended_tick >= self.ticks {
                bail!(
                    "record event {} intended_tick must be within scenario ticks",
                    event.event_id
                );
            }
            if event.record_after_tick >= self.ticks {
                bail!(
                    "record event {} record_after_tick must be within scenario ticks",
                    event.event_id
                );
            }
            if event.record_after_tick < event.intended_tick {
                bail!(
                    "record event {} record_after_tick must not precede intended_tick",
                    event.event_id
                );
            }
            if !event_ids.insert(event.event_id.as_str()) {
                bail!("record event ids must be unique within a scenario");
            }
            if let Err(reason) = validate_record_event(&event.request(&self.run_id)) {
                bail!(
                    "invalid scheduled record event {}: {reason}",
                    event.event_id
                );
            }
        }
        Ok(())
    }
}

fn one_u64() -> u64 {
    1
}

fn ensure_finite(field: &str, value: f64) -> Result<()> {
    if !value.is_finite() {
        bail!("{field} must be finite");
    }
    Ok(())
}

pub fn load_scenario(path: &Path) -> Result<Scenario> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read scenario {}", path.display()))?;
    let scenario: Scenario = serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse scenario {}", path.display()))?;
    scenario
        .validate_behavior()
        .with_context(|| format!("invalid scenario {}", path.display()))?;
    Ok(scenario)
}

/// Deterministic backend: every `sample()` call advances one tick and
/// evaluates each source's deterministic behavior. Values continue past
/// `scenario.ticks`; the tick limit only bounds dry-run fixture output.
pub struct DeterministicBackend {
    descriptors: Vec<SourceDescriptor>,
    sources: Vec<SyntheticSource>,
    tick: u64,
}

impl DeterministicBackend {
    pub fn new(scenario: &Scenario) -> Self {
        Self {
            descriptors: scenario
                .sources
                .iter()
                .map(|s| s.descriptor.clone())
                .collect(),
            sources: scenario.sources.clone(),
            tick: 0,
        }
    }
}

impl SampleBackend for DeterministicBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        let tick = self.tick;
        self.tick += 1;
        self.sources
            .iter()
            .filter(|source| source.emission.emits_at(tick))
            .map(|source| {
                (
                    source.descriptor.local_source_id,
                    RawValue::F64(source.behavior.value_at(tick)),
                )
            })
            .collect()
    }
}

/// Dry-run fixture generation: one batch per tick, bounded by `scenario.ticks`.
pub fn generate_batches(scenario: &Scenario) -> impl Iterator<Item = RawSampleBatch> + '_ {
    (0..scenario.ticks).map(move |tick| {
        let samples = scenario
            .sources
            .iter()
            .filter_map(|source| source.sample_at(tick, scenario.tick_ns))
            .collect();

        RawSampleBatch {
            run_id: scenario.run_id.clone(),
            node_id: scenario.node_id.clone(),
            batch_sequence_no: tick,
            samples,
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_linear_behavior_shape_is_preserved() {
        let behavior: SyntheticBehavior =
            serde_json::from_str(r#"{"start":10.0,"step_per_tick":2.5}"#).unwrap();
        assert_eq!(behavior.value_at(4), 20.0);
    }

    #[test]
    fn piecewise_and_wrapped_behaviors_are_deterministic() {
        let piecewise = SyntheticBehavior::Scripted(ScriptedBehavior::PiecewiseLinear {
            segments: vec![
                LinearSegment {
                    start_tick: 0,
                    start: 10.0,
                    step_per_tick: 1.0,
                },
                LinearSegment {
                    start_tick: 3,
                    start: 20.0,
                    step_per_tick: 2.0,
                },
            ],
        });
        assert_eq!(piecewise.value_at(2), 12.0);
        assert_eq!(piecewise.value_at(3), 20.0);
        assert_eq!(piecewise.value_at(5), 24.0);

        let wrapped = SyntheticBehavior::Scripted(ScriptedBehavior::WrappedCounter {
            start: 95.0,
            step_per_tick: 3.0,
            modulus: 100.0,
        });
        assert_eq!(wrapped.value_at(0), 95.0);
        assert_eq!(wrapped.value_at(1), 98.0);
        assert_eq!(wrapped.value_at(2), 1.0);
    }

    #[test]
    fn invalid_scripted_behaviors_are_rejected() {
        let behavior = SyntheticBehavior::Scripted(ScriptedBehavior::PiecewiseLinear {
            segments: vec![LinearSegment {
                start_tick: 1,
                start: 0.0,
                step_per_tick: 1.0,
            }],
        });
        assert!(behavior.validate().is_err());

        let behavior = SyntheticBehavior::Scripted(ScriptedBehavior::WrappedCounter {
            start: 0.0,
            step_per_tick: 1.0,
            modulus: 0.0,
        });
        assert!(behavior.validate().is_err());
    }

    #[test]
    fn emission_and_timestamp_faults_are_reproducible() {
        let emission = EmissionPattern {
            start_tick: 1,
            stop_after_tick: Some(7),
            skip_ticks: vec![5],
            every_n_ticks: 2,
        };
        let emitted = (0..9)
            .filter(|tick| emission.emits_at(*tick))
            .collect::<Vec<_>>();
        assert_eq!(emitted, vec![1, 3, 7]);

        let timestamp = TimestampPattern {
            offset_ns: 100,
            drift_ns_per_tick: 10,
            jitter_ns: vec![0, -5],
        };
        assert_eq!(timestamp.timestamp_at(2, 1_000), 2_120);
        assert_eq!(timestamp.timestamp_at(3, 1_000), 3_125);
    }

    #[test]
    fn scheduled_record_events_validate_as_client_requests() {
        let event = ScheduledRecordEvent {
            event_id: "target-spawn-delayed".to_string(),
            kind: "TARGET_SPAWN".to_string(),
            intended_tick: 1,
            record_after_tick: 4,
            labels: BTreeMap::from([("fault".to_string(), "marker_delay".to_string())]),
        };

        let request = event.request("run-1");
        assert_eq!(request.run_id, "run-1");
        assert_eq!(request.event_id, "target-spawn-delayed");
        assert_eq!(request.kind, "TARGET_SPAWN");
        assert_eq!(request.labels["fault"], "marker_delay");
    }
}
