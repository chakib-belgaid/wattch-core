import hashlib
from dataclasses import replace

import pytest

from pywattch import SourceDescriptor
from pywattch.analysis import (
    AnalysisError,
    BoundaryMismatchError,
    EnergyOutcome,
    Estimand,
    MeasurementBoundary,
    TrialObservation,
    assess_mechanism_attenuation,
    calibrate_aa_threshold,
    check_placebo_zero,
    compare_cross_boundary_replications,
    conservative_required_matched_blocks,
    detect_ranking_reversal,
    evaluate_study_acceptance,
    matched_difference_in_differences,
    paired_effect,
    paired_normal_sample_size,
)


def boundary(backend: str = "rapl") -> MeasurementBoundary:
    source = SourceDescriptor(
        local_source_id=1,
        source_uid=f"{backend}://node/package/0",
        node_id="node",
        backend=backend,
        kind="CPU_PACKAGE_ENERGY",
        unit="JOULE",
        temporality="CUMULATIVE",
        evidence_class="RAW_HARDWARE_COUNTER",
        source_path="cpu/package/0/energy",
        clock_domain="monotonic",
        sampling_mode="POLLED",
    )
    return MeasurementBoundary.from_descriptors(
        [source], [source.source_uid], boundary_id=f"{backend}-cpu-package"
    )


def outcome(
    joules: float,
    *,
    measurement_boundary: MeasurementBoundary | None = None,
    estimand: Estimand = Estimand.FIXED_HORIZON,
) -> EnergyOutcome:
    selected = measurement_boundary or boundary()
    uid = selected.primary_source_uids[0]
    return EnergyOutcome(
        boundary=selected,
        estimand=estimand,
        start_monotonic_ns=0,
        end_monotonic_ns=1_000_000_000,
        per_source_joules={uid: joules},
        integration_methods={uid: "COUNTER_DELTA"},
    )


def observations_for_differences(
    differences: list[float],
    *,
    context: str,
    estimand: Estimand = Estimand.FIXED_HORIZON,
    measurement_boundary: MeasurementBoundary | None = None,
    policy: str = "DYNAMIC",
    block_prefix: str = "block",
    target_nice: int = 0,
) -> list[TrialObservation]:
    result = []
    selected = measurement_boundary or boundary()
    for index, difference in enumerate(differences):
        result.extend(
            [
                TrialObservation(
                    trial_id=f"{context}-{index}-p1",
                    block_id=f"{block_prefix}-{index}",
                    arm="P1",
                    policy=policy,
                    target_nice=target_nice,
                    match_fingerprint=hashlib.sha256(
                        f"{block_prefix}-{index}".encode()
                    ).hexdigest(),
                    context=context,
                    outcome=outcome(
                        100.0 + difference,
                        measurement_boundary=selected,
                        estimand=estimand,
                    ),
                    work_complete=True,
                    output_valid=True,
                    slo_met=True,
                    quality_accepted=True,
                    completed_work=1_000 if estimand == Estimand.EQUAL_WORK else None,
                ),
                TrialObservation(
                    trial_id=f"{context}-{index}-p2",
                    block_id=f"{block_prefix}-{index}",
                    arm="P2",
                    policy=policy,
                    target_nice=target_nice,
                    match_fingerprint=hashlib.sha256(
                        f"{block_prefix}-{index}".encode()
                    ).hexdigest(),
                    context=context,
                    outcome=outcome(
                        100.0,
                        measurement_boundary=selected,
                        estimand=estimand,
                    ),
                    work_complete=True,
                    output_valid=True,
                    slo_met=True,
                    quality_accepted=True,
                    completed_work=1_000 if estimand == Estimand.EQUAL_WORK else None,
                ),
            ]
        )
    return result


def test_paired_effect_uses_within_block_differences():
    estimate = paired_effect(
        observations_for_differences([8.0, 10.0, 9.0, 11.0], context="idle"),
        treatment_arm="P1",
        control_arm="P2",
    )

    assert estimate.paired_differences_joules == (8.0, 10.0, 9.0, 11.0)
    assert estimate.mean_difference_joules == pytest.approx(9.5)
    assert estimate.ci_lower_joules is not None
    assert estimate.ci_lower_joules > 0
    assert estimate.claim_eligible is True


def test_paired_effect_rejects_missing_arm_and_cross_boundary_joules():
    incomplete = observations_for_differences([1.0], context="idle")[:-1]
    with pytest.raises(AnalysisError, match="missing paired arm"):
        paired_effect(incomplete, treatment_arm="P1", control_arm="P2")

    selected = observations_for_differences([1.0], context="idle")
    selected[1] = TrialObservation(
        trial_id="different-backend",
        block_id="block-0",
        arm="P2",
        policy=selected[0].policy,
        target_nice=selected[0].target_nice,
        match_fingerprint=selected[0].match_fingerprint,
        context="idle",
        outcome=outcome(100.0, measurement_boundary=boundary("powermetrics")),
        work_complete=True,
        output_valid=True,
        slo_met=True,
        quality_accepted=True,
    )
    with pytest.raises(BoundaryMismatchError):
        paired_effect(selected, treatment_arm="P1", control_arm="P2")


def test_aa_threshold_uses_larger_relative_or_empirical_floor():
    relative_wins = calibrate_aa_threshold(
        aa_paired_differences_joules=[1.0, -2.0],
        control_joules=[100.0, 100.0],
    )
    assert relative_wins.relative_floor_joules == pytest.approx(5.0)
    assert relative_wins.meaningful_effect_joules == pytest.approx(5.0)

    noise_wins = calibrate_aa_threshold(
        aa_paired_differences_joules=[1.0, -4.0],
        control_joules=[20.0, 20.0],
    )
    assert noise_wins.meaningful_effect_joules > 7.0


def test_detects_preregistered_context_dependent_ranking_reversal():
    isolated = paired_effect(
        observations_for_differences([9.0, 10.0, 11.0, 10.0], context="idle"),
        treatment_arm="P1",
        control_arm="P2",
    )
    cotenanted = paired_effect(
        observations_for_differences(
            [-8.0, -9.0, -7.0, -8.0], context="service-background"
        ),
        treatment_arm="P1",
        control_arm="P2",
    )

    result = detect_ranking_reversal(
        isolated,
        cotenanted,
        isolated_threshold_joules=5.0,
        cotenanted_threshold_joules=5.0,
    )
    assert result.supported is True
    assert result.reasons == ()


def test_unfinished_or_unequal_work_prevents_an_improvement_claim():
    observations = observations_for_differences(
        [-10.0, -11.0], context="batch", estimand=Estimand.EQUAL_WORK
    )
    first = observations[0]
    observations[0] = TrialObservation(
        trial_id=first.trial_id,
        block_id=first.block_id,
        arm=first.arm,
        policy=first.policy,
        target_nice=first.target_nice,
        match_fingerprint=first.match_fingerprint,
        context=first.context,
        outcome=first.outcome,
        work_complete=False,
        output_valid=True,
        slo_met=True,
        quality_accepted=True,
        completed_work=900,
    )
    estimate = paired_effect(
        observations,
        treatment_arm="P1",
        control_arm="P2",
    )
    assert estimate.claim_eligible is False
    assert "unfinished work" in estimate.ineligibility_reasons
    assert "equal-work arms completed different work" in estimate.ineligibility_reasons


def mechanism_observations() -> list[TrialObservation]:
    selected = boundary()
    result = []
    baseline_differences = [-10.0, -9.0, -11.0, -10.0]
    fixed_differences = [-2.0, -1.0, -3.0, -2.0]
    for index, (baseline, fixed) in enumerate(
        zip(baseline_differences, fixed_differences)
    ):
        for policy, difference in (("DYNAMIC", baseline), ("FIXED", fixed)):
            result.extend(
                [
                    TrialObservation(
                        trial_id=f"{index}-{policy}-p1",
                        block_id=f"mechanism-{index}",
                        arm="P1",
                        policy=policy,
                        target_nice=10 if index % 2 else 0,
                        match_fingerprint=hashlib.sha256(
                            f"mechanism-{index}".encode()
                        ).hexdigest(),
                        context="service-background",
                        outcome=outcome(
                            100.0 + difference, measurement_boundary=selected
                        ),
                        work_complete=True,
                        output_valid=True,
                        slo_met=True,
                        quality_accepted=True,
                    ),
                    TrialObservation(
                        trial_id=f"{index}-{policy}-p2",
                        block_id=f"mechanism-{index}",
                        arm="P2",
                        policy=policy,
                        target_nice=10 if index % 2 else 0,
                        match_fingerprint=hashlib.sha256(
                            f"mechanism-{index}".encode()
                        ).hexdigest(),
                        context="service-background",
                        outcome=outcome(100.0, measurement_boundary=selected),
                        work_complete=True,
                        output_valid=True,
                        slo_met=True,
                        quality_accepted=True,
                    ),
                ]
            )
    return result


def test_matched_difference_in_differences_supports_mechanism_attenuation():
    estimate = matched_difference_in_differences(
        mechanism_observations(),
        treatment_arm="P1",
        control_arm="P2",
        baseline_policy="DYNAMIC",
        intervention_policy="FIXED",
    )
    assert estimate.baseline_mean_difference_joules == pytest.approx(-10.0)
    assert estimate.intervention_mean_difference_joules == pytest.approx(-2.0)
    assert estimate.mean_did_joules == pytest.approx(8.0)
    assert estimate.ci_lower_joules > 0
    assert estimate.claim_eligible is True

    decision = assess_mechanism_attenuation(estimate, minimum_attenuation_joules=5.0)
    assert decision.supported is True


def test_mechanism_did_requires_compatible_boundary_and_complete_work():
    observations = mechanism_observations()
    item = observations[0]
    observations[0] = TrialObservation(
        trial_id=item.trial_id,
        block_id=item.block_id,
        arm=item.arm,
        policy=item.policy,
        target_nice=item.target_nice,
        match_fingerprint=item.match_fingerprint,
        context=item.context,
        outcome=outcome(90.0, measurement_boundary=boundary("powermetrics")),
        work_complete=True,
        output_valid=True,
        slo_met=True,
        quality_accepted=True,
    )
    with pytest.raises(BoundaryMismatchError):
        matched_difference_in_differences(
            observations,
            treatment_arm="P1",
            control_arm="P2",
            baseline_policy="DYNAMIC",
            intervention_policy="FIXED",
        )

    observations = mechanism_observations()
    item = observations[0]
    observations[0] = TrialObservation(
        trial_id=item.trial_id,
        block_id=item.block_id,
        arm=item.arm,
        policy=item.policy,
        target_nice=item.target_nice,
        match_fingerprint=item.match_fingerprint,
        context=item.context,
        outcome=item.outcome,
        work_complete=True,
        output_valid=True,
        slo_met=False,
        quality_accepted=True,
    )
    estimate = matched_difference_in_differences(
        observations,
        treatment_arm="P1",
        control_arm="P2",
        baseline_policy="DYNAMIC",
        intervention_policy="FIXED",
    )
    assert estimate.claim_eligible is False
    assert "service-level objective violation" in estimate.ineligibility_reasons


def test_missing_work_output_and_slo_evidence_is_never_claim_eligible():
    observations = [
        TrialObservation(
            trial_id="p1",
            block_id="block-1",
            arm="P1",
            context="idle",
            outcome=outcome(101.0),
        ),
        TrialObservation(
            trial_id="p2",
            block_id="block-1",
            arm="P2",
            context="idle",
            outcome=outcome(100.0),
        ),
    ]
    estimate = paired_effect(observations, treatment_arm="P1", control_arm="P2")
    assert estimate.claim_eligible is False
    assert set(estimate.ineligibility_reasons) == {
        "output equivalence was not established",
        "service-level objective status was not established",
        "trace quality was not evaluated",
        "target priority was not established",
        "matched execution contract was not established",
        "work completion was not established",
    }


def test_placebo_zero_and_normal_approximation_sample_size():
    placebo = check_placebo_zero(
        [-1.0, 1.0, -0.5, 0.5], meaningful_threshold_joules=2.0
    )
    assert placebo.centered_on_zero is True

    threshold = calibrate_aa_threshold(
        aa_paired_differences_joules=[-1.0, 1.0],
        control_joules=[100.0, 100.0],
    )
    calculation = paired_normal_sample_size(
        [-10.0, 10.0] * 6, aa_threshold=threshold
    )
    assert calculation.power == pytest.approx(0.90)
    assert calculation.two_sided_alpha == pytest.approx(0.05)
    assert calculation.meaningful_effect_joules == pytest.approx(5.0)
    expected = conservative_required_matched_blocks(
        paired_sd_joules=calculation.pilot_paired_sd_joules,
        meaningful_effect_joules=5.0,
        power=0.90,
        two_sided_alpha=0.05,
    )
    assert calculation.required_matched_blocks == expected

    wide_placebo = check_placebo_zero(
        [-1000.0, 1000.0] * 6,
        meaningful_threshold_joules=5.0,
    )
    assert wide_placebo.centered_on_zero is False
    assert "not contained" in " ".join(wide_placebo.reasons)
    with pytest.raises(AnalysisError, match="two-sided 95%"):
        check_placebo_zero(
            [-1.0, 1.0] * 6,
            meaningful_threshold_joules=5.0,
            confidence=0.90,
        )


def test_sample_size_enforces_confirmatory_preregistration_defaults():
    threshold = calibrate_aa_threshold(
        aa_paired_differences_joules=[-1.0, 1.0],
        control_joules=[100.0, 100.0],
    )
    with pytest.raises(AnalysisError, match="at least 12"):
        paired_normal_sample_size([0.0] * 11, aa_threshold=threshold)
    with pytest.raises(AnalysisError, match="at least 0.90"):
        paired_normal_sample_size(
            [0.0] * 12,
            aa_threshold=threshold,
            power=0.80,
        )
    with pytest.raises(AnalysisError, match="at most 0.05"):
        paired_normal_sample_size(
            [0.0] * 12,
            aa_threshold=threshold,
            two_sided_alpha=0.10,
        )


def reversal_for_estimand(estimand: Estimand, *, block_prefix: str = "block"):
    isolated = paired_effect(
        observations_for_differences(
            [9.0, 10.0, 11.0, 10.0],
            context="idle",
            estimand=estimand,
            block_prefix=f"{block_prefix}-isolated",
        ),
        treatment_arm="P1",
        control_arm="P2",
    )
    cotenanted = paired_effect(
        observations_for_differences(
            [-8.0, -9.0, -7.0, -8.0],
            context="service-background",
            estimand=estimand,
            block_prefix=f"{block_prefix}-cotenanted",
        ),
        treatment_arm="P1",
        control_arm="P2",
    )
    return detect_ranking_reversal(
        isolated,
        cotenanted,
        isolated_threshold_joules=5.0,
        cotenanted_threshold_joules=5.0,
    )


def test_cross_backend_replication_reports_no_pooled_raw_joules():
    rapl = paired_effect(
        observations_for_differences([8.0, 10.0, 9.0, 11.0], context="service"),
        treatment_arm="P1",
        control_arm="P2",
    )
    powermetrics = paired_effect(
        observations_for_differences(
            [4.0, 5.0, 4.5, 5.5],
            context="service",
            measurement_boundary=boundary("powermetrics"),
        ),
        treatment_arm="P1",
        control_arm="P2",
    )
    with pytest.raises(BoundaryMismatchError):
        rapl.boundary.assert_compatible(powermetrics.boundary)

    comparison = compare_cross_boundary_replications(
        [rapl, powermetrics], standardized_heterogeneity_tolerance=0.01
    )
    assert comparison.sign_agreement is True
    assert comparison.standardized_effect_agreement is True
    document = comparison.to_dict()
    assert document["raw_joule_pooling_permitted"] is False
    assert not any("joules" in key for key in document)

    with pytest.raises(AnalysisError, match="same analysis policy"):
        compare_cross_boundary_replications(
            [rapl, replace(powermetrics, policy="FIXED_NON_TURBO")]
        )
    with pytest.raises(AnalysisError, match="95% confidence"):
        compare_cross_boundary_replications(
            [rapl, replace(powermetrics, confidence=0.90)]
        )
    ineligible = replace(
        powermetrics,
        claim_eligible=False,
        ineligibility_reasons=("adversarial quality failure",),
    )
    failed = compare_cross_boundary_replications([rapl, ineligible])
    assert failed.all_claim_eligible is False
    assert failed.sign_agreement is False
    assert failed.standardized_effect_agreement is False


def test_top_level_acceptance_requires_both_estimands_placebo_days_and_mechanism():
    fixed = reversal_for_estimand(Estimand.FIXED_HORIZON)
    equal = reversal_for_estimand(Estimand.EQUAL_WORK)
    placebo = check_placebo_zero(
        [-1.0, 1.0, -0.5, 0.5], meaningful_threshold_joules=2.0
    )
    mechanism = assess_mechanism_attenuation(
        matched_difference_in_differences(
            mechanism_observations(),
            treatment_arm="P1",
            control_arm="P2",
            baseline_policy="DYNAMIC",
            intervention_policy="FIXED",
        ),
        minimum_attenuation_joules=5.0,
    )
    accepted = evaluate_study_acceptance(
        fixed_horizon_reversal=fixed,
        equal_work_reversal=equal,
        placebo_checks=[placebo],
        independent_day_reversals={
            "day-1": [
                reversal_for_estimand(
                    Estimand.FIXED_HORIZON, block_prefix="day-1-fixed"
                ),
                reversal_for_estimand(
                    Estimand.EQUAL_WORK, block_prefix="day-1-equal"
                ),
            ],
            "day-2": [
                reversal_for_estimand(
                    Estimand.FIXED_HORIZON, block_prefix="day-2-fixed"
                ),
                reversal_for_estimand(
                    Estimand.EQUAL_WORK, block_prefix="day-2-equal"
                ),
            ],
        },
        mechanism_attenuation=mechanism,
        require_mechanism=True,
    )
    assert accepted.supported is True

    falsified = evaluate_study_acceptance(
        fixed_horizon_reversal=fixed,
        equal_work_reversal=equal,
        placebo_checks=[placebo],
        independent_day_reversals={"day-1": [fixed, equal]},
        require_mechanism=True,
    )
    assert falsified.supported is False
    assert "fewer than two independent" in " ".join(falsified.reasons)
    assert "mechanism attenuation is required" in " ".join(falsified.reasons)
