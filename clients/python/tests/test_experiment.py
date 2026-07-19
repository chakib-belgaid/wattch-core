from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import pytest

from pywattch._frames import FrameKind, decode_header
from pywattch._messages import RawSample, RawSampleBatch, SourceDescriptor
from pywattch.client import WattchClient
from pywattch.analysis import (
    EnergyOutcome,
    Estimand,
    TrialObservation,
    matched_difference_in_differences,
    trial_match_fingerprint,
)
from pywattch.experiment import (
    AggregationRule,
    ArmConfig,
    EqualWork,
    ExperimentArm,
    ExperimentConfig,
    ExperimentRunner,
    FixedHorizon,
    HostPolicyEvidence,
    HostPolicyRestorationEvidence,
    MeasurementBoundary,
    MechanismPolicy,
    SystemProcessLauncher,
    TrialArtifactWriter,
    TrialRunner,
    WorkloadCommand,
    WorkloadMetrics,
    WorkloadMode,
    build_schedule,
    prepare_experiment_layout,
    verify_checksums,
)
from pywattch.experiment.runner import _stop_process


def descriptor(
    source_id: int = 1,
    source_uid: str = "rapl://node/package/0",
    *,
    parent_source_uid: str | None = None,
) -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=source_id,
        source_uid=source_uid,
        node_id="node-1",
        backend="linux-powercap",
        kind="CPU_PACKAGE_ENERGY",
        unit="MICROJOULE",
        temporality="CUMULATIVE",
        evidence_class="RAW_HARDWARE_COUNTER",
        source_path="cpu/package/0/energy",
        clock_domain="monotonic",
        sampling_mode="POLLED",
        parent_source_uid=parent_source_uid,
        max_value=1_000_000.0,
    )


def interval_average_descriptors() -> tuple[SourceDescriptor, SourceDescriptor]:
    duration_uid = "powermetrics://node-1/frame/elapsed_ns"
    power = SourceDescriptor(
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
    )
    duration = SourceDescriptor(
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
    )
    return power, duration


def boundary(sources: tuple[SourceDescriptor, ...]) -> MeasurementBoundary:
    return MeasurementBoundary.from_descriptors(
        sources,
        [sources[0].source_uid],
        AggregationRule.SINGLE_SOURCE,
        boundary_id="rapl-package-0",
    )


def command(name: str) -> WorkloadCommand:
    return WorkloadCommand(("/usr/bin/true", name))


def config(
    sources: tuple[SourceDescriptor, ...],
    *,
    blocks: int = 6,
    observation: FixedHorizon | EqualWork | None = None,
    confirmatory: bool = False,
    digest: str | None = None,
    background: WorkloadCommand | None = None,
    mechanism_policies: tuple[MechanismPolicy, ...] = (MechanismPolicy.NORMAL_DYNAMIC,),
) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id="matched-study",
        boundary=boundary(sources),
        observation=observation or FixedHorizon(0.01, pre_window_s=0.001),
        c0=ArmConfig(ExperimentArm.C0, "sham", command("sham")),
        c1=ArmConfig(
            ExperimentArm.C1,
            "P1",
            command("p1"),
            expected_output_sha256=digest,
        ),
        c2=ArmConfig(
            ExperimentArm.C2,
            "P2",
            command("p2"),
            expected_output_sha256=digest,
        ),
        blocks=blocks,
        seed=42,
        interval_ms=10,
        trial_washout_s=0.25,
        block_washout_s=1.5,
        background=background,
        background_workload_id="cpu-batch",
        target_nice_levels=(0, 10),
        mechanism_policies=mechanism_policies,
        input_id="input-v1",
        confirmatory=confirmatory,
    )


def test_confirmatory_config_requires_shared_p1_p2_output_digest():
    sources = (descriptor(),)
    with pytest.raises(ValueError, match="share the same"):
        ExperimentConfig(
            experiment_id="bad-confirmatory",
            boundary=boundary(sources),
            observation=FixedHorizon(1),
            c0=ArmConfig(ExperimentArm.C0, "sham", command("sham")),
            c1=ArmConfig(
                ExperimentArm.C1,
                "P1",
                command("p1"),
                expected_output_sha256="1" * 64,
            ),
            c2=ArmConfig(
                ExperimentArm.C2,
                "P2",
                command("p2"),
                expected_output_sha256="2" * 64,
            ),
            confirmatory=True,
        )


def test_fixed_horizon_service_requires_an_open_loop_arrival_trace():
    with pytest.raises(ValueError, match="require arrival_trace_id"):
        FixedHorizon(1, workload_mode=WorkloadMode.SERVICE)


def test_fixed_horizon_preregisters_tolerance_independent_of_capture_interval():
    observation = FixedHorizon(1, tolerance_s=0.002)

    assert observation.to_dict()["tolerance_s"] == pytest.approx(0.002)
    with pytest.raises(ValueError, match="not exceed 1%"):
        FixedHorizon(1, tolerance_s=1)
    with pytest.raises(ValueError, match="not exceed 1%"):
        FixedHorizon(100, tolerance_s=99)


def test_schedule_is_reproducible_position_balanced_and_seed_matched():
    sources = (descriptor(),)
    schedule = build_schedule(config(sources))
    assert schedule == build_schedule(config(sources))
    assert len(schedule.trials) == 18

    for start in (0, 9):
        three_blocks = schedule.trials[start : start + 9]
        for position in (1, 2, 3):
            assert {
                trial.arm for trial in three_blocks if trial.order_position == position
            } == set(ExperimentArm)

    for block_index in range(1, 7):
        block = [t for t in schedule.trials if t.block_index == block_index]
        assert len({trial.random_seed for trial in block}) == 1
        assert len({trial.target_nice for trial in block}) == 1
        assert {trial.arm for trial in block} == set(ExperimentArm)

    priorities = [
        next(t.target_nice for t in schedule.trials if t.block_index == block)
        for block in range(1, 7)
    ]
    assert priorities.count(0) == priorities.count(10) == 3


def test_schedule_balances_mechanism_policy_and_priority_cells():
    sources = (descriptor(),)
    policies = tuple(MechanismPolicy)
    schedule = build_schedule(config(sources, blocks=6, mechanism_policies=policies))
    assert len(schedule.trials) == 6 * len(policies) * len(ExperimentArm)
    priority_by_block = []
    position_counts = {
        (policy, position): 0
        for policy in policies
        for position in range(1, len(policies) + 1)
    }
    arm_position_counts = {
        (policy, arm, position): 0
        for policy in policies
        for arm in ExperimentArm
        for position in (1, 2, 3)
    }
    for block_index in range(1, 7):
        block = [t for t in schedule.trials if t.block_index == block_index]
        assert {trial.mechanism_policy for trial in block} == set(policies)
        assert len({trial.target_nice for trial in block}) == 1
        assert len({trial.random_seed for trial in block}) == 1
        assert len({trial.to_dict()["matched_block_id"] for trial in block}) == 1
        priority_by_block.append(block[0].target_nice)
        for policy in policies:
            cell = [trial for trial in block if trial.mechanism_policy is policy]
            assert {trial.arm for trial in cell} == set(ExperimentArm)
            assert len({trial.mechanism_position for trial in cell}) == 1
            position_counts[(policy, cell[0].mechanism_position)] += 1
            for trial in cell:
                arm_position_counts[(policy, trial.arm, trial.order_position)] += 1

    assert priority_by_block.count(0) == priority_by_block.count(10) == 3
    assert set(position_counts.values()) == {2}
    assert set(arm_position_counts.values()) == {2}
    schedule_block = schedule.to_dict()["blocks"][0]
    assert schedule_block["target_nice"] in {0, 10}
    assert {cell["mechanism_policy"] for cell in schedule_block["policy_cells"]} == {
        policy.value for policy in policies
    }
    assert all(len(cell["order"]) == 3 for cell in schedule_block["policy_cells"])


def test_schedule_parent_blocks_are_directly_usable_for_mechanism_did():
    sources = (descriptor(),)
    policies = (
        MechanismPolicy.NORMAL_DYNAMIC,
        MechanismPolicy.FIXED_NON_TURBO,
    )
    schedule = build_schedule(config(sources, blocks=2, mechanism_policies=policies))
    selected_boundary = schedule.experiment.boundary
    source_uid = selected_boundary.primary_source_uids[0]
    observations = []
    for trial in schedule.trials:
        if trial.arm not in {ExperimentArm.C1, ExperimentArm.C2}:
            continue
        if trial.arm is ExperimentArm.C2:
            joules = 100.0
        elif trial.mechanism_policy is MechanismPolicy.NORMAL_DYNAMIC:
            joules = 90.0
        else:
            joules = 98.0
        observations.append(
            TrialObservation(
                trial_id=trial.trial_id,
                block_id=trial.to_dict()["matched_block_id"],
                arm=trial.arm_label,
                context=trial.background_workload_id,
                policy=trial.mechanism_policy.value,
                target_nice=trial.target_nice,
                match_fingerprint=trial_match_fingerprint(trial.to_dict()),
                outcome=EnergyOutcome(
                    boundary=selected_boundary,
                    estimand=Estimand.FIXED_HORIZON,
                    start_monotonic_ns=0,
                    end_monotonic_ns=10_000_000,
                    per_source_joules={source_uid: joules},
                    integration_methods={source_uid: "COUNTER_DELTA"},
                ),
                work_complete=True,
                output_valid=True,
                slo_met=True,
                quality_accepted=True,
            )
        )

    estimate = matched_difference_in_differences(
        observations,
        treatment_arm="P1",
        control_arm="P2",
        baseline_policy=MechanismPolicy.NORMAL_DYNAMIC.value,
        intervention_policy=MechanismPolicy.FIXED_NON_TURBO.value,
    )
    assert estimate.block_ids == (
        "matched-study-block-001",
        "matched-study-block-002",
    )
    assert estimate.mean_did_joules == pytest.approx(8.0)


def test_trial_json_shape_matches_rust_trial_metadata():
    sources = (descriptor(),)
    trial = build_schedule(config(sources, blocks=1)).trials[0]
    document = trial.to_dict()
    assert set(document) == {
        "experiment_id",
        "trial_id",
        "run_id",
        "matched_block_id",
        "arm",
        "background_workload_id",
        "input_id",
        "replay_seed",
        "estimand",
        "measurement_boundary",
        "labels",
    }
    assert document["arm"]["kind"] in {"BACKGROUND_CONTROL", "PROGRAM"}
    assert document["estimand"] == {
        "kind": "FIXED_HORIZON",
        "horizon_ns": 10_000_000,
    }
    assert document["labels"]["fixed_horizon_tolerance_ns"] == "100000"
    assert set(document["measurement_boundary"]) == {
        "boundary_id",
        "backend",
        "primary_source_uids",
        "aggregation_rule",
        "required_sources",
    }


def test_service_equal_work_requires_a_replay_trace():
    with pytest.raises(ValueError, match="arrival_trace_id"):
        EqualWork(
            target_work=100,
            work_unit="requests",
            workload_mode=WorkloadMode.SERVICE,
            timeout_s=10,
        )


def test_artifact_writer_emits_canonical_sidecars_and_checksums(tmp_path):
    sources = (descriptor(),)
    schedule = build_schedule(config(sources, blocks=1))
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = schedule.trials[0]
    writer = TrialArtifactWriter(
        experiment_dir,
        trial,
        schedule.experiment.boundary,
        sources,
        node_id="node-1",
        protocol_version="0.2.0",
        clock_instance_id="clock-1",
        wattch_version="test-version",
        created_unix_ns=123,
    )
    writer.append_raw_batch(
        RawSampleBatch(
            run_id=trial.run_id,
            node_id="node-1",
            batch_sequence_no=0,
            samples=(RawSample(1, 0, 10, 100),),
        )
    )
    event = {
        "event_id": f"{trial.run_id}:00",
        "run_id": trial.run_id,
        "node_id": "node-1",
        "monotonic_ns": 12,
        "kind": "PRE_WINDOW_START",
        "labels": {},
    }
    writer.append_daemon_event(event)
    trial_dir = writer.finish(result={"status": "ok"})

    manifest = json.loads((trial_dir / "manifest.json").read_text())
    assert set(manifest) == {
        "run_id",
        "node_id",
        "protocol_version",
        "wattch_version",
        "created_unix_ns",
        "clock_instance_id",
        "sources",
        "labels",
    }
    assert manifest["labels"]["wattch.clock_instance_id"] == "clock-1"
    assert manifest["labels"]["fixed_horizon_tolerance_ns"] == "100000"
    quality = json.loads((trial_dir / "quality.json").read_text())
    assert set(quality) == {
        "trial_id",
        "disposition",
        "measurement_overhead_ratio",
        "sources",
        "issues",
    }
    assert quality["disposition"] == "REVIEW_REQUIRED"
    assert json.loads((trial_dir / "trial.json").read_text()) == trial.to_dict()
    assert verify_checksums(trial_dir) == []

    raw = (trial_dir / "raw.trace.wch").read_bytes()
    events = (trial_dir / "events.trace.wch").read_bytes()
    assert decode_header(raw[:16])[0] is FrameKind.RAW_SAMPLE_BATCH
    assert decode_header(events[:16])[0] is FrameKind.EVENT_BATCH

    (trial_dir / "trial.json").write_text("{}")
    assert verify_checksums(trial_dir) == ["trial.json"]


class FakeProcess:
    _next_pid = 100

    def __init__(self, nice: int = 0):
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.nice = nice
        self.returncode: int | None = None
        self.group_cleanup_timeouts: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def close(self) -> None:
        pass

    def cleanup_group(self, *, timeout_s: float) -> None:
        self.group_cleanup_timeouts.append(timeout_s)


class FakeLauncher:
    def __init__(
        self,
        stdout: bytes = b"same-output\n",
        metrics: Mapping[str, Any] | None = None,
        metrics_roles: frozenset[str] = frozenset({"target", "background"}),
    ):
        self.stdout = stdout
        self.metrics = metrics
        self.metrics_roles = metrics_roles
        self.starts: list[tuple[WorkloadCommand, int]] = []
        self.processes: list[FakeProcess] = []

    def start(
        self,
        command: WorkloadCommand,
        *,
        nice: int,
        cpu_affinity: tuple[int, ...],
        resource_limits: Mapping[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> FakeProcess:
        self.starts.append((command, nice))
        stdout_path.write_bytes(self.stdout)
        stderr_path.write_bytes(b"")
        role = "background" if stdout_path.name.startswith("background.") else "target"
        if (
            self.metrics is not None
            and role in self.metrics_roles
            and "WATTCH_METRICS_PATH" in command.env
        ):
            document = dict(self.metrics)
            document.setdefault("schema_version", 1)
            document["output_sha256"] = hashlib.sha256(self.stdout).hexdigest()
            Path(command.env["WATTCH_METRICS_PATH"]).write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        process = FakeProcess(nice)
        self.processes.append(process)
        return process

    def read_nice(self, process: FakeProcess) -> int:
        return process.nice


def workload_metrics(
    *,
    completed_work: float = 100,
    work_unit: str = "items",
    throughput_per_s: float = 1_000,
    latency_ms: Mapping[str, float] | None = None,
    backlog_at_end: float | None = None,
    slo_met: bool | None = None,
    arrival_trace_id: str | None = None,
    arrival_trace_valid: bool | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "completed_work": completed_work,
        "work_unit": work_unit,
        "throughput_per_s": throughput_per_s,
        "latency_ms": latency_ms,
        "backlog_at_end": backlog_at_end,
        "slo_met": slo_met,
        "output_sha256": None,
        "arrival_trace_id": arrival_trace_id,
        "arrival_trace_sha256": (
            hashlib.sha256(b"matched-arrival-trace").hexdigest()
            if arrival_trace_id is not None
            else None
        ),
        "arrival_trace_valid": arrival_trace_valid,
    }


def test_system_launcher_fails_when_absolute_nice_cannot_be_reached(
    tmp_path,
    monkeypatch,
):
    if not hasattr(os, "getpriority") or not hasattr(os, "PRIO_PROCESS"):
        pytest.skip("absolute nice read-back is unavailable")
    monkeypatch.setattr(os, "getpriority", lambda *_: 5)
    launcher = SystemProcessLauncher()
    with pytest.raises(PermissionError, match="cannot safely increase priority"):
        launcher.start(
            WorkloadCommand(("/usr/bin/true",)),
            nice=0,
            cpu_affinity=(),
            resource_limits={},
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )


@pytest.mark.skipif(os.name != "posix", reason="process groups require POSIX")
def test_system_launcher_teardown_stops_descendant_process_group(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    launcher = SystemProcessLauncher()
    process = launcher.start(
        WorkloadCommand((sys.executable, "-c", parent_code)),
        nice=os.getpriority(os.PRIO_PROCESS, 0),
        cpu_affinity=(),
        resource_limits={},
        stdout_path=tmp_path / "group.stdout.log",
        stderr_path=tmp_path / "group.stderr.log",
    )
    process_group_id = process.pid
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 2
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text())
        assert os.getpgid(process.pid) == process_group_id
        assert os.getpgid(child_pid) == process_group_id

        assert _stop_process(process, grace_s=0.5) == -signal.SIGTERM
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        with pytest.raises(ProcessLookupError):
            os.killpg(process_group_id, 0)
    finally:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.close()


class FakeSession:
    def __init__(
        self,
        sources: tuple[SourceDescriptor, ...],
        monotonic=lambda: 0.0,
        event_offsets_ns: Mapping[str, int] | None = None,
        *,
        emit_recovery_sample: bool = True,
        recovery_sample_count: int = 1,
    ):
        self.sources = sources
        self.node_id = "node-1"
        self.protocol_version = "0.2.0"
        self.clock_instance_id = "clock-1"
        self.events: list[dict[str, Any]] = []
        self._event_sequence = 0
        self._monotonic = monotonic
        self._event_offsets_ns = dict(event_offsets_ns or {})
        self._on_raw = None
        self._on_event = None
        self._source_ids: tuple[int, ...] = ()
        self._post_window_raw_emitted = False
        self._emit_recovery_sample = emit_recovery_sample
        self._recovery_sample_count = recovery_sample_count

    def start_capture(
        self,
        run_id: str,
        interval_ms: int,
        source_ids: list[int],
        *,
        on_raw_batch,
        on_event_batch,
    ) -> object:
        self._on_raw = on_raw_batch
        self._on_event = on_event_batch
        self._source_ids = tuple(source_ids)
        on_raw_batch(
            RawSampleBatch(
                run_id,
                self.node_id,
                0,
                tuple(RawSample(source_id, 0, 1, 100) for source_id in source_ids),
            )
        )
        return {"run_id": run_id}

    def record_event(
        self,
        run_id: str,
        event_id: str,
        kind: str,
        labels: Mapping[str, str],
    ) -> object:
        monotonic_ns = max(
            self._event_sequence,
            round(self._monotonic() * 1_000_000_000),
        ) + self._event_offsets_ns.get(kind, 0)
        event = {
            "event_id": event_id,
            "run_id": run_id,
            "node_id": self.node_id,
            "monotonic_ns": monotonic_ns,
            "kind": kind,
            "labels": dict(labels),
        }
        self.events.append(event)
        assert self._on_event is not None
        self._on_event(
            {
                "run_id": run_id,
                "node_id": self.node_id,
                "batch_sequence_no": self._event_sequence,
                "events": [event],
            }
        )
        if kind == "RECOVERY_END" and self._emit_recovery_sample:
            assert self._on_raw is not None
            for offset in range(self._recovery_sample_count):
                self._on_raw(
                    RawSampleBatch(
                        run_id,
                        self.node_id,
                        1 + offset,
                        tuple(
                            RawSample(
                                source_id,
                                1 + offset,
                                monotonic_ns + 1 + offset,
                                200,
                            )
                            for source_id in self._source_ids
                        ),
                    )
                )
            self._post_window_raw_emitted = True
        self._event_sequence += 1
        return {"event": event}

    def stop_capture(self, run_id: str) -> object:
        if not self._post_window_raw_emitted:
            assert self._on_raw is not None
            self._on_raw(
                RawSampleBatch(
                    run_id,
                    self.node_id,
                    1,
                    tuple(
                        RawSample(source_id, 1, 20, 200)
                        for source_id in self._source_ids
                    ),
                )
            )
        return {"run_id": run_id}

    def close(self) -> None:
        pass


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class FakeHostPolicyController:
    PRIOR_SETTINGS = {
        "frequency_khz": "dynamic",
        "scaling_governor": "powersave",
        "turbo": "enabled",
    }

    def __init__(self, calls: list[str]):
        self.calls = calls

    def apply_and_verify(self, trial) -> HostPolicyEvidence:
        self.calls.append(f"apply:{trial.mechanism_policy.value}")
        return HostPolicyEvidence.from_verified_settings(
            trial.mechanism_policy,
            "test-host-policy-controller/v1",
            {
                "frequency_khz": "2200000",
                "scaling_governor": "userspace",
                "turbo": "disabled",
            },
            self.PRIOR_SETTINGS,
        )

    def restore_and_verify(self, trial, evidence) -> HostPolicyRestorationEvidence:
        assert evidence.policy is trial.mechanism_policy
        self.calls.append(f"restore:{trial.mechanism_policy.value}")
        return HostPolicyRestorationEvidence.from_verified_settings(
            trial.mechanism_policy,
            evidence.controller_id,
            self.PRIOR_SETTINGS,
            evidence.prior_settings_fingerprint_sha256,
        )


class FakeUnverifiedHostPolicyController(FakeHostPolicyController):
    def apply_and_verify(self, trial) -> HostPolicyEvidence:
        self.calls.append(f"apply:{trial.mechanism_policy.value}")
        settings = {"turbo": "unknown"}
        return HostPolicyEvidence(
            policy=trial.mechanism_policy,
            controller_id="unverified-test-controller/v1",
            settings=settings,
            settings_fingerprint_sha256=HostPolicyEvidence.fingerprint(settings),
            prior_settings=self.PRIOR_SETTINGS,
            prior_settings_fingerprint_sha256=HostPolicyEvidence.fingerprint(
                self.PRIOR_SETTINGS
            ),
            verified=False,
        )


class FakeMismatchedRestorationController(FakeHostPolicyController):
    def restore_and_verify(self, trial, evidence) -> HostPolicyRestorationEvidence:
        self.calls.append(f"restore:{trial.mechanism_policy.value}")
        return HostPolicyRestorationEvidence.from_verified_settings(
            trial.mechanism_policy,
            evidence.controller_id,
            {"frequency_khz": "still-fixed"},
            evidence.prior_settings_fingerprint_sha256,
        )


def test_non_normal_mechanism_policy_fails_before_capture_without_controller(
    tmp_path,
):
    sources = (descriptor(),)
    study = config(
        sources,
        blocks=1,
        mechanism_policies=(MechanismPolicy.FIXED_NON_TURBO,),
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    session_created = False

    def session_factory(_trial):
        nonlocal session_created
        session_created = True
        return FakeSession(sources)

    result = TrialRunner(
        study.boundary,
        session_factory,
        launcher=FakeLauncher(),
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "FAILED"
    assert result.claim_eligibility == "INELIGIBLE"
    assert "requires a HostPolicyController" in (result.error or "")
    assert session_created is False
    assert not (experiment_dir / "trials" / result.trial_id).exists()


def test_unverified_host_policy_is_restored_and_never_captured(tmp_path):
    sources = (descriptor(),)
    study = config(
        sources,
        blocks=1,
        mechanism_policies=(MechanismPolicy.RESTRICTED_DEEP_IDLE,),
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    calls: list[str] = []
    session_created = False

    def session_factory(_trial):
        nonlocal session_created
        session_created = True
        return FakeSession(sources)

    result = TrialRunner(
        study.boundary,
        session_factory,
        launcher=FakeLauncher(),
        host_policy_controller=FakeUnverifiedHostPolicyController(calls),
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "FAILED"
    assert "did not verify settings" in (result.error or "")
    assert result.host_policy_restored is True
    assert calls == [
        "apply:RESTRICTED_DEEP_IDLE",
        "restore:RESTRICTED_DEEP_IDLE",
    ]
    assert session_created is False


def test_verified_host_policy_is_recorded_and_restored(tmp_path):
    sources = (descriptor(),)
    study = config(
        sources,
        blocks=1,
        mechanism_policies=(MechanismPolicy.FIXED_NON_TURBO,),
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = schedule.trials[0]
    calls: list[str] = []
    controller = FakeHostPolicyController(calls)
    clock = FakeClock()
    sessions: list[FakeSession] = []

    def session_factory(runtime_trial):
        calls.append("session")
        assert runtime_trial.labels["host_policy_verification"] == "VERIFIED"
        session = FakeSession(sources, clock.monotonic)
        sessions.append(session)
        return session

    result = TrialRunner(
        study.boundary,
        session_factory,
        launcher=FakeLauncher(),
        host_policy_controller=controller,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        study_preregistration_sha256="b" * 64,
        measurement_overhead_calibration_sha256="a" * 64,
    ).run(experiment_dir, trial)

    assert result.status == "COMPLETED"
    assert result.mechanism_policy == "FIXED_NON_TURBO"
    assert result.host_policy_evidence is not None
    assert result.host_policy_evidence.verified is True
    assert result.host_policy_restored is True
    assert calls == ["apply:FIXED_NON_TURBO", "session", "restore:FIXED_NON_TURBO"]

    trial_dir = experiment_dir / "trials" / trial.trial_id
    trial_document = json.loads((trial_dir / "trial.json").read_text())
    labels = trial_document["labels"]
    assert labels["mechanism_policy"] == "FIXED_NON_TURBO"
    assert labels["host_policy_verification"] == "VERIFIED"
    assert labels["measurement_overhead_calibration_sha256"] == "a" * 64
    assert labels["study_preregistration_sha256"] == "b" * 64
    assert len(labels["host_policy_settings_fingerprint_sha256"]) == 64
    assert json.loads(labels["host_policy_settings_json"])["turbo"] == "disabled"
    assert sessions[0].events[0]["labels"]["host_policy_verification"] == "VERIFIED"

    acquisition = json.loads((trial_dir / "acquisition.json").read_text())
    recorded = acquisition["result"]
    assert recorded["host_policy_restored"] is True
    assert recorded["host_policy_evidence"]["verified"] is True
    assert recorded["host_policy_restoration_evidence"]["verified"] is True
    assert (
        recorded["host_policy_restoration_evidence"][
            "restored_settings_fingerprint_sha256"
        ]
        == recorded["host_policy_evidence"]["prior_settings_fingerprint_sha256"]
    )
    assert recorded["measurement_overhead_calibration_sha256"] == "a" * 64
    assert recorded["study_preregistration_sha256"] == "b" * 64
    contract = recorded["execution_contract"]
    contract_digest = hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert recorded["execution_contract_sha256"] == contract_digest
    assert labels["execution_contract_sha256"] == contract_digest
    assert contract["target"]["argv"] == list(trial.target.argv)
    assert contract["target_nice"] == trial.target_nice
    assert contract["cpu_affinity"] == list(trial.cpu_affinity)


def test_host_policy_restore_requires_matching_readback(tmp_path):
    sources = (descriptor(),)
    study = config(
        sources,
        blocks=1,
        mechanism_policies=(MechanismPolicy.FIXED_NON_TURBO,),
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    clock = FakeClock()
    calls: list[str] = []

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=FakeLauncher(),
        host_policy_controller=FakeMismatchedRestorationController(calls),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "FAILED"
    assert result.host_policy_restored is False
    assert "read-back differs from prior state" in (result.error or "")


def test_experiment_halts_after_failed_host_policy_restoration(tmp_path):
    sources = (descriptor(),)
    study = config(
        sources,
        blocks=1,
        mechanism_policies=(MechanismPolicy.FIXED_NON_TURBO,),
    )
    schedule = build_schedule(study)
    clock = FakeClock()
    calls: list[str] = []
    trial_runner = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=FakeLauncher(),
        host_policy_controller=FakeMismatchedRestorationController(calls),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = ExperimentRunner(trial_runner).run(tmp_path, schedule)

    assert len(result.trials) == 1
    assert result.trials[0].host_policy_restored is False


def test_runner_records_markers_metrics_and_replay_environment(tmp_path):
    sources = (descriptor(),)
    output = b"same-output\n"
    digest = hashlib.sha256(output).hexdigest()
    study = config(
        sources,
        blocks=1,
        confirmatory=True,
        digest=digest,
        background=command("background"),
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = next(trial for trial in schedule.trials if trial.arm is ExperimentArm.C1)
    sessions: list[FakeSession] = []
    clock = FakeClock()

    def session_factory(_trial):
        session = FakeSession(sources, clock.monotonic)
        sessions.append(session)
        return session

    launcher = FakeLauncher(
        stdout=output,
        metrics=workload_metrics(backlog_at_end=0, slo_met=True),
    )
    runner = TrialRunner(
        study.boundary,
        session_factory,
        launcher=launcher,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = runner.run(experiment_dir, trial)

    assert result.status == "COMPLETED"
    assert result.claim_eligibility == "ELIGIBLE"
    assert result.workload_metrics_evidence.source.value == "WORKLOAD_SIDECAR"
    assert (
        result.background_workload_metrics_evidence.source.value == "WORKLOAD_SIDECAR"
    )
    assert result.actual_target_nice == trial.target_nice
    assert result.actual_background_nice == trial.background_nice
    assert result.completed_work == 100
    assert result.recovery_time_s == pytest.approx(0.01)
    assert result.fixed_horizon_target_ns == 10_000_000
    assert result.fixed_horizon_tolerance_ns == 100_000
    assert result.fixed_horizon_observed_ns == 10_000_000
    assert result.fixed_horizon_error_ns == 0
    assert result.fixed_horizon_within_tolerance is True
    assert result.primary_sample_bracket is not None
    bracket = result.primary_sample_bracket[sources[0].source_uid]
    assert bracket["pre_window_sample_ns"] < bracket["recovery_end_ns"]
    assert bracket["post_window_sample_ns"] >= bracket["recovery_end_ns"]
    assert [event["kind"] for event in sessions[0].events] == [
        "PRE_WINDOW_START",
        "BACKGROUND_ARRIVAL",
        "TARGET_SPAWN",
        "TARGET_EXEC",
        "TARGET_EXIT",
        "RECOVERY_END",
        "BACKGROUND_COMPLETION",
    ]

    target_command, _ = launcher.starts[1]
    background_command, _ = launcher.starts[0]
    assert launcher.processes[0].group_cleanup_timeouts
    assert launcher.processes[1].group_cleanup_timeouts
    for launched in (target_command, background_command):
        assert launched.env["WATTCH_REPLAY_SEED"] == str(trial.random_seed)
        assert (
            launched.env["WATTCH_MATCHED_BLOCK_ID"]
            == trial.to_dict()["matched_block_id"]
        )
        assert launched.env["WATTCH_INPUT_ID"] == "input-v1"


def test_caller_supplied_metrics_are_exploratory_only(tmp_path):
    sources = (descriptor(),)
    output = b"same-output\n"
    digest = hashlib.sha256(output).hexdigest()
    study = config(sources, blocks=1, confirmatory=True, digest=digest)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = next(item for item in schedule.trials if item.arm is ExperimentArm.C1)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=FakeLauncher(stdout=output),
        metrics_provider=lambda *_: WorkloadMetrics(
            completed_work=100,
            work_unit="items",
            throughput_per_s=1000,
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, trial)

    assert result.status == "COMPLETED"
    assert result.claim_eligibility == "NOT_EVALUATED"
    assert result.workload_metrics_evidence.source.value == "EXPLORATORY_PROVIDER"


def test_sidecar_output_digest_must_match_captured_stdout(tmp_path):
    class MismatchedOutputLauncher(FakeLauncher):
        def start(self, *args, **kwargs):
            process = super().start(*args, **kwargs)
            command = args[0]
            metrics_path = Path(command.env["WATTCH_METRICS_PATH"])
            document = json.loads(metrics_path.read_text())
            document["output_sha256"] = "0" * 64
            metrics_path.write_text(json.dumps(document) + "\n")
            return process

    sources = (descriptor(),)
    output = b"same-output\n"
    digest = hashlib.sha256(output).hexdigest()
    study = config(sources, blocks=1, confirmatory=True, digest=digest)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = next(item for item in schedule.trials if item.arm is ExperimentArm.C1)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=MismatchedOutputLauncher(
            stdout=output,
            metrics=workload_metrics(),
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, trial)

    assert result.status == "FAILED"
    assert "does not match captured target stdout" in (result.error or "")


def test_background_without_outcome_sidecar_cannot_be_confirmatory(tmp_path):
    sources = (descriptor(),)
    output = b"same-output\n"
    digest = hashlib.sha256(output).hexdigest()
    study = config(
        sources,
        blocks=1,
        confirmatory=True,
        digest=digest,
        background=command("background"),
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = next(item for item in schedule.trials if item.arm is ExperimentArm.C1)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=FakeLauncher(
            stdout=output,
            metrics=workload_metrics(backlog_at_end=0, slo_met=True),
            metrics_roles=frozenset({"target"}),
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, trial)

    assert result.status == "COMPLETED"
    assert result.claim_eligibility == "NOT_EVALUATED"
    assert result.background_workload_metrics_evidence.source.value == "NONE"


def test_arrival_paced_batch_with_invalid_trace_is_ineligible(tmp_path):
    sources = (descriptor(),)
    output = b"same-output\n"
    digest = hashlib.sha256(output).hexdigest()
    study = config(sources, blocks=1, confirmatory=True, digest=digest)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = next(item for item in schedule.trials if item.arm is ExperimentArm.C1)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=FakeLauncher(
            stdout=output,
            metrics=workload_metrics(
                arrival_trace_id="generated-trace-v1",
                arrival_trace_valid=False,
            ),
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, trial)

    assert result.status == "COMPLETED"
    assert result.claim_eligibility == "INELIGIBLE"


def test_nice_readback_mismatch_fails_before_target_exec_marker(tmp_path):
    class WrongNiceLauncher(FakeLauncher):
        def read_nice(self, process: FakeProcess) -> int:
            return process.nice + 1

    sources = (descriptor(),)
    study = replace(config(sources, blocks=1), target_nice_levels=(10,))
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=WrongNiceLauncher(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "FAILED"
    assert result.actual_target_nice is None
    assert "target nice read-back 11 does not match requested 10" in (
        result.error or ""
    )


def test_runner_rejects_capture_without_post_window_primary_sample(tmp_path):
    sources = (descriptor(),)
    study = config(sources, blocks=1)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(
            sources,
            clock.monotonic,
            emit_recovery_sample=False,
        ),
        launcher=FakeLauncher(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        sample_barrier_timeout_s=0.01,
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "FAILED"
    assert result.claim_eligibility == "INELIGIBLE"
    assert "post-window primary-sample barrier timed out" in (result.error or "")


def test_runner_interval_average_waits_for_an_additional_tail_generation(tmp_path):
    sources = interval_average_descriptors()
    study = config(sources, blocks=1)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(
            sources,
            clock.monotonic,
            recovery_sample_count=2,
        ),
        launcher=FakeLauncher(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        sample_barrier_timeout_s=0.01,
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "COMPLETED"
    assert result.primary_sample_bracket is not None
    bracket = result.primary_sample_bracket[sources[0].source_uid]
    assert bracket["post_window_samples_at_or_after"] == 2
    assert bracket["post_window_margin_ns"] == 100_000_000
    assert bracket["post_window_sample_ns"] >= bracket["recovery_end_ns"]


def test_runner_interval_average_rejects_only_one_near_edge_tail_sample(tmp_path):
    sources = interval_average_descriptors()
    study = config(sources, blocks=1)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, clock.monotonic),
        launcher=FakeLauncher(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        sample_barrier_timeout_s=0.01,
    ).run(experiment_dir, schedule.trials[0])

    assert result.status == "FAILED"
    assert "interval-tail support" in (result.error or "")


def test_thirty_second_horizon_rejects_nine_hundred_ms_daemon_error(tmp_path):
    sources = (descriptor(),)
    study = replace(
        config(
            sources,
            blocks=1,
            observation=FixedHorizon(30, tolerance_s=0.01),
        ),
        interval_ms=1_000,
    )
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = schedule.trials[0]
    clock = FakeClock()

    result = TrialRunner(
        study.boundary,
        lambda _trial: FakeSession(
            sources,
            clock.monotonic,
            event_offsets_ns={"RECOVERY_END": 900_000_000},
        ),
        launcher=FakeLauncher(),
        metrics_provider=lambda *_: WorkloadMetrics(
            completed_work=100,
            work_unit="items",
            throughput_per_s=100 / 30,
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(experiment_dir, trial)

    assert result.fixed_horizon_target_ns == 30_000_000_000
    assert result.fixed_horizon_tolerance_ns == 10_000_000
    assert result.fixed_horizon_error_ns == pytest.approx(900_000_000, abs=10)
    assert result.fixed_horizon_within_tolerance is False
    assert result.claim_eligibility == "INELIGIBLE"


def test_service_without_work_and_slo_evidence_is_not_evaluated(tmp_path):
    sources = (descriptor(),)
    observation = EqualWork(
        target_work=10,
        work_unit="requests",
        workload_mode=WorkloadMode.SERVICE,
        timeout_s=1,
        arrival_trace_id="arrivals-v1",
    )
    study = config(sources, blocks=1, observation=observation)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    trial = schedule.trials[0]
    launcher = FakeLauncher()
    runner = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources),
        launcher=launcher,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )
    result = runner.run(experiment_dir, trial)
    assert result.status == "COMPLETED"
    assert result.claim_eligibility == "NOT_EVALUATED"
    assert result.completed_work is None
    assert launcher.starts[0][0].env["WATTCH_ARRIVAL_TRACE_ID"] == "arrivals-v1"


def test_fixed_horizon_service_propagates_arrivals_and_requires_slo_evidence(
    tmp_path,
):
    sources = (descriptor(),)
    output = b"same-output\n"
    digest = hashlib.sha256(output).hexdigest()
    observation = FixedHorizon(
        0.01,
        workload_mode=WorkloadMode.SERVICE,
        arrival_trace_id="open-loop-v1",
    )
    study = config(
        sources,
        blocks=1,
        observation=observation,
        confirmatory=True,
        digest=digest,
    )
    schedule = build_schedule(study)
    trial = next(item for item in schedule.trials if item.arm is ExperimentArm.C1)

    missing_clock = FakeClock()
    missing_dir = prepare_experiment_layout(tmp_path / "missing", schedule)
    missing = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, missing_clock.monotonic),
        launcher=FakeLauncher(stdout=output),
        metrics_provider=lambda *_: WorkloadMetrics(
            completed_work=10,
            work_unit="requests",
            throughput_per_s=1000,
        ),
        monotonic=missing_clock.monotonic,
        sleep=missing_clock.sleep,
    ).run(missing_dir, trial)
    assert missing.status == "COMPLETED"
    assert missing.claim_eligibility == "NOT_EVALUATED"

    verified_clock = FakeClock()
    verified_launcher = FakeLauncher(
        stdout=output,
        metrics=workload_metrics(
            completed_work=10,
            work_unit="requests",
            throughput_per_s=1000,
            latency_ms={"p95": 2.0},
            backlog_at_end=0,
            slo_met=True,
            arrival_trace_id="open-loop-v1",
            arrival_trace_valid=True,
        ),
    )
    verified_dir = prepare_experiment_layout(tmp_path / "verified", schedule)
    verified = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources, verified_clock.monotonic),
        launcher=verified_launcher,
        monotonic=verified_clock.monotonic,
        sleep=verified_clock.sleep,
    ).run(verified_dir, trial)
    assert verified.claim_eligibility == "ELIGIBLE"
    assert verified_launcher.starts[0][0].env["WATTCH_ARRIVAL_TRACE_ID"] == (
        "open-loop-v1"
    )


def test_trial_runner_uses_real_client_callback_and_marker_contract(
    tmp_path, fake_daemon
):
    with WattchClient(fake_daemon.socket_path) as client:
        sources = client.sources
    study = config(sources, blocks=1)
    schedule = build_schedule(study)
    experiment_dir = prepare_experiment_layout(tmp_path, schedule)
    clock = FakeClock()
    runner = TrialRunner(
        study.boundary,
        lambda _: WattchClient(fake_daemon.socket_path),
        launcher=FakeLauncher(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = runner.run(experiment_dir, schedule.trials[0])
    assert result.status == "COMPLETED"
    assert len(result.marker_event_ids) == 7
    assert (
        verify_checksums(experiment_dir / "trials" / schedule.trials[0].trial_id) == []
    )


def test_experiment_runner_honors_washout_and_refuses_overwrite(tmp_path):
    sources = (descriptor(),)
    study = config(sources, blocks=2)
    schedule = build_schedule(study)
    clock = FakeClock()
    trial_runner = TrialRunner(
        study.boundary,
        lambda _: FakeSession(sources),
        launcher=FakeLauncher(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    experiment_runner = ExperimentRunner(trial_runner, sleep=clock.sleep)
    result = experiment_runner.run(tmp_path, schedule)
    assert len(result.trials) == 6
    assert clock.sleeps.count(0.25) == 4
    assert clock.sleeps.count(1.5) == 1
    assert (result.experiment_dir / "run-results.json").is_file()

    with pytest.raises(FileExistsError):
        experiment_runner.run(tmp_path, schedule)
