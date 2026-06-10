use std::collections::BTreeMap;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::time::{Duration, Instant};
use wattch_core::{
    EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind, Temporality, Unit,
};
use wattch_daemon::{spawn, DaemonIdentity, SampleBackend, ServerConfig, ServerHandle};
use wattch_protocol::frame::{write_frame, FrameKind};
use wattch_protocol::messages::{
    error_codes, read_message, write_message, HelloRequest, Message, StartCapture, StopCapture,
};
use wattch_protocol::PROTOCOL_VERSION;

struct CountingBackend {
    descriptors: Vec<SourceDescriptor>,
    tick: u64,
}

fn descriptor(id: u32, path: &str) -> SourceDescriptor {
    SourceDescriptor {
        local_source_id: id,
        source_uid: format!("test://node-test/{path}"),
        node_id: "node-test".to_string(),
        backend: "counting".to_string(),
        kind: SourceKind::CpuPackageEnergy,
        unit: Unit::Joule,
        temporality: Temporality::Cumulative,
        evidence_class: EvidenceClass::RawHardwareCounter,
        source_path: path.to_string(),
        parent_source_uid: None,
        clock_domain: "monotonic".to_string(),
        sampling_mode: SamplingMode::Synthetic,
        nominal_period_ns: Some(1_000_000),
        max_value: None,
        resolution: None,
        labels: BTreeMap::new(),
    }
}

impl CountingBackend {
    fn new() -> Self {
        Self {
            descriptors: vec![
                descriptor(1, "cpu/package/0/energy"),
                descriptor(2, "cpu/package/1/energy"),
            ],
            tick: 0,
        }
    }
}

impl SampleBackend for CountingBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        self.tick += 1;
        vec![
            (1, RawValue::F64(100.0 + self.tick as f64)),
            (2, RawValue::F64(200.0 + self.tick as f64)),
        ]
    }
}

fn start_server(name: &str) -> (ServerHandle, PathBuf) {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-{name}-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);
    let handle = spawn(
        ServerConfig {
            socket_path: socket_path.clone(),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
        },
        || Box::new(CountingBackend::new()),
    )
    .expect("server should start");
    (handle, socket_path)
}

fn hello(stream: &mut UnixStream) -> Message {
    write_message(
        stream,
        &Message::HelloRequest(HelloRequest {
            client_name: "session-test".to_string(),
            client_version: "0.0.0".to_string(),
            protocol_version: PROTOCOL_VERSION.to_string(),
        }),
    )
    .unwrap();
    read_message(stream).unwrap()
}

#[test]
fn hello_exposes_descriptors() {
    let (handle, socket_path) = start_server("hello");
    let mut stream = UnixStream::connect(&socket_path).unwrap();

    match hello(&mut stream) {
        Message::HelloResponse(response) => {
            assert_eq!(response.node_id, "node-test");
            assert_eq!(response.protocol_version, PROTOCOL_VERSION);
            assert_eq!(response.sources.len(), 2);
            assert_eq!(response.sources[0].local_source_id, 1);
        }
        other => panic!("expected HelloResponse, got {other:?}"),
    }

    handle.shutdown();
}

#[test]
fn capture_streams_monotonic_batches_then_acks_stop() {
    let (handle, socket_path) = start_server("capture");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "run-test".to_string(),
            interval_ms: 5,
            source_ids: vec![],
        }),
    )
    .unwrap();

    match read_message(&mut stream).unwrap() {
        Message::StartCaptureAck(ack) => {
            assert_eq!(ack.run_id, "run-test");
            assert_eq!(ack.accepted_source_ids, vec![1, 2]);
        }
        other => panic!("expected StartCaptureAck, got {other:?}"),
    }

    let mut batches = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(5);
    while batches.len() < 3 {
        assert!(Instant::now() < deadline, "timed out waiting for batches");
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(batch) => batches.push(batch),
            other => panic!("expected RawSampleBatch, got {other:?}"),
        }
    }

    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: "run-test".to_string(),
        }),
    )
    .unwrap();

    let ack = loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(batch) => batches.push(batch),
            Message::StopCaptureAck(ack) => break ack,
            other => panic!("expected batch or StopCaptureAck, got {other:?}"),
        }
    };

    assert_eq!(ack.run_id, "run-test");
    assert_eq!(ack.batches_sent, batches.len() as u64);
    assert_eq!(
        ack.samples_sent,
        batches.iter().map(|b| b.samples.len() as u64).sum::<u64>()
    );

    // Batch sequence and per-source sequence numbers must be monotonic.
    for (i, batch) in batches.iter().enumerate() {
        assert_eq!(batch.batch_sequence_no, i as u64);
        assert_eq!(batch.run_id, "run-test");
        for sample in &batch.samples {
            assert_eq!(sample.sequence_no, i as u64);
        }
    }

    // A second capture on the same connection works and keeps sequences rising.
    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "run-test-2".to_string(),
            interval_ms: 5,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    match read_message(&mut stream).unwrap() {
        Message::StartCaptureAck(ack) => assert_eq!(ack.accepted_source_ids, vec![1]),
        other => panic!("expected StartCaptureAck, got {other:?}"),
    }
    let next_batch = match read_message(&mut stream).unwrap() {
        Message::RawSampleBatch(batch) => batch,
        other => panic!("expected RawSampleBatch, got {other:?}"),
    };
    assert_eq!(next_batch.batch_sequence_no, batches.len() as u64);
    assert_eq!(next_batch.samples.len(), 1);
    assert!(next_batch.samples[0].sequence_no >= batches.len() as u64);

    handle.shutdown();
}

#[test]
fn unknown_source_id_is_rejected_without_killing_session() {
    let (handle, socket_path) = start_server("unknown-source");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "run-bad".to_string(),
            interval_ms: 5,
            source_ids: vec![99],
        }),
    )
    .unwrap();

    match read_message(&mut stream).unwrap() {
        Message::Error(err) => assert_eq!(err.code, error_codes::UNKNOWN_SOURCE_ID),
        other => panic!("expected Error, got {other:?}"),
    }

    // Session is still usable.
    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "run-good".to_string(),
            interval_ms: 5,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    handle.shutdown();
}

#[test]
fn stop_without_capture_returns_error() {
    let (handle, socket_path) = start_server("stop-idle");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: "none".to_string(),
        }),
    )
    .unwrap();
    match read_message(&mut stream).unwrap() {
        Message::Error(err) => assert_eq!(err.code, error_codes::NO_ACTIVE_CAPTURE),
        other => panic!("expected Error, got {other:?}"),
    }

    handle.shutdown();
}

#[test]
fn non_hello_first_message_is_rejected() {
    let (handle, socket_path) = start_server("bad-first");
    let mut stream = UnixStream::connect(&socket_path).unwrap();

    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: "x".to_string(),
        }),
    )
    .unwrap();
    match read_message(&mut stream).unwrap() {
        Message::Error(err) => assert_eq!(err.code, error_codes::PROTOCOL_VIOLATION),
        other => panic!("expected Error, got {other:?}"),
    }

    handle.shutdown();
}

#[test]
fn malformed_payload_is_rejected() {
    let (handle, socket_path) = start_server("bad-payload");
    let mut stream = UnixStream::connect(&socket_path).unwrap();

    write_frame(&mut stream, FrameKind::HelloRequest, 0, b"not-json").unwrap();
    match read_message(&mut stream).unwrap() {
        Message::Error(err) => assert_eq!(err.code, error_codes::PROTOCOL_VIOLATION),
        other => panic!("expected Error, got {other:?}"),
    }

    handle.shutdown();
}

#[test]
fn incompatible_protocol_version_is_rejected() {
    let (handle, socket_path) = start_server("bad-version");
    let mut stream = UnixStream::connect(&socket_path).unwrap();

    write_message(
        &mut stream,
        &Message::HelloRequest(HelloRequest {
            client_name: "old-client".to_string(),
            client_version: "0.0.1".to_string(),
            protocol_version: "9.9.9".to_string(),
        }),
    )
    .unwrap();
    match read_message(&mut stream).unwrap() {
        Message::Error(err) => assert_eq!(err.code, error_codes::UNSUPPORTED_VERSION),
        other => panic!("expected Error, got {other:?}"),
    }

    handle.shutdown();
}
