use std::collections::BTreeMap;
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::time::Duration;
use wattch_cli::artifact::{
    inspect_run, read_json_sidecar, read_manifest, replay_events, replay_trace, write_json_sidecar,
    ArtifactError, JsonSidecar, RunWriter,
};
use wattch_cli::client::{CaptureFrame, ClientError, WattchClient};
use wattch_core::{
    EvidenceClass, RawSample, RawSampleBatch, RawValue, RunManifest, SamplingMode,
    SourceDescriptor, SourceKind, Temporality, Unit,
};
use wattch_daemon::{
    DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, ServerHandle, SocketSource,
};
use wattch_protocol::frame::{
    FrameError, FrameKind, WireError, MAGIC, MAX_PAYLOAD_LEN, WIRE_VERSION_MAJOR,
};
use wattch_protocol::messages::{
    read_message, write_message, Event, EventBatch, HelloResponse, Message, RecordEvent,
    RecordEventAck, StartCaptureAck,
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

fn event_batch(run_id: &str, batch_sequence_no: u64, event_id: &str) -> EventBatch {
    EventBatch {
        run_id: run_id.to_string(),
        node_id: "node-cli".to_string(),
        batch_sequence_no,
        events: vec![Event {
            event_id: event_id.to_string(),
            run_id: run_id.to_string(),
            node_id: "node-cli".to_string(),
            monotonic_ns: 42_000 + batch_sequence_no,
            kind: "TARGET_EXEC".to_string(),
            labels: BTreeMap::from([("program".to_string(), "p1".to_string())]),
        }],
    }
}

fn manifest(run_id: &str) -> RunManifest {
    RunManifest {
        run_id: run_id.to_string(),
        node_id: "node-cli".to_string(),
        protocol_version: "0.1.0".to_string(),
        clock_instance_id: String::new(),
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
        clock_instance_id: hello.clock_instance_id.clone(),
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

    // The artifact directory has raw and event evidence streams.
    assert!(run_dir.join("manifest.json").exists());
    assert!(run_dir.join("sources.json").exists());
    assert!(run_dir.join("raw.trace.wch").exists());
    assert!(run_dir.join("events.trace.wch").exists());

    let stored = read_manifest(&run_dir).unwrap();
    assert_eq!(stored.run_id, "run-artifact-test");
    assert_eq!(stored.clock_instance_id, hello.clock_instance_id);
    assert_eq!(stored.sources.len(), 1);

    // Offline replay sees exactly what the daemon acked, gap-free.
    let report = inspect_run(&run_dir).unwrap();
    assert_eq!(report.run_id, "run-artifact-test");
    assert_eq!(report.batches, ack.batches_sent);
    assert_eq!(report.samples, ack.samples_sent);
    assert_eq!(report.event_batches, 0);
    assert_eq!(report.events, 0);
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
fn low_level_session_records_daemon_stamped_events() {
    let (handle, socket_path) = start_server("events");
    let out_root = temp_out("events");
    let mut client = WattchClient::connect(&socket_path).unwrap();
    let hello = client.hello().clone();
    assert!(!hello.clock_instance_id.is_empty());

    let manifest = RunManifest {
        run_id: "run-event-test".to_string(),
        node_id: hello.node_id.clone(),
        protocol_version: hello.protocol_version.clone(),
        clock_instance_id: hello.clock_instance_id.clone(),
        wattch_version: "test".to_string(),
        created_unix_ns: 1,
        sources: hello.sources.clone(),
        labels: BTreeMap::new(),
    };
    let mut writer = RunWriter::create(&out_root, &manifest).unwrap();

    let acknowledged_event = {
        let mut session = client.start_capture("run-event-test", 10).unwrap();
        session
            .record_event(RecordEvent {
                run_id: "run-event-test".to_string(),
                event_id: "target-exec".to_string(),
                kind: "TARGET_EXEC".to_string(),
                labels: BTreeMap::from([("program".to_string(), "p1".to_string())]),
            })
            .unwrap();

        let acknowledged = loop {
            match session.read_next(Duration::from_secs(2)).unwrap() {
                CaptureFrame::RawSampleBatch(batch) => writer.append_batch(&batch).unwrap(),
                CaptureFrame::EventBatch(batch) => {
                    assert_eq!(batch.batch_sequence_no, 0);
                    writer.append_event_batch(&batch).unwrap();
                }
                CaptureFrame::RecordEventAck(ack) => break ack.event,
                CaptureFrame::StopCaptureAck(_) => panic!("capture stopped before event ack"),
            }
        };

        session.stop().unwrap();
        loop {
            match session.read_next(Duration::from_secs(2)).unwrap() {
                CaptureFrame::RawSampleBatch(batch) => writer.append_batch(&batch).unwrap(),
                CaptureFrame::EventBatch(batch) => writer.append_event_batch(&batch).unwrap(),
                CaptureFrame::RecordEventAck(_) => panic!("duplicate event ack"),
                CaptureFrame::StopCaptureAck(_) => break,
            }
        }
        acknowledged
    };

    assert_eq!(acknowledged_event.event_id, "target-exec");
    assert_eq!(acknowledged_event.node_id, "node-cli");
    assert!(acknowledged_event.monotonic_ns > 0);

    let run_dir = writer.finish().unwrap();
    let mut replayed = Vec::new();
    assert_eq!(
        replay_events(&run_dir, |batch| replayed.push(batch)).unwrap(),
        1
    );
    assert_eq!(replayed[0].events, vec![acknowledged_event]);
    let report = inspect_run(&run_dir).unwrap();
    assert_eq!(report.event_batches, 1);
    assert_eq!(report.events, 1);
    assert_eq!(report.per_event_kind.get("TARGET_EXEC"), Some(&1));

    let _ = std::fs::remove_dir_all(out_root);
    handle.shutdown();
}

#[test]
fn event_trace_and_json_sidecars_are_bound_and_create_only() {
    let out_root = temp_out("event-artifact");
    let mut manifest = manifest("event-artifact");
    manifest.protocol_version = PROTOCOL_VERSION.to_string();
    manifest.clock_instance_id = "artifact-clock".to_string();
    let mut writer = RunWriter::create(&out_root, &manifest).unwrap();
    writer
        .append_batch(&raw_batch("event-artifact", 0, 0))
        .unwrap();
    writer
        .append_event_batch(&event_batch("event-artifact", 7, "spawn"))
        .unwrap();
    let run_dir = writer.finish().unwrap();

    for (sidecar, value) in [
        (
            JsonSidecar::Trial,
            serde_json::json!({
                "trial_id": "trial-1",
                "run_id": "event-artifact",
                "arm": "C1"
            }),
        ),
        (
            JsonSidecar::Quality,
            serde_json::json!({"trial_id": "trial-1", "disposition": "ACCEPTED"}),
        ),
        (
            JsonSidecar::Checksums,
            serde_json::json!({"raw.trace.wch": "sha256:abc"}),
        ),
    ] {
        write_json_sidecar(&run_dir, sidecar, &value).unwrap();
        let stored: serde_json::Value = read_json_sidecar(&run_dir, sidecar).unwrap();
        assert_eq!(stored, value);
        assert!(matches!(
            write_json_sidecar(&run_dir, sidecar, &value),
            Err(ArtifactError::Io { .. })
        ));
    }

    let mut replayed = Vec::new();
    assert_eq!(
        replay_events(&run_dir, |batch| replayed.push(batch)).unwrap(),
        1
    );
    assert_eq!(replayed[0].batch_sequence_no, 7);

    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn legacy_v0_1_raw_artifact_remains_readable_without_event_trace() {
    let out_root = temp_out("legacy-v01");
    let mut writer = RunWriter::create(&out_root, &manifest("legacy-v01")).unwrap();
    writer.append_batch(&raw_batch("legacy-v01", 0, 0)).unwrap();
    let run_dir = writer.finish().unwrap();

    let raw_path = run_dir.join("raw.trace.wch");
    let mut raw = std::fs::read(&raw_path).unwrap();
    raw[6..8].copy_from_slice(&1u16.to_le_bytes());
    std::fs::write(&raw_path, raw).unwrap();
    std::fs::remove_file(run_dir.join("events.trace.wch")).unwrap();
    let manifest_path = run_dir.join("manifest.json");
    let mut legacy_manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
    legacy_manifest
        .as_object_mut()
        .unwrap()
        .remove("clock_instance_id");
    std::fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&legacy_manifest).unwrap(),
    )
    .unwrap();

    assert_eq!(replay_trace(&run_dir, |_| {}).unwrap(), 1);
    assert_eq!(replay_events(&run_dir, |_| {}).unwrap(), 0);
    let report = inspect_run(&run_dir).unwrap();
    assert_eq!(report.batches, 1);
    assert_eq!(report.events, 0);
    assert!(read_manifest(&run_dir)
        .unwrap()
        .clock_instance_id
        .is_empty());

    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn legacy_v0_1_raw_artifact_above_live_frame_limit_remains_readable() {
    let out_root = temp_out("legacy-v01-large-frame");
    let writer = RunWriter::create(&out_root, &manifest("legacy-v01-large-frame")).unwrap();
    let run_dir = writer.finish().unwrap();

    let batch = raw_batch("legacy-v01-large-frame", 0, 0);
    let mut payload = serde_json::to_vec(&batch).unwrap();
    payload.resize(MAX_PAYLOAD_LEN as usize + 1, b' ');
    let mut frame = Vec::with_capacity(16 + payload.len());
    frame.extend_from_slice(&MAGIC);
    frame.extend_from_slice(&WIRE_VERSION_MAJOR.to_le_bytes());
    frame.extend_from_slice(&1u16.to_le_bytes());
    frame.extend_from_slice(&(FrameKind::RawSampleBatch as u16).to_le_bytes());
    frame.extend_from_slice(&0u16.to_le_bytes());
    frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    frame.extend_from_slice(&payload);
    std::fs::write(run_dir.join("raw.trace.wch"), frame).unwrap();

    let mut replayed = Vec::new();
    assert_eq!(
        replay_trace(&run_dir, |decoded| replayed.push(decoded)).unwrap(),
        1
    );
    assert_eq!(replayed, vec![batch]);
    assert_eq!(inspect_run(&run_dir).unwrap().samples, 1);

    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn legacy_manifest_does_not_raise_limit_for_a_v0_2_frame() {
    let out_root = temp_out("legacy-manifest-v02-frame");
    let writer = RunWriter::create(&out_root, &manifest("legacy-manifest-v02-frame")).unwrap();
    let run_dir = writer.finish().unwrap();

    let mut header = Vec::with_capacity(16);
    header.extend_from_slice(&MAGIC);
    header.extend_from_slice(&WIRE_VERSION_MAJOR.to_le_bytes());
    header.extend_from_slice(&2u16.to_le_bytes());
    header.extend_from_slice(&(FrameKind::RawSampleBatch as u16).to_le_bytes());
    header.extend_from_slice(&0u16.to_le_bytes());
    header.extend_from_slice(&(MAX_PAYLOAD_LEN + 1).to_le_bytes());
    std::fs::write(run_dir.join("raw.trace.wch"), header).unwrap();

    assert!(matches!(
        replay_trace(&run_dir, |_| {}),
        Err(ArtifactError::Trace {
            source: WireError::Frame(FrameError::PayloadTooLarge(_)),
            ..
        })
    ));

    let _ = std::fs::remove_dir_all(out_root);
}

#[test]
fn run_writer_refuses_to_overwrite_existing_run() {
    let out_root = temp_out("no-overwrite");
    let manifest = RunManifest {
        run_id: "run-dup".to_string(),
        node_id: "node-cli".to_string(),
        protocol_version: "0.1.0".to_string(),
        clock_instance_id: String::new(),
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
        clock_instance_id: String::new(),
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
fn rust_client_accepts_a_patch_compatible_daemon_handshake() {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-patch-compatible-{}.sock",
        std::process::id()
    ));
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
                daemon_name: "patch-daemon".to_string(),
                daemon_version: "test".to_string(),
                protocol_version: "0.2.99".to_string(),
                node_id: "node-cli".to_string(),
                clock_instance_id: "patch-clock".to_string(),
                sources: vec![descriptor(1, "cpu/package/0/energy")],
            }),
        )
        .unwrap();
        std::thread::sleep(Duration::from_millis(50));
    });

    let client = WattchClient::connect(&socket_path).unwrap();
    assert_eq!(client.hello().protocol_version, "0.2.99");
    drop(client);
    server.join().unwrap();
    let _ = std::fs::remove_file(&socket_path);
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
fn inspect_rejects_cross_run_event_trace_splicing() {
    let out_root = temp_out("event-splice");
    let mut run_a_manifest = manifest("event-run-a");
    run_a_manifest.protocol_version = PROTOCOL_VERSION.to_string();
    run_a_manifest.clock_instance_id = "event-splice-clock".to_string();
    let mut run_b_manifest = manifest("event-run-b");
    run_b_manifest.protocol_version = PROTOCOL_VERSION.to_string();
    run_b_manifest.clock_instance_id = "event-splice-clock".to_string();

    let mut first = RunWriter::create(&out_root, &run_a_manifest).unwrap();
    first.append_batch(&raw_batch("event-run-a", 0, 0)).unwrap();
    first
        .append_event_batch(&event_batch("event-run-a", 0, "event-a"))
        .unwrap();
    let run_a = first.finish().unwrap();

    let mut second = RunWriter::create(&out_root, &run_b_manifest).unwrap();
    second
        .append_batch(&raw_batch("event-run-b", 0, 0))
        .unwrap();
    second
        .append_event_batch(&event_batch("event-run-b", 0, "event-b"))
        .unwrap();
    let run_b = second.finish().unwrap();

    std::fs::copy(
        run_b.join("events.trace.wch"),
        run_a.join("events.trace.wch"),
    )
    .unwrap();
    assert!(matches!(
        inspect_run(&run_a),
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
                    clock_instance_id: "hostile-clock".to_string(),
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

#[test]
fn low_level_session_rejects_event_ack_substitution() {
    let socket_path =
        std::env::temp_dir().join(format!("wattch-hostile-event-{}.sock", std::process::id()));
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
                daemon_name: "substitute-event".to_string(),
                daemon_version: "test".to_string(),
                protocol_version: PROTOCOL_VERSION.to_string(),
                node_id: "node-cli".to_string(),
                clock_instance_id: "event-clock".to_string(),
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
                run_id: "event-run".to_string(),
                accepted_source_ids: vec![1],
            }),
        )
        .unwrap();
        let Message::RecordEvent(request) = read_message(&mut stream).unwrap() else {
            panic!("expected RecordEvent");
        };
        let stamped = Event {
            event_id: request.event_id,
            run_id: request.run_id,
            node_id: "node-cli".to_string(),
            monotonic_ns: 99,
            kind: request.kind,
            labels: request.labels,
        };
        write_message(
            &mut stream,
            &Message::EventBatch(EventBatch {
                run_id: "event-run".to_string(),
                node_id: "node-cli".to_string(),
                batch_sequence_no: 0,
                events: vec![stamped.clone()],
            }),
        )
        .unwrap();
        let mut substituted = stamped;
        substituted.monotonic_ns += 1;
        write_message(
            &mut stream,
            &Message::RecordEventAck(RecordEventAck { event: substituted }),
        )
        .unwrap();
        std::thread::sleep(Duration::from_millis(50));
    });

    let mut client = WattchClient::connect(&socket_path).unwrap();
    {
        let mut session = client.start_capture("event-run", 10).unwrap();
        session
            .record_event(RecordEvent {
                run_id: "event-run".to_string(),
                event_id: "exec".to_string(),
                kind: "TARGET_EXEC".to_string(),
                labels: BTreeMap::new(),
            })
            .unwrap();
        assert!(matches!(
            session.read_next(Duration::from_secs(1)).unwrap(),
            CaptureFrame::EventBatch(_)
        ));
        let error = session.read_next(Duration::from_secs(1)).unwrap_err();
        assert!(
            matches!(error, ClientError::Protocol(_)),
            "expected ack substitution to fail protocol validation, got {error:?}"
        );
    }

    server.join().unwrap();
    let _ = std::fs::remove_file(&socket_path);
}
