use std::os::unix::net::UnixStream;
use std::path::Path;
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
