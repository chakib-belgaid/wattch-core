//! Raw Linux OS and CPU-state telemetry.
//!
//! The collector deliberately exposes cumulative counters and gauges. Exact
//! unit scaling is recorded in descriptor labels; it does not derive
//! utilization percentages, rates, deltas, or energy.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use thiserror::Error;
use tracing::warn;
use wattch_core::{
    validate_source_set, EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind,
    Temporality, Unit,
};
use wattch_daemon::SampleBackend;

#[derive(Clone, Debug)]
pub struct LinuxOsPaths {
    pub proc_stat: PathBuf,
    pub cpu_root: PathBuf,
    pub thermal_root: PathBuf,
    pub wakeup_root: PathBuf,
}

impl Default for LinuxOsPaths {
    fn default() -> Self {
        Self {
            proc_stat: PathBuf::from("/proc/stat"),
            cpu_root: PathBuf::from("/sys/devices/system/cpu"),
            thermal_root: PathBuf::from("/sys/class/thermal"),
            wakeup_root: PathBuf::from("/sys/class/wakeup"),
        }
    }
}

#[derive(Debug, Error)]
pub enum LinuxOsError {
    #[error("no supported Linux OS telemetry sources found")]
    NoSources,

    #[error("invalid Linux OS telemetry descriptors: {0}")]
    InvalidSources(#[from] wattch_core::Error),
}

#[derive(Clone, Debug)]
enum ProcStatMetric {
    CpuTime { index: usize },
    Scalar { key: &'static str },
}

#[derive(Clone, Debug)]
struct ProcStatSource {
    source_id: u32,
    metric: ProcStatMetric,
}

#[derive(Clone, Debug)]
enum FileTransform {
    UnsignedInteger,
    UnsignedMultiply(u64),
    MilliToFloat,
}

#[derive(Clone, Debug)]
struct FileSource {
    source_id: u32,
    path: PathBuf,
    transform: FileTransform,
}

/// Probe-once Linux telemetry backend; cheap to clone per daemon connection.
#[derive(Clone, Debug)]
pub struct LinuxOsBackend {
    descriptors: Vec<SourceDescriptor>,
    proc_stat_path: Option<PathBuf>,
    proc_stat_sources: Vec<ProcStatSource>,
    file_sources: Vec<FileSource>,
}

impl LinuxOsBackend {
    pub fn new(paths: LinuxOsPaths, node_id: &str) -> Result<Self, LinuxOsError> {
        let mut backend = Self {
            descriptors: Vec::new(),
            proc_stat_path: None,
            proc_stat_sources: Vec::new(),
            file_sources: Vec::new(),
        };

        backend.probe_proc_stat(&paths.proc_stat, node_id);
        backend.probe_cpu_sysfs(&paths.cpu_root, node_id);
        backend.probe_thermal_sysfs(&paths.thermal_root, node_id);
        backend.probe_wakeup_sources(&paths.wakeup_root, node_id);

        if backend.descriptors.is_empty() {
            return Err(LinuxOsError::NoSources);
        }
        validate_source_set(&backend.descriptors)?;
        Ok(backend)
    }

    fn next_id(&self) -> u32 {
        self.descriptors.len() as u32 + 1
    }

    fn descriptor(
        &self,
        node_id: &str,
        path: &str,
        kind: SourceKind,
        unit: Unit,
        temporality: Temporality,
        mut labels: BTreeMap<String, String>,
    ) -> SourceDescriptor {
        labels.insert("scope".to_string(), "system".to_string());
        SourceDescriptor {
            local_source_id: self.next_id(),
            source_uid: format!("linux-os://{node_id}/{path}"),
            node_id: node_id.to_string(),
            backend: "linux-os".to_string(),
            kind,
            unit,
            temporality,
            evidence_class: EvidenceClass::OsTelemetry,
            source_path: path.to_string(),
            parent_source_uid: None,
            clock_domain: "monotonic".to_string(),
            sampling_mode: SamplingMode::Polled,
            nominal_period_ns: None,
            max_value: None,
            resolution: Some(1.0),
            labels,
        }
    }

    fn probe_proc_stat(&mut self, path: &Path, node_id: &str) {
        let Ok(raw) = fs::read_to_string(path) else {
            return;
        };
        let parsed = parse_proc_stat(&raw);
        self.proc_stat_path = Some(path.to_path_buf());

        const CPU_FIELDS: [&str; 8] = [
            "user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal",
        ];
        for (index, name) in CPU_FIELDS.iter().enumerate() {
            if parsed.cpu_times.get(index).copied().flatten().is_none() {
                continue;
            }
            let mut labels = BTreeMap::new();
            labels.insert("counter_unit".to_string(), "USER_HZ".to_string());
            labels.insert("cpu".to_string(), "aggregate".to_string());
            labels.insert("state".to_string(), (*name).to_string());
            let descriptor = self.descriptor(
                node_id,
                &format!("scheduler/cpu/aggregate/time/{name}"),
                SourceKind::Other,
                Unit::Counter,
                Temporality::Cumulative,
                labels,
            );
            self.proc_stat_sources.push(ProcStatSource {
                source_id: descriptor.local_source_id,
                metric: ProcStatMetric::CpuTime { index },
            });
            self.descriptors.push(descriptor);
        }

        for (key, kind, temporality, source_path) in [
            (
                "ctxt",
                SourceKind::ContextSwitchCounter,
                Temporality::Cumulative,
                "scheduler/context_switches",
            ),
            (
                "intr",
                SourceKind::InterruptCounter,
                Temporality::Cumulative,
                "scheduler/interrupts",
            ),
            (
                "procs_running",
                SourceKind::Other,
                Temporality::Gauge,
                "scheduler/run_queue/runnable",
            ),
            (
                "procs_blocked",
                SourceKind::Other,
                Temporality::Gauge,
                "scheduler/run_queue/blocked",
            ),
        ] {
            if !parsed.scalars.contains_key(key) {
                continue;
            }
            let descriptor = self.descriptor(
                node_id,
                source_path,
                kind,
                Unit::Counter,
                temporality,
                BTreeMap::new(),
            );
            self.proc_stat_sources.push(ProcStatSource {
                source_id: descriptor.local_source_id,
                metric: ProcStatMetric::Scalar { key },
            });
            self.descriptors.push(descriptor);
        }
    }

    fn probe_cpu_sysfs(&mut self, root: &Path, node_id: &str) {
        for cpu_dir in sorted_dirs(root, "cpu") {
            let Some(cpu_name) = cpu_dir.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            let Some(cpu_index) = cpu_name.strip_prefix("cpu") else {
                continue;
            };
            if cpu_index.is_empty() || !cpu_index.chars().all(|ch| ch.is_ascii_digit()) {
                continue;
            }

            let frequency = cpu_dir.join("cpufreq/scaling_cur_freq");
            if is_numeric_file(&frequency) {
                let mut labels = BTreeMap::new();
                labels.insert("cpu".to_string(), cpu_index.to_string());
                labels.insert("native_unit".to_string(), "kilohertz".to_string());
                labels.insert("conversion".to_string(), "khz_to_hz".to_string());
                let mut descriptor = self.descriptor(
                    node_id,
                    &format!("cpu/{cpu_index}/frequency"),
                    SourceKind::PStateFrequency,
                    Unit::Hertz,
                    Temporality::Gauge,
                    labels,
                );
                descriptor.resolution = Some(1_000.0);
                self.file_sources.push(FileSource {
                    source_id: descriptor.local_source_id,
                    path: frequency,
                    transform: FileTransform::UnsignedMultiply(1_000),
                });
                self.descriptors.push(descriptor);
            }

            for state_dir in sorted_dirs(&cpu_dir.join("cpuidle"), "state") {
                let time_path = state_dir.join("time");
                if !is_numeric_file(&time_path) {
                    continue;
                }
                let Some(state_index) = state_dir
                    .file_name()
                    .and_then(|name| name.to_str())
                    .and_then(|name| name.strip_prefix("state"))
                    .filter(|index| {
                        !index.is_empty() && index.chars().all(|ch| ch.is_ascii_digit())
                    })
                else {
                    continue;
                };
                let state_name = fs::read_to_string(state_dir.join("name"))
                    .ok()
                    .map(|name| name.trim().to_string())
                    .filter(|name| !name.is_empty())
                    .unwrap_or_else(|| format!("state{state_index}"));
                let mut labels = BTreeMap::new();
                labels.insert("cpu".to_string(), cpu_index.to_string());
                labels.insert("state".to_string(), state_name);
                labels.insert("state_index".to_string(), state_index.to_string());
                labels.insert("native_unit".to_string(), "microsecond".to_string());
                labels.insert("conversion".to_string(), "us_to_ns".to_string());
                let mut descriptor = self.descriptor(
                    node_id,
                    &format!("cpu/{cpu_index}/cstate/{state_index}/residency"),
                    SourceKind::CStateResidency,
                    Unit::Nanosecond,
                    Temporality::Cumulative,
                    labels,
                );
                descriptor.resolution = Some(1_000.0);
                self.file_sources.push(FileSource {
                    source_id: descriptor.local_source_id,
                    path: time_path,
                    transform: FileTransform::UnsignedMultiply(1_000),
                });
                self.descriptors.push(descriptor);
            }

            for (name, path) in [
                ("core", cpu_dir.join("thermal_throttle/core_throttle_count")),
                (
                    "package",
                    cpu_dir.join("thermal_throttle/package_throttle_count"),
                ),
            ] {
                if !is_numeric_file(&path) {
                    continue;
                }
                let mut labels = BTreeMap::new();
                labels.insert("cpu".to_string(), cpu_index.to_string());
                labels.insert("throttle_scope".to_string(), name.to_string());
                let descriptor = self.descriptor(
                    node_id,
                    &format!("cpu/{cpu_index}/throttling/{name}/count"),
                    SourceKind::Other,
                    Unit::Counter,
                    Temporality::Cumulative,
                    labels,
                );
                self.file_sources.push(FileSource {
                    source_id: descriptor.local_source_id,
                    path,
                    transform: FileTransform::UnsignedInteger,
                });
                self.descriptors.push(descriptor);
            }
        }
    }

    fn probe_thermal_sysfs(&mut self, root: &Path, node_id: &str) {
        for zone_dir in sorted_dirs(root, "thermal_zone") {
            let temp_path = zone_dir.join("temp");
            if !is_signed_numeric_file(&temp_path) {
                continue;
            }
            let zone = zone_dir
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("thermal_zone");
            let zone_type = fs::read_to_string(zone_dir.join("type"))
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| "unknown".to_string());
            let mut labels = BTreeMap::new();
            labels.insert("zone".to_string(), zone.to_string());
            labels.insert("zone_type".to_string(), zone_type);
            labels.insert("native_unit".to_string(), "millicelsius".to_string());
            labels.insert("conversion".to_string(), "mc_to_c".to_string());
            let mut descriptor = self.descriptor(
                node_id,
                &format!("thermal/{zone}/temperature"),
                SourceKind::Other,
                Unit::Celsius,
                Temporality::Gauge,
                labels,
            );
            descriptor.resolution = Some(0.001);
            self.file_sources.push(FileSource {
                source_id: descriptor.local_source_id,
                path: temp_path,
                transform: FileTransform::MilliToFloat,
            });
            self.descriptors.push(descriptor);
        }
    }

    fn probe_wakeup_sources(&mut self, root: &Path, node_id: &str) {
        let mut seen_names = BTreeSet::new();
        for source_dir in sorted_dirs(root, "wakeup") {
            let count_path = source_dir.join("event_count");
            if !is_numeric_file(&count_path) {
                continue;
            }
            let Some(entry) = source_dir.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            let name = fs::read_to_string(source_dir.join("name"))
                .ok()
                .map(|name| name.trim().to_string())
                .filter(|name| !name.is_empty())
                .unwrap_or_else(|| entry.to_string());
            if !seen_names.insert(name.clone()) {
                warn!(
                    wakeup_source = name,
                    entry, "skipping duplicate Linux wakeup-source name"
                );
                continue;
            }
            let encoded_name = hex_component(name.as_bytes());
            let mut labels = BTreeMap::new();
            labels.insert("wakeup_source".to_string(), name.clone());
            labels.insert("sysfs_entry".to_string(), entry.to_string());
            labels.insert("kernel_field".to_string(), "event_count".to_string());
            let descriptor = self.descriptor(
                node_id,
                &format!("power/wakeup_sources/{encoded_name}/event_count"),
                SourceKind::WakeupCounter,
                Unit::Counter,
                Temporality::Cumulative,
                labels,
            );
            self.file_sources.push(FileSource {
                source_id: descriptor.local_source_id,
                path: count_path,
                transform: FileTransform::UnsignedInteger,
            });
            self.descriptors.push(descriptor);
        }
    }
}

impl SampleBackend for LinuxOsBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        let mut readings = Vec::with_capacity(self.descriptors.len());
        if let Some(path) = &self.proc_stat_path {
            if let Ok(raw) = fs::read_to_string(path) {
                let stat = parse_proc_stat(&raw);
                for source in &self.proc_stat_sources {
                    let value = match source.metric {
                        ProcStatMetric::CpuTime { index } => {
                            stat.cpu_times.get(index).copied().flatten()
                        }
                        ProcStatMetric::Scalar { key } => stat.scalars.get(key).copied(),
                    };
                    if let Some(value) = value.and_then(|value| i64::try_from(value).ok()) {
                        readings.push((source.source_id, RawValue::I64(value)));
                    }
                }
            }
        }

        for source in &self.file_sources {
            let Ok(raw) = fs::read_to_string(&source.path) else {
                continue;
            };
            let converted = match source.transform {
                FileTransform::UnsignedInteger => raw
                    .trim()
                    .parse::<u64>()
                    .ok()
                    .and_then(|value| i64::try_from(value).ok())
                    .map(RawValue::I64),
                FileTransform::UnsignedMultiply(scale) => raw
                    .trim()
                    .parse::<u64>()
                    .ok()
                    .and_then(|value| value.checked_mul(scale))
                    .and_then(|value| i64::try_from(value).ok())
                    .map(RawValue::I64),
                FileTransform::MilliToFloat => raw
                    .trim()
                    .parse::<i64>()
                    .ok()
                    .map(|value| RawValue::F64(value as f64 / 1_000.0)),
            };
            if let Some(value) = converted {
                readings.push((source.source_id, value));
            }
        }
        readings
    }
}

#[derive(Default)]
struct ProcStatSnapshot {
    cpu_times: Vec<Option<u64>>,
    scalars: BTreeMap<&'static str, u64>,
}

fn parse_proc_stat(raw: &str) -> ProcStatSnapshot {
    let mut snapshot = ProcStatSnapshot::default();
    for line in raw.lines() {
        let mut fields = line.split_whitespace();
        let Some(key) = fields.next() else {
            continue;
        };
        if key == "cpu" {
            snapshot.cpu_times = fields.map(|value| value.parse().ok()).collect();
            continue;
        }
        let stable_key = match key {
            "ctxt" => "ctxt",
            "intr" => "intr",
            "procs_running" => "procs_running",
            "procs_blocked" => "procs_blocked",
            _ => continue,
        };
        if let Some(value) = fields.next().and_then(|value| value.parse().ok()) {
            snapshot.scalars.insert(stable_key, value);
        }
    }
    snapshot
}

fn hex_component(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(bytes.len().saturating_mul(2));
    for byte in bytes {
        encoded.push(HEX[usize::from(byte >> 4)] as char);
        encoded.push(HEX[usize::from(byte & 0x0f)] as char);
    }
    encoded
}

fn is_numeric_file(path: &Path) -> bool {
    fs::read_to_string(path)
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .is_some()
}

fn is_signed_numeric_file(path: &Path) -> bool {
    fs::read_to_string(path)
        .ok()
        .and_then(|value| value.trim().parse::<i64>().ok())
        .is_some()
}

fn sorted_dirs(root: &Path, prefix: &str) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(root) else {
        return Vec::new();
    };
    let mut paths = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_dir()
                && path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with(prefix))
        })
        .collect::<Vec<_>>();
    paths.sort();
    paths
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_TEMP_ROOT: AtomicU64 = AtomicU64::new(0);

    fn temp_root() -> PathBuf {
        let suffix = NEXT_TEMP_ROOT.fetch_add(1, Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("wattch-linux-os-{}-{suffix}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn exposes_raw_proc_and_sysfs_metrics() {
        let root = temp_root();
        let proc_stat = root.join("proc-stat");
        fs::write(
            &proc_stat,
            "cpu  10 2 3 40 5 6 7 8\nintr 99 1 2\nctxt 123\nprocs_running 4\nprocs_blocked 1\n",
        )
        .unwrap();
        let cpu_root = root.join("cpu");
        let cpu0 = cpu_root.join("cpu0");
        fs::create_dir_all(cpu0.join("cpufreq")).unwrap();
        fs::write(cpu0.join("cpufreq/scaling_cur_freq"), "2400000\n").unwrap();
        fs::create_dir_all(cpu0.join("cpuidle/state1")).unwrap();
        fs::write(cpu0.join("cpuidle/state1/name"), "C6\n").unwrap();
        fs::write(cpu0.join("cpuidle/state1/time"), "321\n").unwrap();
        let thermal_root = root.join("thermal");
        fs::create_dir_all(thermal_root.join("thermal_zone0")).unwrap();
        fs::write(thermal_root.join("thermal_zone0/type"), "x86_pkg_temp\n").unwrap();
        fs::write(thermal_root.join("thermal_zone0/temp"), "42500\n").unwrap();
        let wakeup_root = root.join("wakeup");
        fs::create_dir_all(wakeup_root.join("wakeup0")).unwrap();
        fs::write(wakeup_root.join("wakeup0/name"), "rtc0\n").unwrap();
        fs::write(wakeup_root.join("wakeup0/event_count"), "7\n").unwrap();

        let mut backend = LinuxOsBackend::new(
            LinuxOsPaths {
                proc_stat,
                cpu_root,
                thermal_root,
                wakeup_root,
            },
            "node-0",
        )
        .unwrap();
        let readings = backend.sample();

        let frequency = backend
            .descriptors()
            .iter()
            .find(|source| source.kind == SourceKind::PStateFrequency)
            .unwrap();
        assert!(readings.contains(&(frequency.local_source_id, RawValue::I64(2_400_000_000))));
        let cstate = backend
            .descriptors()
            .iter()
            .find(|source| source.kind == SourceKind::CStateResidency)
            .unwrap();
        assert!(readings.contains(&(cstate.local_source_id, RawValue::I64(321_000))));
        let temperature = backend
            .descriptors()
            .iter()
            .find(|source| source.unit == Unit::Celsius)
            .unwrap();
        assert!(readings.contains(&(temperature.local_source_id, RawValue::F64(42.5))));
        let wakeup = backend
            .descriptors()
            .iter()
            .find(|source| source.kind == SourceKind::WakeupCounter)
            .unwrap();
        assert!(readings.contains(&(wakeup.local_source_id, RawValue::I64(7))));

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn returns_no_sources_for_missing_roots() {
        let root = temp_root();
        let result = LinuxOsBackend::new(
            LinuxOsPaths {
                proc_stat: root.join("missing-proc"),
                cpu_root: root.join("missing-cpu"),
                thermal_root: root.join("missing-thermal"),
                wakeup_root: root.join("missing-wakeup-sources"),
            },
            "node-0",
        );
        assert!(matches!(result, Err(LinuxOsError::NoSources)));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn malformed_cpu_time_does_not_shift_later_fields() {
        let snapshot = parse_proc_stat("cpu  10 invalid 30 40\n");
        assert_eq!(snapshot.cpu_times, vec![Some(10), None, Some(30), Some(40)]);
    }

    #[test]
    fn preserves_negative_thermal_readings() {
        let root = temp_root();
        let thermal_root = root.join("thermal");
        fs::create_dir_all(thermal_root.join("thermal_zone0")).unwrap();
        fs::write(thermal_root.join("thermal_zone0/temp"), "-1250\n").unwrap();
        let mut backend = LinuxOsBackend::new(
            LinuxOsPaths {
                proc_stat: root.join("missing-proc"),
                cpu_root: root.join("missing-cpu"),
                thermal_root,
                wakeup_root: root.join("missing-wakeup-sources"),
            },
            "node-0",
        )
        .unwrap();

        assert_eq!(backend.sample(), vec![(1, RawValue::F64(-1.25))]);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cstate_identity_uses_state_index_not_a_potentially_duplicate_name() {
        let root = temp_root();
        let cpu_root = root.join("cpu");
        for state in ["state1", "state2"] {
            let state_root = cpu_root.join("cpu0/cpuidle").join(state);
            fs::create_dir_all(&state_root).unwrap();
            fs::write(state_root.join("name"), "C6\n").unwrap();
            fs::write(state_root.join("time"), "1\n").unwrap();
        }
        let backend = LinuxOsBackend::new(
            LinuxOsPaths {
                proc_stat: root.join("missing-proc"),
                cpu_root,
                thermal_root: root.join("missing-thermal"),
                wakeup_root: root.join("missing-wakeup-sources"),
            },
            "node-0",
        )
        .unwrap();

        let uids = backend
            .descriptors()
            .iter()
            .map(|source| source.source_uid.as_str())
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(uids.len(), 2);
        assert!(uids.iter().any(|uid| uid.contains("/cstate/1/")));
        assert!(uids.iter().any(|uid| uid.contains("/cstate/2/")));
        fs::remove_dir_all(root).unwrap();
    }
}
