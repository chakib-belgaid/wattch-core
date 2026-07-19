use wattch_core::{
    validate_counterfactual_impact_estimate, CounterfactualEstimateResult,
    CounterfactualImpactEstimate, EvidenceClass, SamplingMode, SourceDescriptor, SourceKind,
    Temporality, Unit,
};

fn source() -> SourceDescriptor {
    SourceDescriptor {
        local_source_id: 1,
        source_uid: "powermetrics://node/cpu/power".to_string(),
        node_id: "node".to_string(),
        backend: "powermetrics".to_string(),
        kind: SourceKind::CpuPower,
        unit: Unit::Milliwatt,
        temporality: Temporality::Instant,
        evidence_class: EvidenceClass::ToolDerivedEstimate,
        source_path: "cpu/power".to_string(),
        parent_source_uid: None,
        clock_domain: "monotonic".to_string(),
        sampling_mode: SamplingMode::Polled,
        nominal_period_ns: Some(100_000_000),
        max_value: None,
        resolution: None,
        labels: Default::default(),
    }
}

fn python_envelope(result: serde_json::Value) -> serde_json::Value {
    let estimand = serde_json::json!({
        "kind": "FIXED_HORIZON",
        "horizon_ns": 4_000_000_000_u64
    });
    let training_provenance = serde_json::json!({
        "control_episode_ids": ["control-1"],
        "causal_ground_truth": {
            "study_id": "study-1",
            "claim_id": "claim-1",
            "measurement_boundary_id": "powermetrics-cpu-power",
            "claims_artifact_sha256": "a".repeat(64),
            "acceptance": {
                "supported": true,
                "fixed_horizon_supported": true,
                "equal_work_supported": true,
                "placebo_supported": true,
                "independent_day_replication_supported": true,
                "mechanism_required": false,
                "mechanism_supported": null,
                "reasons": []
            },
            "preregistration_sha256": "1".repeat(64),
            "measurement_boundary_sha256": "2".repeat(64),
            "source_topology_sha256": "3".repeat(64),
            "accepted_calibration_sha256": ["4".repeat(64)],
            "accepted_machine_provenance_sha256": ["5".repeat(64)],
            "artifact_verified": true
        },
        "accepted_control_trials": [{
            "trial_id": "control-1",
            "experiment_id": "training-experiment",
            "matched_block_id": "training-block-1",
            "measurement_boundary_id": "powermetrics-cpu-power",
            "checksums_sha256": "b".repeat(64),
            "arm": "C0",
            "program_id": null,
            "quality_disposition": "ACCEPTED",
            "background_only": true,
            "trial_bundle_seal": {
                "preregistration_sha256": "1".repeat(64),
                "measurement_boundary_sha256": "2".repeat(64),
                "source_topology_sha256": "3".repeat(64),
                "machine_provenance_sha256": "5".repeat(64),
                "measurement_overhead_calibration_sha256": "4".repeat(64),
                "artifact_verified": true
            },
            "artifact_verified": true
        }],
        "control_model_inputs": [{
            "role": "CONTROL_EPISODE",
            "trial_id": "control-1",
            "trial_checksums_sha256": "b".repeat(64),
            "feature_artifact_sha256": "d".repeat(64),
            "payload_sha256": "e".repeat(64),
            "power_derivation": "RAW_INSTANT_CLIPPED",
            "feature_alignment": "OUTCOME_START_STEP_PREVIOUS_V1",
            "artifact_verified": true
        }],
        "semantic_rebind": null,
        "group_split_validation": {
            "train_rows": 120,
            "test_rows": 40,
            "train_groups": 1,
            "test_groups": 1,
            "strategy": "per_host",
            "disjoint_axes": ["run_id", "replay_seed", "day", "workload_family"],
            "train_episodes": [{
                "episode_id": "control-1",
                "trial_checksums_sha256": "b".repeat(64),
                "model_input_payload_sha256": "e".repeat(64),
                "row_count": 120,
                "group_key": {
                    "run_id": "run-train",
                    "replay_seed": 1,
                    "day": "2026-07-18",
                    "workload_family": "service",
                    "machine": "8".repeat(64)
                }
            }],
            "test_episodes": [{
                "episode_id": "control-holdout",
                "trial_checksums_sha256": "9".repeat(64),
                "model_input_payload_sha256": "7".repeat(64),
                "row_count": 40,
                "group_key": {
                    "run_id": "run-test",
                    "replay_seed": 2,
                    "day": "2026-07-19",
                    "workload_family": "batch",
                    "machine": "8".repeat(64)
                }
            }],
            "split_artifact_sha256": "1".repeat(64),
            "artifact_verified": true
        },
        "held_out_model_validation": {
            "split_artifact_sha256": "1".repeat(64),
            "training_data_id": "sha256:controls",
            "fitted_parameters_sha256": "2".repeat(64),
            "observed_paths_sha256": "3".repeat(64),
            "predicted_paths_sha256": "4".repeat(64),
            "test_episode_ids": ["control-holdout"],
            "test_rows": 40,
            "root_mean_squared_error_watts": 0.5,
            "mean_absolute_error_watts": 0.4,
            "mean_error_watts": -0.1,
            "out_of_distribution_rows": 0,
            "artifact_sha256": "6".repeat(64),
            "artifact_verified": true
        }
    });
    let accepted_target_trial = serde_json::json!({
        "trial_id": "candidate-run",
        "experiment_id": "confirmatory-experiment",
        "matched_block_id": "confirmatory-block-1",
        "measurement_boundary_id": "powermetrics-cpu-power",
        "checksums_sha256": "c".repeat(64),
        "arm": "C1",
        "program_id": "P1",
        "quality_disposition": "ACCEPTED",
        "observed_joules": 44.0,
        "estimand": estimand.clone(),
        "trial_bundle_seal": {
            "preregistration_sha256": "1".repeat(64),
            "measurement_boundary_sha256": "2".repeat(64),
            "source_topology_sha256": "3".repeat(64),
            "machine_provenance_sha256": "5".repeat(64),
            "measurement_overhead_calibration_sha256": "4".repeat(64),
            "artifact_verified": true
        },
        "artifact_verified": true
    });
    let target_model_input = serde_json::json!({
        "role": "TARGET_REQUEST",
        "trial_id": "candidate-run",
        "trial_checksums_sha256": "c".repeat(64),
        "feature_artifact_sha256": "f".repeat(64),
        "payload_sha256": "0".repeat(64),
        "power_derivation": "RAW_INSTANT_CLIPPED",
        "feature_alignment": "OUTCOME_START_STEP_PREVIOUS_V1",
        "artifact_verified": true
    });
    let prediction_path = if result["kind"] == "ABSTAINED_OUT_OF_DISTRIBUTION" {
        serde_json::json!({
            "timestamps_ns": [
                0,
                1_000_000_000_u64,
                2_000_000_000_u64,
                3_000_000_000_u64,
                4_000_000_000_u64
            ],
            "predicted_power_watts": [],
            "lower_power_watts": [],
            "upper_power_watts": [],
            "integration_method": "TRAPEZOIDAL",
            "integrated_counterfactual": null,
            "training_provenance": training_provenance,
            "accepted_target_trial": accepted_target_trial,
            "target_model_input": target_model_input
        })
    } else {
        serde_json::json!({
            "timestamps_ns": [
                0,
                1_000_000_000_u64,
                2_000_000_000_u64,
                3_000_000_000_u64,
                4_000_000_000_u64
            ],
            "predicted_power_watts": [10.0, 10.0, 10.0, 10.0, 10.0],
            "lower_power_watts": [9.5, 9.5, 9.5, 9.5, 9.5],
            "upper_power_watts": [10.5, 10.5, 10.5, 10.5, 10.5],
            "integration_method": "TRAPEZOIDAL",
            "integrated_counterfactual": {
                "joules": 40.0,
                "interval": {
                    "confidence_level": 0.95,
                    "lower_joules": 38.0,
                    "upper_joules": 42.0
                }
            },
            "training_provenance": training_provenance,
            "accepted_target_trial": accepted_target_trial,
            "target_model_input": target_model_input
        })
    };
    serde_json::json!({
        "estimate_id": "candidate-run:dynamic-linear-v1:abc",
        "trial_id": "candidate-run",
        "measurement_boundary": {
            "boundary_id": "powermetrics-cpu-power",
            "backend": "powermetrics",
            "primary_source_uids": ["powermetrics://node/cpu/power"],
            "aggregation_rule": "SINGLE_SOURCE",
            "required_sources": [{
                "source_uid": "powermetrics://node/cpu/power",
                "kind": "CPU_POWER",
                "unit": "MILLIWATT",
                "temporality": "INSTANT",
                "evidence_class": "TOOL_DERIVED_ESTIMATE",
                "parent_source_uid": null,
                "clock_domain": "monotonic",
                "disjoint_group": null,
                "disjoint_member": null
            }]
        },
        "estimand": estimand,
        "evidence_class": "MODEL_DERIVED_ESTIMATE",
        "model": {
            "model_id": "dynamic-linear-v1:abc",
            "model_version": "dynamic-linear-v1",
            "training_data_id": "sha256:controls"
        },
        "result": result,
        "labels": {"prediction_path": "included"},
        "prediction_path": prediction_path
    })
}

#[test]
fn python_numeric_prediction_uses_the_canonical_rust_envelope() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "EXPLORATORY"
    }));

    let estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap();
}

#[test]
fn tampered_impact_is_rejected_even_when_it_remains_inside_the_interval() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 5.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "EXPLORATORY"
    }));

    let estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error
        .to_string()
        .contains("impact_joules must equal observed_joules - counterfactual_joules"));
}

#[test]
fn tampered_prediction_path_integral_is_rejected() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "EXPLORATORY"
    }));
    let mut estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    estimate.prediction_path.predicted_power_watts[2] = 10.25;

    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error
        .to_string()
        .contains("trapezoidal prediction-path integral"));
}

#[test]
fn validated_status_requires_every_referenced_artifact_to_be_verified() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "VALIDATED"
    }));
    let mut estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap();

    let verified_split = estimate
        .prediction_path
        .training_provenance
        .group_split_validation
        .clone()
        .unwrap();
    let verified_held_out = estimate
        .prediction_path
        .training_provenance
        .held_out_model_validation
        .clone()
        .unwrap();
    estimate
        .prediction_path
        .training_provenance
        .group_split_validation = None;
    estimate
        .prediction_path
        .training_provenance
        .held_out_model_validation = None;
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());
    estimate
        .prediction_path
        .training_provenance
        .group_split_validation = Some(verified_split);
    estimate
        .prediction_path
        .training_provenance
        .held_out_model_validation = Some(verified_held_out);
    estimate
        .prediction_path
        .training_provenance
        .group_split_validation
        .as_mut()
        .unwrap()
        .artifact_verified = false;
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());
    estimate
        .prediction_path
        .training_provenance
        .group_split_validation
        .as_mut()
        .unwrap()
        .artifact_verified = true;

    estimate
        .prediction_path
        .accepted_target_trial
        .artifact_verified = false;
    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error.to_string().contains(
        "VALIDATED estimates require verified causal, control, model-input, group-split, and target artifacts"
    ));

    estimate
        .prediction_path
        .accepted_target_trial
        .artifact_verified = true;
    estimate
        .prediction_path
        .training_provenance
        .accepted_control_trials[0]
        .artifact_verified = false;
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());

    estimate
        .prediction_path
        .training_provenance
        .accepted_control_trials[0]
        .artifact_verified = true;
    estimate
        .prediction_path
        .training_provenance
        .causal_ground_truth
        .artifact_verified = false;
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());
}

#[test]
fn validated_status_rejects_missing_or_rebound_model_inputs() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "VALIDATED"
    }));
    let mut estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();

    estimate
        .prediction_path
        .training_provenance
        .control_model_inputs
        .clear();
    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error.to_string().contains("model-input"));

    let mut document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "VALIDATED"
    }));
    document["prediction_path"]["target_model_input"]["trial_checksums_sha256"] =
        serde_json::Value::String("9".repeat(64));
    let estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error
        .to_string()
        .contains("target model input must match the accepted target trial"));
}

#[test]
fn validated_status_rejects_uncalibrated_or_rebound_trial_seals() {
    let result = serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "VALIDATED"
    });
    let mut estimate: CounterfactualImpactEstimate =
        serde_json::from_value(python_envelope(result.clone())).unwrap();
    estimate
        .prediction_path
        .training_provenance
        .accepted_control_trials[0]
        .trial_bundle_seal = None;
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());

    let mut estimate: CounterfactualImpactEstimate =
        serde_json::from_value(python_envelope(result.clone())).unwrap();
    estimate
        .prediction_path
        .accepted_target_trial
        .trial_bundle_seal = None;
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());

    let mut estimate: CounterfactualImpactEstimate =
        serde_json::from_value(python_envelope(result)).unwrap();
    estimate
        .prediction_path
        .accepted_target_trial
        .trial_bundle_seal
        .as_mut()
        .unwrap()
        .measurement_overhead_calibration_sha256 = "f".repeat(64);
    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error
        .to_string()
        .contains("not accepted by causal ground truth"));
}

#[test]
fn exploratory_prediction_allows_no_group_split_but_rejects_malformed_split() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "EXPLORATORY"
    }));
    let mut estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    estimate
        .prediction_path
        .training_provenance
        .group_split_validation = None;
    estimate
        .prediction_path
        .training_provenance
        .held_out_model_validation = None;
    estimate
        .prediction_path
        .training_provenance
        .control_model_inputs
        .clear();
    estimate.prediction_path.target_model_input = None;
    validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap();

    let malformed = wattch_core::GroupSplitValidationProvenance {
        train_rows: 0,
        test_rows: 40,
        train_groups: 12,
        test_groups: 4,
        strategy: wattch_core::GroupSplitStrategy::CrossMachineTransfer,
        disjoint_axes: vec!["machine".to_string()],
        train_episodes: Vec::new(),
        test_episodes: Vec::new(),
        split_artifact_sha256: String::new(),
        artifact_verified: false,
    };
    estimate
        .prediction_path
        .training_provenance
        .group_split_validation = Some(malformed);
    let error = validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap_err();
    assert!(error
        .to_string()
        .contains("group-split row and group counts must be positive"));
}

#[test]
fn prediction_path_and_provenance_are_required_schema_fields() {
    let mut document = python_envelope(serde_json::json!({
        "kind": "ABSTAINED_OUT_OF_DISTRIBUTION",
        "reason": "pre-launch state is outside training support"
    }));
    document.as_object_mut().unwrap().remove("prediction_path");

    assert!(serde_json::from_value::<CounterfactualImpactEstimate>(document).is_err());
}

#[test]
fn non_finite_and_negative_energy_values_are_rejected() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "EXPLORATORY"
    }));
    let mut estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();

    if let CounterfactualEstimateResult::Estimated {
        observed_joules, ..
    } = &mut estimate.result
    {
        *observed_joules = f64::NAN;
    }
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());

    if let CounterfactualEstimateResult::Estimated {
        observed_joules,
        counterfactual_joules,
        ..
    } = &mut estimate.result
    {
        *observed_joules = 44.0;
        *counterfactual_joules = -1.0;
    }
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());
}

#[test]
fn malformed_uncertainty_intervals_are_rejected() {
    let document = python_envelope(serde_json::json!({
        "kind": "ESTIMATED",
        "observed_joules": 44.0,
        "counterfactual_joules": 40.0,
        "impact_joules": 4.0,
        "interval": {
            "confidence_level": 0.95,
            "lower_joules": 2.0,
            "upper_joules": 6.0
        },
        "validation_status": "EXPLORATORY"
    }));
    let mut estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();

    if let CounterfactualEstimateResult::Estimated { interval, .. } = &mut estimate.result {
        interval.confidence_level = 1.0;
    }
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());

    if let CounterfactualEstimateResult::Estimated { interval, .. } = &mut estimate.result {
        interval.confidence_level = 0.95;
        interval.lower_joules = 7.0;
        interval.upper_joules = 6.0;
    }
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());

    if let CounterfactualEstimateResult::Estimated { interval, .. } = &mut estimate.result {
        interval.lower_joules = 5.0;
        interval.upper_joules = 6.0;
    }
    assert!(validate_counterfactual_impact_estimate(&estimate, &[source()]).is_err());
}

#[test]
fn python_ood_prediction_deserializes_as_an_explicit_abstention() {
    let document = python_envelope(serde_json::json!({
        "kind": "ABSTAINED_OUT_OF_DISTRIBUTION",
        "reason": "pre-launch state is outside training support"
    }));

    let estimate: CounterfactualImpactEstimate = serde_json::from_value(document).unwrap();
    validate_counterfactual_impact_estimate(&estimate, &[source()]).unwrap();
    assert!(matches!(
        estimate.result,
        CounterfactualEstimateResult::AbstainedOutOfDistribution { .. }
    ));
    assert!(estimate.prediction_path.predicted_power_watts.is_empty());
    assert!(
        estimate
            .prediction_path
            .training_provenance
            .causal_ground_truth
            .artifact_verified
    );
    assert_eq!(
        estimate
            .prediction_path
            .training_provenance
            .accepted_control_trials
            .len(),
        1
    );
}
