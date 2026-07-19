use serde_json::json;
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Barrier};
use std::time::Duration;
use wattch_daemon::{DaemonIdentity, PeerPolicy, ServerConfig, SocketSource};
use wattch_mcp::{ServiceConfig, WattchService};
use wattch_testd::scenario::{load_scenario, DeterministicBackend};

fn temp_root() -> PathBuf {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "wattch-mcp-capture-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ))
}

#[test]
fn stateful_capture_persists_window_events_tail_and_inspectable_artifact() {
    let root = temp_root();
    fs::create_dir_all(&root).unwrap();
    let socket = root.join("testd.sock");
    let scenario_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../examples/scenarios/laptop_cpu_gpu_nested.json");
    let scenario = load_scenario(&scenario_path).unwrap();
    let identity = DaemonIdentity {
        daemon_name: "wattch-testd".to_string(),
        daemon_version: "integration".to_string(),
        node_id: scenario.node_id.clone(),
    };
    let daemon_scenario = scenario.clone();
    let daemon = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket.clone()),
            identity,
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        move || Box::new(DeterministicBackend::new(&daemon_scenario)),
    )
    .unwrap();

    let service = Arc::new(
        WattchService::new(ServiceConfig {
            default_socket: Some(socket),
            runs_dir: root.join("runs"),
            workspace_root: root.clone(),
            max_active_captures: 1,
            max_capture_duration: Duration::from_secs(10),
            startup_timeout: Duration::from_secs(5),
        })
        .unwrap(),
    );

    let sources = service.sources(json!({})).unwrap();
    assert_eq!(sources["daemon"]["name"], "wattch-testd");
    assert!(sources["source_count"].as_u64().unwrap() >= 4);

    let started = service
        .capture_start(json!({
            "run_id": "mcp-integration-run",
            "interval_ms": 20,
            "labels": {
                "model": "terra-medium",
                "skill_enabled": "true",
                "prompt_sha256": "test-digest",
                "repetition": "1"
            }
        }))
        .unwrap();
    assert_eq!(started["status"], "capturing");
    assert_eq!(started["start_event"]["kind"], "WATTCH_WINDOW_START");
    let capture_id = started["capture_id"].as_str().unwrap();

    let active_inspect = service
        .inspect(json!({"run": "runs/mcp-integration-run"}))
        .unwrap();
    assert_eq!(active_inspect["valid"], false);
    assert_eq!(active_inspect["trace_valid"], true);
    assert_eq!(active_inspect["capture_status"]["status"], "capturing");
    let active_validation = service
        .validate(json!({"run": "runs/mcp-integration-run"}))
        .unwrap_err();
    assert_eq!(active_validation.code, "CAPTURE_NOT_FINALIZED");

    std::thread::sleep(Duration::from_millis(80));
    let stopped = service
        .capture_stop(json!({"capture_id": capture_id}))
        .unwrap();
    assert_eq!(stopped["status"], "stopped");
    assert_eq!(stopped["end_event"]["kind"], "WATTCH_WINDOW_END");
    assert_eq!(stopped["tail_complete"], true);
    assert_eq!(stopped["inspect"]["capture_status"]["status"], "complete");
    assert!(stopped["samples_sent"].as_u64().unwrap() > 0);
    assert_eq!(
        stopped["inspect"]["per_event_kind"]["WATTCH_WINDOW_START"],
        1
    );
    assert_eq!(stopped["inspect"]["per_event_kind"]["WATTCH_WINDOW_END"], 1);
    assert!(stopped["inspect"]["warnings"]
        .as_array()
        .unwrap()
        .iter()
        .any(|warning| warning.as_str().unwrap().contains("plumbing only")));

    let run_dir = PathBuf::from(stopped["run_dir"].as_str().unwrap());
    for name in [
        "manifest.json",
        "sources.json",
        "raw.trace.wch",
        "events.trace.wch",
        "mcp-capture.json",
        "mcp-capture-terminal.json",
    ] {
        assert!(run_dir.join(name).is_file(), "missing {name}");
    }
    assert_eq!(
        service
            .validate(json!({"run": "runs/mcp-integration-run"}))
            .unwrap()["valid"],
        true
    );
    let terminal_path = run_dir.join("mcp-capture-terminal.json");
    let terminal_bytes = fs::read(&terminal_path).unwrap();
    let mut tampered: serde_json::Value = serde_json::from_slice(&terminal_bytes).unwrap();
    tampered["batches_sent"] = json!(tampered["batches_sent"].as_u64().unwrap() + 1);
    fs::write(
        &terminal_path,
        serde_json::to_vec_pretty(&tampered).unwrap(),
    )
    .unwrap();
    assert_eq!(
        service
            .validate(json!({"run": "runs/mcp-integration-run"}))
            .unwrap_err()
            .code,
        "INVALID_ARTIFACT"
    );
    fs::write(&terminal_path, &terminal_bytes).unwrap();

    let mut tampered: serde_json::Value = serde_json::from_slice(&terminal_bytes).unwrap();
    tampered["tail_target_monotonic_ns"] =
        json!(tampered["tail_target_monotonic_ns"].as_u64().unwrap() + 1);
    fs::write(
        &terminal_path,
        serde_json::to_vec_pretty(&tampered).unwrap(),
    )
    .unwrap();
    assert_eq!(
        service
            .validate(json!({"run": "runs/mcp-integration-run"}))
            .unwrap_err()
            .code,
        "INVALID_ARTIFACT"
    );
    fs::write(&terminal_path, &terminal_bytes).unwrap();

    let barrier = Arc::new(Barrier::new(3));
    let contenders = ["mcp-concurrent-a", "mcp-concurrent-b"]
        .into_iter()
        .map(|run_id| {
            let service = Arc::clone(&service);
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                barrier.wait();
                (
                    run_id,
                    service.capture_start(json!({
                        "run_id": run_id,
                        "interval_ms": 20
                    })),
                )
            })
        })
        .collect::<Vec<_>>();
    barrier.wait();
    let results = contenders
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        results.iter().filter(|(_, result)| result.is_ok()).count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter_map(|(_, result)| result.as_ref().err())
            .map(|error| error.code)
            .collect::<Vec<_>>(),
        vec!["CAPTURE_LIMIT"]
    );
    let concurrent_capture_id = results
        .iter()
        .find_map(|(_, result)| {
            result
                .as_ref()
                .ok()
                .and_then(|value| value["capture_id"].as_str())
        })
        .unwrap();
    service
        .capture_stop(json!({"capture_id": concurrent_capture_id}))
        .unwrap();

    let expiry_service = WattchService::new(ServiceConfig {
        default_socket: Some(root.join("testd.sock")),
        runs_dir: root.join("expiry-runs"),
        workspace_root: root.clone(),
        max_active_captures: 1,
        max_capture_duration: Duration::from_millis(60),
        startup_timeout: Duration::from_secs(5),
    })
    .unwrap();
    let expired = expiry_service
        .capture_start(json!({"run_id": "expired-a", "interval_ms": 20}))
        .unwrap();
    let expired_capture_id = expired["capture_id"].as_str().unwrap().to_string();
    std::thread::sleep(Duration::from_millis(300));
    let replacement = expiry_service
        .capture_start(json!({"run_id": "expired-b", "interval_ms": 20}))
        .unwrap();
    let expired_result = expiry_service
        .capture_stop(json!({"capture_id": expired_capture_id}))
        .unwrap();
    assert_eq!(expired_result["status"], "max_duration_reached");
    expiry_service
        .capture_stop(json!({"capture_id": replacement["capture_id"]}))
        .unwrap();

    let eof_started = service
        .capture_start(json!({
            "run_id": "mcp-eof-run",
            "interval_ms": 20
        }))
        .unwrap();
    assert_eq!(eof_started["status"], "capturing");
    service.shutdown_all();
    let eof_inspect = service.inspect(json!({"run": "runs/mcp-eof-run"})).unwrap();
    assert_eq!(eof_inspect["per_event_kind"]["WATTCH_WINDOW_START"], 1);
    assert_eq!(eof_inspect["per_event_kind"]["WATTCH_WINDOW_END"], 1);

    daemon.shutdown();
    let _ = fs::remove_dir_all(root);
}
