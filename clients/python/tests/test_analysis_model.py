import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pywattch import RawSample, SourceDescriptor
from pywattch.analysis import (
    MODEL_EVIDENCE_CLASS,
    MODEL_INPUT_ALIGNMENT,
    MODEL_INPUT_ARTIFACT_KIND,
    MODEL_INPUT_ARTIFACT_LABEL,
    AcceptedControlTrialProvenance,
    AcceptedTargetTrialProvenance,
    AnalysisError,
    BaselineEpisode,
    BoundaryMismatchError,
    CausalGroundTruthReference,
    DynamicLinearBaseline,
    CounterfactualEstimand,
    EpisodeGroupKey,
    MeasurementBoundary,
    PredictionRequest,
    StudyAcceptanceResult,
    TrialBundleSeal,
    validate_semantic_rebind,
    validate_group_split,
)
from pywattch.analysis.artifacts import write_predictions_jsonl
from pywattch.analysis.model import _derive_trial_model_power


def model_source(backend: str = "powermetrics") -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=1,
        source_uid=f"{backend}://node/cpu/power",
        node_id="node",
        backend=backend,
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="cpu/power",
        clock_domain="monotonic",
        sampling_mode="POLLED",
    )


def model_boundary(backend: str = "powermetrics") -> MeasurementBoundary:
    source = model_source(backend)
    return MeasurementBoundary.from_descriptors(
        [source], [source.source_uid], boundary_id=f"{backend}-cpu-power"
    )


def interval_model_sources() -> tuple[SourceDescriptor, SourceDescriptor]:
    duration_uid = "powermetrics://node/frame/elapsed_ns"
    power = SourceDescriptor(
        local_source_id=1,
        source_uid="powermetrics://node/cpu/power",
        node_id="node",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INTERVAL_AVERAGE",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="cpu/power",
        clock_domain="monotonic",
        sampling_mode="PERIODIC",
        labels={
            "wattch.interval_duration_source_uid": duration_uid,
            "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
        },
    )
    duration = SourceDescriptor(
        local_source_id=2,
        source_uid=duration_uid,
        node_id="node",
        backend="powermetrics",
        kind="SAMPLE_INTERVAL_DURATION",
        unit="NANOSECOND",
        temporality="DELTA",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="frame/elapsed_ns",
        clock_domain="monotonic",
        sampling_mode="PERIODIC",
    )
    return power, duration


def accepted_causal_ground_truth(
    boundary: MeasurementBoundary | None = None,
) -> CausalGroundTruthReference:
    selected = boundary or model_boundary()
    return CausalGroundTruthReference(
        study_id="accepted-ranking-reversal-study",
        claim_id="accepted-ranking-reversal-claim",
        measurement_boundary_id=selected.boundary_id,
        claims_artifact_sha256=hashlib.sha256(b"claims.json").hexdigest(),
        acceptance=StudyAcceptanceResult(
            supported=True,
            fixed_horizon_supported=True,
            equal_work_supported=True,
            placebo_supported=True,
            independent_day_replication_supported=True,
            mechanism_required=True,
            mechanism_supported=True,
            reasons=(),
        ),
    )


def accepted_control_provenance(
    episode_id: str, boundary: MeasurementBoundary
) -> AcceptedControlTrialProvenance:
    return AcceptedControlTrialProvenance(
        trial_id=episode_id,
        experiment_id="background-model-training",
        matched_block_id=f"block-{episode_id}",
        measurement_boundary_id=boundary.boundary_id,
        checksums_sha256=hashlib.sha256(episode_id.encode()).hexdigest(),
    )


def accepted_target_provenance(
    run_id: str,
    boundary: MeasurementBoundary,
    estimand: CounterfactualEstimand,
    *,
    observed_joules: float = 100.0,
) -> AcceptedTargetTrialProvenance:
    return AcceptedTargetTrialProvenance(
        trial_id=run_id,
        experiment_id="candidate-experiment",
        matched_block_id="candidate-block",
        measurement_boundary_id=boundary.boundary_id,
        checksums_sha256=hashlib.sha256(run_id.encode()).hexdigest(),
        arm="C1",
        program_id="P1",
        estimand=estimand,
        observed_joules=observed_joules,
    )


def control_episodes(
    selected: MeasurementBoundary | None = None, *, prefix: str = "control"
) -> list[BaselineEpisode]:
    selected = selected or model_boundary()
    episodes = []
    timestamps = tuple(index * 1_000_000_000 for index in range(5))
    for episode_index in range(6):
        pre_load = 0.5 + episode_index * 0.25
        initial_power = 8.0 + episode_index
        demand = tuple(0.5 + episode_index * 0.1 + index * 0.2 for index in range(5))
        values = []
        previous = initial_power
        for offered in demand:
            current = 2.0 + pre_load + 3.0 * offered + 0.4 * previous
            values.append(current)
            previous = current
        episodes.append(
            BaselineEpisode(
                episode_id=f"{prefix}-{episode_index}",
                arm="C0",
                provenance=accepted_control_provenance(
                    f"{prefix}-{episode_index}", selected
                ),
                boundary=selected,
                timestamps_ns=timestamps,
                power_watts=tuple(values),
                initial_power_watts=initial_power,
                pre_treatment_features={"pre_load": pre_load},
                exogenous_features={"offered_demand": demand},
            )
        )
    return episodes


def fitted_model() -> DynamicLinearBaseline:
    return DynamicLinearBaseline(
        pre_treatment_feature_names=["pre_load"],
        exogenous_feature_names=["offered_demand"],
    ).fit(
        control_episodes(),
        causal_ground_truth=accepted_causal_ground_truth(),
    )


def prediction_request(**overrides) -> PredictionRequest:
    boundary = overrides.get("boundary", model_boundary())
    estimand = overrides.get(
        "estimand",
        CounterfactualEstimand.fixed_horizon(4_000_000_000),
    )
    run_id = overrides.get("run_id", "candidate-run")
    fields = {
        "run_id": run_id,
        "boundary": boundary,
        "estimand": estimand,
        "target_trial_provenance": accepted_target_provenance(
            run_id,
            boundary,
            estimand,
        ),
        "timestamps_ns": tuple(index * 1_000_000_000 for index in range(5)),
        "initial_power_watts": 10.0,
        "pre_treatment_features": {"pre_load": 1.0},
        "exogenous_features": {"offered_demand": (0.8, 1.0, 1.2, 1.4, 1.6)},
    }
    fields.update(overrides)
    return PredictionRequest(**fields)


def test_model_interval_power_uses_same_affine_receipt_synchronization():
    power, duration = interval_model_sources()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu-power"
    )
    receipts = (
        1_000_000_000,
        2_001_000_000,
        2_999_000_000,
        4_000_500_000,
        4_999_500_000,
    )
    samples = []
    for sequence_no, (receipt_ns, watts) in enumerate(
        zip(receipts, (8.0, 10.0, 12.0, 14.0, 16.0))
    ):
        samples.extend(
            (
                RawSample(1, sequence_no, receipt_ns, watts * 1_000.0),
                RawSample(2, sequence_no, receipt_ns, 1_000_000_000),
            )
        )
    evidence = SimpleNamespace(
        descriptors=(power, duration),
        samples=tuple(samples),
        outcome=SimpleNamespace(
            boundary=boundary,
            start_monotonic_ns=1_500_000_000,
            end_monotonic_ns=4_500_000_000,
        ),
    )

    timestamps, values, initial, method = _derive_trial_model_power(evidence)

    assert timestamps[0] == 1_500_000_000
    assert timestamps[-1] == 4_500_000_000
    assert all(right > left for left, right in zip(timestamps, timestamps[1:]))
    assert len(timestamps) == len(values)
    assert initial == pytest.approx(8.0)
    assert method == "RAW_INTERVAL_AVERAGE_AFFINE_SYNC_V1"


def test_model_trains_only_on_control_episodes():
    episodes = control_episodes()
    treatment = episodes[0]
    episodes[0] = BaselineEpisode(
        episode_id=treatment.episode_id,
        arm="P1",
        provenance=treatment.provenance,
        boundary=treatment.boundary,
        timestamps_ns=treatment.timestamps_ns,
        power_watts=treatment.power_watts,
        initial_power_watts=treatment.initial_power_watts,
        pre_treatment_features=treatment.pre_treatment_features,
        exogenous_features=treatment.exogenous_features,
    )
    model = DynamicLinearBaseline(
        pre_treatment_feature_names=["pre_load"],
        exogenous_feature_names=["offered_demand"],
    )
    with pytest.raises(AnalysisError, match="only control arm"):
        model.fit(
            episodes,
            causal_ground_truth=accepted_causal_ground_truth(),
        )


def test_model_fit_requires_accepted_causal_ground_truth_reference():
    model = DynamicLinearBaseline(
        pre_treatment_feature_names=["pre_load"],
        exogenous_feature_names=["offered_demand"],
    )

    with pytest.raises(TypeError, match="causal_ground_truth"):
        model.fit(control_episodes())

    rejected = StudyAcceptanceResult(
        supported=False,
        fixed_horizon_supported=True,
        equal_work_supported=True,
        placebo_supported=True,
        independent_day_replication_supported=False,
        mechanism_required=False,
        mechanism_supported=None,
        reasons=("independent-day replication failed",),
    )
    with pytest.raises(AnalysisError, match="every confirmatory acceptance gate"):
        CausalGroundTruthReference(
            study_id="rejected-study",
            claim_id="rejected-claim",
            measurement_boundary_id=model_boundary().boundary_id,
            claims_artifact_sha256=hashlib.sha256(b"rejected").hexdigest(),
            acceptance=rejected,
        )


def test_model_fit_rejects_ground_truth_or_control_seal_from_another_boundary():
    episodes = control_episodes()
    model = DynamicLinearBaseline(
        pre_treatment_feature_names=["pre_load"],
        exogenous_feature_names=["offered_demand"],
    )
    with pytest.raises(AnalysisError, match="ground-truth measurement boundary"):
        model.fit(
            episodes,
            causal_ground_truth=accepted_causal_ground_truth(
                model_boundary("rapl")
            ),
        )

    wrong_seal = replace(
        episodes[0].provenance,
        measurement_boundary_id="different-boundary",
    )
    episodes[0] = replace(episodes[0], provenance=wrong_seal)
    with pytest.raises(AnalysisError, match="provenance uses a different"):
        model.fit(
            episodes,
            causal_ground_truth=accepted_causal_ground_truth(),
        )


def test_model_fit_rejects_control_seal_for_a_different_trial():
    episodes = control_episodes()
    episodes[0] = replace(
        episodes[0],
        provenance=replace(episodes[0].provenance, trial_id="another-trial"),
    )
    model = DynamicLinearBaseline(
        pre_treatment_feature_names=["pre_load"],
        exogenous_feature_names=["offered_demand"],
    )
    with pytest.raises(AnalysisError, match="sealed control trial id"):
        model.fit(
            episodes,
            causal_ground_truth=accepted_causal_ground_truth(),
        )


def test_control_provenance_owns_c0_background_and_quality_state():
    provenance = accepted_control_provenance("control-0", model_boundary())

    assert provenance.arm == "C0"
    assert provenance.program_id is None
    assert provenance.quality_disposition == "ACCEPTED"
    assert provenance.background_only is True
    assert provenance.to_dict()["checksums_sha256"] == hashlib.sha256(
        b"control-0"
    ).hexdigest()

    with pytest.raises(AnalysisError, match="64 lowercase hexadecimal"):
        replace(provenance, checksums_sha256="not-a-seal")


def test_validated_prediction_requires_artifact_verifying_factories():
    model = fitted_model()
    with pytest.raises(AnalysisError, match="factory-verified"):
        model.predict(prediction_request(), validation_status="VALIDATED")

    exploratory = model.predict(prediction_request())
    forged = replace(exploratory, validation_status="VALIDATED")
    with pytest.raises(AnalysisError, match="factory-verified"):
        forged.to_dict()


def test_factories_verify_claim_control_and_target_artifacts(
    tmp_path, monkeypatch
):
    boundary = model_boundary()
    source = model_source()
    acceptance = accepted_causal_ground_truth(boundary).acceptance
    boundary_document = boundary.to_dict()
    source_topology = [source.to_dict()]

    def canonical_sha256(value):
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()

    preregistration_digest = "1" * 64
    calibration_digest = "2" * 64
    host_provenance = {
        "identity_scheme": "OS_UNAME_AND_OPTIONAL_MACHINE_ID_V1",
        "identity_sha256": "3" * 64,
    }
    boundary_digest = canonical_sha256(boundary_document)
    topology_digest = canonical_sha256(source_topology)
    machine_digest = canonical_sha256(
        {
            "node_id": "node",
            "host_provenance": host_provenance,
            "source_topology": source_topology,
        }
    )
    claim_path = tmp_path / "claims.json"
    claim_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "measurement_boundary_ids": [boundary.boundary_id],
                "measurement_boundaries": [boundary_document],
                "claims": {
                    "claim_id": "accepted-ranking-reversal-claim",
                    "study_id": "accepted-ranking-reversal-study",
                    "claim_kind": "CONTEXT_DEPENDENT_RANKING_REVERSAL",
                    "supported": True,
                    "acceptance": acceptance.to_dict(),
                    "preregistration": {
                        "artifact_sha256": preregistration_digest,
                        "preregistration_id": "pre-1",
                    },
                    "measurement_boundary": boundary_document,
                    "measurement_boundary_sha256": boundary_digest,
                    "source_topology": source_topology,
                    "source_topology_sha256": topology_digest,
                    "trial_evidence": [
                        {
                            "arm": arm,
                            "boundary_id": boundary.boundary_id,
                            "boundary_sha256": boundary_digest,
                            "source_topology_sha256": topology_digest,
                            "machine_provenance_sha256": machine_digest,
                            "measurement_overhead_calibration_sha256": (
                                calibration_digest
                            ),
                            "checksums_sha256": hashlib.sha256(
                                arm.encode()
                            ).hexdigest(),
                        }
                        for arm in ("C0", "C1", "C2")
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    claim_wrapper = json.loads(claim_path.read_text(encoding="utf-8"))
    claim_document = claim_wrapper["claims"]
    claim_wrapper["verified_claim_digests"] = [canonical_sha256(claim_document)]
    claim_path.write_text(json.dumps(claim_wrapper), encoding="utf-8")
    with pytest.raises(AnalysisError, match="factory-issued StudyClaimRecord"):
        CausalGroundTruthReference.from_claims_artifact(
            claim_path,
            verified_claim=claim_document,
        )

    class FakeVerifiedClaim:
        def require_verified(self):
            return None

        def to_dict(self):
            return claim_document

    monkeypatch.setattr(
        "pywattch.analysis.artifacts.StudyClaimRecord",
        FakeVerifiedClaim,
    )
    verified_claim = FakeVerifiedClaim()
    ground_truth = CausalGroundTruthReference.from_claims_artifact(
        claim_path,
        verified_claim=verified_claim,
    )
    assert ground_truth.artifact_verified is True

    tampered_path = tmp_path / "tampered-claims.json"
    tampered = json.loads(claim_path.read_text(encoding="utf-8"))
    tampered["claims"]["measurement_boundary"]["backend"] = "tampered"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(
        AnalysisError,
        match="factory-issued claim|wrapper declaration|digest does not match",
    ):
        CausalGroundTruthReference.from_claims_artifact(
            tampered_path,
            verified_claim=verified_claim,
        )

    estimand = CounterfactualEstimand.fixed_horizon(4_000_000_000)
    relative_offsets = tuple(index * 1_000_000_000 for index in range(5))
    evidence_by_trial = {}

    class FakeTrialReference:
        def __init__(self, *, trial_id, arm, checksums_sha256):
            self.trial_id = trial_id
            self.arm = arm
            self.checksums_sha256 = checksums_sha256
            self.boundary_sha256 = boundary_digest
            self.source_topology_sha256 = topology_digest
            self.machine_provenance_sha256 = machine_digest
            self.measurement_overhead_calibration_sha256 = calibration_digest

        def _require_verified(self):
            return None

    class FakeVerifiedTrialEvidenceBundle:
        def __init__(self, reference):
            self.references = (reference,)
            self.boundary_sha256 = boundary_digest
            self.source_topology_sha256 = topology_digest
            self.preregistration_sha256 = preregistration_digest

        def _require_verified(self):
            return None

    monkeypatch.setattr(
        "pywattch.analysis.artifacts.VerifiedTrialEvidenceBundle",
        FakeVerifiedTrialEvidenceBundle,
    )

    def create_trial(
        *,
        trial_id,
        arm_code,
        program_id,
        power_watts,
        pre_treatment_features,
        exogenous_features,
        replay_seed,
        created_unix_ns,
        workload_family,
    ):
        trial_dir = tmp_path / trial_id
        trial_dir.mkdir()
        (trial_dir / "checksums.json").write_text(
            json.dumps({"trial_id": trial_id}),
            encoding="utf-8",
        )
        input_id = f"input-{trial_id}"
        outcome_origin_ns = 50_000_000_000 + replay_seed * 10_000_000_000
        timestamps = tuple(
            outcome_origin_ns + offset for offset in relative_offsets
        )
        model_input = {
            "schema_version": 1,
            "artifact_kind": MODEL_INPUT_ARTIFACT_KIND,
            "trial_id": trial_id,
            "input_id": input_id,
            "time_origin": "OUTCOME_START",
            "alignment_method": "STEP_PREVIOUS_HOLD_LAST",
            "relative_offsets_ns": list(relative_offsets),
            "static_features": {},
            "prelaunch_features": pre_treatment_features,
            "exogenous_features": {
                name: list(values) for name, values in exogenous_features.items()
            },
        }
        model_input_path = tmp_path / f"{trial_id}.model-input.json"
        model_input_path.write_text(
            json.dumps(model_input, sort_keys=True),
            encoding="utf-8",
        )
        input_digest = hashlib.sha256(model_input_path.read_bytes()).hexdigest()
        evidence_by_trial[trial_id] = SimpleNamespace(
            trial={
                "trial_id": trial_id,
                "run_id": f"run-{trial_id}",
                "experiment_id": "artifact-verified-experiment",
                "matched_block_id": f"block-{trial_id}",
                "input_id": input_id,
                "replay_seed": replay_seed,
                "background_workload_id": f"background-{workload_family}",
                "labels": {
                    "arm_code": arm_code,
                    "workload_mode": "SERVICE",
                    "workload_family": workload_family,
                    MODEL_INPUT_ARTIFACT_LABEL: input_digest,
                },
                "arm": {"program_id": program_id},
                "measurement_boundary": boundary.to_dict(),
                "estimand": estimand.to_dict(),
            },
            manifest={
                "created_unix_ns": created_unix_ns,
                "node_id": "node",
            },
            acquisition={
                "result": {
                    "measurement_overhead_calibration_sha256": calibration_digest,
                    "host_provenance": host_provenance,
                }
            },
            descriptors=(source,),
            samples=tuple(
                RawSample(
                    source_id=source.local_source_id,
                    sequence_no=index,
                    monotonic_ns=timestamp,
                    value=float(power) * 1_000.0,
                )
                for index, (timestamp, power) in enumerate(
                    zip(timestamps, power_watts)
                )
            ),
            quality={"disposition": "ACCEPTED"},
            outcome=SimpleNamespace(
                boundary=boundary,
                start_monotonic_ns=timestamps[0],
                end_monotonic_ns=timestamps[-1],
                total_joules=100.0,
            ),
            observation=SimpleNamespace(
                work_complete=True,
                output_valid=True,
                quality_accepted=True,
                slo_met=True,
            ),
        )
        checksums_sha256 = hashlib.sha256(
            (trial_dir / "checksums.json").read_bytes()
        ).hexdigest()
        bundle = FakeVerifiedTrialEvidenceBundle(
            FakeTrialReference(
                trial_id=trial_id,
                arm=arm_code,
                checksums_sha256=checksums_sha256,
            )
        )
        return trial_dir, model_input_path, bundle

    def fake_load_trial_evidence(path):
        return evidence_by_trial[path.name]

    monkeypatch.setattr(
        "pywattch.analysis.replay.load_trial_evidence",
        fake_load_trial_evidence,
    )
    episodes = control_episodes(boundary)
    verified_episodes = []
    exploratory_episodes = []
    first_day_ns = 1_752_796_800_000_000_000
    second_day_ns = first_day_ns + 86_400_000_000_000
    for index, episode in enumerate(episodes):
        trial_dir, model_input_path, trial_bundle = create_trial(
            trial_id=episode.episode_id,
            arm_code="C0",
            program_id=None,
            power_watts=episode.power_watts,
            pre_treatment_features=dict(episode.pre_treatment_features),
            exogenous_features=episode.exogenous_features,
            replay_seed=(10 + index) if index < 4 else (20 + index),
            created_unix_ns=first_day_ns if index < 4 else second_day_ns,
            workload_family="train-family" if index < 4 else "test-family",
        )
        exploratory_episode = BaselineEpisode.from_trial_dir(
            trial_dir,
            model_input_artifact=model_input_path,
        )
        assert exploratory_episode.artifact_verified is False
        exploratory_episodes.append(exploratory_episode)
        verified_episode = BaselineEpisode.from_verified_trial_bundle(
            trial_dir,
            model_input_artifact=model_input_path,
            trial_bundle=trial_bundle,
        )
        assert verified_episode.artifact_verified is True
        assert verified_episode.provenance.artifact_verified is True
        assert verified_episode.input_binding.artifact_verified is True
        verified_episodes.append(verified_episode)

    with pytest.raises(AnalysisError, match="replay-issued"):
        validate_group_split(
            train_episodes=exploratory_episodes[:4],
            test_episodes=exploratory_episodes[4:],
        )

    direct_request = prediction_request()
    target_dir, target_input_path, target_bundle = create_trial(
        trial_id="candidate-run",
        arm_code="C1",
        program_id="P1",
        power_watts=(10.0, 10.5, 11.0, 11.5, 12.0),
        pre_treatment_features=dict(direct_request.pre_treatment_features),
        exogenous_features=direct_request.exogenous_features,
        replay_seed=99,
        created_unix_ns=second_day_ns,
        workload_family="target-family",
    )
    exploratory_request = PredictionRequest.from_trial_dir(
        target_dir,
        model_input_artifact=target_input_path,
    )
    assert exploratory_request.artifact_verified is False
    request = PredictionRequest.from_verified_trial_bundle(
        target_dir,
        model_input_artifact=target_input_path,
        trial_bundle=target_bundle,
    )
    assert request.artifact_verified is True
    assert request.target_trial_provenance.artifact_verified is True
    assert request.input_binding.artifact_verified is True
    assert request.input_binding.feature_alignment == MODEL_INPUT_ALIGNMENT
    assert request.timestamps_ns[0] > 0

    split_validation = validate_group_split(
        train_episodes=verified_episodes[:4],
        test_episodes=verified_episodes[4:],
    )

    model = DynamicLinearBaseline(
        pre_treatment_feature_names=["pre_load"],
        exogenous_feature_names=["offered_demand"],
    ).fit(
        verified_episodes,
        causal_ground_truth=ground_truth,
        group_split_validation=split_validation,
    )
    prediction = model.predict(request, validation_status="VALIDATED")
    assert prediction.artifact_chain_verified is True
    document = prediction.to_dict()
    assert document["result"]["validation_status"] == "VALIDATED"
    split = document["prediction_path"]["training_provenance"][
        "group_split_validation"
    ]
    assert split["artifact_verified"] is True
    assert [item["episode_id"] for item in split["train_episodes"]] == [
        f"control-{index}" for index in range(4)
    ]
    assert [item["episode_id"] for item in split["test_episodes"]] == [
        "control-4",
        "control-5",
    ]
    training_inputs = document["prediction_path"]["training_provenance"][
        "control_model_inputs"
    ]
    assert len(training_inputs) == 4
    assert all(item["role"] == "CONTROL_EPISODE" for item in training_inputs)
    assert document["prediction_path"]["training_provenance"][
        "control_episode_ids"
    ] == [f"control-{index}" for index in range(4)]
    held_out = document["prediction_path"]["training_provenance"][
        "held_out_model_validation"
    ]
    assert held_out["artifact_verified"] is True
    assert held_out["split_artifact_sha256"] == split["split_artifact_sha256"]
    assert held_out["test_episode_ids"] == ["control-4", "control-5"]
    assert held_out["test_rows"] == 10
    assert held_out["root_mean_squared_error_watts"] >= 0
    assert document["prediction_path"]["target_model_input"]["role"] == (
        "TARGET_REQUEST"
    )
    published = write_predictions_jsonl(
        tmp_path / "validated-predictions.jsonl",
        [prediction],
    )
    assert json.loads(published.read_text(encoding="utf-8"))["result"][
        "validation_status"
    ] == "VALIDATED"

    with pytest.raises(AnalysisError, match="VALIDATED prediction requires"):
        model.predict(exploratory_request, validation_status="VALIDATED")

    for seal_field in (
        "measurement_overhead_calibration_sha256",
        "source_topology_sha256",
        "machine_provenance_sha256",
    ):
        original_seal = verified_episodes[0].provenance.trial_bundle_seal
        assert original_seal is not None
        bad_seal_values = {
            "preregistration_sha256": original_seal.preregistration_sha256,
            "measurement_boundary_sha256": (
                original_seal.measurement_boundary_sha256
            ),
            "source_topology_sha256": original_seal.source_topology_sha256,
            "machine_provenance_sha256": (
                original_seal.machine_provenance_sha256
            ),
            "measurement_overhead_calibration_sha256": (
                original_seal.measurement_overhead_calibration_sha256
            ),
        }
        bad_seal_values[seal_field] = "f" * 64
        bad_seal = TrialBundleSeal(**bad_seal_values)
        object.__setattr__(bad_seal, "_artifact_verified", True)
        bad_provenance = replace(
            verified_episodes[0].provenance,
            trial_bundle_seal=bad_seal,
        )
        object.__setattr__(bad_provenance, "_artifact_verified", True)
        bad_episode = replace(
            verified_episodes[0],
            provenance=bad_provenance,
        )
        object.__setattr__(bad_episode, "_artifact_verified", True)
        object.__setattr__(
            bad_episode,
            "_group_key",
            verified_episodes[0].group_key,
        )
        with pytest.raises(AnalysisError, match="not accepted by causal ground truth"):
            DynamicLinearBaseline(
                pre_treatment_feature_names=["pre_load"],
                exogenous_feature_names=["offered_demand"],
            ).fit(
                [bad_episode, *verified_episodes[1:]],
                causal_ground_truth=ground_truth,
                group_split_validation=split_validation,
            )

    target_seal = request.target_trial_provenance.trial_bundle_seal
    assert target_seal is not None
    bad_target_seal = TrialBundleSeal(
        preregistration_sha256=target_seal.preregistration_sha256,
        measurement_boundary_sha256=target_seal.measurement_boundary_sha256,
        source_topology_sha256=target_seal.source_topology_sha256,
        machine_provenance_sha256="e" * 64,
        measurement_overhead_calibration_sha256=(
            target_seal.measurement_overhead_calibration_sha256
        ),
    )
    object.__setattr__(bad_target_seal, "_artifact_verified", True)
    bad_target_provenance = replace(
        request.target_trial_provenance,
        trial_bundle_seal=bad_target_seal,
    )
    object.__setattr__(bad_target_provenance, "_artifact_verified", True)
    bad_request = replace(
        request,
        target_trial_provenance=bad_target_provenance,
    )
    object.__setattr__(bad_request, "_artifact_verified", True)
    object.__setattr__(
        bad_request,
        "_observed_power_watts",
        request._observed_power_watts,
    )
    with pytest.raises(AnalysisError, match="not accepted by causal ground truth"):
        model.predict(bad_request, validation_status="VALIDATED")

    altered_episode = replace(
        verified_episodes[0],
        power_watts=(999.0, *verified_episodes[0].power_watts[1:]),
    )
    with pytest.raises(AnalysisError, match="replay-bound split entry"):
        DynamicLinearBaseline(
            pre_treatment_feature_names=["pre_load"],
            exogenous_feature_names=["offered_demand"],
        ).fit(
            [altered_episode, *verified_episodes[1:]],
            causal_ground_truth=ground_truth,
            group_split_validation=split_validation,
        )

    with pytest.raises(AnalysisError, match="exactly match"):
        DynamicLinearBaseline(
            pre_treatment_feature_names=["pre_load"],
            exogenous_feature_names=["offered_demand"],
        ).fit(
            verified_episodes[:-1],
            causal_ground_truth=ground_truth,
            group_split_validation=split_validation,
        )

    with pytest.raises(AnalysisError, match="duplicated|both split partitions"):
        validate_group_split(
            train_episodes=[verified_episodes[0], verified_episodes[0]],
            test_episodes=verified_episodes[4:],
        )
    with pytest.raises(AnalysisError, match="duplicated|both split partitions"):
        validate_group_split(
            train_episodes=verified_episodes[:4],
            test_episodes=[verified_episodes[3], verified_episodes[4]],
        )

    altered_request = replace(
        request,
        exogenous_features={
            "offered_demand": (999.0,) * len(relative_offsets)
        },
    )
    object.__setattr__(altered_request, "_artifact_verified", True)
    object.__setattr__(
        altered_request,
        "_observed_power_watts",
        request._observed_power_watts,
    )
    with pytest.raises(AnalysisError, match="model inputs differ"):
        model.predict(altered_request, validation_status="VALIDATED")

    tampered_input_path = tmp_path / "tampered-model-input.json"
    tampered_input = json.loads(
        (tmp_path / "control-0.model-input.json").read_text(encoding="utf-8")
    )
    tampered_input["prelaunch_features"]["pre_load"] = 999.0
    tampered_input_path.write_text(json.dumps(tampered_input), encoding="utf-8")
    with pytest.raises(AnalysisError, match="not precommitted"):
        BaselineEpisode.from_trial_dir(
            tmp_path / "control-0",
            model_input_artifact=tampered_input_path,
        )

    tampered_input["relative_offsets_ns"][0] = 1
    mismatched_offset_path = tmp_path / "mismatched-offsets.json"
    mismatched_offset_path.write_text(
        json.dumps(tampered_input),
        encoding="utf-8",
    )
    evidence_by_trial["control-0"].trial["labels"][
        MODEL_INPUT_ARTIFACT_LABEL
    ] = hashlib.sha256(mismatched_offset_path.read_bytes()).hexdigest()
    with pytest.raises(AnalysisError, match="relative offsets"):
        BaselineEpisode.from_trial_dir(
            tmp_path / "control-0",
            model_input_artifact=mismatched_offset_path,
        )


def test_prediction_rejects_observed_energy_not_bound_to_target_trial():
    with pytest.raises(AnalysisError, match="does not match accepted target"):
        fitted_model().predict(prediction_request(), observed_joules=99.0)


def test_dynamic_baseline_rejects_a_non_c0_control_contract():
    with pytest.raises(AnalysisError, match="restricted to the C0"):
        DynamicLinearBaseline(
            pre_treatment_feature_names=["pre_load"],
            exogenous_feature_names=["offered_demand"],
            control_arm="sham",
        )


def test_model_rejects_post_treatment_mediator_features():
    with pytest.raises(AnalysisError, match="mediator"):
        DynamicLinearBaseline(
            pre_treatment_feature_names=["initial_backlog"],
            exogenous_feature_names=["cpu_frequency_hz"],
        )
    # Initial backlog is a valid pre-treatment condition; treatment-period
    # backlog would be rejected in the exogenous feature list.
    DynamicLinearBaseline(
        pre_treatment_feature_names=["initial_backlog"],
        exogenous_feature_names=["offered_demand"],
    )


def test_in_distribution_prediction_has_uncertainty_and_model_label():
    model = fitted_model()
    request = prediction_request()
    prediction_without_observed = model.predict(request)
    assert prediction_without_observed.abstained is False
    assert prediction_without_observed.counterfactual_joules is not None
    observed = prediction_without_observed.counterfactual_joules + 7.5

    request = replace(
        request,
        target_trial_provenance=replace(
            request.target_trial_provenance,
            observed_joules=observed,
        ),
    )
    prediction = model.predict(request, observed_joules=observed)
    assert prediction.evidence_class == MODEL_EVIDENCE_CLASS
    assert prediction.model_id.startswith("dynamic-linear-v1:")
    assert prediction.validation_status == "EXPLORATORY"
    assert prediction.training_episode_ids == tuple(
        f"control-{index}" for index in range(6)
    )
    assert len(prediction.predicted_power_watts) == 5
    assert prediction.counterfactual_lower_joules <= prediction.counterfactual_joules
    assert prediction.counterfactual_upper_joules >= prediction.counterfactual_joules
    assert prediction.effect_joules == pytest.approx(7.5)
    assert prediction.effect_lower_joules <= prediction.effect_joules
    assert prediction.effect_upper_joules >= prediction.effect_joules
    serialized = prediction.to_dict()
    assert serialized["evidence_class"] == "MODEL_DERIVED_ESTIMATE"
    assert serialized["trial_id"] == "candidate-run"
    assert serialized["estimand"] == {
        "kind": "FIXED_HORIZON",
        "horizon_ns": 4_000_000_000,
    }
    assert serialized["model"]["training_data_id"].startswith("sha256:")
    assert serialized["result"]["kind"] == "ESTIMATED"
    assert serialized["result"]["impact_joules"] == pytest.approx(7.5)
    required = serialized["measurement_boundary"]["required_sources"]
    assert required[0]["evidence_class"] == "TOOL_DERIVED_ESTIMATE"
    assert len(serialized["prediction_path"]["predicted_power_watts"]) == 5
    assert serialized["prediction_path"]["integration_method"] == "TRAPEZOIDAL"
    integrated = serialized["prediction_path"]["integrated_counterfactual"]
    assert integrated["joules"] == pytest.approx(
        serialized["result"]["counterfactual_joules"]
    )
    training = serialized["prediction_path"]["training_provenance"]
    ground_truth = training["causal_ground_truth"]
    assert ground_truth["study_id"] == "accepted-ranking-reversal-study"
    assert ground_truth["acceptance"]["supported"] is True
    assert ground_truth["measurement_boundary_id"] == "powermetrics-cpu-power"
    controls = training["accepted_control_trials"]
    assert len(controls) == 6
    assert all(control["arm"] == "C0" for control in controls)
    assert all(control["quality_disposition"] == "ACCEPTED" for control in controls)
    assert all(control["background_only"] is True for control in controls)
    target = serialized["prediction_path"]["accepted_target_trial"]
    assert target["trial_id"] == "candidate-run"
    assert target["observed_joules"] == pytest.approx(observed)


def test_numeric_prediction_uses_observed_energy_bound_to_target_provenance():
    prediction = fitted_model().predict(prediction_request())
    document = prediction.to_dict()
    assert document["result"]["observed_joules"] == 100.0
    assert (
        document["prediction_path"]["accepted_target_trial"]["observed_joules"]
        == 100.0
    )


def test_model_prediction_passes_offline_publication_validation(tmp_path):
    prediction = fitted_model().predict(prediction_request())
    path = write_predictions_jsonl(
        tmp_path / "predictions.jsonl",
        [prediction],
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["result"]["validation_status"] == "EXPLORATORY"
    assert record["prediction_path"]["integration_method"] == "TRAPEZOIDAL"


def test_ood_prediction_abstains_instead_of_forcing_an_effect():
    model = fitted_model()
    request = prediction_request(exogenous_features={"offered_demand": (100.0,) * 5})
    prediction = model.predict(request, observed_joules=100.0)

    assert prediction.abstained is True
    assert prediction.out_of_distribution is True
    assert prediction.validation_status == "ABSTAINED_OUT_OF_DISTRIBUTION"
    assert "outside the training range" in prediction.abstention_reason
    assert prediction.counterfactual_joules is None
    assert prediction.effect_joules is None
    serialized = prediction.to_dict()
    assert serialized["evidence_class"] == MODEL_EVIDENCE_CLASS
    assert serialized["result"] == {
        "kind": "ABSTAINED_OUT_OF_DISTRIBUTION",
        "reason": prediction.abstention_reason,
    }
    assert "impact_joules" not in serialized["result"]


def test_prediction_rejects_cross_backend_boundary():
    model = fitted_model()
    request = prediction_request(boundary=model_boundary("rapl"))
    with pytest.raises(BoundaryMismatchError):
        model.predict(request)


def test_second_host_requires_semantic_rebind_and_fresh_target_recalibration():
    source_descriptor = SourceDescriptor(
        local_source_id=1,
        source_uid="powermetrics://node/cpu/power",
        node_id="node",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="cpu/power",
        clock_domain="monotonic",
        sampling_mode="POLLED",
    )
    target_descriptor = SourceDescriptor(
        local_source_id=7,
        source_uid="powermetrics://node-b/cpu/power",
        node_id="node-b",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="cpu/power",
        clock_domain="monotonic",
        sampling_mode="POLLED",
    )
    source_boundary = model_boundary()
    target_boundary = MeasurementBoundary.from_descriptors(
        [target_descriptor],
        [target_descriptor.source_uid],
        boundary_id="powermetrics-node-b-cpu-power",
    )
    rebind = validate_semantic_rebind(
        source_boundary=source_boundary,
        target_boundary=target_boundary,
        source_descriptors=[source_descriptor],
        target_descriptors=[target_descriptor],
        source_to_target_uids={
            source_descriptor.source_uid: target_descriptor.source_uid
        },
    )

    source_model = fitted_model()
    target_model = source_model.recalibrate_for_target(
        rebind,
        control_episodes(target_boundary, prefix="target-control"),
        causal_ground_truth=accepted_causal_ground_truth(target_boundary),
    )
    prediction = target_model.predict(
        prediction_request(boundary=target_boundary), observed_joules=100.0
    )
    assert target_model.boundary.boundary_id == "powermetrics-node-b-cpu-power"
    assert prediction.training_episode_ids[0].startswith("target-control-")
    provenance = prediction.to_dict()["prediction_path"]["training_provenance"][
        "semantic_rebind"
    ]
    assert provenance["source_boundary_id"] == "powermetrics-cpu-power"
    assert provenance["target_boundary_id"] == "powermetrics-node-b-cpu-power"
    assert provenance["raw_joule_pooling_permitted"] is False

    changed_path = SourceDescriptor(
        **{
            **target_descriptor.to_dict(),
            "source_path": "different/cpu/power",
        }
    )
    with pytest.raises(BoundaryMismatchError, match="source_path"):
        validate_semantic_rebind(
            source_boundary=source_boundary,
            target_boundary=target_boundary,
            source_descriptors=[source_descriptor],
            target_descriptors=[changed_path],
            source_to_target_uids={
                source_descriptor.source_uid: target_descriptor.source_uid
            },
        )


def test_group_split_rejects_unsealed_or_caller_supplied_row_metadata():
    train = EpisodeGroupKey("run-train", 1, "2026-07-18", "service", "host-a")
    test = EpisodeGroupKey("run-test", 2, "2026-07-19", "batch", "host-a")

    with pytest.raises(TypeError, match="unexpected keyword"):
        validate_group_split(  # type: ignore[call-arg]
            train_row_groups=[train],
            test_row_groups=[test],
        )

    episodes = control_episodes()
    with pytest.raises(AnalysisError, match="replay-issued"):
        validate_group_split(
            train_episodes=episodes[:4],
            test_episodes=episodes[4:],
        )
