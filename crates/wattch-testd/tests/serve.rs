use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;
use wattch_core::RawValue;
use wattch_daemon::{DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, SocketSource};
use wattch_protocol::messages::{
    read_message, write_message, HelloRequest, Message, StartCapture, StopCapture,
};
use wattch_protocol::PROTOCOL_VERSION;
use wattch_testd::scenario::{generate_batches, load_scenario, DeterministicBackend};

fn example_scenario_path() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../examples/scenarios/laptop_cpu_gpu_nested.json")
}

fn quality_scenario_path() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../examples/scenarios/quality_faults.json")
}

#[test]
fn backend_matches_dry_run_golden_values() {
    let scenario = load_scenario(&example_scenario_path()).unwrap();
    let mut backend = DeterministicBackend::new(&scenario);

    for batch in generate_batches(&scenario) {
        let readings = backend.sample();
        assert_eq!(readings.len(), batch.samples.len());
        for (reading, sample) in readings.iter().zip(&batch.samples) {
            assert_eq!(reading.0, sample.source_id);
            assert_eq!(reading.1, sample.value);
        }
    }
}

#[test]
fn serves_example_scenario_over_socket() {
    let scenario = load_scenario(&example_scenario_path()).unwrap();
    let node_id = scenario.node_id.clone();
    let socket_path =
        std::env::temp_dir().join(format!("wattch-testd-test-{}.sock", std::process::id()));
    let _ = std::fs::remove_file(&socket_path);

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-testd".to_string(),
                daemon_version: "test".to_string(),
                node_id,
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        move || Box::new(DeterministicBackend::new(&scenario)),
    )
    .unwrap();

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    write_message(
        &mut stream,
        &Message::HelloRequest(HelloRequest {
            client_name: "serve-test".to_string(),
            client_version: "0.0.0".to_string(),
            protocol_version: PROTOCOL_VERSION.to_string(),
        }),
    )
    .unwrap();

    let sources = match read_message(&mut stream).unwrap() {
        Message::HelloResponse(response) => {
            assert_eq!(response.daemon_name, "wattch-testd");
            assert_eq!(response.node_id, "local-dev");
            response.sources
        }
        other => panic!("expected HelloResponse, got {other:?}"),
    };
    assert_eq!(sources.len(), 6);

    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: "serve-test-run".to_string(),
            interval_ms: 10,
            source_ids: vec![],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    let first_batch = match read_message(&mut stream).unwrap() {
        Message::RawSampleBatch(batch) => batch,
        other => panic!("expected RawSampleBatch, got {other:?}"),
    };
    assert_eq!(first_batch.samples.len(), 6);
    // Tick 0 of the example scenario: package energy starts at 1000.0.
    let package = first_batch
        .samples
        .iter()
        .find(|s| s.source_id == 1)
        .unwrap();
    assert_eq!(package.value, RawValue::F64(1000.0));

    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: "serve-test-run".to_string(),
        }),
    )
    .unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => continue,
            Message::StopCaptureAck(_) => break,
            other => panic!("expected StopCaptureAck, got {other:?}"),
        }
    }

    handle.shutdown();
}

#[test]
fn quality_faults_cross_the_live_socket_without_faking_source_clocks() {
    let scenario = load_scenario(&quality_scenario_path()).unwrap();
    let scheduled_event = scenario
        .record_events
        .first()
        .expect("quality scenario must schedule a delayed marker")
        .clone();
    let ticks = scenario.ticks as usize;
    let source_count = scenario.sources.len();
    let node_id = scenario.node_id.clone();
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-testd-quality-faults-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-testd".to_string(),
                daemon_version: "test".to_string(),
                node_id,
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        move || Box::new(DeterministicBackend::new(&scenario)),
    )
    .unwrap();

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    write_message(
        &mut stream,
        &Message::HelloRequest(HelloRequest {
            client_name: "quality-live-test".to_string(),
            client_version: "0.0.0".to_string(),
            protocol_version: PROTOCOL_VERSION.to_string(),
        }),
    )
    .unwrap();

    match read_message(&mut stream).unwrap() {
        Message::HelloResponse(response) => {
            assert_eq!(response.sources.len(), source_count);
            assert!(!response.clock_instance_id.is_empty());
        }
        other => panic!("expected HelloResponse, got {other:?}"),
    }

    const RUN_ID: &str = "quality-live-run";
    write_message(
        &mut stream,
        &Message::StartCapture(StartCapture {
            run_id: RUN_ID.to_string(),
            interval_ms: 10,
            source_ids: vec![],
        }),
    )
    .unwrap();
    assert!(matches!(
        read_message(&mut stream).unwrap(),
        Message::StartCaptureAck(_)
    ));

    let mut batches = Vec::with_capacity(ticks);
    let mut marker = None;
    let mut marker_sent_after_timestamp = None;
    while batches.len() < ticks || marker.is_none() {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(batch) => {
                assert_eq!(batch.batch_sequence_no, batches.len() as u64);
                let send_marker = batch.batch_sequence_no == scheduled_event.record_after_tick;
                if send_marker {
                    marker_sent_after_timestamp =
                        batch.samples.iter().map(|sample| sample.monotonic_ns).max();
                }
                batches.push(batch);

                if send_marker {
                    write_message(
                        &mut stream,
                        &Message::RecordEvent(scheduled_event.request(RUN_ID)),
                    )
                    .unwrap();
                }
            }
            Message::EventBatch(batch) => {
                assert_eq!(batch.run_id, RUN_ID);
                assert_eq!(batch.events.len(), 1);
                let event = batch.events[0].clone();
                assert_eq!(event.event_id, scheduled_event.event_id);
                assert_eq!(event.kind, scheduled_event.kind);
                assert_eq!(event.labels, scheduled_event.labels);

                // EventBatch and its acknowledgement are emitted adjacently
                // under the daemon writer lock.
                match read_message(&mut stream).unwrap() {
                    Message::RecordEventAck(ack) => assert_eq!(ack.event, event),
                    other => panic!("expected RecordEventAck, got {other:?}"),
                }
                marker = Some(event);
            }
            other => panic!("unexpected message during quality capture: {other:?}"),
        }
    }

    write_message(
        &mut stream,
        &Message::StopCapture(StopCapture {
            run_id: RUN_ID.to_string(),
        }),
    )
    .unwrap();
    loop {
        match read_message(&mut stream).unwrap() {
            Message::RawSampleBatch(_) => continue,
            Message::StopCaptureAck(_) => break,
            other => panic!("expected StopCaptureAck, got {other:?}"),
        }
    }

    let sample = |batch_index: usize, source_id: u32| {
        batches[batch_index]
            .samples
            .iter()
            .find(|sample| sample.source_id == source_id)
    };

    // Counter wrap crosses the backend, runtime timestamping, framing, and
    // socket path as raw values. No delta interpretation occurs here.
    assert_eq!(sample(0, 1).unwrap().value, RawValue::F64(95.0));
    assert_eq!(sample(1, 1).unwrap().value, RawValue::F64(98.0));
    assert_eq!(sample(2, 1).unwrap().value, RawValue::F64(1.0));

    // Missing acquisitions remain absent from their live batches, and a
    // source can resume afterwards. Per-source sequence numbers count emitted
    // samples; elapsed daemon timestamps carry the acquisition gap.
    assert!(sample(3, 2).is_none());
    assert!(sample(4, 2).is_none());
    assert_eq!(sample(2, 2).unwrap().sequence_no, 2);
    assert_eq!(sample(5, 2).unwrap().sequence_no, 3);
    assert!(sample(5, 2).unwrap().monotonic_ns > sample(2, 2).unwrap().monotonic_ns);

    // Disappearance is observable through the bounded capture tail.
    assert!(sample(5, 3).is_some());
    assert!((6..ticks).all(|tick| sample(tick, 3).is_none()));

    // Polling a two-state source only on even ticks produces the intended
    // alias: every live observation sees the same phase.
    let aliased = batches
        .iter()
        .flat_map(|batch| &batch.samples)
        .filter(|sample| sample.source_id == 6)
        .collect::<Vec<_>>();
    assert_eq!(aliased.len(), 5);
    assert!(aliased
        .iter()
        .all(|sample| sample.value == RawValue::F64(0.0)));

    // Per-source TimestampPattern belongs only to dry-run/replay generation.
    // A live capture deliberately gives every sample in a batch the daemon's
    // shared monotonic timestamp, including the source tagged as drifting.
    for batch in &batches {
        if let Some(first) = batch.samples.first() {
            assert!(batch
                .samples
                .iter()
                .all(|sample| sample.monotonic_ns == first.monotonic_ns));
        }
    }

    // Marker delay is represented honestly by sending RecordEvent after the
    // scheduled batch. The daemon stamps receipt time; it does not backdate
    // the event to the intended benchmark tick in its label.
    let marker = marker.unwrap();
    assert!(scheduled_event.record_after_tick > scheduled_event.intended_tick);
    assert!(marker.monotonic_ns >= marker_sent_after_timestamp.unwrap());

    handle.shutdown();
}
