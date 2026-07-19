from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from pywattch import RawSample, RawSampleBatch, SourceDescriptor
from pywattch._frames import MAX_PAYLOAD_LEN, FrameKind
from pywattch._workload_metrics import (
    WORKLOAD_METRICS_CONTRACT,
    WORKLOAD_METRICS_FILENAME,
)
from pywattch.analysis import (
    AnalysisError,
    AggregationRule,
    MeasurementBoundary,
    evaluate_trial_quality,
    load_trial_evidence,
)
from pywattch.experiment import (
    ArmConfig,
    ExperimentArm,
    ExperimentConfig,
    FixedHorizon,
    TrialArtifactWriter,
    WorkloadCommand,
    build_schedule,
    finalize_trial_quality,
    prepare_experiment_layout,
    verify_checksums,
)
from pywattch.analysis.replay import (
    _read_trace,
    _validate_interval_post_window_support,
)


def _descriptor() -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=1,
        source_uid="rapl://node-1/package/0",
        node_id="node-1",
        backend="linux-powercap",
        kind="CPU_PACKAGE_ENERGY",
        unit="MICROJOULE",
        temporality="CUMULATIVE",
        evidence_class="RAW_HARDWARE_COUNTER",
        source_path="package/0/energy_uj",
        clock_domain="monotonic",
        sampling_mode="POLLED",
        nominal_period_ns=10,
        max_value=1_000_000.0,
        resolution=1.0,
    )


def _interval_descriptors() -> tuple[SourceDescriptor, SourceDescriptor]:
    duration_uid = "powermetrics://node-1/frame/elapsed_ns"
    return (
        SourceDescriptor(
            local_source_id=7,
            source_uid="powermetrics://node-1/cpu/power",
            node_id="node-1",
            backend="apple-powermetrics",
            kind="CPU_POWER",
            unit="MILLIWATT",
            temporality="INTERVAL_AVERAGE",
            evidence_class="TOOL_DERIVED_ESTIMATE",
            source_path="cpu/package/0/power",
            clock_domain="monotonic",
            sampling_mode="PERIODIC",
            nominal_period_ns=100_000_000,
            labels={
                "wattch.interval_duration_source_uid": duration_uid,
                "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
                "wattch.interval_timeline_reconstruction": (
                    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"
                ),
            },
        ),
        SourceDescriptor(
            local_source_id=8,
            source_uid=duration_uid,
            node_id="node-1",
            backend="apple-powermetrics",
            kind="SAMPLE_INTERVAL_DURATION",
            unit="NANOSECOND",
            temporality="DELTA",
            evidence_class="TOOL_DERIVED_ESTIMATE",
            source_path="frame/elapsed_ns",
            clock_domain="monotonic",
            sampling_mode="PERIODIC",
            nominal_period_ns=100_000_000,
        ),
    )


def test_replay_requires_interval_average_post_window_tail_support():
    descriptors = _interval_descriptors()
    power = descriptors[0]
    boundary = MeasurementBoundary.from_descriptors(
        descriptors,
        [power.source_uid],
        boundary_id="powermetrics-cpu",
    )
    recovery_end_ns = 1_000_000_000
    trial = {"labels": {"interval_ms": "10"}}
    one_near_edge_sample = (
        RawSample(7, 0, 900_000_000, 10_000.0),
        RawSample(8, 0, 900_000_000, 100_000_000),
        RawSample(7, 1, 1_000_000_001, 10_000.0),
        RawSample(8, 1, 1_000_000_001, 100_000_000),
    )
    result = {
        "primary_sample_bracket": {
            power.source_uid: {
                "pre_window_sample_ns": 900_000_000,
                "post_window_sample_ns": 1_000_000_001,
                "post_window_samples_at_or_after": 1,
                "post_window_margin_ns": 100_000_000,
                "recovery_end_ns": recovery_end_ns,
            }
        }
    }

    with pytest.raises(AnalysisError, match="post-window native generation"):
        _validate_interval_post_window_support(
            result=result,
            trial=trial,
            boundary=boundary,
            descriptors=descriptors,
            samples=one_near_edge_sample,
            recovery_end_ns=recovery_end_ns,
        )

    supported_samples = one_near_edge_sample + (
        RawSample(7, 2, 1_100_000_001, 10_000.0),
        RawSample(8, 2, 1_100_000_001, 100_000_000),
    )
    supported_result = json.loads(json.dumps(result))
    entry = supported_result["primary_sample_bracket"][power.source_uid]
    entry["post_window_sample_ns"] = 1_100_000_001
    entry["post_window_samples_at_or_after"] = 2
    _validate_interval_post_window_support(
        result=supported_result,
        trial=trial,
        boundary=boundary,
        descriptors=descriptors,
        samples=supported_samples,
        recovery_end_ns=recovery_end_ns,
    )


def test_legacy_v01_replay_accepts_batch_above_live_socket_limit(tmp_path):
    batch = RawSampleBatch(
        run_id="legacy-large",
        node_id="node-1",
        batch_sequence_no=0,
        samples=(RawSample(1, 0, 1, 1),),
    )
    payload = json.dumps(batch.to_dict(), separators=(",", ":")).encode()
    payload += b" " * (MAX_PAYLOAD_LEN + 1 - len(payload))
    header = b"".join(
        (
            b"WCH0",
            (0).to_bytes(2, "little"),
            (1).to_bytes(2, "little"),
            int(FrameKind.RAW_SAMPLE_BATCH).to_bytes(2, "little"),
            (0).to_bytes(2, "little"),
            len(payload).to_bytes(4, "little"),
        )
    )
    trace = tmp_path / "raw.trace.wch"
    trace.write_bytes(header + payload)

    with pytest.raises(AnalysisError, match="too large"):
        _read_trace(trace, FrameKind.RAW_SAMPLE_BATCH)
    assert _read_trace(
        trace,
        FrameKind.RAW_SAMPLE_BATCH,
        allow_large_legacy_frames=True,
    ) == (batch,)


def _sealed_trial(
    tmp_path,
    *,
    finalize: bool = True,
    bad_count: bool = False,
    bad_missingness: bool = False,
    batch_sequence_start: int = 0,
    result_tolerance_ns: int = 1,
    horizon_terminated: bool = False,
):
    source = _descriptor()
    boundary = MeasurementBoundary.from_descriptors(
        [source],
        [source.source_uid],
        AggregationRule.SINGLE_SOURCE,
        boundary_id="rapl-package-0",
    )
    output = b"equivalent\n"
    digest = hashlib.sha256(output).hexdigest()
    command = WorkloadCommand(("/usr/bin/true",))
    config = ExperimentConfig(
        experiment_id="replay-study",
        boundary=boundary,
        observation=FixedHorizon(1e-8, tolerance_s=1e-9),
        c0=ArmConfig(ExperimentArm.C0, "sham", command),
        c1=ArmConfig(
            ExperimentArm.C1,
            "P1",
            command,
            expected_output_sha256=digest,
        ),
        c2=ArmConfig(
            ExperimentArm.C2,
            "P2",
            command,
            expected_output_sha256=digest,
        ),
        blocks=1,
        interval_ms=1,
        confirmatory=True,
        labels={"workload_mode": "BATCH", "mechanism_policy": "NORMAL_DYNAMIC"},
    )
    schedule = build_schedule(config)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = next(item for item in schedule.trials if item.arm is ExperimentArm.C1)
    trial = replace(
        trial,
        labels={
            **trial.labels,
            "workload_metrics_contract": WORKLOAD_METRICS_CONTRACT,
            "workload_metrics_path": WORKLOAD_METRICS_FILENAME,
        },
    )
    writer = TrialArtifactWriter(
        experiment_dir,
        trial,
        boundary,
        [source],
        node_id="node-1",
        protocol_version="0.2.0",
        clock_instance_id="clock-1",
        created_unix_ns=1,
    )
    (writer.trial_dir / "target.stdout.log").write_bytes(output)
    (writer.trial_dir / "target.stderr.log").write_bytes(b"")
    metrics_document = {
        "schema_version": 1,
        "completed_work": 1.0,
        "work_unit": "items",
        "throughput_per_s": 100_000_000.0,
        "latency_ms": None,
        "backlog_at_end": None,
        "slo_met": None,
        "output_sha256": digest,
    }
    metrics_bytes = (
        json.dumps(metrics_document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (writer.trial_dir / WORKLOAD_METRICS_FILENAME).write_bytes(metrics_bytes)
    samples = tuple(
        RawSample(1, sequence, timestamp, value)
        for sequence, (timestamp, value) in enumerate(
            ((0, 0), (10, 100_000), (20, 200_000), (30, 300_000))
        )
    )
    for batch_offset, sample_value in enumerate(samples):
        writer.append_raw_batch(
            RawSampleBatch(
                trial.run_id,
                "node-1",
                batch_sequence_start + batch_offset,
                (sample_value,),
            )
        )
    markers = (
        ("PRE_WINDOW_START", 1),
        ("BACKGROUND_ARRIVAL", 2),
        ("TARGET_SPAWN", 10),
        ("TARGET_EXEC", 10),
        *(
            (
                ("RECOVERY_END", 20),
                ("TARGET_EXIT", 22),
                ("BACKGROUND_COMPLETION", 23),
            )
            if horizon_terminated
            else (
                ("TARGET_EXIT", 18),
                ("RECOVERY_END", 20),
                ("BACKGROUND_COMPLETION", 21),
            )
        ),
    )
    marker_labels = {
        "experiment_id": trial.experiment_id,
        "trial_id": trial.trial_id,
        "arm": trial.arm.value,
        "target_nice": str(trial.target_nice),
        "replay_seed": str(trial.random_seed),
        "mechanism_policy": trial.mechanism_policy.value,
        "actual_target_nice": str(trial.target_nice),
    }
    for sequence, (kind, timestamp) in enumerate(markers):
        writer.append_daemon_event(
            {
                "event_id": f"{trial.run_id}:{sequence:02d}",
                "run_id": trial.run_id,
                "node_id": "node-1",
                "monotonic_ns": timestamp,
                "kind": kind,
                "labels": marker_labels,
            }
        )
    result = {
        "trial_id": trial.trial_id,
        "mechanism_policy": "NORMAL_DYNAMIC",
        "host_policy_evidence": None,
        "host_policy_restored": None,
        "status": "COMPLETED",
        "target_returncode": 0,
        "background_returncode": None,
        "actual_target_nice": trial.target_nice,
        "actual_background_nice": None,
        "target_timed_out": False,
        "horizon_terminated": horizon_terminated,
        "target_runtime_s": 1e-8,
        "target_stdout_sha256": digest,
        "target_stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "background_stdout_sha256": None,
        "background_stderr_sha256": None,
        "marker_event_ids": [
            f"{trial.run_id}:{sequence:02d}" for sequence in range(len(markers))
        ],
        "recovery_time_s": 0.0,
        "fixed_horizon_target_ns": 10,
        "fixed_horizon_tolerance_ns": result_tolerance_ns,
        "fixed_horizon_observed_ns": 10,
        "fixed_horizon_error_ns": 0,
        "fixed_horizon_within_tolerance": True,
        "completed_work": 1.0,
        "work_unit": "items",
        "throughput_per_s": 100_000_000.0,
        "latency_ms": None,
        "backlog_at_end": None,
        "slo_met": None,
        "arrival_trace_id": None,
        "arrival_trace_sha256": None,
        "arrival_trace_valid": None,
        "workload_metrics_evidence": {
            "contract": WORKLOAD_METRICS_CONTRACT,
            "source": "WORKLOAD_SIDECAR",
            "relative_path": WORKLOAD_METRICS_FILENAME,
            "sha256": hashlib.sha256(metrics_bytes).hexdigest(),
            "metrics": {
                "completed_work": 1.0,
                "work_unit": "items",
                "throughput_per_s": 100_000_000.0,
                "latency_ms": None,
                "backlog_at_end": None,
                "slo_met": None,
                "output_sha256": digest,
                "arrival_trace_id": None,
                "arrival_trace_sha256": None,
                "arrival_trace_valid": None,
            },
        },
        "background_workload_metrics_evidence": {
            "contract": WORKLOAD_METRICS_CONTRACT,
            "source": "NONE",
            "relative_path": None,
            "sha256": None,
            "metrics": {
                "completed_work": None,
                "work_unit": None,
                "throughput_per_s": None,
                "latency_ms": None,
                "backlog_at_end": None,
                "slo_met": None,
                "output_sha256": None,
                "arrival_trace_id": None,
                "arrival_trace_sha256": None,
                "arrival_trace_valid": None,
            },
        },
        "claim_eligibility": "INELIGIBLE" if horizon_terminated else "ELIGIBLE",
        "error": None,
    }
    trial_dir = writer.finish(result=result)
    quality = evaluate_trial_quality(
        trial_dir,
        measurement_overhead_ratio=0.005,
    )
    if bad_count:
        quality["sources"][0]["observed_samples"] = 3
    if bad_missingness:
        quality["sources"][0]["missing_sample_ratio"] = 0.0005
    raw_before = (trial_dir / "raw.trace.wch").read_bytes()
    if finalize:
        finalize_trial_quality(trial_dir, quality)
    return trial_dir, quality, raw_before


def _rewrite_sealed_json(trial_dir, filename, mutate):
    path = trial_dir / filename
    document = json.loads(path.read_text())
    mutate(document)
    contents = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(contents)
    checksums_path = trial_dir / "checksums.json"
    checksums = json.loads(checksums_path.read_text())
    checksums["files"][filename] = {
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }
    checksums_path.write_text(json.dumps(checksums, indent=2, sort_keys=True) + "\n")


def test_quality_finalization_reseals_without_rewriting_raw_trace(tmp_path):
    trial_dir, quality, raw_before = _sealed_trial(tmp_path)

    assert (trial_dir / "raw.trace.wch").read_bytes() == raw_before
    assert verify_checksums(trial_dir) == []
    evidence = load_trial_evidence(trial_dir)
    assert evidence.outcome.total_joules == pytest.approx(0.1)
    assert evidence.observation.work_complete is True
    assert evidence.observation.output_valid is True
    assert evidence.observation.slo_met is True
    assert evidence.observation.quality_accepted is True
    assert evidence.observation.policy == "NORMAL_DYNAMIC"

    with pytest.raises(ValueError, match="already finalized"):
        finalize_trial_quality(trial_dir, quality)


def test_replay_accepts_horizon_marker_before_forced_target_teardown(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path, horizon_terminated=True)

    evidence = load_trial_evidence(trial_dir)

    assert evidence.acquisition["result"]["horizon_terminated"] is True
    assert evidence.observation.work_complete is False


def test_replay_rejects_provisional_quality(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path, finalize=False)
    with pytest.raises(AnalysisError, match="not ACCEPTED"):
        load_trial_evidence(trial_dir)


def test_replay_cross_checks_quality_summary_against_raw_trace(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path, bad_count=True)
    with pytest.raises(AnalysisError, match="sample count disagrees"):
        load_trial_evidence(trial_dir)


def test_replay_recomputes_timing_missingness_instead_of_trusting_sidecar(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path, bad_missingness=True)
    with pytest.raises(AnalysisError, match="missingness disagrees"):
        load_trial_evidence(trial_dir)


def test_replay_rejects_tampered_sealed_artifact(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path)
    with (trial_dir / "events.trace.wch").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(AnalysisError, match="checksum verification failed"):
        load_trial_evidence(trial_dir)


def test_replay_recomputes_work_metrics_instead_of_trusting_acquisition(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path)

    _rewrite_sealed_json(
        trial_dir,
        "acquisition.json",
        lambda document: document["result"].__setitem__("completed_work", 999),
    )

    with pytest.raises(AnalysisError, match="completed_work does not match"):
        load_trial_evidence(trial_dir)


def test_replay_rejects_forged_eligible_provider_metrics(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path)

    def forge(document):
        evidence = document["result"]["workload_metrics_evidence"]
        evidence.update(
            {
                "source": "EXPLORATORY_PROVIDER",
                "relative_path": None,
                "sha256": None,
            }
        )

    _rewrite_sealed_json(trial_dir, "acquisition.json", forge)

    with pytest.raises(
        AnalysisError,
        match="confirmatory eligibility requires workload-written sidecar",
    ):
        load_trial_evidence(trial_dir)


def test_replay_rejects_acquisition_tolerance_that_disagrees_with_schedule(tmp_path):
    trial_dir, _, _ = _sealed_trial(tmp_path, result_tolerance_ns=2)
    with pytest.raises(AnalysisError, match="sealed tolerance disagree"):
        load_trial_evidence(trial_dir)


def test_quality_rejects_missing_raw_batch_sequence(tmp_path):
    trial_dir, quality, _ = _sealed_trial(tmp_path, batch_sequence_start=1)
    assert quality["disposition"] == "REJECTED"
    assert any(issue["code"] == "RAW_BATCH_SEQUENCE_GAP" for issue in quality["issues"])
    with pytest.raises(AnalysisError, match="not ACCEPTED"):
        load_trial_evidence(trial_dir)
