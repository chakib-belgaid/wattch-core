"""Offline meter calibration and raw-trace quality summaries.

Nothing in this module runs in, or feeds interpretation back into, a Wattch
daemon.  It consumes immutable source descriptors, raw samples, and explicit
calibration observations after capture.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

from .._messages import RawSample, SourceDescriptor
from .boundary import AnalysisError, MeasurementBoundary


@dataclass(frozen=True)
class SourceCalibrationSummary:
    """Cadence and evidence-quality facts for one raw source.

    Timestamp jitter is calculated after accounting for whole missing sample
    periods, so a two-period gap is reported as one missing sample rather than
    one period of jitter.  ``minimum_observed_step`` is descriptive only; the
    descriptor's ``declared_resolution`` remains the source's resolution claim.
    """

    source_uid: str
    local_source_id: int
    observed_samples: int
    first_monotonic_ns: int | None
    last_monotonic_ns: int | None
    declared_nominal_period_ns: int | None
    cadence_reference: str | None
    cadence_reference_period_ns: float | None
    mean_interval_ns: float | None
    median_interval_ns: float | None
    effective_cadence_hz: float | None
    timestamp_jitter_rms_ns: float | None
    max_timestamp_jitter_ns: float | None
    declared_resolution: float | None
    minimum_observed_step: float | None
    counter_wraps: int
    unexplained_backward_steps: int
    sequence_missing_samples: int
    timing_missing_samples: int
    opportunity_missing_samples: int
    estimated_missing_samples: int
    missing_sample_ratio: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_uid": self.source_uid,
            "local_source_id": self.local_source_id,
            "observed_samples": self.observed_samples,
            "first_monotonic_ns": self.first_monotonic_ns,
            "last_monotonic_ns": self.last_monotonic_ns,
            "declared_nominal_period_ns": self.declared_nominal_period_ns,
            "cadence_reference": self.cadence_reference,
            "cadence_reference_period_ns": self.cadence_reference_period_ns,
            "mean_interval_ns": self.mean_interval_ns,
            "median_interval_ns": self.median_interval_ns,
            "effective_cadence_hz": self.effective_cadence_hz,
            "timestamp_jitter_rms_ns": self.timestamp_jitter_rms_ns,
            "max_timestamp_jitter_ns": self.max_timestamp_jitter_ns,
            "declared_resolution": self.declared_resolution,
            "minimum_observed_step": self.minimum_observed_step,
            "counter_wraps": self.counter_wraps,
            "unexplained_backward_steps": self.unexplained_backward_steps,
            "sequence_missing_samples": self.sequence_missing_samples,
            "timing_missing_samples": self.timing_missing_samples,
            "opportunity_missing_samples": self.opportunity_missing_samples,
            "estimated_missing_samples": self.estimated_missing_samples,
            "missing_sample_ratio": self.missing_sample_ratio,
        }

    def to_quality_dict(self) -> dict[str, object]:
        """Return the canonical Rust ``SourceQualitySummary`` document.

        The core schema deliberately keeps this record compact.  Detailed
        cadence, resolution, wrap, and missingness diagnostics remain in
        :meth:`to_dict`.  When fewer than two samples make missingness
        indeterminate, the screening record uses the conservative value 1.0;
        :func:`build_trial_quality_metadata` always attaches an explicit issue
        so this substitution cannot be accepted silently.
        """

        if not isinstance(self.source_uid, str) or not self.source_uid.strip():
            raise AnalysisError("source quality summary UID must not be empty")
        for value, label in (
            (self.observed_samples, "observed_samples"),
            (self.sequence_missing_samples, "sequence_gaps"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AnalysisError(
                    f"source quality {label} must be a non-negative integer"
                )
        if self.missing_sample_ratio is not None and (
            isinstance(self.missing_sample_ratio, bool)
            or not isinstance(self.missing_sample_ratio, (int, float))
            or not math.isfinite(float(self.missing_sample_ratio))
            or not 0 <= float(self.missing_sample_ratio) <= 1
        ):
            raise AnalysisError("source missing sample ratio must be in [0, 1]")
        if self.max_timestamp_jitter_ns is not None and (
            isinstance(self.max_timestamp_jitter_ns, bool)
            or not isinstance(self.max_timestamp_jitter_ns, (int, float))
            or not math.isfinite(float(self.max_timestamp_jitter_ns))
            or float(self.max_timestamp_jitter_ns) < 0
        ):
            raise AnalysisError(
                "source timestamp jitter must be finite and non-negative"
            )

        missing_ratio = (
            self.missing_sample_ratio
            if self.missing_sample_ratio is not None
            else 1.0
        )
        max_jitter = (
            math.ceil(self.max_timestamp_jitter_ns)
            if self.max_timestamp_jitter_ns is not None
            else None
        )
        return {
            "source_uid": self.source_uid,
            "observed_samples": self.observed_samples,
            "sequence_gaps": self.sequence_missing_samples,
            "missing_sample_ratio": missing_ratio,
            "max_timestamp_jitter_ns": max_jitter,
        }


def _positive_finite_optional(value: float | int | None, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError(f"{label} must be numeric")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise AnalysisError(f"{label} must be finite and greater than zero")


def _numeric_value(sample: RawSample) -> float | None:
    if isinstance(sample.value, bytes):
        return None
    if isinstance(sample.value, bool):
        raise AnalysisError("boolean raw samples are not numeric telemetry")
    value = float(sample.value)
    if not math.isfinite(value):
        raise AnalysisError("raw sample values must be finite")
    return value


def _rounded_period_count(interval_ns: int, reference_ns: float) -> int:
    # ``round`` uses ties-to-even, which is surprising at exactly 1.5 periods.
    return max(1, math.floor(interval_ns / reference_ns + 0.5))


def _summarize_one_source(
    descriptor: SourceDescriptor,
    samples: Sequence[RawSample],
    *,
    capture_start_monotonic_ns: int | None = None,
    capture_end_monotonic_ns: int | None = None,
    expected_sample_opportunities: int | None = None,
) -> SourceCalibrationSummary:
    if expected_sample_opportunities is not None and (
        isinstance(expected_sample_opportunities, bool)
        or not isinstance(expected_sample_opportunities, int)
        or expected_sample_opportunities < len(samples)
    ):
        raise AnalysisError(
            "expected sample opportunities must be an integer no smaller "
            "than observed samples"
        )
    sequence_missing = 0
    intervals: list[int] = []
    previous: RawSample | None = None
    for sample in samples:
        if sample.source_id != descriptor.local_source_id:
            raise AnalysisError("sample grouped under the wrong source descriptor")
        if sample.sequence_no < 0 or sample.monotonic_ns < 0:
            raise AnalysisError("sample sequence and timestamp must be non-negative")
        _numeric_value(sample)
        if previous is not None:
            if sample.sequence_no <= previous.sequence_no:
                raise AnalysisError(
                    f"source {descriptor.source_uid!r} sequence numbers must "
                    "strictly increase"
                )
            if sample.monotonic_ns <= previous.monotonic_ns:
                raise AnalysisError(
                    f"source {descriptor.source_uid!r} timestamps must strictly "
                    "increase"
                )
            sequence_missing += sample.sequence_no - previous.sequence_no - 1
            intervals.append(sample.monotonic_ns - previous.monotonic_ns)
        previous = sample

    declared_period = descriptor.nominal_period_ns
    reference: float | None = None
    reference_kind: str | None = None
    if declared_period is not None:
        reference = float(declared_period)
        reference_kind = "DECLARED_NOMINAL_PERIOD"
    elif intervals:
        reference = float(statistics.median(intervals))
        reference_kind = "OBSERVED_MEDIAN_INTERVAL"

    timing_missing = 0
    jitter: list[float] = []
    if reference is not None:
        for interval in intervals:
            period_count = _rounded_period_count(interval, reference)
            timing_missing += period_count - 1
            jitter.append(abs(interval - period_count * reference))
        # Explicit daemon-clock bounds let us detect a source that appears
        # late or disappears before the analysis window ends. Without these
        # bounds, no leading/trailing absence is invented.
        if samples and capture_start_monotonic_ns is not None:
            leading_interval = samples[0].monotonic_ns - capture_start_monotonic_ns
            if leading_interval > 0:
                timing_missing += max(
                    0, math.ceil(leading_interval / reference - 1e-12) - 1
                )
        if samples and capture_end_monotonic_ns is not None:
            trailing_interval = capture_end_monotonic_ns - samples[-1].monotonic_ns
            if trailing_interval > 0:
                timing_missing += max(
                    0, math.ceil(trailing_interval / reference - 1e-12) - 1
                )

    numeric_values = [_numeric_value(sample) for sample in samples]
    value_steps: list[float] = []
    wraps = 0
    unexplained_backwards = 0
    if all(value is not None for value in numeric_values):
        values = [float(value) for value in numeric_values if value is not None]
        for earlier, later in zip(values, values[1:]):
            if descriptor.temporality == "CUMULATIVE":
                if later < earlier:
                    if descriptor.max_value is not None:
                        wraps += 1
                        step = descriptor.max_value - earlier + later
                    else:
                        unexplained_backwards += 1
                        continue
                else:
                    step = later - earlier
            else:
                step = abs(later - earlier)
            if step > 0:
                value_steps.append(step)

    observed_count = len(samples)
    opportunity_missing = (
        0
        if expected_sample_opportunities is None
        else expected_sample_opportunities - observed_count
    )
    estimated_missing = max(
        sequence_missing,
        timing_missing,
        opportunity_missing,
    )
    missing_ratio = (
        estimated_missing / (observed_count + estimated_missing)
        if observed_count >= 2
        else None
    )
    elapsed_ns = (
        samples[-1].monotonic_ns - samples[0].monotonic_ns
        if observed_count >= 2
        else None
    )
    effective_cadence_hz = (
        (observed_count - 1) * 1e9 / elapsed_ns
        if elapsed_ns is not None and elapsed_ns > 0
        else None
    )

    return SourceCalibrationSummary(
        source_uid=descriptor.source_uid,
        local_source_id=descriptor.local_source_id,
        observed_samples=observed_count,
        first_monotonic_ns=samples[0].monotonic_ns if samples else None,
        last_monotonic_ns=samples[-1].monotonic_ns if samples else None,
        declared_nominal_period_ns=declared_period,
        cadence_reference=reference_kind,
        cadence_reference_period_ns=reference,
        mean_interval_ns=statistics.fmean(intervals) if intervals else None,
        median_interval_ns=float(statistics.median(intervals)) if intervals else None,
        effective_cadence_hz=effective_cadence_hz,
        timestamp_jitter_rms_ns=(
            math.sqrt(statistics.fmean(value * value for value in jitter))
            if jitter
            else None
        ),
        max_timestamp_jitter_ns=max(jitter) if jitter else None,
        declared_resolution=(
            float(descriptor.resolution) if descriptor.resolution is not None else None
        ),
        minimum_observed_step=min(value_steps) if value_steps else None,
        counter_wraps=wraps,
        unexplained_backward_steps=unexplained_backwards,
        sequence_missing_samples=sequence_missing,
        timing_missing_samples=timing_missing,
        opportunity_missing_samples=opportunity_missing,
        estimated_missing_samples=estimated_missing,
        missing_sample_ratio=missing_ratio,
    )


def summarize_source_quality(
    descriptors: Iterable[SourceDescriptor],
    samples: Iterable[RawSample],
    *,
    capture_start_monotonic_ns: int | None = None,
    capture_end_monotonic_ns: int | None = None,
    expected_sample_opportunities: Mapping[int, int] | None = None,
) -> tuple[SourceCalibrationSummary, ...]:
    """Summarize each descriptor's raw samples in descriptor order.

    Missingness is inferred independently from sequence gaps and elapsed
    cadence gaps, then conservatively takes their maximum to avoid counting the
    same disappearance twice. Leading or trailing absence is counted only when
    both the caller supplies daemon-clock capture bounds and a cadence
    reference can be established.
    """

    for value, name in (
        (capture_start_monotonic_ns, "capture_start_monotonic_ns"),
        (capture_end_monotonic_ns, "capture_end_monotonic_ns"),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise AnalysisError(f"{name} must be a non-negative integer or None")
    if (
        capture_start_monotonic_ns is not None
        and capture_end_monotonic_ns is not None
        and capture_end_monotonic_ns <= capture_start_monotonic_ns
    ):
        raise AnalysisError("capture end must be after capture start")

    descriptor_list = tuple(descriptors)
    by_id: dict[int, SourceDescriptor] = {}
    source_uids: set[str] = set()
    for descriptor in descriptor_list:
        if descriptor.local_source_id < 0:
            raise AnalysisError("source local id must be non-negative")
        if descriptor.local_source_id in by_id:
            raise AnalysisError("source descriptors must have unique local ids")
        if not descriptor.source_uid or descriptor.source_uid in source_uids:
            raise AnalysisError("source descriptors must have unique non-empty UIDs")
        _positive_finite_optional(
            descriptor.nominal_period_ns, "descriptor nominal_period_ns"
        )
        _positive_finite_optional(descriptor.resolution, "descriptor resolution")
        _positive_finite_optional(descriptor.max_value, "descriptor max_value")
        by_id[descriptor.local_source_id] = descriptor
        source_uids.add(descriptor.source_uid)

    opportunity_counts = dict(expected_sample_opportunities or {})
    unknown_opportunities = set(opportunity_counts) - set(by_id)
    if unknown_opportunities:
        raise AnalysisError(
            "sample opportunities refer to unknown source ids: "
            + ", ".join(str(value) for value in sorted(unknown_opportunities))
        )

    grouped: dict[int, list[RawSample]] = {source_id: [] for source_id in by_id}
    for sample in samples:
        if sample.source_id not in grouped:
            raise AnalysisError(
                f"raw sample refers to unknown source id {sample.source_id}"
            )
        grouped[sample.source_id].append(sample)

    return tuple(
        _summarize_one_source(
            descriptor,
            grouped[descriptor.local_source_id],
            capture_start_monotonic_ns=capture_start_monotonic_ns,
            capture_end_monotonic_ns=capture_end_monotonic_ns,
            expected_sample_opportunities=opportunity_counts.get(
                descriptor.local_source_id
            ),
        )
        for descriptor in descriptor_list
    )


def build_trial_quality_metadata(
    *,
    trial_id: str,
    summaries: Iterable[SourceCalibrationSummary],
    measurement_overhead_ratio: float | None,
    required_source_uids: Iterable[str],
    max_overhead_ratio: float = 0.01,
    max_missing_sample_ratio: float = 0.001,
) -> dict[str, object]:
    """Build canonical, conservative ``TrialQualityMetadata``.

    A trial is accepted only when every required measurement-boundary source
    has enough evidence to estimate missingness, remains strictly below the
    missing-sample threshold, has no unexplained backwards counter step, and a
    separately measured collection overhead is strictly below its threshold.
    Missing calibration evidence requires review; concrete evidence failures
    reject the trial.

    Negative measured overhead can occur as paired calibration noise.  The
    canonical core schema defines overhead as a non-negative burden, so such a
    value is clamped to zero and retained as a structured warning.
    """

    if not isinstance(trial_id, str) or not trial_id.strip():
        raise AnalysisError("quality metadata trial_id must not be empty")
    if (
        isinstance(max_overhead_ratio, bool)
        or not isinstance(max_overhead_ratio, (int, float))
        or not math.isfinite(float(max_overhead_ratio))
        or float(max_overhead_ratio) <= 0
    ):
        raise AnalysisError("maximum overhead ratio must be finite and positive")
    if (
        isinstance(max_missing_sample_ratio, bool)
        or not isinstance(max_missing_sample_ratio, (int, float))
        or not math.isfinite(float(max_missing_sample_ratio))
        or not 0 < float(max_missing_sample_ratio) <= 1
    ):
        raise AnalysisError(
            "maximum missing sample ratio must be finite and in (0, 1]"
        )

    required = tuple(required_source_uids)
    if not required:
        raise AnalysisError("quality metadata needs at least one required source UID")
    if any(
        not isinstance(source_uid, str) or not source_uid.strip()
        for source_uid in required
    ):
        raise AnalysisError("required source UIDs must be non-empty strings")
    if len(set(required)) != len(required):
        raise AnalysisError("required source UIDs must be unique")
    required_set = set(required)

    summary_list = tuple(summaries)
    by_uid: dict[str, SourceCalibrationSummary] = {}
    for summary in summary_list:
        if not summary.source_uid.strip():
            raise AnalysisError("source quality summary UID must not be empty")
        if summary.source_uid in by_uid:
            raise AnalysisError(
                f"duplicate source quality summary {summary.source_uid!r}"
            )
        summary.to_quality_dict()
        if (
            isinstance(summary.unexplained_backward_steps, bool)
            or not isinstance(summary.unexplained_backward_steps, int)
            or summary.unexplained_backward_steps < 0
        ):
            raise AnalysisError(
                "source unexplained backwards count must be a non-negative integer"
            )
        by_uid[summary.source_uid] = summary

    issues: list[dict[str, object]] = []

    def add_issue(
        code: str,
        severity: str,
        message: str,
        source_uid: str | None = None,
    ) -> None:
        issues.append(
            {
                "code": code,
                "severity": severity,
                "message": message,
                "source_uid": source_uid,
            }
        )

    canonical_overhead: float | None
    review_required = False
    if measurement_overhead_ratio is None:
        canonical_overhead = None
        review_required = True
        add_issue(
            "OVERHEAD_NOT_EVALUATED",
            "WARNING",
            "collection overhead has not been evaluated by paired calibration",
        )
    else:
        if (
            isinstance(measurement_overhead_ratio, bool)
            or not isinstance(measurement_overhead_ratio, (int, float))
            or not math.isfinite(float(measurement_overhead_ratio))
        ):
            raise AnalysisError("measurement overhead ratio must be finite")
        observed_overhead = float(measurement_overhead_ratio)
        canonical_overhead = max(0.0, observed_overhead)
        if observed_overhead < 0:
            add_issue(
                "NEGATIVE_OVERHEAD_ESTIMATE_CLAMPED",
                "WARNING",
                "paired calibration estimated negative overhead; canonical "
                "overhead was clamped to zero",
            )
        if canonical_overhead >= float(max_overhead_ratio):
            add_issue(
                "OVERHEAD_LIMIT_EXCEEDED",
                "ERROR",
                "collection overhead is not strictly below the preregistered limit",
            )

    for source_uid in required:
        if source_uid not in by_uid:
            add_issue(
                "REQUIRED_SOURCE_MISSING",
                "ERROR",
                "required measurement-boundary source has no quality summary",
                source_uid,
            )

    for summary in summary_list:
        required_source = summary.source_uid in required_set
        severity = "ERROR" if required_source else "WARNING"
        if summary.observed_samples < 2 or summary.missing_sample_ratio is None:
            add_issue(
                "MISSINGNESS_INDETERMINATE",
                severity,
                "at least two samples are required to estimate source missingness",
                summary.source_uid,
            )
        elif summary.missing_sample_ratio >= float(max_missing_sample_ratio):
            add_issue(
                "MISSING_SAMPLE_LIMIT_EXCEEDED",
                severity,
                "source missingness is not strictly below the preregistered limit",
                summary.source_uid,
            )
        if summary.unexplained_backward_steps > 0:
            add_issue(
                "UNEXPLAINED_BACKWARD_STEP",
                severity,
                "source contains an unexplained backwards counter step",
                summary.source_uid,
            )

    has_error = any(issue["severity"] == "ERROR" for issue in issues)
    if has_error:
        disposition = "REJECTED"
    elif review_required:
        disposition = "REVIEW_REQUIRED"
    else:
        disposition = "ACCEPTED"

    return {
        "trial_id": trial_id,
        "disposition": disposition,
        "measurement_overhead_ratio": canonical_overhead,
        "sources": [summary.to_quality_dict() for summary in summary_list],
        "issues": issues,
    }


@dataclass(frozen=True)
class ShamTrialObservation:
    """One arm of a matched sham-versus-sham block."""

    trial_id: str
    block_id: str
    arm: str
    energy_joules: float

    def __post_init__(self) -> None:
        if not self.trial_id or not self.block_id or not self.arm:
            raise AnalysisError("sham trial identifiers and arm must not be empty")
        if not math.isfinite(self.energy_joules) or self.energy_joules < 0:
            raise AnalysisError("sham trial energy must be finite and non-negative")


@dataclass(frozen=True)
class ShamComparison:
    """Paired A/A contrast and its empirical absolute-difference uncertainty."""

    left_arm: str
    right_arm: str
    block_ids: tuple[str, ...]
    paired_differences_joules: tuple[float, ...]
    mean_difference_joules: float
    sample_standard_deviation_joules: float | None
    standard_error_joules: float | None
    confidence: float
    ci_lower_joules: float | None
    ci_upper_joules: float | None
    uncertainty_quantile: float
    uncertainty_joules: float
    centers_on_zero: bool | None

    @property
    def sample_size(self) -> int:
        return len(self.paired_differences_joules)

    def to_dict(self) -> dict[str, object]:
        return {
            "contrast": f"{self.left_arm}-{self.right_arm}",
            "block_ids": list(self.block_ids),
            "paired_differences_joules": list(self.paired_differences_joules),
            "mean_difference_joules": self.mean_difference_joules,
            "sample_standard_deviation_joules": (self.sample_standard_deviation_joules),
            "standard_error_joules": self.standard_error_joules,
            "confidence": self.confidence,
            "ci_lower_joules": self.ci_lower_joules,
            "ci_upper_joules": self.ci_upper_joules,
            "uncertainty_quantile": self.uncertainty_quantile,
            "uncertainty_joules": self.uncertainty_joules,
            "centers_on_zero": self.centers_on_zero,
        }


def _linear_quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] * (1 - fraction) + ordered[upper_index] * fraction


def compare_sham_trials(
    observations: Iterable[ShamTrialObservation],
    *,
    left_arm: str,
    right_arm: str,
    confidence: float = 0.95,
    uncertainty_quantile: float = 0.95,
) -> ShamComparison:
    """Compare two nominally identical arms inside complete matched blocks."""

    if not left_arm or not right_arm or left_arm == right_arm:
        raise AnalysisError("sham comparison arms must be distinct and non-empty")
    if not 0 < confidence < 1:
        raise AnalysisError("confidence must be between zero and one")
    if not 0 < uncertainty_quantile < 1:
        raise AnalysisError("uncertainty_quantile must be between zero and one")

    by_block: dict[str, dict[str, ShamTrialObservation]] = {}
    items = tuple(observations)
    if not items:
        raise AnalysisError("sham comparison needs at least one matched block")
    for observation in items:
        if observation.arm not in {left_arm, right_arm}:
            raise AnalysisError(
                f"unexpected sham arm {observation.arm!r} in requested comparison"
            )
        block = by_block.setdefault(observation.block_id, {})
        if observation.arm in block:
            raise AnalysisError(
                f"block {observation.block_id!r} has duplicate arm {observation.arm!r}"
            )
        block[observation.arm] = observation

    block_ids = tuple(sorted(by_block))
    differences: list[float] = []
    for block_id in block_ids:
        block = by_block[block_id]
        missing = {left_arm, right_arm} - set(block)
        if missing:
            raise AnalysisError(
                f"block {block_id!r} is missing sham arm(s): "
                + ", ".join(sorted(missing))
            )
        differences.append(
            block[left_arm].energy_joules - block[right_arm].energy_joules
        )

    mean = statistics.fmean(differences)
    deviation: float | None = None
    standard_error: float | None = None
    lower: float | None = None
    upper: float | None = None
    centered: bool | None = None
    if len(differences) >= 2:
        deviation = statistics.stdev(differences)
        standard_error = deviation / math.sqrt(len(differences))
        critical = NormalDist().inv_cdf((1 + confidence) / 2)
        lower = mean - critical * standard_error
        upper = mean + critical * standard_error
        centered = lower <= 0 <= upper

    uncertainty = _linear_quantile(
        tuple(abs(value) for value in differences), uncertainty_quantile
    )
    return ShamComparison(
        left_arm=left_arm,
        right_arm=right_arm,
        block_ids=block_ids,
        paired_differences_joules=tuple(differences),
        mean_difference_joules=mean,
        sample_standard_deviation_joules=deviation,
        standard_error_joules=standard_error,
        confidence=confidence,
        ci_lower_joules=lower,
        ci_upper_joules=upper,
        uncertainty_quantile=uncertainty_quantile,
        uncertainty_joules=uncertainty,
        centers_on_zero=centered,
    )


@dataclass(frozen=True)
class MeasurementOverhead:
    """Paired same-work timing overhead for one collection configuration."""

    baseline_durations_s: tuple[float, ...]
    instrumented_durations_s: tuple[float, ...]
    paired_overhead_ratios: tuple[float, ...]
    baseline_mean_s: float
    instrumented_mean_s: float
    overhead_ratio: float

    @property
    def sample_size(self) -> int:
        return len(self.baseline_durations_s)

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline_durations_s": list(self.baseline_durations_s),
            "instrumented_durations_s": list(self.instrumented_durations_s),
            "paired_overhead_ratios": list(self.paired_overhead_ratios),
            "baseline_mean_s": self.baseline_mean_s,
            "instrumented_mean_s": self.instrumented_mean_s,
            "overhead_ratio": self.overhead_ratio,
        }


def calculate_measurement_overhead(
    *,
    baseline_durations_s: Iterable[float],
    instrumented_durations_s: Iterable[float],
) -> MeasurementOverhead:
    """Calculate mean paired relative runtime overhead.

    Durations must come from matched runs completing identical work.  A
    negative ratio is retained as observed calibration noise rather than being
    truncated to zero.
    """

    baseline = tuple(float(value) for value in baseline_durations_s)
    instrumented = tuple(float(value) for value in instrumented_durations_s)
    if not baseline or len(baseline) != len(instrumented):
        raise AnalysisError(
            "measurement overhead needs equally sized non-empty paired durations"
        )
    if any(not math.isfinite(value) or value <= 0 for value in baseline):
        raise AnalysisError("baseline durations must be finite and greater than zero")
    if any(not math.isfinite(value) or value <= 0 for value in instrumented):
        raise AnalysisError(
            "instrumented durations must be finite and greater than zero"
        )
    ratios = tuple(
        (measured - reference) / reference
        for reference, measured in zip(baseline, instrumented)
    )
    return MeasurementOverhead(
        baseline_durations_s=baseline,
        instrumented_durations_s=instrumented,
        paired_overhead_ratios=ratios,
        baseline_mean_s=statistics.fmean(baseline),
        instrumented_mean_s=statistics.fmean(instrumented),
        overhead_ratio=statistics.fmean(ratios),
    )


@dataclass(frozen=True)
class SamplingIntervalObservation:
    """One regime's aggregate calibration result at a sampling interval.

    ``phase_sensitivity_ratio`` is the maximum relative spread of the measured
    energy outcome across otherwise matched repetitions whose workload onset
    is deliberately shifted within one sampling period.  It makes aliasing a
    measured calibration property instead of an implicit assumption.
    """

    interval_ns: int
    regime: str
    measurement_overhead_ratio: float
    missing_sample_ratio: float
    phase_sensitivity_ratio: float

    def __post_init__(self) -> None:
        if isinstance(self.interval_ns, bool) or self.interval_ns <= 0:
            raise AnalysisError("sampling interval must be greater than zero")
        if not self.regime:
            raise AnalysisError("sampling calibration regime must not be empty")
        if not math.isfinite(self.measurement_overhead_ratio):
            raise AnalysisError("measurement overhead ratio must be finite")
        if (
            not math.isfinite(self.missing_sample_ratio)
            or not 0 <= self.missing_sample_ratio <= 1
        ):
            raise AnalysisError("missing sample ratio must be between zero and one")
        if (
            not math.isfinite(self.phase_sensitivity_ratio)
            or self.phase_sensitivity_ratio < 0
        ):
            raise AnalysisError(
                "phase sensitivity ratio must be finite and non-negative"
            )


@dataclass(frozen=True)
class SamplingIntervalAssessment:
    interval_ns: int
    regimes: tuple[str, ...]
    worst_measurement_overhead_ratio: float
    worst_missing_sample_ratio: float
    worst_phase_sensitivity_ratio: float
    eligible: bool
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "interval_ns": self.interval_ns,
            "regimes": list(self.regimes),
            "worst_measurement_overhead_ratio": (self.worst_measurement_overhead_ratio),
            "worst_missing_sample_ratio": self.worst_missing_sample_ratio,
            "worst_phase_sensitivity_ratio": self.worst_phase_sensitivity_ratio,
            "eligible": self.eligible,
            "rejection_reasons": list(self.rejection_reasons),
        }


class SamplingSelectionStatus(str, Enum):
    SELECTED = "SELECTED"
    NO_ELIGIBLE_INTERVAL = "NO_ELIGIBLE_INTERVAL"


@dataclass(frozen=True)
class SamplingIntervalSelection:
    status: SamplingSelectionStatus
    max_overhead_ratio: float
    max_missing_sample_ratio: float
    max_phase_sensitivity_ratio: float
    required_regimes: tuple[str, ...]
    selected_interval_ns: int | None
    assessments: tuple[SamplingIntervalAssessment, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "max_overhead_ratio": self.max_overhead_ratio,
            "max_missing_sample_ratio": self.max_missing_sample_ratio,
            "max_phase_sensitivity_ratio": self.max_phase_sensitivity_ratio,
            "required_regimes": list(self.required_regimes),
            "selected_interval_ns": self.selected_interval_ns,
            "assessments": [item.to_dict() for item in self.assessments],
        }


@dataclass(frozen=True)
class SealedMeterCalibrationArtifact:
    """Content address returned after exclusive calibration publication."""

    path: Path
    sha256: str
    bytes: int
    document: Mapping[str, Any]


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_meter_calibration_document(
    *,
    calibration_id: str,
    boundary: MeasurementBoundary,
    selection: SamplingIntervalSelection,
) -> dict[str, object]:
    """Build the strict artifact consumed by sealed study preregistration.

    The selected metrics are the worst observed values across all required
    regimes at the chosen interval.  This binds trial quality to the same
    boundary, interval, overhead, missingness, and phase-shift calibration that
    made the interval eligible.
    """

    if not isinstance(calibration_id, str) or not calibration_id.strip():
        raise AnalysisError("meter calibration_id must not be empty")
    if not isinstance(boundary, MeasurementBoundary):
        raise AnalysisError("meter calibration requires a MeasurementBoundary")
    if not isinstance(selection, SamplingIntervalSelection):
        raise AnalysisError(
            "meter calibration selection must be SamplingIntervalSelection"
        )
    if (
        selection.status is not SamplingSelectionStatus.SELECTED
        or selection.selected_interval_ns is None
    ):
        raise AnalysisError("meter calibration has no eligible selected interval")
    if selection.max_overhead_ratio > 0.01:
        raise AnalysisError("meter calibration overhead threshold exceeds 1%")
    if selection.max_missing_sample_ratio > 0.001:
        raise AnalysisError("meter calibration missingness threshold exceeds 0.1%")
    selected = next(
        (
            assessment
            for assessment in selection.assessments
            if assessment.interval_ns == selection.selected_interval_ns
        ),
        None,
    )
    if selected is None or not selected.eligible:
        raise AnalysisError("selected meter interval lacks an eligible assessment")
    canonical_regimes = {
        "idle": "IDLE",
        "fixed-load": "FIXED_LOAD",
        "aa": "AA",
    }
    regimes = [
        canonical_regimes.get(regime, regime.upper().replace("-", "_"))
        for regime in selection.required_regimes
    ]
    required = {"IDLE", "FIXED_LOAD", "AA"}
    if not required.issubset(regimes):
        raise AnalysisError(
            "meter calibration must require idle, fixed-load, and A/A regimes"
        )
    boundary_document = boundary.to_dict()
    return {
        "schema_version": 1,
        "kind": "METER_CALIBRATION",
        "calibration_id": calibration_id,
        "measurement_boundary": boundary_document,
        "measurement_boundary_sha256": _canonical_json_sha256(boundary_document),
        "selected_interval_ns": selection.selected_interval_ns,
        "required_regimes": regimes,
        "selected_metrics": {
            "measurement_overhead_ratio": (
                selected.worst_measurement_overhead_ratio
            ),
            "missing_sample_ratio": selected.worst_missing_sample_ratio,
            "phase_sensitivity_ratio": selected.worst_phase_sensitivity_ratio,
        },
        "thresholds": {
            "max_measurement_overhead_ratio": selection.max_overhead_ratio,
            "max_missing_sample_ratio": selection.max_missing_sample_ratio,
            "max_phase_sensitivity_ratio": (
                selection.max_phase_sensitivity_ratio
            ),
        },
    }


def write_meter_calibration_artifact(
    path: str | Path,
    *,
    calibration_id: str,
    boundary: MeasurementBoundary,
    selection: SamplingIntervalSelection,
) -> SealedMeterCalibrationArtifact:
    """Exclusively publish one immutable, content-addressable calibration."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = build_meter_calibration_document(
        calibration_id=calibration_id,
        boundary=boundary,
        selection=selection,
    )
    contents = (
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise AnalysisError(
            f"refusing to overwrite meter calibration artifact {target}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(target.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return SealedMeterCalibrationArtifact(
        path=target,
        sha256=hashlib.sha256(contents).hexdigest(),
        bytes=len(contents),
        document=document,
    )


def select_fastest_sampling_interval(
    observations: Iterable[SamplingIntervalObservation],
    *,
    max_overhead_ratio: float = 0.01,
    max_missing_sample_ratio: float = 0.001,
    max_phase_sensitivity_ratio: float = 0.01,
    required_regimes: Iterable[str] = ("idle", "fixed-load", "aa"),
) -> SamplingIntervalSelection:
    """Select the shortest interval passing every observation and regime.

    All thresholds are strict: observations equal to 1% overhead, 0.1%
    missingness, or 1% phase sensitivity are rejected under the defaults.
    """

    if not math.isfinite(max_overhead_ratio) or max_overhead_ratio <= 0:
        raise AnalysisError("maximum overhead ratio must be finite and positive")
    if (
        not math.isfinite(max_missing_sample_ratio)
        or not 0 < max_missing_sample_ratio <= 1
    ):
        raise AnalysisError("maximum missing sample ratio must be finite and in (0, 1]")
    if (
        not math.isfinite(max_phase_sensitivity_ratio)
        or max_phase_sensitivity_ratio <= 0
    ):
        raise AnalysisError(
            "maximum phase sensitivity ratio must be finite and positive"
        )
    required = tuple(dict.fromkeys(required_regimes))
    if any(not regime for regime in required):
        raise AnalysisError("required calibration regimes must not be empty")

    grouped: dict[int, dict[str, SamplingIntervalObservation]] = {}
    for observation in observations:
        regimes = grouped.setdefault(observation.interval_ns, {})
        if observation.regime in regimes:
            raise AnalysisError(
                f"interval {observation.interval_ns} has duplicate regime "
                f"{observation.regime!r}"
            )
        regimes[observation.regime] = observation

    assessments: list[SamplingIntervalAssessment] = []
    for interval_ns, regime_map in sorted(grouped.items()):
        values = tuple(regime_map.values())
        worst_overhead = max(value.measurement_overhead_ratio for value in values)
        worst_missing = max(value.missing_sample_ratio for value in values)
        worst_phase_sensitivity = max(
            value.phase_sensitivity_ratio for value in values
        )
        reasons: list[str] = []
        missing_regimes = set(required) - set(regime_map)
        if missing_regimes:
            reasons.append(
                "missing required regimes: " + ", ".join(sorted(missing_regimes))
            )
        if worst_overhead >= max_overhead_ratio:
            reasons.append("measurement overhead is not strictly below the limit")
        if worst_missing >= max_missing_sample_ratio:
            reasons.append("missingness is not strictly below the limit")
        if worst_phase_sensitivity >= max_phase_sensitivity_ratio:
            reasons.append(
                "sampling phase sensitivity is not strictly below the limit"
            )
        assessments.append(
            SamplingIntervalAssessment(
                interval_ns=interval_ns,
                regimes=tuple(sorted(regime_map)),
                worst_measurement_overhead_ratio=worst_overhead,
                worst_missing_sample_ratio=worst_missing,
                worst_phase_sensitivity_ratio=worst_phase_sensitivity,
                eligible=not reasons,
                rejection_reasons=tuple(reasons),
            )
        )

    selected = next((item for item in assessments if item.eligible), None)
    return SamplingIntervalSelection(
        status=(
            SamplingSelectionStatus.SELECTED
            if selected is not None
            else SamplingSelectionStatus.NO_ELIGIBLE_INTERVAL
        ),
        max_overhead_ratio=max_overhead_ratio,
        max_missing_sample_ratio=max_missing_sample_ratio,
        max_phase_sensitivity_ratio=max_phase_sensitivity_ratio,
        required_regimes=required,
        selected_interval_ns=selected.interval_ns if selected is not None else None,
        assessments=tuple(assessments),
    )


@dataclass(frozen=True)
class MeaningfulEffectThreshold:
    """The larger of the control-relative and A/A uncertainty floors."""

    control_energies_joules: tuple[float, ...]
    control_mean_joules: float
    aa_uncertainty_joules: float
    relative_floor_fraction: float
    relative_floor_joules: float
    aa_uncertainty_multiplier: float
    aa_floor_joules: float
    meaningful_effect_joules: float

    def to_dict(self) -> dict[str, object]:
        return {
            "control_energies_joules": list(self.control_energies_joules),
            "control_mean_joules": self.control_mean_joules,
            "aa_uncertainty_joules": self.aa_uncertainty_joules,
            "relative_floor_fraction": self.relative_floor_fraction,
            "relative_floor_joules": self.relative_floor_joules,
            "aa_uncertainty_multiplier": self.aa_uncertainty_multiplier,
            "aa_floor_joules": self.aa_floor_joules,
            "meaningful_effect_joules": self.meaningful_effect_joules,
        }


def compute_smallest_meaningful_effect(
    *,
    control_energies_joules: Iterable[float],
    aa_uncertainty_joules: float,
    relative_floor_fraction: float = 0.05,
    aa_uncertainty_multiplier: float = 2.0,
) -> MeaningfulEffectThreshold:
    """Compute ``max(5% of control energy, 2 * A/A uncertainty)`` by default."""

    controls = tuple(float(value) for value in control_energies_joules)
    if not controls:
        raise AnalysisError("meaningful-effect threshold needs control energy")
    if any(not math.isfinite(value) or value < 0 for value in controls):
        raise AnalysisError("control energies must be finite and non-negative")
    if not math.isfinite(aa_uncertainty_joules) or aa_uncertainty_joules < 0:
        raise AnalysisError("A/A uncertainty must be finite and non-negative")
    if (
        not math.isfinite(relative_floor_fraction)
        or relative_floor_fraction < 0
        or not math.isfinite(aa_uncertainty_multiplier)
        or aa_uncertainty_multiplier < 0
    ):
        raise AnalysisError("meaningful-effect factors must be finite and non-negative")

    control_mean = statistics.fmean(controls)
    relative_floor = relative_floor_fraction * control_mean
    aa_floor = aa_uncertainty_multiplier * aa_uncertainty_joules
    return MeaningfulEffectThreshold(
        control_energies_joules=controls,
        control_mean_joules=control_mean,
        aa_uncertainty_joules=aa_uncertainty_joules,
        relative_floor_fraction=relative_floor_fraction,
        relative_floor_joules=relative_floor,
        aa_uncertainty_multiplier=aa_uncertainty_multiplier,
        aa_floor_joules=aa_floor,
        meaningful_effect_joules=max(relative_floor, aa_floor),
    )
