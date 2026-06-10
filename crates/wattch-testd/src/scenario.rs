use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;
use wattch_core::{RawSample, RawSampleBatch, RawValue, SourceDescriptor};
use wattch_daemon::SampleBackend;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Scenario {
    pub scenario_id: String,
    pub run_id: String,
    pub node_id: String,
    pub tick_ns: u64,
    pub ticks: u64,
    pub sources: Vec<SyntheticSource>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SyntheticSource {
    #[serde(flatten)]
    pub descriptor: SourceDescriptor,
    pub behavior: LinearBehavior,
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

pub fn load_scenario(path: &Path) -> Result<Scenario> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("failed to read scenario {}", path.display()))?;
    serde_json::from_str(&raw)
        .with_context(|| format!("failed to parse scenario {}", path.display()))
}

/// Deterministic backend: every `sample()` call advances one tick and
/// evaluates each source's linear behavior. Values continue past
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
            .map(|source| RawSample {
                source_id: source.descriptor.local_source_id,
                sequence_no: tick,
                monotonic_ns: tick * scenario.tick_ns,
                value: RawValue::F64(source.behavior.value_at(tick)),
            })
            .collect();

        RawSampleBatch {
            run_id: scenario.run_id.clone(),
            node_id: scenario.node_id.clone(),
            batch_sequence_no: tick,
            samples,
        }
    })
}
