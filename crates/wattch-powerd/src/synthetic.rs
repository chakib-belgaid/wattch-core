//! Synthetic powermeter backend for unprivileged development and CI.
//!
//! Mimics a laptop topology (package + core + dram cumulative counters and
//! a GPU instant-power source) with a deterministic pseudo-random walk, so
//! clients exercise realistic shapes without hardware access.

use std::collections::BTreeMap;
use wattch_core::{
    EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind, Temporality, Unit,
};
use wattch_daemon::SampleBackend;

const PACKAGE_ID: u32 = 1;
const CORE_ID: u32 = 2;
const DRAM_ID: u32 = 3;
const GPU_ID: u32 = 4;

#[derive(Clone)]
pub struct SyntheticBackend {
    descriptors: Vec<SourceDescriptor>,
    rng_state: u64,
    package_uj: u64,
    core_uj: u64,
    dram_uj: u64,
}

impl SyntheticBackend {
    pub fn new(node_id: &str, seed: u64) -> Self {
        let energy =
            |id: u32, kind: SourceKind, path: &str, parent: Option<&str>| SourceDescriptor {
                local_source_id: id,
                source_uid: format!("synthetic://{node_id}/{path}"),
                node_id: node_id.to_string(),
                backend: "synthetic".to_string(),
                kind,
                unit: Unit::Microjoule,
                temporality: Temporality::Cumulative,
                evidence_class: EvidenceClass::RawHardwareCounter,
                source_path: path.to_string(),
                parent_source_uid: parent.map(|p| format!("synthetic://{node_id}/{p}")),
                clock_domain: "monotonic".to_string(),
                sampling_mode: SamplingMode::Synthetic,
                nominal_period_ns: None,
                max_value: Some(262_143_328_850.0),
                resolution: Some(1.0),
                labels: BTreeMap::new(),
            };

        let mut gpu = energy(GPU_ID, SourceKind::GpuPower, "gpu/0/power", None);
        gpu.unit = Unit::Watt;
        gpu.temporality = Temporality::Instant;
        gpu.evidence_class = EvidenceClass::RawSensorReading;
        gpu.max_value = None;
        gpu.resolution = Some(0.01);

        Self {
            descriptors: vec![
                energy(
                    PACKAGE_ID,
                    SourceKind::CpuPackageEnergy,
                    "cpu/package/0/energy",
                    None,
                ),
                energy(
                    CORE_ID,
                    SourceKind::CpuCoreEnergy,
                    "cpu/package/0/core/energy",
                    Some("cpu/package/0/energy"),
                ),
                energy(
                    DRAM_ID,
                    SourceKind::DramEnergy,
                    "cpu/package/0/dram/energy",
                    Some("cpu/package/0/energy"),
                ),
                gpu,
            ],
            rng_state: seed.max(1),
            package_uj: 1_000_000_000,
            core_uj: 400_000_000,
            dram_uj: 150_000_000,
        }
    }

    /// xorshift64: deterministic per seed, no dependency.
    fn next_random(&mut self) -> u64 {
        let mut x = self.rng_state;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.rng_state = x;
        x
    }

    /// A pseudo-random value in `[base, base + spread)` microjoules.
    fn jitter(&mut self, base: u64, spread: u64) -> u64 {
        base + self.next_random() % spread.max(1)
    }
}

impl SampleBackend for SyntheticBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        // Roughly 8-20 W package power assuming ~10ms-1s polling; the exact
        // wattage is irrelevant, the cumulative/instant shapes are the point.
        let package_step = self.jitter(80_000, 120_000);
        let core_step = package_step / 2 + self.jitter(0, 10_000);
        let dram_step = package_step / 8 + self.jitter(0, 4_000);
        self.package_uj += package_step;
        self.core_uj += core_step;
        self.dram_uj += dram_step;
        let gpu_watts = 5.0 + (self.next_random() % 1_000) as f64 / 100.0;

        vec![
            (PACKAGE_ID, RawValue::I64(self.package_uj as i64)),
            (CORE_ID, RawValue::I64(self.core_uj as i64)),
            (DRAM_ID, RawValue::I64(self.dram_uj as i64)),
            (GPU_ID, RawValue::F64(gpu_watts)),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wattch_core::validate_source_set;

    #[test]
    fn descriptors_are_valid_and_nested() {
        let backend = SyntheticBackend::new("dev-node", 42);
        validate_source_set(backend.descriptors()).unwrap();
        let core = &backend.descriptors()[1];
        assert_eq!(
            core.parent_source_uid.as_deref(),
            Some("synthetic://dev-node/cpu/package/0/energy")
        );
    }

    #[test]
    fn cumulative_counters_are_strictly_increasing_and_deterministic() {
        let mut a = SyntheticBackend::new("dev-node", 7);
        let mut b = SyntheticBackend::new("dev-node", 7);

        let mut last_package = 0i64;
        for _ in 0..50 {
            let sample_a = a.sample();
            let sample_b = b.sample();
            assert_eq!(sample_a, sample_b, "same seed must replay identically");

            let RawValue::I64(package) = sample_a[0].1 else {
                panic!("package counter must be an integer");
            };
            assert!(package > last_package);
            last_package = package;
        }
    }
}
