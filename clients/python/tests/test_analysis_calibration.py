import hashlib
import json
import math
from dataclasses import replace

import pytest

from pywattch import RawSample, SourceDescriptor
from pywattch.analysis.boundary import (
    AggregationRule,
    AnalysisError,
    MeasurementBoundary,
)
from pywattch.analysis.calibration import (
    SamplingIntervalObservation,
    SamplingSelectionStatus,
    ShamTrialObservation,
    build_trial_quality_metadata,
    build_meter_calibration_document,
    calculate_measurement_overhead,
    compare_sham_trials,
    compute_smallest_meaningful_effect,
    select_fastest_sampling_interval,
    summarize_source_quality,
    write_meter_calibration_artifact,
)


def descriptor() -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=7,
        source_uid="rapl://node-a/package/0",
        node_id="node-a",
        backend="rapl",
        kind="CPU_PACKAGE_ENERGY",
        unit="MICROJOULE",
        temporality="CUMULATIVE",
        evidence_class="RAW_HARDWARE_COUNTER",
        source_path="cpu/package/0/energy",
        clock_domain="daemon-monotonic",
        sampling_mode="POLLED",
        nominal_period_ns=100,
        max_value=100.0,
        resolution=0.5,
    )


def sample(sequence_no: int, monotonic_ns: int, value: float) -> RawSample:
    return RawSample(
        source_id=7,
        sequence_no=sequence_no,
        monotonic_ns=monotonic_ns,
        value=value,
    )


def test_source_summary_separates_gaps_jitter_resolution_and_wraps():
    summary = summarize_source_quality(
        [descriptor()],
        [
            sample(0, 0, 98.0),
            sample(1, 100, 99.0),
            sample(3, 305, 1.0),
            sample(4, 410, 2.5),
        ],
    )[0]

    assert summary.observed_samples == 4
    assert summary.declared_resolution == 0.5
    assert summary.minimum_observed_step == 1.0
    assert summary.counter_wraps == 1
    assert summary.sequence_missing_samples == 1
    assert summary.timing_missing_samples == 1
    assert summary.estimated_missing_samples == 1
    assert summary.missing_sample_ratio == pytest.approx(0.2)
    assert summary.mean_interval_ns == pytest.approx(410 / 3)
    assert summary.median_interval_ns == 105.0
    assert summary.effective_cadence_hz == pytest.approx(3e9 / 410)
    assert summary.max_timestamp_jitter_ns == 5.0
    assert summary.timestamp_jitter_rms_ns == pytest.approx(math.sqrt(50 / 3))
    assert summary.to_quality_dict() == {
        "source_uid": "rapl://node-a/package/0",
        "observed_samples": 4,
        "sequence_gaps": 1,
        "missing_sample_ratio": pytest.approx(0.2),
        "max_timestamp_jitter_ns": 5,
    }


def test_source_summary_preserves_indeterminate_missingness_without_a_window():
    summary = summarize_source_quality([descriptor()], [sample(8, 900, 12.0)])[0]

    assert summary.observed_samples == 1
    assert summary.missing_sample_ratio is None
    assert summary.effective_cadence_hz is None

    with pytest.raises(AnalysisError, match="unknown source id"):
        summarize_source_quality(
            [descriptor()],
            [RawSample(source_id=99, sequence_no=0, monotonic_ns=0, value=1.0)],
        )


def test_explicit_daemon_window_detects_trailing_source_disappearance():
    summary = summarize_source_quality(
        [descriptor()],
        [sample(0, 0, 1.0), sample(1, 100, 2.0), sample(2, 200, 3.0)],
        capture_start_monotonic_ns=0,
        capture_end_monotonic_ns=500,
    )[0]

    assert summary.timing_missing_samples == 2
    assert summary.estimated_missing_samples == 2
    assert summary.missing_sample_ratio == pytest.approx(0.4)


def test_sampling_opportunities_detect_regular_loss_that_median_cannot():
    source = replace(descriptor(), nominal_period_ns=None)
    summary = summarize_source_quality(
        [source],
        [sample(0, 0, 1.0), sample(1, 200, 2.0), sample(2, 400, 3.0)],
        expected_sample_opportunities={source.local_source_id: 6},
    )[0]

    assert summary.sequence_missing_samples == 0
    assert summary.timing_missing_samples == 0
    assert summary.opportunity_missing_samples == 3
    assert summary.missing_sample_ratio == pytest.approx(0.5)


def test_sham_comparison_is_matched_and_centers_on_zero():
    observations = [
        ShamTrialObservation("a-1", "b1", "A", 100.0),
        ShamTrialObservation("b-1", "b1", "B", 101.0),
        ShamTrialObservation("a-2", "b2", "A", 102.0),
        ShamTrialObservation("b-2", "b2", "B", 100.0),
        ShamTrialObservation("a-3", "b3", "A", 99.0),
        ShamTrialObservation("b-3", "b3", "B", 100.0),
    ]

    comparison = compare_sham_trials(observations, left_arm="A", right_arm="B")

    assert comparison.paired_differences_joules == (-1.0, 2.0, -1.0)
    assert comparison.mean_difference_joules == 0.0
    assert comparison.uncertainty_joules == pytest.approx(1.9)
    assert comparison.centers_on_zero is True

    with pytest.raises(AnalysisError, match="missing sham arm"):
        compare_sham_trials(observations[:-1], left_arm="A", right_arm="B")


def test_measurement_overhead_is_mean_of_paired_relative_differences():
    overhead = calculate_measurement_overhead(
        baseline_durations_s=[10.0, 20.0],
        instrumented_durations_s=[10.05, 20.2],
    )

    assert overhead.paired_overhead_ratios == pytest.approx((0.005, 0.01))
    assert overhead.overhead_ratio == pytest.approx(0.0075)
    assert overhead.sample_size == 2


def test_sampling_selection_uses_strict_limits_and_all_required_regimes():
    selection = select_fastest_sampling_interval(
        [
            SamplingIntervalObservation(500_000, "idle", 0.01, 0.0005, 0.02),
            SamplingIntervalObservation(
                500_000, "fixed-load", 0.009, 0.0005, 0.015
            ),
            SamplingIntervalObservation(500_000, "aa", 0.009, 0.0005, 0.012),
            SamplingIntervalObservation(1_000_000, "idle", 0.009, 0.0009, 0.004),
            SamplingIntervalObservation(
                1_000_000, "fixed-load", 0.008, 0.0008, 0.006
            ),
            SamplingIntervalObservation(1_000_000, "aa", 0.008, 0.0008, 0.005),
            SamplingIntervalObservation(2_000_000, "idle", 0.002, 0.0001, 0.002),
            SamplingIntervalObservation(
                2_000_000, "fixed-load", 0.002, 0.0001, 0.002
            ),
        ],
    )

    assert selection.status == SamplingSelectionStatus.SELECTED
    assert selection.selected_interval_ns == 1_000_000
    assert selection.assessments[0].eligible is False
    assert "not strictly below" in selection.assessments[0].rejection_reasons[0]
    assert selection.assessments[0].worst_phase_sensitivity_ratio == 0.02
    assert any(
        "phase sensitivity" in reason
        for reason in selection.assessments[0].rejection_reasons
    )
    assert selection.max_phase_sensitivity_ratio == 0.01
    assert selection.required_regimes == ("idle", "fixed-load", "aa")
    assert selection.assessments[2].eligible is False
    assert "missing required regimes: aa" in selection.assessments[2].rejection_reasons

    none = select_fastest_sampling_interval(
        [SamplingIntervalObservation(1_000_000, "idle", 0.001, 0.001, 0.01)]
    )
    assert none.status == SamplingSelectionStatus.NO_ELIGIBLE_INTERVAL
    assert none.selected_interval_ns is None


def test_meter_calibration_artifact_binds_boundary_interval_and_phase_gate(tmp_path):
    source = descriptor()
    boundary = MeasurementBoundary.from_descriptors(
        [source],
        [source.source_uid],
        AggregationRule.SINGLE_SOURCE,
        boundary_id="rapl-package-node-a",
    )
    selection = select_fastest_sampling_interval(
        [
            SamplingIntervalObservation(1_000_000, "idle", 0.004, 0.0002, 0.003),
            SamplingIntervalObservation(
                1_000_000, "fixed-load", 0.006, 0.0004, 0.005
            ),
            SamplingIntervalObservation(1_000_000, "aa", 0.005, 0.0003, 0.004),
        ]
    )

    document = build_meter_calibration_document(
        calibration_id="node-a-rapl-v1",
        boundary=boundary,
        selection=selection,
    )
    assert document["selected_interval_ns"] == 1_000_000
    assert document["required_regimes"] == ["IDLE", "FIXED_LOAD", "AA"]
    assert document["selected_metrics"] == {
        "measurement_overhead_ratio": 0.006,
        "missing_sample_ratio": 0.0004,
        "phase_sensitivity_ratio": 0.005,
    }

    sealed = write_meter_calibration_artifact(
        tmp_path / "meter-calibration.json",
        calibration_id="node-a-rapl-v1",
        boundary=boundary,
        selection=selection,
    )
    contents = sealed.path.read_bytes()
    assert sealed.sha256 == hashlib.sha256(contents).hexdigest()
    assert json.loads(contents) == document
    with pytest.raises(AnalysisError, match="refusing to overwrite"):
        write_meter_calibration_artifact(
            sealed.path,
            calibration_id="node-a-rapl-v1",
            boundary=boundary,
            selection=selection,
        )


def test_trial_quality_metadata_is_canonical_and_uses_strict_thresholds():
    clean = summarize_source_quality(
        [descriptor()],
        [sample(0, 0, 1.0), sample(1, 100, 2.0), sample(2, 200, 3.0)],
    )[0]

    accepted = build_trial_quality_metadata(
        trial_id="trial-1",
        summaries=[clean],
        measurement_overhead_ratio=0.009,
        required_source_uids=[clean.source_uid],
    )
    assert set(accepted) == {
        "trial_id",
        "disposition",
        "measurement_overhead_ratio",
        "sources",
        "issues",
    }
    assert accepted["disposition"] == "ACCEPTED"
    assert accepted["issues"] == []
    assert accepted["sources"] == [
        {
            "source_uid": clean.source_uid,
            "observed_samples": 3,
            "sequence_gaps": 0,
            "missing_sample_ratio": 0.0,
            "max_timestamp_jitter_ns": 0,
        }
    ]

    overhead_at_limit = build_trial_quality_metadata(
        trial_id="trial-overhead-limit",
        summaries=[clean],
        measurement_overhead_ratio=0.01,
        required_source_uids=[clean.source_uid],
    )
    assert overhead_at_limit["disposition"] == "REJECTED"
    assert overhead_at_limit["issues"][0]["code"] == "OVERHEAD_LIMIT_EXCEEDED"

    missing_at_limit = build_trial_quality_metadata(
        trial_id="trial-missing-limit",
        summaries=[replace(clean, missing_sample_ratio=0.001)],
        measurement_overhead_ratio=0.0,
        required_source_uids=[clean.source_uid],
    )
    assert missing_at_limit["disposition"] == "REJECTED"
    assert missing_at_limit["issues"][0]["code"] == "MISSING_SAMPLE_LIMIT_EXCEEDED"


def test_trial_quality_metadata_requires_coverage_and_calibration_evidence():
    clean = summarize_source_quality(
        [descriptor()], [sample(0, 0, 1.0), sample(1, 100, 2.0)]
    )[0]

    review = build_trial_quality_metadata(
        trial_id="trial-review",
        summaries=[clean],
        measurement_overhead_ratio=None,
        required_source_uids=[clean.source_uid],
    )
    assert review["disposition"] == "REVIEW_REQUIRED"
    assert review["issues"][0]["code"] == "OVERHEAD_NOT_EVALUATED"

    missing = build_trial_quality_metadata(
        trial_id="trial-missing",
        summaries=[clean],
        measurement_overhead_ratio=0.0,
        required_source_uids=[clean.source_uid, "rapl://node-a/package/1"],
    )
    assert missing["disposition"] == "REJECTED"
    assert any(
        issue["code"] == "REQUIRED_SOURCE_MISSING"
        and issue["source_uid"] == "rapl://node-a/package/1"
        for issue in missing["issues"]
    )

    indeterminate = summarize_source_quality([descriptor()], [sample(0, 0, 1.0)])[0]
    rejected = build_trial_quality_metadata(
        trial_id="trial-short",
        summaries=[indeterminate],
        measurement_overhead_ratio=0.0,
        required_source_uids=[indeterminate.source_uid],
    )
    assert rejected["disposition"] == "REJECTED"
    assert rejected["sources"][0]["missing_sample_ratio"] == 1.0
    assert rejected["issues"][0]["code"] == "MISSINGNESS_INDETERMINATE"


def test_meaningful_effect_uses_larger_five_percent_or_twice_aa_uncertainty():
    relative_wins = compute_smallest_meaningful_effect(
        control_energies_joules=[100.0, 110.0],
        aa_uncertainty_joules=2.0,
    )
    assert relative_wins.relative_floor_joules == pytest.approx(5.25)
    assert relative_wins.meaningful_effect_joules == pytest.approx(5.25)

    aa_wins = compute_smallest_meaningful_effect(
        control_energies_joules=[100.0, 110.0],
        aa_uncertainty_joules=4.0,
    )
    assert aa_wins.aa_floor_joules == 8.0
    assert aa_wins.meaningful_effect_joules == 8.0
