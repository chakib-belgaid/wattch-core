//! Linux RAPL acquisition through the powercap sysfs interface.
//!
//! Layout under the powercap root (default `/sys/class/powercap`):
//!
//! ```text
//! intel-rapl:0/            package (or psys) zone
//!   name                   e.g. "package-0"
//!   energy_uj              cumulative microjoules
//!   max_energy_range_uj    wrap point
//!   intel-rapl:0:0/        subzone, e.g. "core", "uncore", "dram"
//! ```
//!
//! The scan is rooted at a configurable directory so it can be exercised on
//! any OS with a fake tree. Reads stay raw: cumulative microjoule counters,
//! no deltas, no aggregation.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use thiserror::Error;
use tracing::warn;
use wattch_core::{
    EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind, Temporality, Unit,
};
use wattch_daemon::SampleBackend;

#[derive(Debug, Error)]
pub enum RaplError {
    #[error("powercap root {0} does not exist")]
    MissingRoot(PathBuf),

    #[error("no RAPL zones found under {0}")]
    NoZones(PathBuf),

    #[error("failed to read {path}: {source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },

    #[error("failed to parse {path} content {content:?} as integer")]
    Parse { path: PathBuf, content: String },
}

fn read_trimmed(path: &Path) -> Result<String, RaplError> {
    fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .map_err(|source| RaplError::Read {
            path: path.to_path_buf(),
            source,
        })
}

fn read_u64(path: &Path) -> Result<u64, RaplError> {
    let content = read_trimmed(path)?;
    content.parse::<u64>().map_err(|_| RaplError::Parse {
        path: path.to_path_buf(),
        content,
    })
}

struct Zone {
    dir: PathBuf,
    /// Zone id segments, e.g. `[0]` for a package, `[0, 1]` for a subzone.
    id_segments: Vec<u32>,
    name: String,
    max_energy_range_uj: Option<u64>,
}

/// Find RAPL zones (`intel-rapl:N` and `intel-rapl:N:M` directories,
/// including `intel-rapl-mmio` variants) under the root.
fn scan_zones(root: &Path) -> Result<Vec<Zone>, RaplError> {
    if !root.is_dir() {
        return Err(RaplError::MissingRoot(root.to_path_buf()));
    }

    let mut zones = Vec::new();
    let mut seen = BTreeSet::new();
    let mut stack: Vec<PathBuf> = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = fs::read_dir(&dir).map_err(|source| RaplError::Read {
            path: dir.clone(),
            source,
        })?;
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            let file_name = entry.file_name().to_string_lossy().to_string();
            let Some(id_part) = file_name
                .strip_prefix("intel-rapl-mmio:")
                .or_else(|| file_name.strip_prefix("intel-rapl:"))
            else {
                continue;
            };
            let Ok(id_segments) = id_part
                .split(':')
                .map(|s| s.parse::<u32>())
                .collect::<Result<Vec<u32>, _>>()
            else {
                continue;
            };
            if !path.join("energy_uj").exists() {
                continue;
            }
            // /sys/class/powercap links both packages and their children.
            // A child is also reachable through its package, so compare
            // canonical directories before recording or descending a zone.
            let path = fs::canonicalize(&path).map_err(|source| RaplError::Read {
                path: path.clone(),
                source,
            })?;
            if !seen.insert(path.clone()) {
                continue;
            }
            let name = read_trimmed(&path.join("name"))?;
            let max_energy_range_uj = read_u64(&path.join("max_energy_range_uj")).ok();
            stack.push(path.clone());
            zones.push(Zone {
                dir: path,
                id_segments,
                name,
                max_energy_range_uj,
            });
        }
    }

    if zones.is_empty() {
        return Err(RaplError::NoZones(root.to_path_buf()));
    }
    zones.sort_by(|a, b| a.id_segments.cmp(&b.id_segments).then(a.dir.cmp(&b.dir)));
    Ok(zones)
}

fn kind_for(zone_name: &str) -> SourceKind {
    if zone_name.starts_with("package") {
        SourceKind::CpuPackageEnergy
    } else {
        match zone_name {
            "core" => SourceKind::CpuCoreEnergy,
            "dram" => SourceKind::DramEnergy,
            _ => SourceKind::Other,
        }
    }
}

fn source_path_for(zone: &Zone, parent: Option<&Zone>) -> String {
    match parent {
        None => {
            if zone.name.starts_with("package") {
                format!("cpu/package/{}/energy", zone.id_segments[0])
            } else {
                format!("platform/{}/energy", zone.name)
            }
        }
        Some(parent_zone) => {
            let parent_base = if parent_zone.name.starts_with("package") {
                format!("cpu/package/{}", parent_zone.id_segments[0])
            } else {
                format!("platform/{}", parent_zone.name)
            };
            format!("{parent_base}/{}/energy", zone.name)
        }
    }
}

/// RAPL backend: one source per powercap zone, cumulative microjoules.
///
/// Cheaply `Clone`: both fields are plain `Vec`s of already-probed data, no
/// open file handles or filesystem state. This lets callers scan the
/// powercap tree once (e.g. at startup) and hand every connection its own
/// clone instead of re-scanning per connection.
#[derive(Clone, Debug)]
pub struct RaplBackend {
    descriptors: Vec<SourceDescriptor>,
    energy_files: Vec<(u32, PathBuf)>,
}

impl RaplBackend {
    pub fn new(powercap_root: &Path, node_id: &str) -> Result<Self, RaplError> {
        let zones = scan_zones(powercap_root)?;

        let mut descriptors = Vec::new();
        let mut energy_files = Vec::new();
        for (index, zone) in zones.iter().enumerate() {
            let local_source_id = index as u32 + 1;
            let parent = if zone.id_segments.len() > 1 {
                zones
                    .iter()
                    .find(|z| z.id_segments == zone.id_segments[..zone.id_segments.len() - 1])
            } else {
                None
            };

            let dir_name = zone
                .dir
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default();
            let mut labels = BTreeMap::new();
            labels.insert("zone".to_string(), dir_name.clone());
            labels.insert("zone_name".to_string(), zone.name.clone());

            descriptors.push(SourceDescriptor {
                local_source_id,
                source_uid: format!("rapl://{node_id}/{dir_name}"),
                node_id: node_id.to_string(),
                backend: "linux-rapl".to_string(),
                kind: kind_for(&zone.name),
                unit: Unit::Microjoule,
                temporality: Temporality::Cumulative,
                evidence_class: EvidenceClass::RawHardwareCounter,
                source_path: source_path_for(zone, parent),
                parent_source_uid: parent.map(|p| {
                    let parent_dir = p
                        .dir
                        .file_name()
                        .map(|n| n.to_string_lossy().to_string())
                        .unwrap_or_default();
                    format!("rapl://{node_id}/{parent_dir}")
                }),
                clock_domain: "monotonic".to_string(),
                sampling_mode: SamplingMode::Polled,
                nominal_period_ns: None,
                max_value: zone.max_energy_range_uj.map(|v| v as f64),
                resolution: Some(1.0),
                labels,
            });
            energy_files.push((local_source_id, zone.dir.join("energy_uj")));
        }

        Ok(Self {
            descriptors,
            energy_files,
        })
    }
}

impl SampleBackend for RaplBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    /// Read every zone's counter. A zone that fails to read (hot-unplug,
    /// permission change) is skipped for that tick rather than aborting the
    /// capture; trace consumers see the gap.
    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        self.energy_files
            .iter()
            .filter_map(|(source_id, path)| match read_u64(path) {
                Ok(value) => match i64::try_from(value) {
                    Ok(value) => Some((*source_id, RawValue::I64(value))),
                    Err(_) => {
                        warn!(
                            source_id,
                            value, "skipping RAPL counter that exceeds the i64 sample range"
                        );
                        None
                    }
                },
                Err(err) => {
                    warn!(source_id, error = %err, "skipping unreadable RAPL zone");
                    None
                }
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_zone(dir: &Path, name: &str, energy: u64, max: u64) {
        fs::create_dir_all(dir).unwrap();
        fs::write(dir.join("name"), format!("{name}\n")).unwrap();
        fs::write(dir.join("energy_uj"), format!("{energy}\n")).unwrap();
        fs::write(dir.join("max_energy_range_uj"), format!("{max}\n")).unwrap();
    }

    fn fake_tree(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "wattch-powercap-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = fs::remove_dir_all(&root);
        let pkg0 = root.join("intel-rapl:0");
        write_zone(&pkg0, "package-0", 1_000_000, 262_143_328_850);
        write_zone(
            &pkg0.join("intel-rapl:0:0"),
            "core",
            400_000,
            262_143_328_850,
        );
        write_zone(
            &pkg0.join("intel-rapl:0:1"),
            "dram",
            200_000,
            65_712_999_613,
        );
        write_zone(
            &root.join("intel-rapl:1"),
            "psys",
            5_000_000,
            262_143_328_850,
        );
        root
    }

    #[test]
    fn scans_zone_hierarchy_into_descriptors() {
        let root = fake_tree("scan");
        let backend = RaplBackend::new(&root, "node-x").unwrap();
        let descriptors = backend.descriptors();
        assert_eq!(descriptors.len(), 4);

        let package = &descriptors[0];
        assert_eq!(package.source_path, "cpu/package/0/energy");
        assert_eq!(package.kind, SourceKind::CpuPackageEnergy);
        assert_eq!(package.unit, Unit::Microjoule);
        assert_eq!(package.temporality, Temporality::Cumulative);
        assert_eq!(package.evidence_class, EvidenceClass::RawHardwareCounter);
        assert_eq!(package.max_value, Some(262_143_328_850.0));
        assert_eq!(package.parent_source_uid, None);
        assert_eq!(package.source_uid, "rapl://node-x/intel-rapl:0");

        let core = descriptors
            .iter()
            .find(|d| d.source_path == "cpu/package/0/core/energy")
            .expect("core subzone");
        assert_eq!(core.kind, SourceKind::CpuCoreEnergy);
        assert_eq!(
            core.parent_source_uid.as_deref(),
            Some("rapl://node-x/intel-rapl:0")
        );

        let dram = descriptors
            .iter()
            .find(|d| d.source_path == "cpu/package/0/dram/energy")
            .expect("dram subzone");
        assert_eq!(dram.kind, SourceKind::DramEnergy);

        let psys = descriptors
            .iter()
            .find(|d| d.source_path == "platform/psys/energy")
            .expect("psys zone");
        assert_eq!(psys.kind, SourceKind::Other);

        let _ = fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn sysfs_class_aliases_emit_each_zone_once() {
        use std::os::unix::fs::symlink;

        let root = fake_tree("class-aliases");
        let class_root = root.join("class");
        fs::create_dir(&class_root).unwrap();
        // Linux exposes each zone in /sys/class/powercap as a symlink,
        // including children already reachable through the package link.
        for relative in [
            "intel-rapl:0",
            "intel-rapl:0/intel-rapl:0:0",
            "intel-rapl:0/intel-rapl:0:1",
            "intel-rapl:1",
        ] {
            let zone = root.join(relative);
            symlink(&zone, class_root.join(zone.file_name().unwrap())).unwrap();
        }

        let mut backend = RaplBackend::new(&class_root, "node-x").unwrap();
        assert_eq!(backend.descriptors().len(), 4, "one source per real zone");
        wattch_core::validate_source_set(backend.descriptors()).unwrap();
        assert_eq!(backend.sample().len(), 4, "read every counter only once");
        let core = backend
            .descriptors()
            .iter()
            .find(|d| d.kind == SourceKind::CpuCoreEnergy)
            .unwrap();
        assert_eq!(
            core.parent_source_uid.as_deref(),
            Some("rapl://node-x/intel-rapl:0")
        );

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn distinct_msr_and_mmio_zones_are_not_merged() {
        let root = fake_tree("msr-mmio");
        write_zone(
            &root.join("intel-rapl-mmio:0"),
            "package-0",
            1_000_000,
            262_143_328_850,
        );
        let mut backend = RaplBackend::new(&root, "node-x").unwrap();
        assert_eq!(backend.descriptors().len(), 5);
        wattch_core::validate_source_set(backend.descriptors()).unwrap();
        assert!(backend
            .descriptors()
            .iter()
            .any(|d| d.source_uid == "rapl://node-x/intel-rapl-mmio:0"));
        assert_eq!(backend.sample().len(), 5);

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn samples_raw_microjoule_counters() {
        let root = fake_tree("sample");
        let mut backend = RaplBackend::new(&root, "node-x").unwrap();
        let readings = backend.sample();
        assert_eq!(readings.len(), 4);
        let package = readings.iter().find(|(id, _)| *id == 1).unwrap();
        assert_eq!(package.1, RawValue::I64(1_000_000));

        // Counter advances between reads.
        fs::write(root.join("intel-rapl:0/energy_uj"), "1500000\n").unwrap();
        let readings = backend.sample();
        let package = readings.iter().find(|(id, _)| *id == 1).unwrap();
        assert_eq!(package.1, RawValue::I64(1_500_000));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn unreadable_zone_is_skipped_not_fatal() {
        let root = fake_tree("skip");
        let mut backend = RaplBackend::new(&root, "node-x").unwrap();
        fs::write(root.join("intel-rapl:0/energy_uj"), "garbage\n").unwrap();
        let readings = backend.sample();
        assert_eq!(readings.len(), 3);
        assert!(!readings.iter().any(|(id, _)| *id == 1));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn out_of_range_counter_is_skipped_not_wrapped() {
        let root = fake_tree("overflow");
        let mut backend = RaplBackend::new(&root, "node-x").unwrap();
        fs::write(
            root.join("intel-rapl:0/energy_uj"),
            format!("{}\n", u64::MAX),
        )
        .unwrap();
        let readings = backend.sample();
        assert_eq!(readings.len(), 3);
        assert!(!readings.iter().any(|(id, _)| *id == 1));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn cloned_backend_keeps_sampling_after_root_removed() {
        // Exercises the option (b) design from the powerd factory fix: a
        // backend probed once at startup is cloned per connection instead
        // of re-scanning the powercap tree. If the tree disappears at
        // runtime, a clone made *before* the disappearance must still
        // sample without panicking (unreadable zones are skipped, per
        // `SampleBackend::sample`'s documented behavior) rather than
        // requiring a fresh, now-failing `RaplBackend::new` rescan.
        let root = fake_tree("clone-removed");
        let backend = RaplBackend::new(&root, "node-x").unwrap();
        let mut cloned = backend.clone();

        fs::remove_dir_all(&root).unwrap();

        let readings = cloned.sample();
        assert!(
            readings.is_empty(),
            "every zone file is gone; all reads should be skipped, not panic"
        );
        assert_eq!(
            cloned.descriptors().len(),
            4,
            "descriptors survive independent of disk"
        );
    }

    #[test]
    fn missing_root_is_an_error() {
        let err = RaplBackend::new(Path::new("/definitely/missing/powercap"), "n").unwrap_err();
        assert!(matches!(err, RaplError::MissingRoot(_)));
    }

    #[test]
    fn empty_root_reports_no_zones() {
        let root =
            std::env::temp_dir().join(format!("wattch-powercap-empty-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let err = RaplBackend::new(&root, "n").unwrap_err();
        assert!(matches!(err, RaplError::NoZones(_)));
        let _ = fs::remove_dir_all(root);
    }
}
