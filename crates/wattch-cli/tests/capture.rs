use std::collections::BTreeMap;
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::time::Duration;
use wattch_cli::artifact::{inspect_run, read_manifest, replay_trace, ArtifactError, RunWriter};
use wattch_cli::client::{ClientError, WattchClient};
use wattch_core::{
    EvidenceClass, RawSample, RawSampleBatch, RawValue, RunManifest, SamplingMode,
    SourceDescriptor, SourceKind, Temporality, Unit,
};
use wattch_daemon::{
    DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, ServerHandle, SocketSource,
};
use wattch_protocol::messages::{
    read_message, write_message, HelloResponse, Message, StartCaptureAck,
};
use wattch_protocol::PROTOCOL_VERSION;

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
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "ramp-daemon".to_string(),
                daemon_version: "test".to_string(),
                node_id: "node-cli".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
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

fn raw_batch(run_id: &str, batch_sequence_no: u64, sequence_no: u64) -> RawSampleBatch {
    RawSampleBatch {
        run_id: run_id.to_string(),
        node_id: "node-cli".to_string(),
        batch_sequence_no,
        samples: vec![RawSample {
            source_id: 1,
            sequence_no,
            monotonic_ns: sequence_no * 1_000,
            value: RawValue::I64(sequence_no as i64),
        }],
    }
}

fn manifest(run_id: &str) -> RunManifest {
    RunManifest {
        run_id: run_id.to_string(),
        node_id: "node-cli".to_string(),
        protocol_version: "0.1.0".to_string(),
        wattch_version: "test".to_string(),
        created_unix_ns: 1,
        sources: vec![descriptor(1, "cpu/package/0/energy")],
        labels: BTreeMap::new(),
    }
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
        .capture(
            "run-artifact-test",
            10,
            Duration::from_millis(60),
            |batch| writer.append_batch(&batch).unwrap(),
        )
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
        node_id: "node-cli".to_string(),
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
        node_id: "node-cli".to_string(),
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

#[test]
fn run_writer_rejects_path_traversal_and_spliced_batches() {
    let out_root = temp_out("integrity-writer");
    assert!(matches!(
        RunWriter::create(&out_root, &manifest("../escape")),
        Err(ArtifactError::MetadataMismatch(_))
    ));

    let mut writer = RunWriter::create(&out_root, &manifest("bound-run")).unwrap();
    assert!(matches!(
        writer.append_batch(&raw_batch("other-run", 0, 0)),
        Err(ArtifactError::Integrity(_))
    ));
    writer.finish().unwrap();
    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn inspect_rejects_metadata_drift_and_cross_run_trace_splicing() {
    let out_root = temp_out("integrity-inspect");
    let mut first = RunWriter::create(&out_root, &manifest("run-a")).unwrap();
    first.append_batch(&raw_batch("run-a", 0, 0)).unwrap();
    let run_a = first.finish().unwrap();

    let mut second = RunWriter::create(&out_root, &manifest("run-b")).unwrap();
    second.append_batch(&raw_batch("run-b", 0, 0)).unwrap();
    let run_b = second.finish().unwrap();

    std::fs::copy(run_b.join("raw.trace.wch"), run_a.join("raw.trace.wch")).unwrap();
    assert!(matches!(
        replay_trace(&run_a, |_| {}),
        Err(ArtifactError::Integrity(_))
    ));
    assert!(matches!(
        inspect_run(&run_a),
        Err(ArtifactError::Integrity(_))
    ));

    std::fs::write(run_b.join("sources.json"), "[]").unwrap();
    assert!(matches!(
        inspect_run(&run_b),
        Err(ArtifactError::MetadataMismatch(_))
    ));
    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn rust_client_rejects_ack_and_batch_identity_substitution() {
    for substitute_ack in [true, false] {
        let tag = if substitute_ack {
            "bad-ack"
        } else {
            "bad-batch"
        };
        let socket_path =
            std::env::temp_dir().join(format!("wattch-hostile-{tag}-{}.sock", std::process::id()));
        let _ = std::fs::remove_file(&socket_path);
        let listener = UnixListener::bind(&socket_path).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            assert!(matches!(
                read_message(&mut stream).unwrap(),
                Message::HelloRequest(_)
            ));
            write_message(
                &mut stream,
                &Message::HelloResponse(HelloResponse {
                    daemon_name: "substitute".to_string(),
                    daemon_version: "test".to_string(),
                    protocol_version: PROTOCOL_VERSION.to_string(),
                    node_id: "node-cli".to_string(),
                    sources: vec![descriptor(1, "cpu/package/0/energy")],
                }),
            )
            .unwrap();
            assert!(matches!(
                read_message(&mut stream).unwrap(),
                Message::StartCapture(_)
            ));
            write_message(
                &mut stream,
                &Message::StartCaptureAck(StartCaptureAck {
                    run_id: if substitute_ack {
                        "other-run".to_string()
                    } else {
                        "expected-run".to_string()
                    },
                    accepted_source_ids: vec![1],
                }),
            )
            .unwrap();
            if !substitute_ack {
                write_message(
                    &mut stream,
                    &Message::RawSampleBatch(raw_batch("other-run", 0, 0)),
                )
                .unwrap();
            }
            // Keep the socket alive long enough for the client to parse the
            // deliberately inconsistent message before transport teardown.
            std::thread::sleep(Duration::from_millis(50));
        });

        let mut client = WattchClient::connect(&socket_path).unwrap();
        let error = client
            .capture("expected-run", 10, Duration::from_millis(20), |_| {})
            .unwrap_err();
        if substitute_ack {
            assert!(
                matches!(error, ClientError::Protocol(_)),
                "expected protocol identity error, got {error:?}"
            );
        } else {
            assert!(
                matches!(error, ClientError::InvalidEvidence(_)),
                "expected invalid-evidence error, got {error:?}"
            );
        }
        server.join().unwrap();
        let _ = std::fs::remove_file(&socket_path);
    }
}
