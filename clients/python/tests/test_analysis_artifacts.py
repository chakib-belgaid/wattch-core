import hashlib
import json
import math
from dataclasses import replace

import pytest

from pywattch import SourceDescriptor
from pywattch.analysis.artifacts import (
    AnalysisArtifactError,
    PrimaryRegimePreregistration,
    StudyClaimRecord,
    StudyPreregistration,
    TrialEvidenceReference,
    seal_study_preregistration,
    write_analysis_artifacts,
    write_claims_json,
    write_predictions_jsonl,
    write_report_markdown,
    write_verified_prestudy_evidence,
)
from pywattch.analysis.boundary import MeasurementBoundary
from pywattch.analysis.effects import (
    NormalApproximationSampleSize,
    conservative_required_matched_blocks,
)


def boundary(backend: str = "rapl") -> MeasurementBoundary:
    source = SourceDescriptor(
        local_source_id=1,
        source_uid=f"{backend}://node-a/package/0",
        node_id="node-a",
        backend=backend,
        kind="CPU_PACKAGE_ENERGY",
        unit="MICROJOULE",
        temporality="CUMULATIVE",
        evidence_class="RAW_HARDWARE_COUNTER",
        source_path="package/0",
        clock_domain="monotonic",
        sampling_mode="POLLED",
    )
    return MeasurementBoundary.from_descriptors(
        [source], [source.source_uid], boundary_id=f"{backend}-package"
    )


def prediction(selected: MeasurementBoundary) -> dict[str, object]:
    return {
        "estimate_id": "run-1:model-1",
        "trial_id": "run-1",
        "measurement_boundary": selected.to_dict(),
        "estimand": {"kind": "FIXED_HORIZON", "horizon_ns": 1_000_000_000},
        "evidence_class": "MODEL_DERIVED_ESTIMATE",
        "model": {
            "model_id": "dynamic-linear-v1:abc",
            "model_version": "dynamic-linear-v1",
            "training_data_id": "sha256:controls",
        },
        "result": {
            "kind": "ESTIMATED",
            "observed_joules": 13.0,
            "counterfactual_joules": 12.5,
            "impact_joules": 0.5,
            "interval": {
                "confidence_level": 0.95,
                "lower_joules": 0.1,
                "upper_joules": 0.9,
            },
            "validation_status": "EXPLORATORY",
        },
        "labels": {},
        "prediction_path": {
            "timestamps_ns": [0, 1_000_000_000],
            "integration_method": "TRAPEZOIDAL",
            "predicted_power_watts": [12.0, 13.0],
            "lower_power_watts": [11.0, 12.0],
            "upper_power_watts": [13.0, 14.0],
            "integrated_counterfactual": {
                "joules": 12.5,
                "interval": {
                    "confidence_level": 0.95,
                    "lower_joules": 11.5,
                    "upper_joules": 13.5,
                },
            },
            "training_provenance": {
                "control_episode_ids": ["control-1"],
                "causal_ground_truth": {
                    "study_id": "study-1",
                    "claim_id": "claim-1",
                    "measurement_boundary_id": selected.boundary_id,
                    "claims_artifact_sha256": "a" * 64,
                    "acceptance": {
                        "supported": True,
                        "fixed_horizon_supported": True,
                        "equal_work_supported": True,
                        "placebo_supported": True,
                        "independent_day_replication_supported": True,
                        "mechanism_required": False,
                        "mechanism_supported": None,
                        "reasons": [],
                    },
                    "artifact_verified": False,
                },
                "accepted_control_trials": [
                    {
                        "trial_id": "control-1",
                        "experiment_id": "training",
                        "matched_block_id": "control-block-1",
                        "measurement_boundary_id": selected.boundary_id,
                        "checksums_sha256": "b" * 64,
                        "arm": "C0",
                        "program_id": None,
                        "quality_disposition": "ACCEPTED",
                        "background_only": True,
                        "artifact_verified": False,
                    }
                ],
                "group_split_validation": None,
                "semantic_rebind": None,
            },
            "accepted_target_trial": {
                "trial_id": "run-1",
                "experiment_id": "experiment-1",
                "matched_block_id": "block-1",
                "measurement_boundary_id": selected.boundary_id,
                "checksums_sha256": "c" * 64,
                "arm": "C1",
                "program_id": "P1",
                "quality_disposition": "ACCEPTED",
                "observed_joules": 13.0,
                "estimand": {
                    "kind": "FIXED_HORIZON",
                    "horizon_ns": 1_000_000_000,
                },
                "artifact_verified": False,
            },
            "artifact_chain_verified": False,
        },
    }


def regime_preregistration(
    estimand: str,
    context: str,
    confirmatory_block_ids: tuple[str, ...],
) -> PrimaryRegimePreregistration:
    regime_id = f"{estimand}:{context}"
    pilot_ids = tuple(
        f"discarded-pilot:{regime_id}:{index}" for index in range(12)
    )
    return PrimaryRegimePreregistration(
        regime_id=regime_id,
        estimand=estimand,
        context=context,
        pilot_block_ids=pilot_ids,
        pilot_discarded=True,
        sample_size=NormalApproximationSampleSize(
            pilot_blocks=len(pilot_ids),
            pilot_paired_sd_joules=1.0,
            meaningful_effect_joules=10.0,
            power=0.90,
            two_sided_alpha=0.05,
            required_matched_blocks=12,
        ),
        confirmatory_block_ids=confirmatory_block_ids,
    )


def test_prediction_writer_requires_canonical_model_derived_schema(tmp_path):
    selected = boundary()
    path = tmp_path / "predictions.jsonl"

    invalid = prediction(selected)
    invalid["evidence_class"] = "TOOL_DERIVED_ESTIMATE"
    with pytest.raises(AnalysisArtifactError, match="MODEL_DERIVED_ESTIMATE"):
        write_predictions_jsonl(path, [invalid])
    assert not path.exists()

    invalid = prediction(selected)
    invalid.pop("model")
    with pytest.raises(AnalysisArtifactError, match="model identity"):
        write_predictions_jsonl(path, [invalid])
    assert not path.exists()

    invalid = prediction(selected)
    invalid["result"]["impact_joules"] = 0.0
    with pytest.raises(AnalysisArtifactError, match="observed minus counterfactual"):
        write_predictions_jsonl(path, [invalid])
    assert not path.exists()

    invalid = prediction(selected)
    invalid["prediction_path"]["predicted_power_watts"] = [12.2, 13.0]
    with pytest.raises(AnalysisArtifactError, match="does not match its power path"):
        write_predictions_jsonl(path, [invalid])
    assert not path.exists()

    invalid = prediction(selected)
    invalid["result"]["validation_status"] = "VALIDATED"
    with pytest.raises(AnalysisArtifactError, match="raw mappings cannot publish"):
        write_predictions_jsonl(path, [invalid])
    assert not path.exists()

    write_predictions_jsonl(path, [prediction(selected)])
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["measurement_boundary"]["boundary_id"] == "rapl-package"
    assert record["result"]["kind"] == "ESTIMATED"
    assert record["model"]["training_data_id"] == "sha256:controls"


def test_prediction_writer_preserves_explicit_ood_abstention(tmp_path):
    selected = boundary()
    abstention = prediction(selected)
    abstention["result"] = {
        "kind": "ABSTAINED_OUT_OF_DISTRIBUTION",
        "reason": "pre-launch state is outside training support",
    }
    abstention["prediction_path"]["predicted_power_watts"] = []
    abstention["prediction_path"]["lower_power_watts"] = []
    abstention["prediction_path"]["upper_power_watts"] = []
    abstention["prediction_path"]["integrated_counterfactual"] = None

    path = write_predictions_jsonl(tmp_path / "predictions.jsonl", [abstention])
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["evidence_class"] == "MODEL_DERIVED_ESTIMATE"
    assert record["result"]["kind"] == "ABSTAINED_OUT_OF_DISTRIBUTION"
    assert "impact_joules" not in record["result"]

    invalid = prediction(selected)
    invalid["result"] = {
        "kind": "ABSTAINED_OUT_OF_DISTRIBUTION",
        "reason": "outside training support",
        "impact_joules": 0.0,
    }
    with pytest.raises(AnalysisArtifactError, match="cannot contain numeric"):
        write_predictions_jsonl(tmp_path / "invalid.jsonl", [invalid])


def test_prediction_writer_is_exclusive_and_preserves_existing_file(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_text("existing evidence\n", encoding="utf-8")

    with pytest.raises(AnalysisArtifactError, match="refusing to overwrite"):
        write_predictions_jsonl(path, [prediction(boundary())])

    assert path.read_text(encoding="utf-8") == "existing evidence\n"
    assert not list(tmp_path.glob(".predictions.jsonl.*.tmp"))


def test_claims_and_report_preserve_boundary_ids(tmp_path):
    selected = boundary()
    claims_path = tmp_path / "claims.json"
    report_path = tmp_path / "report.md"

    write_claims_json(
        claims_path,
        {"ranking_reversal_supported": False},
        boundaries=[selected],
    )
    write_report_markdown(
        report_path,
        "# Result\n\nNo confirmatory claim was made.",
        boundaries=[selected],
    )

    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    assert claims["measurement_boundary_ids"] == ["rapl-package"]
    assert claims["measurement_boundaries"][0] == selected.to_dict()
    report = report_path.read_text(encoding="utf-8")
    assert report.startswith(
        '<!-- wattch-measurement-boundary-ids: ["rapl-package"] -->'
    )


def test_positive_claim_requires_typed_acceptance_and_checksum_evidence(tmp_path):
    selected = boundary()
    with pytest.raises(AnalysisArtifactError, match="typed StudyClaimRecord"):
        write_claims_json(
            tmp_path / "unbound.json",
            {"ranking_reversal_supported": True},
            boundaries=[selected],
        )
    with pytest.raises(AnalysisArtifactError, match="typed StudyClaimRecord"):
        write_claims_json(
            tmp_path / "split-field-positive.json",
            {
                "claim_kind": "CONTEXT_DEPENDENT_RANKING_REVERSAL",
                "verdict": "SUPPORTED",
            },
            boundaries=[selected],
        )
    with pytest.raises(AnalysisArtifactError, match="issued"):
        TrialEvidenceReference()
    with pytest.raises(AnalysisArtifactError, match="from_verified_results"):
        StudyClaimRecord()
    with pytest.raises(AnalysisArtifactError, match="must bind"):
        write_report_markdown(
            tmp_path / "unbound-report.md",
            "# Result\n\nThe ranking reversal is supported.",
            boundaries=[selected],
        )
    negative_report = write_report_markdown(
        tmp_path / "negative-report.md",
        "# Result\n\nThe ranking reversal is not supported.",
        boundaries=[selected],
    )
    assert "not supported" in negative_report.read_text(encoding="utf-8")


def test_preregistration_enforces_pilot_power_and_evidence_gates():
    confirmatory_ids = tuple(f"confirmatory-{index}" for index in range(12))
    valid_regime = regime_preregistration(
        "FIXED_HORIZON", "idle", confirmatory_ids
    )

    with pytest.raises(AnalysisArtifactError, match="must be discarded"):
        replace(valid_regime, pilot_discarded=False)
    with pytest.raises(AnalysisArtifactError, match="at least 12"):
        replace(
            valid_regime,
            pilot_block_ids=valid_regime.pilot_block_ids[:11],
        )
    with pytest.raises(AnalysisArtifactError, match="at least 0.90"):
        replace(
            valid_regime,
            sample_size=replace(valid_regime.sample_size, power=0.89),
        )
    with pytest.raises(AnalysisArtifactError, match="exactly two-sided 95%"):
        replace(
            valid_regime,
            sample_size=replace(
                valid_regime.sample_size, two_sided_alpha=0.051
            ),
        )
    with pytest.raises(AnalysisArtifactError, match="exactly two-sided 95%"):
        replace(
            valid_regime,
            sample_size=replace(
                valid_regime.sample_size, two_sided_alpha=0.01
            ),
        )
    with pytest.raises(AnalysisArtifactError, match="does not match its inputs"):
        replace(
            valid_regime,
            sample_size=replace(
                valid_regime.sample_size,
                required_matched_blocks=3,
            ),
        )
    with pytest.raises(AnalysisArtifactError, match="must be disjoint"):
        replace(
            valid_regime,
            confirmatory_block_ids=(
                valid_regime.pilot_block_ids[0],
                *valid_regime.confirmatory_block_ids[1:],
            ),
        )
    high_variance_required = conservative_required_matched_blocks(
        paired_sd_joules=20.0,
        meaningful_effect_joules=10.0,
        power=0.90,
        two_sided_alpha=0.05,
    )
    with pytest.raises(
        AnalysisArtifactError, match=f"at least {high_variance_required}"
    ):
        replace(
            valid_regime,
            sample_size=replace(
                valid_regime.sample_size,
                pilot_paired_sd_joules=20.0,
                required_matched_blocks=high_variance_required,
            ),
        )

    valid = StudyPreregistration(
        preregistration_id="prereg-v1",
        primary_regimes=(valid_regime,),
    )

    second_regime = regime_preregistration(
        "EQUAL_WORK",
        "service-background",
        tuple(f"confirmatory-service-{index}" for index in range(12)),
    )
    with pytest.raises(AnalysisArtifactError, match="pilot block ids.*unique"):
        replace(
            valid,
            primary_regimes=(
                valid_regime,
                replace(
                    second_regime,
                    pilot_block_ids=valid_regime.pilot_block_ids,
                ),
            ),
        )
    with pytest.raises(
        AnalysisArtifactError, match="confirmatory block ids.*unique"
    ):
        replace(
            valid,
            primary_regimes=(
                valid_regime,
                replace(
                    second_regime,
                    confirmatory_block_ids=valid_regime.confirmatory_block_ids,
                ),
            ),
        )
    cross_type_pilot_ids = (
        valid_regime.confirmatory_block_ids[0],
        *second_regime.pilot_block_ids[1:],
    )
    with pytest.raises(AnalysisArtifactError, match="disjoint across the study"):
        replace(
            valid,
            primary_regimes=(
                valid_regime,
                replace(second_regime, pilot_block_ids=cross_type_pilot_ids),
            ),
        )


def test_verified_prestudy_rejects_tunable_confirmatory_statistics(tmp_path):
    with pytest.raises(AnalysisArtifactError, match=r"exactly max\(5%"):
        write_verified_prestudy_evidence(
            tmp_path / "aa.json",
            role="AA_CALIBRATION",
            regime_id="FIXED_HORIZON:idle",
            trial_directories=(),
            relative_floor_fraction=0.01,
        )
    with pytest.raises(AnalysisArtifactError, match="two-sided 95%"):
        write_verified_prestudy_evidence(
            tmp_path / "placebo.json",
            role="PLACEBO",
            regime_id="FIXED_HORIZON:idle",
            trial_directories=(),
            confidence=0.90,
        )


def test_sealed_preregistration_rejects_self_authored_numeric_evidence(tmp_path):
    selected = boundary()
    block_ids = tuple(f"confirmatory-{index}" for index in range(12))
    regime = regime_preregistration("FIXED_HORIZON", "idle", block_ids)
    plan = StudyPreregistration("prereg-v1", (regime,))

    schedule_trials = []
    for block_id in block_ids:
        for arm in ("C0", "C1", "C2"):
            schedule_trials.append(
                {
                    "trial_metadata": {
                        "trial_id": f"{block_id}-{arm}",
                        "matched_block_id": block_id,
                        "background_workload_id": "idle",
                        "estimand": {"kind": "FIXED_HORIZON"},
                        "labels": {"arm_code": arm},
                    },
                    "mechanism_policy": "NORMAL_DYNAMIC",
                }
            )
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "experiment": {"confirmatory": True},
                "trials": schedule_trials,
            }
        )
    )
    pilot_scale = math.sqrt(11 / 12)
    pilot_path = tmp_path / "pilot.json"
    pilot_path.write_text(
        json.dumps(
            {
                "kind": "DISCARDED_PILOT",
                "regime_id": regime.regime_id,
                "block_ids": list(regime.pilot_block_ids),
                "paired_differences_joules": [
                    *([-pilot_scale] * 6),
                    *([pilot_scale] * 6),
                ],
            }
        )
    )
    aa_path = tmp_path / "aa.json"
    aa_path.write_text(
        json.dumps(
            {
                "kind": "AA_CALIBRATION",
                "regime_id": regime.regime_id,
                "paired_differences_joules": [0.1, -0.1],
                "control_joules": [200.0, 200.0],
            }
        )
    )
    placebo_path = tmp_path / "placebo.json"
    placebo_path.write_text(
        json.dumps(
            {
                "kind": "PLACEBO",
                "regime_id": regime.regime_id,
                "paired_differences_joules": [0.1, -0.1],
                "meaningful_threshold_joules": 10.0,
                "confidence": 0.95,
            }
        )
    )
    boundary_document = selected.to_dict()
    boundary_digest = hashlib.sha256(
        json.dumps(
            boundary_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    calibration_path = tmp_path / "calibration.json"
    calibration_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "METER_CALIBRATION",
                "calibration_id": "meter-v1",
                "measurement_boundary": boundary_document,
                "measurement_boundary_sha256": boundary_digest,
                "selected_interval_ns": 1_000_000,
                "required_regimes": ["IDLE", "FIXED_LOAD", "AA"],
                "selected_metrics": {
                    "measurement_overhead_ratio": 0.005,
                    "missing_sample_ratio": 0.0005,
                    "phase_sensitivity_ratio": 0.01,
                },
                "thresholds": {
                    "max_measurement_overhead_ratio": 0.01,
                    "max_missing_sample_ratio": 0.001,
                    "max_phase_sensitivity_ratio": 0.02,
                },
            }
        )
    )
    with pytest.raises(AnalysisArtifactError, match="factory-issued"):
        seal_study_preregistration(
            tmp_path / "preregistration.json",
            plan,
            schedule_paths=[schedule_path],
            pilot_evidence_paths={regime.regime_id: pilot_path},
            aa_evidence_paths={regime.regime_id: aa_path},
            placebo_evidence_paths={regime.regime_id: placebo_path},
            calibration_evidence_paths=[calibration_path],
            sealed_at_unix_ns=1,
        )


@pytest.mark.parametrize(
    "claims,report",
    [
        ({"conclusion": "lower whole-machine energy"}, "# Safe report"),
        ({"conclusion": "domain-specific"}, "# Whole-system energy result"),
    ],
)
def test_domain_meter_outputs_reject_whole_machine_claims(tmp_path, claims, report):
    selected = boundary("powermetrics")
    if "whole" in json.dumps(claims).lower():
        with pytest.raises(AnalysisArtifactError, match="whole-machine energy"):
            write_claims_json(
                tmp_path / "claims.json", claims, boundaries=[selected]
            )
    else:
        with pytest.raises(AnalysisArtifactError, match="whole-machine energy"):
            write_report_markdown(
                tmp_path / "report.md", report, boundaries=[selected]
            )


def test_bundle_writes_real_parquet_and_all_separate_outputs(tmp_path):
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    selected = boundary()
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()

    paths = write_analysis_artifacts(
        analysis_dir,
        features=[
            {
                "run_id": "run-1",
                "measurement_boundary_id": selected.boundary_id,
                "pre_temperature_c": 42.0,
            }
        ],
        predictions=[prediction(selected)],
        claims={"ranking_reversal_supported": False},
        report_markdown="# Result\n\nBoundary-specific result.",
        boundaries=[selected],
    )

    assert paths.features.read_bytes()[:4] == b"PAR1"
    table = parquet.read_table(paths.features)
    assert isinstance(table, pyarrow.Table)
    assert table.to_pylist()[0]["measurement_boundary_id"] == "rapl-package"
    assert paths.predictions.is_file()
    assert paths.claims.is_file()
    assert paths.report.is_file()

    with pytest.raises(AnalysisArtifactError, match="refusing to overwrite"):
        write_analysis_artifacts(
            analysis_dir,
            features=[],
            predictions=[prediction(selected)],
            claims={},
            report_markdown="# Re-run",
            boundaries=[selected],
        )
