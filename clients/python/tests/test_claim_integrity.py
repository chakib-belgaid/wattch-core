from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pywattch import RawSample, RawSampleBatch, SourceDescriptor
from pywattch.analysis import (
    CausalGroundTruthReference,
    Estimand,
    NoCollectorRuntimeSpec,
    PrimaryRegimePreregistration,
    StudyClaimRecord,
    StudyPreregistration,
    VerifiedTrialEvidenceBundle,
    calibrate_aa_threshold,
    detect_ranking_reversal,
    evaluate_trial_quality,
    paired_effect,
    paired_normal_sample_size,
    seal_study_preregistration,
    write_verified_meter_calibration,
    write_verified_no_collector_runtime,
    write_verified_prestudy_evidence,
)
from pywattch.analysis.artifacts import (
    AnalysisArtifactError,
    write_claims_json,
    write_report_markdown,
)
from pywattch.experiment import (
    ArmConfig,
    EqualWork,
    ExperimentArm,
    ExperimentConfig,
    FixedHorizon,
    MechanismPolicy,
    TrialRunner,
    WorkloadCommand,
    WorkloadMode,
    build_schedule,
    finalize_trial_quality,
    prepare_experiment_layout,
)

from test_experiment import (
    FakeClock,
    FakeHostPolicyController,
    FakeLauncher,
    FakeSession,
    boundary,
    descriptor,
    workload_metrics,
)


class CompletedLauncher(FakeLauncher):
    def start(self, *args, **kwargs):
        process = super().start(*args, **kwargs)
        process.returncode = 0
        return process


class TimedCompletedLauncher(CompletedLauncher):
    def __init__(self, stdout, *, metrics, clock, delay_s):
        super().__init__(stdout, metrics=metrics)
        self._clock = clock
        self._delay_s = delay_s

    def start(self, *args, **kwargs):
        self._clock.sleep(self._delay_s)
        return super().start(*args, **kwargs)


class EffectSession(FakeSession):
    def __init__(
        self,
        sources,
        monotonic,
        post_value: int,
        event_offsets_ns=None,
    ):
        super().__init__(
            sources,
            monotonic,
            event_offsets_ns=event_offsets_ns,
            emit_recovery_sample=False,
        )
        self._post_value = post_value
        self._sample_offset_ns = 0

    def start_capture(self, *args, on_raw_batch, **kwargs):
        def adjusted(batch):
            if batch.batch_sequence_no != 0 or self._sample_offset_ns == 0:
                on_raw_batch(batch)
                return
            on_raw_batch(
                RawSampleBatch(
                    batch.run_id,
                    batch.node_id,
                    batch.batch_sequence_no,
                    tuple(
                        RawSample(
                            sample.source_id,
                            sample.sequence_no,
                            sample.monotonic_ns + self._sample_offset_ns,
                            sample.value,
                        )
                        for sample in batch.samples
                    ),
                )
            )

        return super().start_capture(*args, on_raw_batch=adjusted, **kwargs)

    def record_event(self, run_id, event_id, kind, labels):
        response = super().record_event(run_id, event_id, kind, labels)
        if kind == "RECOVERY_END":
            event = response["event"]
            self._on_raw(
                RawSampleBatch(
                    run_id,
                    self.node_id,
                    1,
                    tuple(
                        RawSample(
                            source_id,
                            1,
                            event["monotonic_ns"] + 1,
                            self._post_value if source_id == 1 else 40.0,
                        )
                        for source_id in self._source_ids
                    ),
                )
            )
            self._post_window_raw_emitted = True
        return response


def _temperature_descriptor() -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=2,
        source_uid="os://node/temperature/package-0",
        node_id="node-1",
        backend="linux-os-telemetry",
        kind="OTHER",
        unit="CELSIUS",
        temporality="GAUGE",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="cpu/package/0/temperature",
        clock_domain="monotonic",
        sampling_mode="POLLED",
        labels={"wattch.metric": "temperature"},
    )


def _sidecar_metrics(observation):
    return workload_metrics(
        completed_work=(
            observation.target_work if isinstance(observation, EqualWork) else 1
        ),
        work_unit=(
            observation.work_unit if isinstance(observation, EqualWork) else "items"
        ),
        throughput_per_s=100.0,
        backlog_at_end=0.0,
        slo_met=True,
        arrival_trace_id="open-loop-v1",
        arrival_trace_valid=True,
    )


def _run_trials(
    *,
    experiment_dir,
    trials,
    selected_boundary,
    sources,
    output,
    post_value,
    created_ns,
    monkeypatch,
    calibration_sha256=None,
    preregistration_sha256=None,
    event_offsets_ns=None,
    launcher_delay_s=0.0,
):
    current_created_ns = [created_ns]
    monkeypatch.setattr(
        "pywattch.experiment.artifacts.time.time_ns",
        lambda: current_created_ns[0],
    )
    directories = []
    for trial in trials:
        value = post_value(trial) if callable(post_value) else post_value
        clock = FakeClock()
        runner = TrialRunner(
            selected_boundary,
            lambda _, local_clock=clock, current=value: EffectSession(
                sources,
                local_clock.monotonic,
                current,
                event_offsets_ns=event_offsets_ns,
            ),
            launcher=TimedCompletedLauncher(
                output,
                metrics=_sidecar_metrics(trial.observation),
                clock=clock,
                delay_s=launcher_delay_s,
            ),
            host_policy_controller=FakeHostPolicyController([]),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            measurement_overhead_calibration_sha256=calibration_sha256,
            study_preregistration_sha256=preregistration_sha256,
        )
        result = runner.run(experiment_dir, trial)
        assert result.status == "COMPLETED"
        trial_dir = experiment_dir / "trials" / trial.trial_id
        finalize_trial_quality(
            trial_dir,
            evaluate_trial_quality(trial_dir, measurement_overhead_ratio=0.0),
        )
        directories.append(trial_dir)
    return directories


def _reversal(observations, estimand: Estimand, thresholds, *, confidence=0.95):
    selected = tuple(item for item in observations if item.outcome.estimand == estimand)
    isolated = paired_effect(
        (item for item in selected if item.context == "idle"),
        treatment_arm="C1",
        control_arm="C2",
        confidence=confidence,
    )
    cotenanted = paired_effect(
        (item for item in selected if item.context == "service-background"),
        treatment_arm="C1",
        control_arm="C2",
        confidence=confidence,
    )
    return detect_ranking_reversal(
        isolated,
        cotenanted,
        isolated_threshold_joules=thresholds[f"{estimand.value}:idle"],
        cotenanted_threshold_joules=(
            thresholds[f"{estimand.value}:service-background"]
        ),
    )


def test_positive_claim_is_issued_only_from_replayed_sealed_trials(
    tmp_path, monkeypatch
):
    source = descriptor()
    temperature = _temperature_descriptor()
    sources = (source, temperature)
    selected_boundary = boundary(sources)
    output = b"same-output\n"
    output_digest = hashlib.sha256(output).hexdigest()
    command = WorkloadCommand(
        ("/bin/echo", "same-output"),
        env={"WATTCH_ARRIVAL_TRACE_ID": "open-loop-v1"},
    )
    background_command = WorkloadCommand(
        ("/bin/echo", "same-output"),
        env={"WATTCH_ARRIVAL_TRACE_ID": "open-loop-v1"},
    )
    experiment_root = tmp_path / "experiments"
    schedules: list[Path] = []
    experiment_dirs = []
    regimes = []
    pilot_artifacts = {}
    aa_artifacts = {}
    placebo_artifacts = {}
    thresholds = {}
    common_labels = {
        "cache_regime": "cold-start",
        "prelaunch_temperature_source_uid": temperature.source_uid,
        "arrival_trace_id": "open-loop-v1",
    }
    auxiliary_created_ns = int(
        datetime(2024, 12, 1, tzinfo=UTC).timestamp() * 1e9
    )

    for estimand_name, observation in (
        (
            "FIXED_HORIZON",
            FixedHorizon(0.01, pre_window_s=0.001, tolerance_s=0.0001),
        ),
        (
            "EQUAL_WORK",
            EqualWork(
                target_work=1,
                work_unit="items",
                workload_mode=WorkloadMode.BATCH,
                timeout_s=1,
                recovery_tail_s=0.01,
            ),
        ),
    ):
        for context in ("idle", "service-background"):
            regime_id = f"{estimand_name}:{context}"
            prestudy_id = f"prestudy-{estimand_name.lower()}-{context}"
            prestudy_config = ExperimentConfig(
                experiment_id=prestudy_id,
                boundary=selected_boundary,
                observation=observation,
                c0=ArmConfig(ExperimentArm.C0, "sham", command),
                c1=ArmConfig(
                    ExperimentArm.C1,
                    "sham-pair",
                    command,
                    expected_output_sha256=output_digest,
                ),
                c2=ArmConfig(
                    ExperimentArm.C2,
                    "sham-pair",
                    command,
                    expected_output_sha256=output_digest,
                ),
                blocks=12,
                seed=17,
                interval_ms=10,
                background=(
                    None if context == "idle" else background_command
                ),
                background_workload_id=context,
                target_nice_levels=(0,),
                mechanism_policies=(MechanismPolicy.NORMAL_DYNAMIC,),
                input_id="prestudy-input-v1",
                labels=common_labels,
                confirmatory=True,
            )
            prestudy_schedule = build_schedule(prestudy_config)
            prestudy_dir = prepare_experiment_layout(
                experiment_root, prestudy_schedule
            )
            prestudy_dirs = _run_trials(
                experiment_dir=prestudy_dir,
                trials=prestudy_schedule.trials,
                selected_boundary=selected_boundary,
                sources=sources,
                output=output,
                post_value=lambda trial: (
                    201_100
                    if trial.arm is ExperimentArm.C1 and trial.block_index % 2
                    else 199_100
                    if trial.arm is ExperimentArm.C1
                    else 200_100
                ),
                created_ns=auxiliary_created_ns,
                monkeypatch=monkeypatch,
            )
            artifact_number = len(pilot_artifacts) + 1
            pilot = write_verified_prestudy_evidence(
                tmp_path / f"pilot-{artifact_number}.json",
                role="DISCARDED_PILOT",
                regime_id=regime_id,
                trial_directories=prestudy_dirs,
            )
            aa = write_verified_prestudy_evidence(
                tmp_path / f"aa-{artifact_number}.json",
                role="AA_CALIBRATION",
                regime_id=regime_id,
                trial_directories=prestudy_dirs,
            )
            placebo = write_verified_prestudy_evidence(
                tmp_path / f"placebo-{artifact_number}.json",
                role="PLACEBO",
                regime_id=regime_id,
                trial_directories=prestudy_dirs,
            )
            aa_threshold = calibrate_aa_threshold(
                aa_paired_differences_joules=aa.paired_differences_joules,
                control_joules=aa.control_joules,
            )
            sample_size = paired_normal_sample_size(
                pilot.paired_differences_joules,
                aa_threshold=aa_threshold,
            )
            pilot_artifacts[regime_id] = pilot
            aa_artifacts[regime_id] = aa
            placebo_artifacts[regime_id] = placebo
            thresholds[regime_id] = aa_threshold.meaningful_effect_joules

            experiment_id = f"claim-{estimand_name.lower()}-{context}"
            config = ExperimentConfig(
                experiment_id=experiment_id,
                boundary=selected_boundary,
                observation=observation,
                c0=ArmConfig(ExperimentArm.C0, "sham", command),
                c1=ArmConfig(
                    ExperimentArm.C1,
                    "P1",
                    command,
                    expected_output_sha256=output_digest,
                ),
                c2=ArmConfig(
                    ExperimentArm.C2,
                    "P2",
                    command,
                    expected_output_sha256=output_digest,
                ),
                blocks=12,
                seed=7,
                interval_ms=10,
                background=(
                    None if context == "idle" else background_command
                ),
                background_workload_id=context,
                target_nice_levels=(0,),
                mechanism_policies=(MechanismPolicy.NORMAL_DYNAMIC,),
                input_id="input-v1",
                confirmatory=True,
                labels=common_labels,
            )
            schedule = build_schedule(config)
            experiment_dir = prepare_experiment_layout(experiment_root, schedule)
            schedules.append(experiment_dir / "schedule.json")
            experiment_dirs.append((experiment_dir, schedule))
            block_ids = tuple(
                f"{experiment_id}-block-{index:03d}" for index in range(1, 13)
            )
            regime_id = f"{estimand_name}:{context}"
            regimes.append(
                PrimaryRegimePreregistration(
                    regime_id=regime_id,
                    estimand=estimand_name,
                    context=context,
                    pilot_block_ids=pilot.block_ids,
                    pilot_discarded=True,
                    sample_size=sample_size,
                    confirmatory_block_ids=block_ids,
                )
            )

    calibration_dirs = []
    marker_kinds = (
        "PRE_WINDOW_START",
        "BACKGROUND_ARRIVAL",
        "TARGET_SPAWN",
        "TARGET_EXEC",
        "TARGET_EXIT",
        "RECOVERY_END",
        "BACKGROUND_COMPLETION",
    )
    calibration_observation = EqualWork(
        target_work=1,
        work_unit="items",
        workload_mode=WorkloadMode.BATCH,
        timeout_s=1,
        recovery_tail_s=0.01,
    )
    calibration_created_ns = time.time_ns()
    calibration_commands = {
        regime: WorkloadCommand(
            command.argv,
            env={
                "WATTCH_ARRIVAL_TRACE_ID": "open-loop-v1",
                "WATTCH_INPUT_ID": "calibration-input-v1",
                "WATTCH_CALIBRATION_REGIME": regime,
            },
        )
        for regime in ("IDLE", "FIXED_LOAD", "AA")
    }
    calibration_replay_seeds = {}
    for calibration_regime in ("IDLE", "FIXED_LOAD", "AA"):
        calibration_command = calibration_commands[calibration_regime]
        for mode, phase_ns in (
            ("INSTRUMENTED", 1_000_000),
            ("INSTRUMENTED", 2_000_000),
        ):
            calibration_id = (
                f"calibration-{calibration_regime.lower()}-{mode.lower()}-{phase_ns}"
            )
            calibration_config = ExperimentConfig(
                experiment_id=calibration_id,
                boundary=selected_boundary,
                observation=calibration_observation,
                c0=ArmConfig(ExperimentArm.C0, "sham", calibration_command),
                c1=ArmConfig(
                    ExperimentArm.C1,
                    "calibration-target",
                    calibration_command,
                    expected_output_sha256=output_digest,
                ),
                c2=ArmConfig(
                    ExperimentArm.C2,
                    "calibration-target",
                    calibration_command,
                    expected_output_sha256=output_digest,
                ),
                blocks=1,
                seed=31,
                interval_ms=10,
                background_workload_id="calibration",
                input_id="calibration-input-v1",
                labels={
                    **common_labels,
                    "meter_calibration_regime": calibration_regime,
                    "meter_calibration_mode": mode,
                    "meter_calibration_pair_id": f"{calibration_regime}-pair",
                },
                confirmatory=True,
            )
            calibration_schedule = build_schedule(calibration_config)
            calibration_dir = prepare_experiment_layout(
                experiment_root, calibration_schedule
            )
            calibration_trial = next(
                trial
                for trial in calibration_schedule.trials
                if trial.arm is ExperimentArm.C1
            )
            calibration_replay_seeds[calibration_regime] = (
                calibration_trial.random_seed
            )
            calibration_dirs.extend(
                _run_trials(
                    experiment_dir=calibration_dir,
                    trials=(calibration_trial,),
                    selected_boundary=selected_boundary,
                    sources=sources,
                    output=output,
                    post_value=200_100,
                    created_ns=calibration_created_ns,
                    monkeypatch=monkeypatch,
                    event_offsets_ns={kind: phase_ns for kind in marker_kinds},
                    launcher_delay_s=0.01,
                )
            )
    no_collector_ticks = iter(
        value
        for index in range(3)
        for value in (index * 20_000_000, index * 20_000_000 + 10_000_000)
    )
    monkeypatch.setattr(
        "pywattch.analysis.artifacts.time.monotonic_ns",
        lambda: next(no_collector_ticks),
    )
    no_collector_runtime = write_verified_no_collector_runtime(
        tmp_path / "no-collector-runtime.json",
        specs=(
            NoCollectorRuntimeSpec(
                regime=regime,
                pair_id=f"{regime}-pair",
                interval_ns=10_000_000,
                command=calibration_commands[regime],
                machine_id="node-1",
                replay_seed=calibration_replay_seeds[regime],
                matched_block_id=f"calibration-{regime.lower()}-pair",
                cache_regime="cold-start",
                prelaunch_temperature_source_uid=temperature.source_uid,
                prelaunch_temperature_celsius=100.0,
                timeout_s=1.0,
            )
            for regime in ("IDLE", "FIXED_LOAD", "AA")
        ),
    )
    calibration = write_verified_meter_calibration(
        tmp_path / "meter-calibration.json",
        calibration_id="meter-v1",
        boundary=selected_boundary,
        trial_directories=calibration_dirs,
        no_collector_runtime=no_collector_runtime,
        max_phase_sensitivity_ratio=0.1,
    )
    sealed_at = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1e9)
    verified_preregistration = seal_study_preregistration(
        tmp_path / "preregistration.json",
        StudyPreregistration("claim-prereg-v1", tuple(regimes)),
        schedule_paths=schedules,
        pilot_evidence_paths=pilot_artifacts,
        aa_evidence_paths=aa_artifacts,
        placebo_evidence_paths=placebo_artifacts,
        calibration_evidence_paths=[calibration],
        sealed_at_unix_ns=sealed_at,
    )
    calibration_digest = next(iter(verified_preregistration.calibration_digests))
    day_timestamps = {
        index: int(
            datetime(
                2025,
                1,
                2 if index <= 6 else 3,
                tzinfo=UTC,
            ).timestamp()
            * 1e9
        )
        for index in range(1, 13)
    }
    trial_dirs = []
    for experiment_dir, schedule in experiment_dirs:
        for trial in schedule.trials:
            if trial.arm is ExperimentArm.C0:
                post_value = 350_000
            elif trial.background_workload_id == "idle":
                post_value = 700_000 if trial.arm is ExperimentArm.C1 else 400_000
            else:
                post_value = 250_000 if trial.arm is ExperimentArm.C1 else 600_000
            trial_dirs.extend(
                _run_trials(
                    experiment_dir=experiment_dir,
                    trials=(trial,),
                    selected_boundary=selected_boundary,
                    sources=sources,
                    output=output,
                    post_value=post_value,
                    created_ns=day_timestamps[trial.block_index],
                    monkeypatch=monkeypatch,
                    calibration_sha256=calibration_digest,
                    preregistration_sha256=(
                        verified_preregistration.artifact_sha256
                    ),
                )
            )

    bundle = VerifiedTrialEvidenceBundle.from_trial_directories(
        trial_dirs,
        preregistration=verified_preregistration,
    )
    observations = bundle.observations()
    fixed = _reversal(
        observations,
        Estimand.FIXED_HORIZON,
        thresholds,
    )
    equal = _reversal(
        observations,
        Estimand.EQUAL_WORK,
        thresholds,
    )
    day_reversals = {
        day: (
            _reversal(
                bundle.observations(day_utc=day),
                Estimand.FIXED_HORIZON,
                thresholds,
            ),
            _reversal(
                bundle.observations(day_utc=day),
                Estimand.EQUAL_WORK,
                thresholds,
            ),
        )
        for day in ("2025-01-02", "2025-01-03")
    }
    claim = StudyClaimRecord.from_verified_results(
        claim_id="ranking-reversal-v1",
        study_id="polling-vs-batching",
        treatment_program_id="P1",
        control_program_id="P2",
        preregistration=verified_preregistration,
        trial_bundle=bundle,
        fixed_horizon_reversal=fixed,
        equal_work_reversal=equal,
        placebo_checks=verified_preregistration.placebo_checks,
        independent_day_reversals=day_reversals,
    )
    assert claim.acceptance.supported is True
    low_confidence_fixed = _reversal(
        observations,
        Estimand.FIXED_HORIZON,
        thresholds,
        confidence=0.50,
    )
    with pytest.raises(AnalysisArtifactError, match="two-sided 95%"):
        StudyClaimRecord.from_verified_results(
            claim_id="invalid-low-confidence",
            study_id="polling-vs-batching",
            treatment_program_id="P1",
            control_program_id="P2",
            preregistration=verified_preregistration,
            trial_bundle=bundle,
            fixed_horizon_reversal=low_confidence_fixed,
            equal_work_reversal=equal,
            placebo_checks=verified_preregistration.placebo_checks,
            independent_day_reversals=day_reversals,
        )
    claims_path = write_claims_json(
        tmp_path / "claims.json",
        claim,
        boundaries=[selected_boundary],
    )
    report_path = write_report_markdown(
        tmp_path / "report.md",
        "# Result\n\nThe ranking reversal is supported.",
        boundaries=[selected_boundary],
        claim=claim,
    )
    assert claim.digest_sha256 in claims_path.read_text()
    assert claim.digest_sha256 in report_path.read_text()
    ground_truth = CausalGroundTruthReference.from_claims_artifact(
        claims_path,
        verified_claim=claim,
    )
    assert ground_truth.artifact_verified is True

    records = no_collector_runtime.document["records"]
    assert isinstance(records, list)
    records[0]["duration_ns"] = int(records[0]["duration_ns"]) + 1
    with pytest.raises(AnalysisArtifactError, match="differs from its verified file"):
        no_collector_runtime._require_verified()
