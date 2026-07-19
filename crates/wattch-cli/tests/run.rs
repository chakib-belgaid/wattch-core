use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU32, Ordering};
use wattch_cli::artifact::{
    inspect_run, replay_events, replay_trace, CommandLifecycleStatus, COMMAND_LIFECYCLE_FILE,
};
use wattch_core::{
    EvidenceClass, RawValue, SamplingMode, SourceDescriptor, SourceKind, Temporality, Unit,
    INTERVAL_ANCHOR_LABEL, INTERVAL_DURATION_SOURCE_UID_LABEL,
    INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP,
};
use wattch_daemon::{
    DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, ServerHandle, SocketSource,
};

const NODE_ID: &str = "node-run-cli";
const INTERVAL_NS: u64 = 10_000_000;

#[derive(Clone, Copy)]
enum BackendShape {
    Cumulative,
    IntervalAverage,
    SparsePeriodic,
}

struct TestBackend {
    descriptors: Vec<SourceDescriptor>,
    shape: BackendShape,
    tick: u64,
}

impl SampleBackend for TestBackend {
    fn descriptors(&self) -> &[SourceDescriptor] {
        &self.descriptors
    }

    fn sample(&mut self) -> Vec<(u32, RawValue)> {
        self.tick += 1;
        match self.shape {
            BackendShape::Cumulative => vec![(
                1,
                RawValue::I64(1_000_000 + i64::try_from(self.tick).unwrap() * 1_000),
            )],
            BackendShape::IntervalAverage => vec![
                (1, RawValue::F64(2_500.0 + self.tick as f64)),
                (2, RawValue::I64(INTERVAL_NS as i64)),
            ],
            BackendShape::SparsePeriodic => {
                let source_id = if self.tick % 2 == 1 { 1 } else { 2 };
                vec![(source_id, RawValue::I64(self.tick as i64))]
            }
        }
    }
}

struct TestServer(Option<ServerHandle>);

impl Drop for TestServer {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            handle.shutdown();
        }
    }
}

fn source(
    id: u32,
    uid: &str,
    path: &str,
    semantics: (SourceKind, Unit, Temporality, EvidenceClass),
    labels: BTreeMap<String, String>,
) -> SourceDescriptor {
    let (kind, unit, temporality, evidence_class) = semantics;
    SourceDescriptor {
        local_source_id: id,
        source_uid: uid.to_string(),
        node_id: NODE_ID.to_string(),
        backend: "run-test".to_string(),
        kind,
        unit,
        temporality,
        evidence_class,
        source_path: path.to_string(),
        parent_source_uid: None,
        clock_domain: "monotonic".to_string(),
        sampling_mode: SamplingMode::Synthetic,
        nominal_period_ns: Some(INTERVAL_NS),
        max_value: None,
        resolution: None,
        labels,
    }
}

fn descriptors(shape: BackendShape) -> Vec<SourceDescriptor> {
    match shape {
        BackendShape::Cumulative => vec![source(
            1,
            "test://node-run-cli/cpu/package/0/energy",
            "cpu/package/0/energy",
            (
                SourceKind::CpuPackageEnergy,
                Unit::Microjoule,
                Temporality::Cumulative,
                EvidenceClass::RawHardwareCounter,
            ),
            BTreeMap::new(),
        )],
        BackendShape::IntervalAverage => {
            let duration_uid = "test://node-run-cli/collector/frame/elapsed-ns";
            let power_labels = BTreeMap::from([
                (
                    INTERVAL_DURATION_SOURCE_UID_LABEL.to_string(),
                    duration_uid.to_string(),
                ),
                (
                    INTERVAL_ANCHOR_LABEL.to_string(),
                    INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP.to_string(),
                ),
            ]);
            vec![
                source(
                    1,
                    "test://node-run-cli/cpu/power",
                    "cpu/power",
                    (
                        SourceKind::CpuPower,
                        Unit::Milliwatt,
                        Temporality::IntervalAverage,
                        EvidenceClass::ToolDerivedEstimate,
                    ),
                    power_labels,
                ),
                source(
                    2,
                    duration_uid,
                    "collector/frame/elapsed-ns",
                    (
                        SourceKind::SampleIntervalDuration,
                        Unit::Nanosecond,
                        Temporality::Delta,
                        EvidenceClass::ToolDerivedEstimate,
                    ),
                    BTreeMap::new(),
                ),
            ]
        }
        BackendShape::SparsePeriodic => vec![
            source(
                1,
                "test://node-run-cli/sparse/one",
                "sparse/one",
                (
                    SourceKind::WakeupCounter,
                    Unit::Counter,
                    Temporality::Cumulative,
                    EvidenceClass::OsTelemetry,
                ),
                BTreeMap::new(),
            ),
            source(
                2,
                "test://node-run-cli/sparse/two",
                "sparse/two",
                (
                    SourceKind::CStateResidency,
                    Unit::Percent,
                    Temporality::Gauge,
                    EvidenceClass::OsTelemetry,
                ),
                BTreeMap::new(),
            ),
        ],
    }
}

fn unique_path(tag: &str, suffix: &str) -> PathBuf {
    static COUNTER: AtomicU32 = AtomicU32::new(0);
    let sequence = COUNTER.fetch_add(1, Ordering::SeqCst);
    std::env::temp_dir().join(format!(
        "wattch-run-{tag}-{}-{sequence}{suffix}",
        std::process::id()
    ))
}

fn start_server(tag: &str, shape: BackendShape) -> (TestServer, PathBuf) {
    let socket = unique_path(tag, ".sock");
    let _ = std::fs::remove_file(&socket);
    let template = descriptors(shape);
    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket.clone()),
            identity: DaemonIdentity {
                daemon_name: "run-testd".to_string(),
                daemon_version: "test".to_string(),
                node_id: NODE_ID.to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
            limits: Default::default(),
        },
        move || {
            Box::new(TestBackend {
                descriptors: template.clone(),
                shape,
                tick: 0,
            })
        },
    )
    .unwrap();
    (TestServer(Some(handle)), socket)
}

fn output_root(tag: &str) -> PathBuf {
    let root = unique_path(tag, "");
    let _ = std::fs::remove_dir_all(&root);
    root
}

fn run_cli(socket: &Path, out: &Path, run_id: &str, labels: &[&str], program: &[&str]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_wattch"));
    command
        .arg("run")
        .arg("--socket")
        .arg(socket)
        .arg("--out")
        .arg(out)
        .arg("--interval-ms")
        .arg("10")
        .arg("--run-id")
        .arg(run_id);
    for label in labels {
        command.arg("--label").arg(label);
    }
    command
        .arg("--json")
        .arg("--")
        .args(program)
        .output()
        .unwrap()
}

fn summary(output: &Output) -> Value {
    assert!(
        output.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout.clone()).unwrap();
    assert_eq!(stdout.lines().count(), 1, "{stdout:?}");
    serde_json::from_str(stdout.trim()).unwrap()
}

#[test]
fn sources_json_is_one_stable_discovery_object_with_full_descriptors() {
    let (_server, socket) = start_server("sources-json", BackendShape::Cumulative);

    let output = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("sources")
        .arg("--socket")
        .arg(&socket)
        .arg("--json")
        .output()
        .unwrap();
    assert!(output.status.success());
    let result = summary(&output);
    assert_eq!(
        result
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "clock_instance_id".to_string(),
            "daemon".to_string(),
            "protocol_version".to_string(),
            "sources".to_string(),
        ])
    );
    assert_eq!(
        result["daemon"],
        serde_json::json!({
            "name": "run-testd",
            "version": "test",
            "node_id": NODE_ID,
        })
    );
    assert_eq!(
        result["protocol_version"],
        wattch_protocol::PROTOCOL_VERSION
    );
    assert!(result["clock_instance_id"]
        .as_str()
        .is_some_and(|value| !value.is_empty()));
    assert_eq!(
        result["sources"],
        serde_json::to_value(descriptors(BackendShape::Cumulative)).unwrap()
    );

    let human = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("sources")
        .arg("--socket")
        .arg(&socket)
        .output()
        .unwrap();
    assert!(human.status.success());
    let human_stdout = String::from_utf8(human.stdout).unwrap();
    assert!(human_stdout.starts_with(&format!(
        "daemon: run-testd test (node {NODE_ID})\nid   path"
    )));
    assert!(human_stdout.contains("cpu/package/0/energy"));
}

fn replayed_events(run_dir: &Path) -> Vec<wattch_protocol::messages::Event> {
    let mut events = Vec::new();
    replay_events(run_dir, |batch| events.extend(batch.events)).unwrap();
    events
}

#[test]
fn run_success_emits_clean_json_and_complete_artifact() {
    let (_server, socket) = start_server("success", BackendShape::Cumulative);
    let out = output_root("success-out");
    let output = run_cli(
        &socket,
        &out,
        "run-success",
        &["model=terra", "note=a=b"],
        &["/bin/sh", "-c", "printf '%s' \"$$\"; printf err >&2"],
    );
    assert_eq!(output.status.code(), Some(0));
    let result = summary(&output);
    assert_eq!(
        result
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "batches".to_string(),
            "exit_code".to_string(),
            "run_dir".to_string(),
            "run_id".to_string(),
            "samples".to_string(),
            "signal".to_string(),
        ])
    );
    assert_eq!(result["run_id"], "run-success");
    assert_eq!(result["exit_code"], 0);
    assert!(result["signal"].is_null());

    let run_dir = PathBuf::from(result["run_dir"].as_str().unwrap());
    let child_pid = std::fs::read_to_string(run_dir.join("target.stdout.log")).unwrap();
    assert!(!child_pid.is_empty());
    assert_eq!(
        std::fs::read_to_string(run_dir.join("target.stderr.log")).unwrap(),
        "err"
    );

    let command: Value =
        serde_json::from_str(&std::fs::read_to_string(run_dir.join("command.json")).unwrap())
            .unwrap();
    assert_eq!(command["schema_version"], 1);
    assert_eq!(command["run_id"], "run-success");
    assert_eq!(
        command["argv"],
        serde_json::json!(["/bin/sh", "-c", "printf '%s' \"$$\"; printf err >&2"])
    );
    assert!(Path::new(command["cwd"].as_str().unwrap()).is_absolute());
    assert_eq!(command["environment"], serde_json::json!({"inherit": true}));
    assert_eq!(command["process_group"], "dedicated");
    assert_eq!(
        command["stdio"],
        serde_json::json!({
            "stdin": "null",
            "stdout": "target.stdout.log",
            "stderr": "target.stderr.log"
        })
    );
    assert_eq!(
        command["labels"],
        serde_json::json!({"model": "terra", "note": "a=b"})
    );

    let events = replayed_events(&run_dir);
    assert_eq!(
        events
            .iter()
            .map(|event| event.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["TARGET_SPAWN", "TARGET_EXEC", "TARGET_EXIT"]
    );
    assert!(events
        .windows(2)
        .all(|pair| pair[0].monotonic_ns <= pair[1].monotonic_ns));
    assert!(events.iter().all(|event| {
        event.labels.get("model").map(String::as_str) == Some("terra")
            && event.labels.get("note").map(String::as_str) == Some("a=b")
    }));
    assert_eq!(events[1].labels.get("pid"), Some(&child_pid));
    assert_eq!(
        events[2].labels.get("returncode").map(String::as_str),
        Some("0")
    );
    assert!(!events[2].labels.contains_key("signal"));

    let mut sample_timestamps = Vec::new();
    replay_trace(&run_dir, |batch| {
        sample_timestamps.extend(batch.samples.into_iter().map(|sample| sample.monotonic_ns));
    })
    .unwrap();
    assert!(sample_timestamps
        .iter()
        .any(|timestamp| *timestamp <= events[0].monotonic_ns));
    assert!(sample_timestamps
        .iter()
        .any(|timestamp| *timestamp >= events[2].monotonic_ns));

    let report = inspect_run(&run_dir).unwrap();
    assert_eq!(result["batches"], report.batches);
    assert_eq!(result["samples"], report.samples);
    assert_eq!(report.events, 3);
    let lifecycle = report.command_lifecycle.unwrap();
    assert_eq!(lifecycle.status, CommandLifecycleStatus::Complete);
    assert_eq!(lifecycle.exit_code, Some(0));
    assert_eq!(lifecycle.batches, result["batches"].as_u64());
    assert_eq!(lifecycle.samples, result["samples"].as_u64());
    let lifecycle_records = std::fs::read_to_string(run_dir.join(COMMAND_LIFECYCLE_FILE)).unwrap();
    let states = lifecycle_records
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap()["state"].clone())
        .collect::<Vec<_>>();
    assert_eq!(states, vec!["STARTED", "COMPLETE"]);

    let inspect = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("inspect")
        .arg("--run")
        .arg(&run_dir)
        .arg("--json")
        .output()
        .unwrap();
    assert!(inspect.status.success());
    let inspection = summary(&inspect);
    assert_eq!(
        inspection
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "batches".to_string(),
            "command_lifecycle".to_string(),
            "event_batches".to_string(),
            "events".to_string(),
            "node_id".to_string(),
            "per_event_kind".to_string(),
            "per_source".to_string(),
            "run_id".to_string(),
            "samples".to_string(),
            "unknown_source_samples".to_string(),
        ])
    );
    assert_eq!(inspection["run_id"], "run-success");
    assert_eq!(inspection["node_id"], NODE_ID);
    assert_eq!(inspection["batches"], result["batches"]);
    assert_eq!(inspection["samples"], result["samples"]);
    assert_eq!(inspection["event_batches"], 3);
    assert_eq!(inspection["events"], 3);
    assert_eq!(inspection["unknown_source_samples"], 0);
    assert_eq!(
        inspection["per_event_kind"],
        serde_json::json!({
            "TARGET_EXEC": 1,
            "TARGET_EXIT": 1,
            "TARGET_SPAWN": 1,
        })
    );
    assert_eq!(
        inspection["per_source"]["1"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "first_monotonic_ns".to_string(),
            "first_sequence_no".to_string(),
            "last_monotonic_ns".to_string(),
            "last_sequence_no".to_string(),
            "samples".to_string(),
            "sequence_gaps".to_string(),
        ])
    );
    assert_eq!(
        inspection["command_lifecycle"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "batches".to_string(),
            "exit_code".to_string(),
            "reason".to_string(),
            "samples".to_string(),
            "signal".to_string(),
            "status".to_string(),
        ])
    );
    assert_eq!(inspection["command_lifecycle"]["status"], "COMPLETE");
    assert_eq!(inspection["command_lifecycle"]["exit_code"], 0);
    assert!(inspection["command_lifecycle"]["reason"].is_null());

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn run_records_and_drains_during_pre_spawn_delay() {
    let (_server, socket) = start_server("pre-spawn-delay", BackendShape::Cumulative);
    let out = output_root("pre-spawn-delay-out");
    let started = std::time::Instant::now();
    let output = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("run")
        .arg("--socket")
        .arg(&socket)
        .arg("--out")
        .arg(&out)
        .arg("--interval-ms")
        .arg("10")
        .arg("--pre-spawn-delay-us")
        .arg("25000")
        .arg("--run-id")
        .arg("run-pre-spawn-delay")
        .arg("--json")
        .arg("--")
        .arg("/bin/sh")
        .arg("-c")
        .arg("exit 0")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(started.elapsed() >= std::time::Duration::from_millis(25));
    let result = summary(&output);
    let run_dir = PathBuf::from(result["run_dir"].as_str().unwrap());
    let command: Value =
        serde_json::from_str(&std::fs::read_to_string(run_dir.join("command.json")).unwrap())
            .unwrap();
    assert_eq!(command["pre_spawn_delay_us"], 25_000);
    let spawn_ns = replayed_events(&run_dir)
        .into_iter()
        .find(|event| event.kind == "TARGET_SPAWN")
        .unwrap()
        .monotonic_ns;
    let mut samples_before_spawn = 0;
    replay_trace(&run_dir, |batch| {
        samples_before_spawn += batch
            .samples
            .into_iter()
            .filter(|sample| sample.monotonic_ns <= spawn_ns)
            .count();
    })
    .unwrap();
    assert!(samples_before_spawn >= 2);

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn run_passes_through_nonzero_and_signaled_child_statuses() {
    let (_server, socket) = start_server("statuses", BackendShape::Cumulative);
    let out = output_root("statuses-out");

    let failed = run_cli(
        &socket,
        &out,
        "run-exit-23",
        &[],
        &["/bin/sh", "-c", "printf out; printf err >&2; exit 23"],
    );
    assert_eq!(failed.status.code(), Some(23));
    let failed_result = summary(&failed);
    assert_eq!(failed_result["exit_code"], 23);
    assert!(failed_result["signal"].is_null());
    let failed_dir = PathBuf::from(failed_result["run_dir"].as_str().unwrap());
    assert_eq!(
        std::fs::read_to_string(failed_dir.join("target.stdout.log")).unwrap(),
        "out"
    );
    assert_eq!(
        std::fs::read_to_string(failed_dir.join("target.stderr.log")).unwrap(),
        "err"
    );
    assert_eq!(
        replayed_events(&failed_dir)[2]
            .labels
            .get("returncode")
            .map(String::as_str),
        Some("23")
    );
    let lifecycle = inspect_run(&failed_dir).unwrap().command_lifecycle.unwrap();
    assert_eq!(lifecycle.status, CommandLifecycleStatus::Complete);
    assert_eq!(lifecycle.exit_code, Some(23));

    let signaled = run_cli(
        &socket,
        &out,
        "run-signal",
        &[],
        &["/bin/sh", "-c", "kill -TERM $$"],
    );
    assert_eq!(signaled.status.code(), Some(143));
    let signal_result = summary(&signaled);
    assert_eq!(signal_result["exit_code"], 143);
    assert_eq!(signal_result["signal"], 15);
    let signal_dir = PathBuf::from(signal_result["run_dir"].as_str().unwrap());
    let exit = &replayed_events(&signal_dir)[2];
    assert_eq!(
        exit.labels.get("returncode").map(String::as_str),
        Some("143")
    );
    assert_eq!(exit.labels.get("signal").map(String::as_str), Some("15"));
    inspect_run(&signal_dir).unwrap();

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn spawn_failure_finishes_evidence_and_releases_the_daemon_session() {
    let (_server, socket) = start_server("spawn-failure", BackendShape::Cumulative);
    let out = output_root("spawn-failure-out");
    let missing = unique_path("missing-program", "");
    let output = run_cli(
        &socket,
        &out,
        "run-spawn-failure",
        &[],
        &[missing.to_str().unwrap()],
    );
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert!(String::from_utf8_lossy(&output.stderr).contains("failed to spawn command"));

    let failed_dir = out.join("run-spawn-failure");
    assert!(failed_dir.join("command.json").exists());
    assert_eq!(
        std::fs::metadata(failed_dir.join("target.stdout.log"))
            .unwrap()
            .len(),
        0
    );
    assert_eq!(
        std::fs::metadata(failed_dir.join("target.stderr.log"))
            .unwrap()
            .len(),
        0
    );
    assert_eq!(
        replayed_events(&failed_dir)
            .iter()
            .map(|event| event.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["TARGET_SPAWN"]
    );
    let report = inspect_run(&failed_dir).unwrap();
    let lifecycle = report.command_lifecycle.unwrap();
    assert_eq!(lifecycle.status, CommandLifecycleStatus::Incomplete);
    assert!(lifecycle
        .reason
        .as_deref()
        .is_some_and(|reason| reason.contains("failed to spawn command")));
    let lifecycle_records =
        std::fs::read_to_string(failed_dir.join(COMMAND_LIFECYCLE_FILE)).unwrap();
    let states = lifecycle_records
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap()["state"].clone())
        .collect::<Vec<_>>();
    assert_eq!(states, vec!["STARTED", "INCOMPLETE"]);

    let inspect = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("inspect")
        .arg("--run")
        .arg(&failed_dir)
        .output()
        .unwrap();
    assert!(inspect.status.success());
    assert!(String::from_utf8_lossy(&inspect.stdout).contains("command lifecycle INCOMPLETE"));

    let inspect_json = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("inspect")
        .arg("--run")
        .arg(&failed_dir)
        .arg("--json")
        .output()
        .unwrap();
    assert!(inspect_json.status.success());
    let inspection = summary(&inspect_json);
    assert_eq!(inspection["command_lifecycle"]["status"], "INCOMPLETE");
    assert!(inspection["command_lifecycle"]["reason"]
        .as_str()
        .is_some_and(|reason| reason.contains("failed to spawn command")));
    assert!(inspection["command_lifecycle"]["exit_code"].is_null());
    assert!(inspection["command_lifecycle"]["batches"].is_null());
    assert!(inspection["command_lifecycle"]["samples"].is_null());

    let retry = run_cli(
        &socket,
        &out,
        "run-after-spawn-failure",
        &[],
        &["/bin/sh", "-c", "exit 0"],
    );
    assert_eq!(retry.status.code(), Some(0));
    summary(&retry);

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn interval_average_capture_waits_for_a_fresh_post_exit_tail() {
    let (_server, socket) = start_server("interval-tail", BackendShape::IntervalAverage);
    let out = output_root("interval-tail-out");
    let output = run_cli(
        &socket,
        &out,
        "run-interval-tail",
        &[],
        &["/bin/sh", "-c", "exit 0"],
    );
    assert_eq!(output.status.code(), Some(0));
    let result = summary(&output);
    let run_dir = PathBuf::from(result["run_dir"].as_str().unwrap());
    let target_exit_ns = replayed_events(&run_dir)
        .into_iter()
        .find(|event| event.kind == "TARGET_EXIT")
        .unwrap()
        .monotonic_ns;
    let mut power_timestamps = Vec::new();
    replay_trace(&run_dir, |batch| {
        power_timestamps.extend(
            batch
                .samples
                .into_iter()
                .filter(|sample| sample.source_id == 1)
                .map(|sample| sample.monotonic_ns),
        );
    })
    .unwrap();
    let qualifying = power_timestamps
        .iter()
        .copied()
        .filter(|timestamp| *timestamp >= target_exit_ns)
        .collect::<Vec<_>>();
    assert!(
        qualifying.len() >= 2
            || qualifying
                .last()
                .is_some_and(|timestamp| *timestamp >= target_exit_ns + INTERVAL_NS),
        "exit={target_exit_ns}, samples={power_timestamps:?}"
    );

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn sparse_periodic_pre_roll_covers_every_source_before_spawn() {
    let (_server, socket) = start_server("sparse-pre-roll", BackendShape::SparsePeriodic);
    let out = output_root("sparse-pre-roll-out");
    let output = run_cli(
        &socket,
        &out,
        "run-sparse-pre-roll",
        &[],
        &["/bin/sh", "-c", "exit 0"],
    );
    assert_eq!(output.status.code(), Some(0));
    let result = summary(&output);
    let run_dir = PathBuf::from(result["run_dir"].as_str().unwrap());
    let spawn_ns = replayed_events(&run_dir)
        .into_iter()
        .find(|event| event.kind == "TARGET_SPAWN")
        .unwrap()
        .monotonic_ns;
    let mut pre_spawn_sources = BTreeSet::new();
    replay_trace(&run_dir, |batch| {
        pre_spawn_sources.extend(
            batch
                .samples
                .into_iter()
                .filter(|sample| sample.monotonic_ns <= spawn_ns)
                .map(|sample| sample.source_id),
        );
    })
    .unwrap();
    assert_eq!(pre_spawn_sources, BTreeSet::from([1, 2]));

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn run_terminates_descendants_in_its_dedicated_process_group() {
    let (_server, socket) = start_server("process-group", BackendShape::Cumulative);
    let out = output_root("process-group-out");
    let script =
        "trap '' TERM; (trap '' TERM; while :; do sleep 1; done) & printf '%s' \"$!\"; exit 0";
    let started = std::time::Instant::now();
    let output = run_cli(
        &socket,
        &out,
        "run-process-group",
        &[],
        &["/bin/sh", "-c", script],
    );
    assert_eq!(output.status.code(), Some(0));
    assert!(started.elapsed() < std::time::Duration::from_secs(3));
    let result = summary(&output);
    let run_dir = PathBuf::from(result["run_dir"].as_str().unwrap());
    let descendant_pid: i32 = std::fs::read_to_string(run_dir.join("target.stdout.log"))
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(
        nix::sys::signal::kill(nix::unistd::Pid::from_raw(descendant_pid), None),
        Err(nix::errno::Errno::ESRCH)
    );

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn run_timeout_terminates_the_target_group_and_finalizes_evidence() {
    let (_server, socket) = start_server("target-timeout", BackendShape::Cumulative);
    let out = output_root("target-timeout-out");
    let script = "trap '' TERM; (trap '' TERM; while :; do sleep 1; done) & printf '%s' \"$!\"; while :; do sleep 1; done";
    let started = std::time::Instant::now();
    let output = Command::new(env!("CARGO_BIN_EXE_wattch"))
        .arg("run")
        .arg("--socket")
        .arg(&socket)
        .arg("--out")
        .arg(&out)
        .arg("--interval-ms")
        .arg("10")
        .arg("--timeout-ms")
        .arg("100")
        .arg("--run-id")
        .arg("run-target-timeout")
        .arg("--json")
        .arg("--")
        .args(["/bin/sh", "-c", script])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(137));
    assert!(started.elapsed() < std::time::Duration::from_secs(4));
    let result = summary(&output);
    assert_eq!(result["exit_code"], 137);
    assert_eq!(result["signal"], 9);
    let run_dir = PathBuf::from(result["run_dir"].as_str().unwrap());
    let descendant_pid: i32 = std::fs::read_to_string(run_dir.join("target.stdout.log"))
        .unwrap()
        .parse()
        .unwrap();
    assert_eq!(
        nix::sys::signal::kill(nix::unistd::Pid::from_raw(descendant_pid), None),
        Err(nix::errno::Errno::ESRCH)
    );
    let exit = &replayed_events(&run_dir)[2];
    assert_eq!(
        exit.labels.get("timed_out").map(String::as_str),
        Some("true")
    );
    let lifecycle = inspect_run(&run_dir).unwrap().command_lifecycle.unwrap();
    assert_eq!(lifecycle.status, CommandLifecycleStatus::Complete);
    assert_eq!(lifecycle.exit_code, Some(137));
    let command: Value =
        serde_json::from_str(&std::fs::read_to_string(run_dir.join("command.json")).unwrap())
            .unwrap();
    assert_eq!(command["timeout_ms"], 100);

    let _ = std::fs::remove_dir_all(out);
}

#[test]
fn existing_run_is_create_only_and_never_executes_the_second_target() {
    let (_server, socket) = start_server("create-only", BackendShape::Cumulative);
    let out = output_root("create-only-out");
    let first = run_cli(
        &socket,
        &out,
        "run-create-only",
        &[],
        &["/bin/sh", "-c", "exit 0"],
    );
    assert_eq!(first.status.code(), Some(0));
    summary(&first);
    let raw_path = out.join("run-create-only/raw.trace.wch");
    let raw_before = std::fs::read(&raw_path).unwrap();

    let sentinel = unique_path("must-not-execute", "");
    let script = format!("touch '{}'", sentinel.display());
    let second = run_cli(
        &socket,
        &out,
        "run-create-only",
        &[],
        &["/bin/sh", "-c", &script],
    );
    assert_eq!(second.status.code(), Some(1));
    assert!(second.stdout.is_empty());
    assert!(String::from_utf8_lossy(&second.stderr).contains("already exists"));
    assert!(!sentinel.exists());
    assert_eq!(std::fs::read(raw_path).unwrap(), raw_before);

    let _ = std::fs::remove_dir_all(out);
}
