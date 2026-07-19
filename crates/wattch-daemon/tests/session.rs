use std::collections::BTreeMap;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use wattch_core::{
    EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind, Temporality, Unit,
};
use wattch_daemon::{
    spawn, DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, ServerHandle, ServerLimits,
    SocketSource,
};
use wattch_protocol::frame::{write_frame, FrameKind, WireError};
use wattch_protocol::messages::{
    error_codes, read_message, write_message, HelloRequest, Message, RecordEvent, StartCapture,
    StopCapture,
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
    start_server_with_limits(name, ServerLimits::default())
}

fn start_server_with_limits(name: &str, limits: ServerLimits) -> (ServerHandle, PathBuf) {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-{name}-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);
    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits,
        },
        || Box::new(CountingBackend::new()),
    )
    .expect("server should start");
    (handle, socket_path)
}

#[test]
fn rejects_amplifying_capture_requests_without_killing_session() {
    let (handle, socket_path) = start_server("bounded-requests");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    let invalid = [
        StartCapture {
            run_id: String::new(),
            interval_ms: 10,
            source_ids: vec![],
        },
        StartCapture {
            run_id: "x".repeat(257),
            interval_ms: 10,
            source_ids: vec![],
        },
        StartCapture {
            run_id: "too-fast".to_string(),
            interval_ms: 1,
            source_ids: vec![],
        },
        StartCapture {
            run_id: "duplicates".to_string(),
            interval_ms: 10,
            source_ids: vec![1, 1],
        },
    ];
    for request in invalid {
        write_message(&mut stream, &Message::StartCapture(request)).unwrap();
        match read_message(&mut stream).unwrap() {
            Message::Error(error) => assert_eq!(error.code, error_codes::INVALID_REQUEST),
            other => panic!("expected INVALID_REQUEST, got {other:?}"),
        }
    }

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "valid-after-errors".to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));
    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: "valid-after-errors".to_string(),
        }),
    )
    .unwrap();
    loop {
        if matches!(
            read_message(&mut stream).unwrap(),
            Message::StopCaptureAck(_)
        ) {
            break;
        }
    }
    handle.shutdown();
}

#[test]
fn handshake_and_idle_reads_have_deadlines() {
    let limits = ServerLimits {
        hello_timeout: Duration::from_millis(40),
        session_idle_timeout: Duration::from_millis(40),
        ..Default::default()
    };
    let (handle, socket_path) = start_server_with_limits("read-deadlines", limits);

    let mut silent = UnixStream::connect(&socket_path).unwrap();
    silent
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    match read_message(&mut silent).unwrap() {
        Message::Error(error) => assert_eq!(error.code, error_codes::TIMEOUT),
        other => panic!("expected handshake TIMEOUT, got {other:?}"),
    }

    let mut trickle = UnixStream::connect(&socket_path).unwrap();
    trickle
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    trickle.write_all(b"W").unwrap();
    std::thread::sleep(Duration::from_millis(60));
    match read_message(&mut trickle).unwrap() {
        Message::Error(error) => assert_eq!(error.code, error_codes::TIMEOUT),
        other => panic!("expected partial-frame TIMEOUT, got {other:?}"),
    }

    let mut idle = UnixStream::connect(&socket_path).unwrap();
    idle.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
    hello(&mut idle);
    match read_message(&mut idle).unwrap() {
        Message::Error(error) => assert_eq!(error.code, error_codes::TIMEOUT),
        other => panic!("expected idle TIMEOUT, got {other:?}"),
    }
    handle.shutdown();
}

#[test]
fn global_connection_limit_rejects_excess_sessions() {
    let limits = ServerLimits {
        max_connections: 1,
        max_connections_per_uid: 1,
        ..Default::default()
    };
    let (handle, socket_path) = start_server_with_limits("connection-limit", limits);
    let mut first = UnixStream::connect(&socket_path).unwrap();
    hello(&mut first);

    let mut second = UnixStream::connect(&socket_path).unwrap();
    second
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    match read_message(&mut second).unwrap() {
        Message::Error(error) => assert_eq!(error.code, error_codes::RESOURCE_EXHAUSTED),
        other => panic!("expected RESOURCE_EXHAUSTED, got {other:?}"),
    }
    drop(first);
    handle.shutdown();
}

#[test]
fn active_capture_limit_and_max_duration_are_enforced() {
    let limits = ServerLimits {
        max_active_captures: 1,
        max_active_captures_per_uid: 1,
        max_capture_duration: Duration::from_millis(80),
        ..Default::default()
    };
    let (handle, socket_path) = start_server_with_limits("capture-limit", limits);
    let mut first = UnixStream::connect(&socket_path).unwrap();
    let mut second = UnixStream::connect(&socket_path).unwrap();
    hello(&mut first);
    hello(&mut second);

    let start = |run_id: &str| {
        Message::StartCapture(StartCapture {
            run_id: run_id.to_string(),
            interval_ms: 10,
            source_ids: vec![],
        })
    };
    write_message(&mut first, &start("held-capture")).unwrap();
    assert!(matches!(
        read_message(&mut first).unwrap(),
        Message::StartCaptureAck(_)
    ));

    write_message(&mut second, &start("rejected-capture")).unwrap();
    match read_message(&mut second).unwrap() {
        Message::Error(error) => assert_eq!(error.code, error_codes::RESOURCE_EXHAUSTED),
        other => panic!("expected RESOURCE_EXHAUSTED, got {other:?}"),
    }

    first
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    loop {
        match read_message(&mut first).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::Error(error) => {
                assert_eq!(error.code, error_codes::TIMEOUT);
                break;
            }
            other => panic!("expected batch or TIMEOUT, got {other:?}"),
        }
    }
    handle.shutdown();
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
fn daemon_clock_identity_is_shared_across_connections_and_changes_on_restart() {
    let (handle, socket_path) = start_server("clock-identity");
    let mut first = UnixStream::connect(&socket_path).unwrap();
    let mut second = UnixStream::connect(&socket_path).unwrap();

    let first_clock = match hello(&mut first) {
        Message::HelloResponse(response) => response.clock_instance_id,
        other => panic!("expected HelloResponse, got {other:?}"),
    };
    let second_clock = match hello(&mut second) {
        Message::HelloResponse(response) => response.clock_instance_id,
        other => panic!("expected HelloResponse, got {other:?}"),
    };
    assert!(!first_clock.is_empty());
    assert_eq!(second_clock, first_clock);
    drop(first);
    drop(second);
    handle.shutdown();

    let (restarted, _) = start_server("clock-identity");
    let mut stream = UnixStream::connect(restarted.socket_path()).unwrap();
    let restarted_clock = match hello(&mut stream) {
        Message::HelloResponse(response) => response.clock_instance_id,
        other => panic!("expected HelloResponse, got {other:?}"),
    };
    assert_ne!(restarted_clock, first_clock);
    drop(stream);
    restarted.shutdown();
}

#[test]
fn sample_timestamps_share_the_daemon_epoch_across_connections() {
    let (handle, socket_path) = start_server("shared-clock-epoch");

    let mut first = UnixStream::connect(&socket_path).unwrap();
    hello(&mut first);
    // A connection-relative clock would include this delay only in the first
    // capture and reset close to zero for the second connection.
    std::thread::sleep(Duration::from_millis(50));
    let first_timestamp = capture_one_timestamp(&mut first, "first-clock-run");
    drop(first);

    let mut second = UnixStream::connect(&socket_path).unwrap();
    hello(&mut second);
    let second_timestamp = capture_one_timestamp(&mut second, "second-clock-run");
    assert!(
        second_timestamp > first_timestamp,
        "later connections must continue the daemon clock: {second_timestamp} <= {first_timestamp}"
    );

    drop(second);
    handle.shutdown();
}

fn capture_one_timestamp(stream: &mut UnixStream, run_id: &str) -> u64 {
    write_message(
        stream,
        &Message::StartCapture(StartCapture {
            run_id: run_id.to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(stream).unwrap(),
        Message::StartCaptureAck(_)
    ));
    let timestamp = loop {
        if let Message::RawSampleBatch(batch) = read_message(stream).unwrap() {
            break batch.samples[0].monotonic_ns;
        }
    };
    write_message(
        stream,
        &Message::StopCapture(StopCapture {
            run_id: run_id.to_string(),
        }),
    )
    .unwrap();
    loop {
        if matches!(read_message(stream).unwrap(), Message::StopCaptureAck(_)) {
            break;
        }
    }
    timestamp
}

struct CaptureLifecycleBackend {
    descriptors: Vec<SourceDescriptor>,
    events: Arc<Mutex<Vec<&'static str>>>,
    samples: Arc<AtomicUsize>,
}

impl SampleBackend for CaptureLifecycleBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn begin_capture(&mut self) {
        self.events
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push("begin");
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        self.events
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push("sample");
        let value = self.samples.fetch_add(1, Ordering::SeqCst) as f64;
        vec![(1, RawValue::F64(value))]
    }
}

#[test]
fn accepted_capture_calls_begin_once_before_sampling() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let samples = Arc::new(AtomicUsize::new(0));
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-capture-begin-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);

    let factory_events = Arc::clone(&events);
    let factory_samples = Arc::clone(&samples);
    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        move || {
            Box::new(CaptureLifecycleBackend {
                descriptors: vec![descriptor(1, "cpu/package/0/energy")],
                events: Arc::clone(&factory_events),
                samples: Arc::clone(&factory_samples),
            })
        },
    )
    .expect("server should start");

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "invalid-before-capture".to_string(),
            interval_ms: 1,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::Error(_)
    ));

    capture_one_timestamp(&mut stream, "capture-one");
    capture_one_timestamp(&mut stream, "capture-two");

    let events = events
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let starts: Vec<usize> = events
        .iter()
        .enumerate()
        .filter_map(|(index, event)| (*event == "begin").then_some(index))
        .collect();
    assert_eq!(starts.len(), 2, "only accepted StartCapture calls prepare");
    assert_eq!(starts[0], 0, "capture preparation precedes every sample");
    assert!(events[starts[0] + 1..starts[1]].contains(&"sample"));
    assert!(events[starts[1] + 1..].contains(&"sample"));

    drop(events);
    drop(stream);
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
            interval_ms: 10,
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
            interval_ms: 10,
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
fn record_events_are_daemon_stamped_batched_then_acknowledged() {
    const EVENT_COUNT: usize = 24;

    let (handle, socket_path) = start_server("record-events");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "event-run".to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    for index in 0..EVENT_COUNT {
        write_message(
            &mut stream,
            &Message::RecordEvent(RecordEvent {
                run_id: "event-run".to_string(),
                event_id: format!("event-{index}"),
                kind: "TARGET_PROGRESS".to_string(),
                labels: BTreeMap::from([("index".to_string(), index.to_string())]),
            }),
        )
        .unwrap();
    }

    let mut raw_batches = Vec::new();
    let mut events_seen = 0;
    while events_seen < EVENT_COUNT {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(batch) => raw_batches.push(batch),
            Message::EventBatch(batch) => {
                assert_eq!(batch.run_id, "event-run");
                assert_eq!(batch.node_id, "node-test");
                assert_eq!(batch.batch_sequence_no, events_seen as u64);
                assert_eq!(batch.events.len(), 1);
                let event = &batch.events[0];
                assert_eq!(event.event_id, format!("event-{events_seen}"));
                assert_eq!(event.run_id, batch.run_id);
                assert_eq!(event.node_id, batch.node_id);
                assert_eq!(event.kind, "TARGET_PROGRESS");
                assert_eq!(event.labels.get("index"), Some(&events_seen.to_string()));

                // The pair is written under one lock: no raw batch can be
                // interleaved, and the acknowledgement proves this exact
                // authoritative event has already been emitted.
                match read_message(&mut stream).unwrap() {
                    Message::RecordEventAck(ack) => assert_eq!(ack.event, *event),
                    other => panic!("expected RecordEventAck after EventBatch, got {other:?}"),
                }
                events_seen += 1;
            }
            other => panic!("expected raw/event frame, got {other:?}"),
        }
    }

    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: "event-run".to_string(),
        }),
    )
    .unwrap();
    let stop_ack = loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(batch) => raw_batches.push(batch),
            Message::StopCaptureAck(ack) => break ack,
            other => panic!("expected raw batch or StopCaptureAck, got {other:?}"),
        }
    };
    assert_eq!(stop_ack.batches_sent, raw_batches.len() as u64);
    assert_eq!(
        stop_ack.samples_sent,
        raw_batches
            .iter()
            .map(|batch| batch.samples.len() as u64)
            .sum::<u64>()
    );
    for (expected, batch) in raw_batches.iter().enumerate() {
        assert_eq!(batch.batch_sequence_no, expected as u64);
    }

    // Event batches keep a sequence namespace separate from raw samples and
    // continue monotonically across captures on the same connection.
    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "event-run-2".to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));
    write_message(
        &mut stream,
        &Message::RecordEvent(RecordEvent {
            run_id: "event-run-2".to_string(),
            event_id: "event-0".to_string(),
            kind: "TARGET_EXIT".to_string(),
            labels: BTreeMap::new(),
        }),
    )
    .unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::EventBatch(batch) => {
                assert_eq!(batch.batch_sequence_no, EVENT_COUNT as u64);
                assert!(matches!(
                    read_message(&mut stream).unwrap(),
                    Message::RecordEventAck(_)
                ));
                break;
            }
            other => panic!("expected raw/event frame, got {other:?}"),
        }
    }

    drop(stream);
    handle.shutdown();
}

#[test]
fn record_event_outside_capture_is_rejected_without_killing_session() {
    let (handle, socket_path) = start_server("event-outside-capture");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::RecordEvent(RecordEvent {
            run_id: "not-active".to_string(),
            event_id: "target-spawn".to_string(),
            kind: "TARGET_SPAWN".to_string(),
            labels: BTreeMap::new(),
        }),
    )
    .unwrap();
    match read_message(&mut stream).unwrap() {
        Message::Error(error) => assert_eq!(error.code, error_codes::NO_ACTIVE_CAPTURE),
        other => panic!("expected NO_ACTIVE_CAPTURE, got {other:?}"),
    }

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "usable-after-event-error".to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    drop(stream);
    handle.shutdown();
}

#[test]
fn malformed_and_duplicate_events_are_rejected_without_stopping_capture() {
    let (handle, socket_path) = start_server("invalid-events");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "event-validation".to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    let event = |event_id: &str| {
        Message::RecordEvent(RecordEvent {
            run_id: "event-validation".to_string(),
            event_id: event_id.to_string(),
            kind: "TARGET_EXEC".to_string(),
            labels: BTreeMap::new(),
        })
    };
    write_message(&mut stream, &event("")).unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::Error(error) => {
                assert_eq!(error.code, error_codes::INVALID_REQUEST);
                break;
            }
            other => panic!("expected raw batch or INVALID_REQUEST, got {other:?}"),
        }
    }

    write_message(&mut stream, &event("target-exec")).unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::EventBatch(_) => {
                assert!(matches!(
                    read_message(&mut stream).unwrap(),
                    Message::RecordEventAck(_)
                ));
                break;
            }
            other => panic!("expected raw/event frame, got {other:?}"),
        }
    }

    write_message(&mut stream, &event("target-exec")).unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::Error(error) => {
                assert_eq!(error.code, error_codes::INVALID_REQUEST);
                assert!(error.message.contains("unique"));
                break;
            }
            other => panic!("expected raw batch or duplicate error, got {other:?}"),
        }
    }

    // The capture remains usable after both request errors.
    write_message(&mut stream, &event("target-exit")).unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::EventBatch(_) => {
                assert!(matches!(
                    read_message(&mut stream).unwrap(),
                    Message::RecordEventAck(_)
                ));
                break;
            }
            other => panic!("expected raw/event frame, got {other:?}"),
        }
    }

    drop(stream);
    handle.shutdown();
}

#[test]
fn record_event_cannot_inject_a_marker_into_another_run() {
    let (handle, socket_path) = start_server("event-run-mismatch");
    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "active-run".to_string(),
            interval_ms: 10,
            source_ids: vec![1],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));
    write_message(
        &mut stream,
        &Message::RecordEvent(RecordEvent {
            run_id: "other-run".to_string(),
            event_id: "target-exec".to_string(),
            kind: "TARGET_EXEC".to_string(),
            labels: BTreeMap::new(),
        }),
    )
    .unwrap();

    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => {}
            Message::Error(error) => {
                assert_eq!(error.code, error_codes::PROTOCOL_VIOLATION);
                assert!(error.message.contains("does not match"));
                break;
            }
            other => panic!("expected raw batch or protocol error, got {other:?}"),
        }
    }
    assert!(matches!(
        read_message(&mut stream),
        Err(WireError::ConnectionClosed)
    ));

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
            interval_ms: 10,
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
            interval_ms: 10,
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

/// A backend that samples cleanly `panic_after` times, then panics on the
/// next `sample()` call (simulating a producer that dies mid-capture), first
/// flipping `panicked` so tests can wait for the panic to have actually
/// happened before racing it against a `StopCapture`.
struct FlakyBackend {
    descriptors: Vec<SourceDescriptor>,
    calls: u32,
    panic_after: u32,
    panicked: Arc<AtomicBool>,
}

impl SampleBackend for FlakyBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        self.calls += 1;
        if self.calls > self.panic_after {
            self.panicked.store(true, Ordering::SeqCst);
            panic!("simulated backend failure after {} calls", self.panic_after);
        }
        vec![
            (1, RawValue::F64(100.0 + self.calls as f64)),
            (2, RawValue::F64(200.0 + self.calls as f64)),
        ]
    }
}

#[test]
fn producer_panic_closes_capture_instead_of_leaving_reader_blocked() {
    let panicked = Arc::new(AtomicBool::new(false));
    let panic_after: u32 = 3;

    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-panic-counts-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);

    let factory_panicked = Arc::clone(&panicked);
    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        move || {
            Box::new(FlakyBackend {
                descriptors: vec![
                    descriptor(1, "cpu/package/0/energy"),
                    descriptor(2, "cpu/package/1/energy"),
                ],
                calls: 0,
                panic_after,
                panicked: Arc::clone(&factory_panicked),
            })
        },
    )
    .expect("server should start");

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    hello(&mut stream);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "run-panic".to_string(),
            interval_ms: 10,
            source_ids: vec![],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    let mut batches = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(5);
    while batches.len() < panic_after as usize {
        assert!(Instant::now() < deadline, "timed out waiting for batches");
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(batch) => batches.push(batch),
            other => panic!("expected RawSampleBatch, got {other:?}"),
        }
    }

    // Wait until the producer has actually panicked on its next sample().
    let deadline = Instant::now() + Duration::from_secs(5);
    while !panicked.load(Ordering::SeqCst) {
        assert!(Instant::now() < deadline, "producer never panicked");
        std::thread::sleep(Duration::from_millis(5));
    }

    assert!(
        matches!(read_message(&mut stream), Err(WireError::ConnectionClosed)),
        "a failed producer must tear down the session without waiting for StopCapture"
    );
    assert_eq!(batches.len(), panic_after as usize);

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

/// SocketSource::Activated covers the systemd-socket-activation serving
/// path on every platform: the test plays the role of systemd by binding
/// the listener itself. The server must serve sessions on it and must NOT
/// unlink the socket file on shutdown — the activator owns its lifecycle.
#[test]
fn activated_listener_serves_and_socket_survives_shutdown() {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-activated-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);
    let listener = std::os::unix::net::UnixListener::bind(&socket_path).unwrap();

    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Activated(listener),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        || Box::new(CountingBackend::new()),
    )
    .expect("server should serve a pre-bound listener");
    assert_eq!(handle.socket_path(), socket_path.as_path());

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    match hello(&mut stream) {
        Message::HelloResponse(response) => assert_eq!(response.node_id, "node-test"),
        other => panic!("expected HelloResponse, got {other:?}"),
    }
    drop(stream);

    handle.shutdown();
    assert!(
        socket_path.exists(),
        "activated sockets belong to the activator; shutdown must not unlink"
    );
    let _ = std::fs::remove_file(&socket_path);
}

/// Linux-only: on non-Linux dev builds there are no peer credentials, so
/// there is nothing to deny (the server allows all peers by design there).
#[cfg(target_os = "linux")]
#[test]
fn deny_all_policy_rejects_with_access_denied() {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-deny-all-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);

    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            // Built literally (fields are public) — with_grants() would
            // always re-admit our own euid.
            peer_policy: PeerPolicy {
                allowed_uids: vec![],
                allowed_gids: vec![],
            },
            limits: Default::default(),
        },
        || Box::new(CountingBackend::new()),
    )
    .expect("server should start");

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    match read_message(&mut stream) {
        Ok(Message::Error(err)) => {
            assert_eq!(err.code, error_codes::ACCESS_DENIED);
            assert_eq!(err.message, "access denied");
        }
        other => panic!("expected ACCESS_DENIED Error frame, got {other:?}"),
    }
    // After the error frame the daemon closes the connection.
    assert!(
        read_message(&mut stream).is_err(),
        "connection must be closed after rejection"
    );

    // Shutdown still works under deny-all: the wake-up connect only needs
    // the accept() to fire; rejection happens after accept.
    handle.shutdown();
}
