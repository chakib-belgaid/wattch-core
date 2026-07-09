use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;
use wattch_cli::artifact::{inspect_run, read_manifest, replay_trace, ArtifactError, RunWriter};
use wattch_cli::client::WattchClient;
use wattch_core::{
    EvidenceClass, RawValue, RunManifest, SamplingMode, SourceDescriptor, SourceKind, Temporality,
    Unit,
};
use wattch_daemon::{DaemonIdentity, SampleBackend, ServerConfig, ServerHandle};

struct RampBackend {
    descriptors: Vec<SourceDescriptor>,
    tick: u64,
}

fn descriptor(id: u32, path: &str) -> SourceDescriptor {
    SourceDescriptor {
        local_source_id: id,
        source_uid: format!("test://node-cli/{path}"),
        node_id: "node-cli".to_string(),
        backend: "ramp".to_string(),
        kind: SourceKind::CpuPackageEnergy,
        unit: Unit::Microjoule,
        temporality: Temporality::Cumulative,
        evidence_class: EvidenceClass::RawHardwareCounter,
        source_path: path.to_string(),
        parent_source_uid: None,
        clock_domain: "monotonic".to_string(),
        sampling_mode: SamplingMode::Synthetic,
        nominal_period_ns: Some(5_000_000),
        max_value: None,
        resolution: None,
        labels: BTreeMap::new(),
    }
}

impl SampleBackend for RampBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        self.tick += 1;
        vec![(1, RawValue::I64(1_000_000 + 1_000 * self.tick as i64))]
    }
}

fn start_server(name: &str) -> (ServerHandle, PathBuf) {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-cli-test-{name}-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);
    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket_path: socket_path.clone(),
            identity: DaemonIdentity {
                daemon_name: "ramp-daemon".to_string(),
                daemon_version: "test".to_string(),
                node_id: "node-cli".to_string(),
            },
        },
        || {
            Box::new(RampBackend {
                descriptors: vec![descriptor(1, "cpu/package/0/energy")],
                tick: 0,
            })
        },
    )
    .unwrap();
    (handle, socket_path)
}

fn temp_out(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("wattch-cli-out-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

#[test]
fn capture_writes_replayable_artifact() {
    let (handle, socket_path) = start_server("capture");
    let out_root = temp_out("capture");

    let mut client = WattchClient::connect(&socket_path).unwrap();
    let hello = client.hello().clone();
    assert_eq!(hello.sources.len(), 1);

    let manifest = RunManifest {
        run_id: "run-artifact-test".to_string(),
        node_id: hello.node_id.clone(),
        protocol_version: hello.protocol_version.clone(),
        wattch_version: "test".to_string(),
        created_unix_ns: 1,
        sources: hello.sources.clone(),
        labels: BTreeMap::new(),
    };

    let mut writer = RunWriter::create(&out_root, &manifest).unwrap();
    let ack = client
        .capture("run-artifact-test", 5, Duration::from_millis(60), |batch| {
            writer.append_batch(&batch).unwrap()
        })
        .unwrap();
    let run_dir = writer.finish().unwrap();
    assert!(ack.batches_sent >= 2);

    // The artifact directory has the three production files.
    assert!(run_dir.join("manifest.json").exists());
    assert!(run_dir.join("sources.json").exists());
    assert!(run_dir.join("raw.trace.wch").exists());

    let stored = read_manifest(&run_dir).unwrap();
    assert_eq!(stored.run_id, "run-artifact-test");
    assert_eq!(stored.sources.len(), 1);

    // Offline replay sees exactly what the daemon acked, gap-free.
    let report = inspect_run(&run_dir).unwrap();
    assert_eq!(report.run_id, "run-artifact-test");
    assert_eq!(report.batches, ack.batches_sent);
    assert_eq!(report.samples, ack.samples_sent);
    assert_eq!(report.unknown_source_samples, 0);
    let stats = report.per_source.get(&1).unwrap();
    assert_eq!(stats.samples, ack.samples_sent);
    assert_eq!(stats.sequence_gaps, 0);
    assert_eq!(stats.first_sequence_no, 0);
    assert!(stats.last_monotonic_ns > stats.first_monotonic_ns);

    let _ = std::fs::remove_dir_all(out_root);
    handle.shutdown();
}

#[test]
fn run_writer_refuses_to_overwrite_existing_run() {
    let out_root = temp_out("no-overwrite");
    let manifest = RunManifest {
        run_id: "run-dup".to_string(),
        node_id: "n".to_string(),
        protocol_version: "0.1.0".to_string(),
        wattch_version: "test".to_string(),
        created_unix_ns: 1,
        sources: vec![descriptor(1, "cpu/package/0/energy")],
        labels: BTreeMap::new(),
    };
    let writer = RunWriter::create(&out_root, &manifest).unwrap();
    writer.finish().unwrap();

    assert!(matches!(
        RunWriter::create(&out_root, &manifest),
        Err(ArtifactError::AlreadyExists(_))
    ));
    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn replay_and_inspect_on_truncated_trace_return_clean_errors_not_panics() {
    let out_root = temp_out("truncated-trace");
    let manifest = RunManifest {
        run_id: "run-truncated".to_string(),
        node_id: "n".to_string(),
        protocol_version: "0.1.0".to_string(),
        wattch_version: "test".to_string(),
        created_unix_ns: 1,
        sources: vec![descriptor(1, "cpu/package/0/energy")],
        labels: BTreeMap::new(),
    };
    let writer = RunWriter::create(&out_root, &manifest).unwrap();
    let run_dir = writer.finish().unwrap();

    // Simulate a crash mid-write: a handful of bytes that can't possibly be
    // a complete frame header, let alone a full frame.
    std::fs::write(run_dir.join("raw.trace.wch"), [0xAB, 0xCD, 0xEF]).unwrap();

    let err = replay_trace(&run_dir, |_| {}).unwrap_err();
    assert!(
        matches!(err, ArtifactError::Trace { .. }),
        "expected a clean ArtifactError::Trace, got {err:?}"
    );

    let err = inspect_run(&run_dir).unwrap_err();
    assert!(
        matches!(err, ArtifactError::Trace { .. }),
        "expected inspect_run to surface the same clean error, got {err:?}"
    );

    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn connect_to_missing_socket_fails_clearly() {
    let err = WattchClient::connect(std::path::Path::new("/tmp/wattch-definitely-missing.sock"))
        .unwrap_err();
    assert!(err.to_string().contains("failed to connect"));
}
