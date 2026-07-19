"""Paired causal contrasts and preregistered ranking-reversal checks."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .boundary import AnalysisError, MeasurementBoundary
from .outcomes import EnergyOutcome, Estimand


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Numerically stable continued fraction for incomplete beta."""

    max_iterations = 200
    epsilon = 3e-14
    floor = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < floor:
        d = floor
    d = 1.0 / d
    result = d
    for iteration in range(1, max_iterations + 1):
        doubled = 2 * iteration
        coefficient = (
            iteration * (b - iteration) * x / ((qam + doubled) * (a + doubled))
        )
        d = 1.0 + coefficient * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + coefficient / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        result *= d * c
        coefficient = -(
            (a + iteration)
            * (qab + iteration)
            * x
            / ((a + doubled) * (qap + doubled))
        )
        d = 1.0 + coefficient * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + coefficient / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            return result
    raise AnalysisError("Student-t critical value did not converge")


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    factor = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return factor * _beta_continued_fraction(a, b, x) / a
    return 1.0 - factor * _beta_continued_fraction(b, a, 1.0 - x) / b


def _student_t_cdf(value: float, degrees_of_freedom: int) -> float:
    if degrees_of_freedom <= 0:
        raise AnalysisError("Student-t degrees of freedom must be positive")
    if value == 0:
        return 0.5
    x = degrees_of_freedom / (degrees_of_freedom + value * value)
    tail = 0.5 * _regularized_incomplete_beta(
        degrees_of_freedom / 2.0, 0.5, x
    )
    return 1.0 - tail if value > 0 else tail


def _student_t_two_sided_critical(confidence: float, sample_size: int) -> float:
    """Invert the Student-t CDF without adding a statistics dependency."""

    if not 0 < confidence < 1 or sample_size < 2:
        raise AnalysisError("Student-t confidence needs at least two observations")
    target = (1.0 + confidence) / 2.0
    degrees_of_freedom = sample_size - 1
    lower = 0.0
    upper = 1.0
    while _student_t_cdf(upper, degrees_of_freedom) < target:
        upper *= 2.0
    for _ in range(80):
        midpoint = (lower + upper) / 2.0
        if _student_t_cdf(midpoint, degrees_of_freedom) < target:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2.0


@dataclass(frozen=True)
class TrialObservation:
    """One randomized trial arm reduced to a boundary-specific outcome."""

    trial_id: str
    block_id: str
    arm: str
    context: str
    outcome: EnergyOutcome
    # These outcomes are intentionally tri-state.  Missing evidence must not
    # silently become a successful confirmatory check.
    work_complete: bool | None = None
    output_valid: bool | None = None
    slo_met: bool | None = None
    quality_accepted: bool | None = None
    completed_work: float | None = None
    policy: str = "UNSPECIFIED"
    target_nice: int | None = None
    match_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("trial_id", self.trial_id),
            ("block_id", self.block_id),
            ("arm", self.arm),
            ("context", self.context),
            ("policy", self.policy),
        ):
            if not value:
                raise AnalysisError(f"{name} must not be empty")
        if self.completed_work is not None:
            if not math.isfinite(self.completed_work) or self.completed_work < 0:
                raise AnalysisError("completed_work must be finite and non-negative")
        for name in (
            "work_complete",
            "output_valid",
            "slo_met",
            "quality_accepted",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise AnalysisError(f"{name} must be a boolean or None")
        if self.target_nice is not None and (
            isinstance(self.target_nice, bool)
            or not isinstance(self.target_nice, int)
            or not 0 <= self.target_nice <= 19
        ):
            raise AnalysisError("target_nice must be an integer from 0 through 19")
        if self.match_fingerprint is not None and (
            not isinstance(self.match_fingerprint, str)
            or len(self.match_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.match_fingerprint)
        ):
            raise AnalysisError(
                "match_fingerprint must be 64 lowercase hexadecimal characters"
            )


@dataclass(frozen=True)
class PairedEffectEstimate:
    """Mean within-block treatment-minus-control energy contrast."""

    treatment_arm: str
    control_arm: str
    context: str
    policy: str
    estimand: Estimand
    boundary: MeasurementBoundary
    block_ids: tuple[str, ...]
    paired_differences_joules: tuple[float, ...]
    control_mean_joules: float
    mean_difference_joules: float
    standard_error_joules: float | None
    confidence: float
    ci_lower_joules: float | None
    ci_upper_joules: float | None
    standardized_effect: float | None
    claim_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    target_nice_by_block: tuple[tuple[str, int], ...]

    @property
    def sample_size(self) -> int:
        return len(self.paired_differences_joules)

    def to_dict(self) -> dict[str, object]:
        return {
            "contrast": f"{self.treatment_arm}-{self.control_arm}",
            "context": self.context,
            "policy": self.policy,
            "estimand": self.estimand.value,
            "measurement_boundary": self.boundary.to_dict(),
            "block_ids": list(self.block_ids),
            "paired_differences_joules": list(self.paired_differences_joules),
            "control_mean_joules": self.control_mean_joules,
            "mean_difference_joules": self.mean_difference_joules,
            "standard_error_joules": self.standard_error_joules,
            "confidence": self.confidence,
            "ci_lower_joules": self.ci_lower_joules,
            "ci_upper_joules": self.ci_upper_joules,
            "standardized_effect": self.standardized_effect,
            "claim_eligible": self.claim_eligible,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "target_nice_by_block": {
                block_id: target_nice
                for block_id, target_nice in self.target_nice_by_block
            },
        }


def paired_effect(
    observations: Iterable[TrialObservation],
    *,
    treatment_arm: str,
    control_arm: str,
    confidence: float = 0.95,
) -> PairedEffectEstimate:
    """Estimate a randomized within-block energy effect.

    Every block containing either requested arm must contain exactly one of
    each.  This intentionally rejects silently unpaired data rather than
    turning a matched design into an unpaired comparison.
    """

    if treatment_arm == control_arm:
        raise AnalysisError("treatment and control arms must differ")
    if not 0 < confidence < 1:
        raise AnalysisError("confidence must be between zero and one")
    relevant = [
        item for item in observations if item.arm in {treatment_arm, control_arm}
    ]
    if not relevant:
        raise AnalysisError("no observations for the requested contrast")

    by_block: dict[str, dict[str, TrialObservation]] = {}
    for item in relevant:
        arms = by_block.setdefault(item.block_id, {})
        if item.arm in arms:
            raise AnalysisError(
                f"block {item.block_id!r} has duplicate arm {item.arm!r}"
            )
        arms[item.arm] = item
    for block_id, arms in by_block.items():
        missing = {treatment_arm, control_arm} - set(arms)
        if missing:
            raise AnalysisError(
                f"block {block_id!r} is missing paired arm(s): "
                + ", ".join(sorted(missing))
            )

    first = relevant[0]
    boundary = first.outcome.boundary
    estimand = first.outcome.estimand
    context = first.context
    policy = first.policy
    for item in relevant[1:]:
        boundary.assert_compatible(item.outcome.boundary)
        if item.outcome.estimand != estimand:
            raise AnalysisError("paired outcomes must use the same estimand")
        if item.context != context:
            raise AnalysisError("paired outcomes must use the same workload context")
        if item.policy != policy:
            raise AnalysisError("paired outcomes must use the same mechanism policy")

    differences: list[float] = []
    controls: list[float] = []
    reasons: set[str] = set()
    target_nice_by_block: list[tuple[str, int]] = []
    ordered_blocks = tuple(sorted(by_block))
    for block_id in ordered_blocks:
        treatment = by_block[block_id][treatment_arm]
        control = by_block[block_id][control_arm]
        differences.append(
            treatment.outcome.total_joules - control.outcome.total_joules
        )
        controls.append(control.outcome.total_joules)
        if treatment.target_nice is None or control.target_nice is None:
            reasons.add("target priority was not established")
        elif treatment.target_nice != control.target_nice:
            reasons.add("paired arms used different target priorities")
        else:
            target_nice_by_block.append((block_id, treatment.target_nice))
        if (
            treatment.match_fingerprint is None
            or control.match_fingerprint is None
        ):
            reasons.add("matched execution contract was not established")
        elif treatment.match_fingerprint != control.match_fingerprint:
            reasons.add("paired arms used different execution contracts")
        for item in (treatment, control):
            if item.work_complete is not True:
                if item.work_complete is None:
                    reasons.add("work completion was not established")
                else:
                    reasons.add("unfinished work")
            if item.output_valid is not True:
                if item.output_valid is None:
                    reasons.add("output equivalence was not established")
                else:
                    reasons.add("non-equivalent or invalid output")
            if item.slo_met is not True:
                if item.slo_met is None:
                    reasons.add("service-level objective status was not established")
                else:
                    reasons.add("service-level objective violation")
            if item.quality_accepted is not True:
                if item.quality_accepted is None:
                    reasons.add("trace quality was not evaluated")
                else:
                    reasons.add("trace quality was not accepted")
        if estimand == Estimand.EQUAL_WORK:
            if treatment.completed_work is None or control.completed_work is None:
                reasons.add("equal-work outcome has no completed-work count")
            elif not math.isclose(
                treatment.completed_work,
                control.completed_work,
                rel_tol=1e-9,
                abs_tol=1e-12,
            ):
                reasons.add("equal-work arms completed different work")

    mean = statistics.fmean(differences)
    control_mean = statistics.fmean(controls)
    standard_error: float | None = None
    lower: float | None = None
    upper: float | None = None
    standardized: float | None = None
    if len(differences) >= 2:
        sample_deviation = statistics.stdev(differences)
        standard_error = sample_deviation / math.sqrt(len(differences))
        critical = _student_t_two_sided_critical(confidence, len(differences))
        lower = mean - critical * standard_error
        upper = mean + critical * standard_error
        if sample_deviation > 0:
            standardized = mean / sample_deviation

    return PairedEffectEstimate(
        treatment_arm=treatment_arm,
        control_arm=control_arm,
        context=context,
        policy=policy,
        estimand=estimand,
        boundary=boundary,
        block_ids=ordered_blocks,
        paired_differences_joules=tuple(differences),
        control_mean_joules=control_mean,
        mean_difference_joules=mean,
        standard_error_joules=standard_error,
        confidence=confidence,
        ci_lower_joules=lower,
        ci_upper_joules=upper,
        standardized_effect=standardized,
        claim_eligible=not reasons,
        ineligibility_reasons=tuple(sorted(reasons)),
        target_nice_by_block=tuple(target_nice_by_block),
    )


def _linear_quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise AnalysisError("quantile needs at least one value")
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] * (1 - fraction) + ordered[upper_index] * fraction


@dataclass(frozen=True)
class AAThreshold:
    """A/A-calibrated minimum meaningful energy difference."""

    paired_differences_joules: tuple[float, ...]
    control_mean_joules: float
    uncertainty_quantile: float
    uncertainty_joules: float
    relative_floor_fraction: float
    relative_floor_joules: float
    uncertainty_multiplier: float
    meaningful_effect_joules: float

    def to_dict(self) -> dict[str, object]:
        return {
            "paired_differences_joules": list(self.paired_differences_joules),
            "control_mean_joules": self.control_mean_joules,
            "uncertainty_quantile": self.uncertainty_quantile,
            "uncertainty_joules": self.uncertainty_joules,
            "relative_floor_fraction": self.relative_floor_fraction,
            "relative_floor_joules": self.relative_floor_joules,
            "uncertainty_multiplier": self.uncertainty_multiplier,
            "meaningful_effect_joules": self.meaningful_effect_joules,
        }


def calibrate_aa_threshold(
    *,
    aa_paired_differences_joules: Iterable[float],
    control_joules: Iterable[float],
    relative_floor_fraction: float = 0.05,
    uncertainty_multiplier: float = 2.0,
    uncertainty_quantile: float = 0.95,
) -> AAThreshold:
    """Return ``max(relative floor, multiplier * A/A uncertainty)``.

    A/A uncertainty is the empirical quantile of absolute paired sham
    differences.  This remains meaningful for small non-Gaussian meter noise
    samples and directly preserves the plan's sham-versus-sham calibration.
    """

    differences = tuple(float(value) for value in aa_paired_differences_joules)
    controls = tuple(float(value) for value in control_joules)
    if not differences or not controls:
        raise AnalysisError("A/A threshold needs differences and controls")
    if not 0 < uncertainty_quantile < 1:
        raise AnalysisError("uncertainty_quantile must be between zero and one")
    if relative_floor_fraction < 0 or uncertainty_multiplier < 0:
        raise AnalysisError("threshold factors must be non-negative")
    if any(not math.isfinite(value) for value in differences + controls):
        raise AnalysisError("A/A threshold inputs must be finite")
    if any(value < 0 for value in controls):
        raise AnalysisError("control energy must be non-negative")

    control_mean = statistics.fmean(controls)
    uncertainty = _linear_quantile(
        tuple(abs(value) for value in differences), uncertainty_quantile
    )
    relative_floor = relative_floor_fraction * control_mean
    threshold = max(relative_floor, uncertainty_multiplier * uncertainty)
    return AAThreshold(
        paired_differences_joules=differences,
        control_mean_joules=control_mean,
        uncertainty_quantile=uncertainty_quantile,
        uncertainty_joules=uncertainty,
        relative_floor_fraction=relative_floor_fraction,
        relative_floor_joules=relative_floor,
        uncertainty_multiplier=uncertainty_multiplier,
        meaningful_effect_joules=threshold,
    )


@dataclass(frozen=True)
class RankingReversalResult:
    """Decision for the preregistered P1-heavier-to-P1-lighter reversal."""

    supported: bool
    isolated_effect: PairedEffectEstimate
    cotenanted_effect: PairedEffectEstimate
    isolated_threshold_joules: float
    cotenanted_threshold_joules: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "definition": "P1-P2 > 0 when isolated and P1-P2 < 0 when cotenanted",
            "isolated_effect": self.isolated_effect.to_dict(),
            "cotenanted_effect": self.cotenanted_effect.to_dict(),
            "isolated_threshold_joules": self.isolated_threshold_joules,
            "cotenanted_threshold_joules": self.cotenanted_threshold_joules,
            "reasons": list(self.reasons),
        }


def detect_ranking_reversal(
    isolated_p1_minus_p2: PairedEffectEstimate,
    cotenanted_p1_minus_p2: PairedEffectEstimate,
    *,
    isolated_threshold_joules: float,
    cotenanted_threshold_joules: float | None = None,
    require_confidence_interval: bool = True,
) -> RankingReversalResult:
    """Check the plan's directional and minimum-effect reversal criteria."""

    co_threshold = (
        isolated_threshold_joules
        if cotenanted_threshold_joules is None
        else cotenanted_threshold_joules
    )
    if isolated_threshold_joules < 0 or co_threshold < 0:
        raise AnalysisError("meaningful-effect thresholds must be non-negative")
    isolated_p1_minus_p2.boundary.assert_compatible(cotenanted_p1_minus_p2.boundary)
    if isolated_p1_minus_p2.estimand != cotenanted_p1_minus_p2.estimand:
        raise AnalysisError("ranking reversal effects must use the same estimand")
    if (
        isolated_p1_minus_p2.treatment_arm != cotenanted_p1_minus_p2.treatment_arm
        or isolated_p1_minus_p2.control_arm != cotenanted_p1_minus_p2.control_arm
    ):
        raise AnalysisError("ranking reversal effects must use the same contrast")
    if isolated_p1_minus_p2.context == cotenanted_p1_minus_p2.context:
        raise AnalysisError(
            "ranking reversal requires distinct isolated and co-tenanted contexts"
        )
    if isolated_p1_minus_p2.policy != cotenanted_p1_minus_p2.policy:
        raise AnalysisError("ranking reversal effects must use the same host policy")

    reasons: list[str] = []
    if isolated_p1_minus_p2.mean_difference_joules <= isolated_threshold_joules:
        reasons.append("isolated P1-P2 effect does not exceed the positive threshold")
    if cotenanted_p1_minus_p2.mean_difference_joules >= -co_threshold:
        reasons.append("cotenanted P1-P2 effect does not exceed the negative threshold")
    for label, effect in (
        ("isolated", isolated_p1_minus_p2),
        ("cotenanted", cotenanted_p1_minus_p2),
    ):
        if not effect.claim_eligible:
            reasons.append(
                f"{label} contrast is claim-ineligible: "
                + ", ".join(effect.ineligibility_reasons)
            )
    if require_confidence_interval:
        if isolated_p1_minus_p2.ci_lower_joules is None:
            reasons.append("isolated contrast has no confidence interval")
        elif isolated_p1_minus_p2.ci_lower_joules <= 0:
            reasons.append("isolated confidence interval includes zero")
        if cotenanted_p1_minus_p2.ci_upper_joules is None:
            reasons.append("cotenanted contrast has no confidence interval")
        elif cotenanted_p1_minus_p2.ci_upper_joules >= 0:
            reasons.append("cotenanted confidence interval includes zero")

    return RankingReversalResult(
        supported=not reasons,
        isolated_effect=isolated_p1_minus_p2,
        cotenanted_effect=cotenanted_p1_minus_p2,
        isolated_threshold_joules=isolated_threshold_joules,
        cotenanted_threshold_joules=co_threshold,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class DifferenceInDifferencesEstimate:
    """Matched policy-intervention difference-in-differences estimate."""

    treatment_arm: str
    control_arm: str
    baseline_policy: str
    intervention_policy: str
    context: str
    estimand: Estimand
    boundary: MeasurementBoundary
    block_ids: tuple[str, ...]
    baseline_differences_joules: tuple[float, ...]
    intervention_differences_joules: tuple[float, ...]
    per_block_did_joules: tuple[float, ...]
    baseline_mean_difference_joules: float
    intervention_mean_difference_joules: float
    mean_did_joules: float
    standard_error_joules: float | None
    confidence: float
    ci_lower_joules: float | None
    ci_upper_joules: float | None
    claim_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    target_nice_by_block: tuple[tuple[str, int], ...]

    @property
    def sample_size(self) -> int:
        return len(self.per_block_did_joules)

    @property
    def attenuation_joules(self) -> float:
        return abs(self.baseline_mean_difference_joules) - abs(
            self.intervention_mean_difference_joules
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contrast": f"{self.treatment_arm}-{self.control_arm}",
            "baseline_policy": self.baseline_policy,
            "intervention_policy": self.intervention_policy,
            "context": self.context,
            "estimand": self.estimand.value,
            "measurement_boundary": self.boundary.to_dict(),
            "block_ids": list(self.block_ids),
            "baseline_differences_joules": list(self.baseline_differences_joules),
            "intervention_differences_joules": list(
                self.intervention_differences_joules
            ),
            "per_block_did_joules": list(self.per_block_did_joules),
            "baseline_mean_difference_joules": (self.baseline_mean_difference_joules),
            "intervention_mean_difference_joules": (
                self.intervention_mean_difference_joules
            ),
            "mean_did_joules": self.mean_did_joules,
            "standard_error_joules": self.standard_error_joules,
            "confidence": self.confidence,
            "ci_lower_joules": self.ci_lower_joules,
            "ci_upper_joules": self.ci_upper_joules,
            "attenuation_joules": self.attenuation_joules,
            "claim_eligible": self.claim_eligible,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "target_nice_by_block": {
                block_id: target_nice
                for block_id, target_nice in self.target_nice_by_block
            },
        }


def matched_difference_in_differences(
    observations: Iterable[TrialObservation],
    *,
    treatment_arm: str,
    control_arm: str,
    baseline_policy: str,
    intervention_policy: str,
    confidence: float = 0.95,
) -> DifferenceInDifferencesEstimate:
    """Compare the arm contrast under dynamic and intervention policies.

    Each matched block must contain all four arm-by-policy cells.  The result
    is ``(treatment-control)_intervention - (treatment-control)_baseline``.
    Use this once for fixed-frequency blocks and again for restricted-idle
    blocks; the policies are never pooled implicitly.
    """

    if treatment_arm == control_arm:
        raise AnalysisError("treatment and control arms must differ")
    if baseline_policy == intervention_policy:
        raise AnalysisError("baseline and intervention policies must differ")
    if not 0 < confidence < 1:
        raise AnalysisError("confidence must be between zero and one")
    cells = {
        (baseline_policy, treatment_arm),
        (baseline_policy, control_arm),
        (intervention_policy, treatment_arm),
        (intervention_policy, control_arm),
    }
    relevant = [item for item in observations if (item.policy, item.arm) in cells]
    if not relevant:
        raise AnalysisError("no observations for the requested mechanism contrast")

    by_block: dict[str, dict[tuple[str, str], TrialObservation]] = {}
    for item in relevant:
        block = by_block.setdefault(item.block_id, {})
        key = (item.policy, item.arm)
        if key in block:
            raise AnalysisError(
                f"block {item.block_id!r} duplicates policy/arm cell {key!r}"
            )
        block[key] = item
    for block_id, block in by_block.items():
        missing = cells - set(block)
        if missing:
            raise AnalysisError(
                f"block {block_id!r} is missing mechanism cells: {sorted(missing)!r}"
            )

    first = relevant[0]
    boundary = first.outcome.boundary
    estimand = first.outcome.estimand
    context = first.context
    for item in relevant[1:]:
        boundary.assert_compatible(item.outcome.boundary)
        if item.outcome.estimand != estimand:
            raise AnalysisError("mechanism outcomes must use the same estimand")
        if item.context != context:
            raise AnalysisError("mechanism outcomes must use the same context")

    ordered_blocks = tuple(sorted(by_block))
    baseline_differences: list[float] = []
    intervention_differences: list[float] = []
    did_values: list[float] = []
    reasons: set[str] = set()
    target_nice_by_block: list[tuple[str, int]] = []
    for block_id in ordered_blocks:
        block = by_block[block_id]
        baseline_treatment = block[(baseline_policy, treatment_arm)]
        baseline_control = block[(baseline_policy, control_arm)]
        intervention_treatment = block[(intervention_policy, treatment_arm)]
        intervention_control = block[(intervention_policy, control_arm)]
        baseline = (
            baseline_treatment.outcome.total_joules
            - baseline_control.outcome.total_joules
        )
        intervention = (
            intervention_treatment.outcome.total_joules
            - intervention_control.outcome.total_joules
        )
        baseline_differences.append(baseline)
        intervention_differences.append(intervention)
        did_values.append(intervention - baseline)

        block_items = (
            baseline_treatment,
            baseline_control,
            intervention_treatment,
            intervention_control,
        )
        priorities = {item.target_nice for item in block_items}
        if None in priorities:
            reasons.add("target priority was not established")
        elif len(priorities) != 1:
            reasons.add("mechanism cells used different target priorities")
        else:
            target_nice_by_block.append((block_id, int(next(iter(priorities)))))
        fingerprints = {item.match_fingerprint for item in block_items}
        if None in fingerprints:
            reasons.add("matched execution contract was not established")
        elif len(fingerprints) != 1:
            reasons.add("mechanism cells used different execution contracts")
        for item in block_items:
            if item.work_complete is not True:
                if item.work_complete is None:
                    reasons.add("work completion was not established")
                else:
                    reasons.add("unfinished work")
            if item.output_valid is not True:
                if item.output_valid is None:
                    reasons.add("output equivalence was not established")
                else:
                    reasons.add("non-equivalent or invalid output")
            if item.slo_met is not True:
                if item.slo_met is None:
                    reasons.add("service-level objective status was not established")
                else:
                    reasons.add("service-level objective violation")
            if item.quality_accepted is not True:
                if item.quality_accepted is None:
                    reasons.add("trace quality was not evaluated")
                else:
                    reasons.add("trace quality was not accepted")
        if estimand == Estimand.EQUAL_WORK:
            work = [item.completed_work for item in block_items]
            if any(value is None for value in work):
                reasons.add("equal-work mechanism block has no completed-work count")
            else:
                numeric_work = tuple(
                    float(value) for value in work if value is not None
                )
                if any(
                    not math.isclose(
                        numeric_work[0], value, rel_tol=1e-9, abs_tol=1e-12
                    )
                    for value in numeric_work[1:]
                ):
                    reasons.add("equal-work mechanism cells completed different work")

    mean_did = statistics.fmean(did_values)
    standard_error: float | None = None
    lower: float | None = None
    upper: float | None = None
    if len(did_values) >= 2:
        standard_error = statistics.stdev(did_values) / math.sqrt(len(did_values))
        critical = _student_t_two_sided_critical(confidence, len(did_values))
        lower = mean_did - critical * standard_error
        upper = mean_did + critical * standard_error
    return DifferenceInDifferencesEstimate(
        treatment_arm=treatment_arm,
        control_arm=control_arm,
        baseline_policy=baseline_policy,
        intervention_policy=intervention_policy,
        context=context,
        estimand=estimand,
        boundary=boundary,
        block_ids=ordered_blocks,
        baseline_differences_joules=tuple(baseline_differences),
        intervention_differences_joules=tuple(intervention_differences),
        per_block_did_joules=tuple(did_values),
        baseline_mean_difference_joules=statistics.fmean(baseline_differences),
        intervention_mean_difference_joules=statistics.fmean(intervention_differences),
        mean_did_joules=mean_did,
        standard_error_joules=standard_error,
        confidence=confidence,
        ci_lower_joules=lower,
        ci_upper_joules=upper,
        claim_eligible=not reasons,
        ineligibility_reasons=tuple(sorted(reasons)),
        target_nice_by_block=tuple(target_nice_by_block),
    )


@dataclass(frozen=True)
class MechanismAttenuationResult:
    supported: bool
    estimate: DifferenceInDifferencesEstimate
    minimum_attenuation_joules: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "minimum_attenuation_joules": self.minimum_attenuation_joules,
            "estimate": self.estimate.to_dict(),
            "reasons": list(self.reasons),
        }


def assess_mechanism_attenuation(
    estimate: DifferenceInDifferencesEstimate,
    *,
    minimum_attenuation_joules: float = 0.0,
    require_confidence_interval: bool = True,
) -> MechanismAttenuationResult:
    """Require the intervention to move a baseline effect toward zero."""

    if minimum_attenuation_joules < 0:
        raise AnalysisError("minimum attenuation must be non-negative")
    reasons: list[str] = []
    baseline = estimate.baseline_mean_difference_joules
    if baseline == 0:
        reasons.append("baseline arm contrast is zero")
    if estimate.attenuation_joules <= minimum_attenuation_joules:
        reasons.append("intervention does not attenuate the baseline contrast")
    expected_did_sign = -1 if baseline > 0 else 1
    if expected_did_sign * estimate.mean_did_joules <= minimum_attenuation_joules:
        reasons.append("difference-in-differences has the wrong direction")
    if not estimate.claim_eligible:
        reasons.append(
            "mechanism contrast is claim-ineligible: "
            + ", ".join(estimate.ineligibility_reasons)
        )
    if require_confidence_interval:
        if estimate.ci_lower_joules is None or estimate.ci_upper_joules is None:
            reasons.append("mechanism contrast has no confidence interval")
        elif expected_did_sign > 0 and estimate.ci_lower_joules <= 0:
            reasons.append("mechanism confidence interval includes zero")
        elif expected_did_sign < 0 and estimate.ci_upper_joules >= 0:
            reasons.append("mechanism confidence interval includes zero")
    return MechanismAttenuationResult(
        supported=not reasons,
        estimate=estimate,
        minimum_attenuation_joules=minimum_attenuation_joules,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class PlaceboZeroCheck:
    centered_on_zero: bool
    paired_differences_joules: tuple[float, ...]
    mean_difference_joules: float
    meaningful_threshold_joules: float
    confidence: float
    ci_lower_joules: float | None
    ci_upper_joules: float | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "centered_on_zero": self.centered_on_zero,
            "paired_differences_joules": list(self.paired_differences_joules),
            "mean_difference_joules": self.mean_difference_joules,
            "meaningful_threshold_joules": self.meaningful_threshold_joules,
            "confidence": self.confidence,
            "ci_lower_joules": self.ci_lower_joules,
            "ci_upper_joules": self.ci_upper_joules,
            "reasons": list(self.reasons),
        }


def check_placebo_zero(
    paired_differences_joules: Iterable[float],
    *,
    meaningful_threshold_joules: float,
    confidence: float = 0.95,
) -> PlaceboZeroCheck:
    """Check that a sham/placebo contrast is practically and statistically zero."""

    values = tuple(float(value) for value in paired_differences_joules)
    if len(values) < 2:
        raise AnalysisError("placebo check needs at least two paired differences")
    if any(not math.isfinite(value) for value in values):
        raise AnalysisError("placebo differences must be finite")
    if meaningful_threshold_joules < 0 or not math.isclose(
        confidence, 0.95, rel_tol=0.0, abs_tol=1e-12
    ):
        raise AnalysisError(
            "placebo equivalence requires a two-sided 95% confidence interval"
        )
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    critical = _student_t_two_sided_critical(confidence, len(values))
    lower = mean - critical * standard_error
    upper = mean + critical * standard_error
    reasons: list[str] = []
    if lower < -meaningful_threshold_joules or upper > meaningful_threshold_joules:
        reasons.append(
            "placebo 95% confidence interval is not contained in the "
            "meaningful-effect equivalence band"
        )
    return PlaceboZeroCheck(
        centered_on_zero=not reasons,
        paired_differences_joules=values,
        mean_difference_joules=mean,
        meaningful_threshold_joules=meaningful_threshold_joules,
        confidence=confidence,
        ci_lower_joules=lower,
        ci_upper_joules=upper,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class NormalApproximationSampleSize:
    """Confirmatory matched-block count from the paired normal approximation."""

    pilot_blocks: int
    pilot_paired_sd_joules: float
    meaningful_effect_joules: float
    power: float
    two_sided_alpha: float
    required_matched_blocks: int

    def to_dict(self) -> dict[str, object]:
        return {
            "method": "PAIRED_STUDENT_T_APPROXIMATION",
            "pilot_blocks": self.pilot_blocks,
            "pilot_paired_sd_joules": self.pilot_paired_sd_joules,
            "meaningful_effect_joules": self.meaningful_effect_joules,
            "power": self.power,
            "two_sided_alpha": self.two_sided_alpha,
            "required_matched_blocks": self.required_matched_blocks,
        }


def conservative_required_matched_blocks(
    *,
    paired_sd_joules: float,
    meaningful_effect_joules: float,
    power: float,
    two_sided_alpha: float,
) -> int:
    """Iterate Student-t critical values with a 12-block confirmatory floor."""

    if paired_sd_joules < 0 or meaningful_effect_joules <= 0:
        raise AnalysisError("sample-size SD and meaningful effect are invalid")
    required = 12
    for _ in range(100):
        alpha_confidence = 1.0 - two_sided_alpha
        alpha_critical = _student_t_two_sided_critical(
            alpha_confidence, required
        )
        power_critical = _student_t_two_sided_critical(2.0 * power - 1.0, required)
        raw_size = (
            (alpha_critical + power_critical)
            * paired_sd_joules
            / meaningful_effect_joules
        ) ** 2
        updated = max(12, math.ceil(raw_size))
        if updated <= required:
            return required
        required = updated
    raise AnalysisError("Student-t sample-size iteration did not converge")


def paired_normal_sample_size(
    pilot_paired_differences_joules: Iterable[float],
    *,
    aa_threshold: AAThreshold,
    power: float = 0.90,
    two_sided_alpha: float = 0.05,
) -> NormalApproximationSampleSize:
    """Size confirmatory matched blocks from discarded-pilot paired SD.

    Iterates Student-t critical values for the alpha and power quantiles and
    applies a minimum of 12 confirmatory matched blocks.  The pilot is deliberately
    required to contain at least 12 matched blocks, matching the study's
    preregistered discarded-pilot design.  ``aa_threshold`` already implements
    the preregistered max of 5% control energy and twice A/A uncertainty.
    """

    pilot = tuple(float(value) for value in pilot_paired_differences_joules)
    if len(pilot) < 12:
        raise AnalysisError(
            "sample-size pilot needs at least 12 discarded matched blocks"
        )
    if any(not math.isfinite(value) for value in pilot):
        raise AnalysisError("pilot paired differences must be finite")
    if not 0.90 <= power < 1:
        raise AnalysisError("confirmatory target power must be at least 0.90")
    if not 0 < two_sided_alpha <= 0.05:
        raise AnalysisError(
            "confirmatory two-sided alpha must be positive and at most 0.05"
        )
    effect = aa_threshold.meaningful_effect_joules
    if effect <= 0:
        raise AnalysisError("meaningful-effect threshold must be positive")
    deviation = statistics.stdev(pilot)
    required = conservative_required_matched_blocks(
        paired_sd_joules=deviation,
        meaningful_effect_joules=effect,
        power=power,
        two_sided_alpha=two_sided_alpha,
    )
    return NormalApproximationSampleSize(
        pilot_blocks=len(pilot),
        pilot_paired_sd_joules=deviation,
        meaningful_effect_joules=effect,
        power=power,
        two_sided_alpha=two_sided_alpha,
        required_matched_blocks=required,
    )


@dataclass(frozen=True)
class CrossBoundaryReplicationComparison:
    """Sign and standardized-effect comparison without raw-joule pooling."""

    boundary_ids: tuple[str, ...]
    backends: tuple[str, ...]
    effect_directions: tuple[int, ...]
    standardized_effects: tuple[float, ...]
    sign_agreement: bool
    standardized_heterogeneity: float
    heterogeneity_tolerance: float
    standardized_effect_agreement: bool
    all_claim_eligible: bool
    raw_joule_pooling_permitted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "boundary_ids": list(self.boundary_ids),
            "backends": list(self.backends),
            "effect_directions": list(self.effect_directions),
            "standardized_effects": list(self.standardized_effects),
            "sign_agreement": self.sign_agreement,
            "standardized_heterogeneity": self.standardized_heterogeneity,
            "heterogeneity_tolerance": self.heterogeneity_tolerance,
            "standardized_effect_agreement": self.standardized_effect_agreement,
            "all_claim_eligible": self.all_claim_eligible,
            "raw_joule_pooling_permitted": False,
        }


def compare_cross_boundary_replications(
    effects: Iterable[PairedEffectEstimate],
    *,
    standardized_heterogeneity_tolerance: float = 0.5,
) -> CrossBoundaryReplicationComparison:
    """Compare replications using direction and paired standardized effects.

    Raw effect joules are deliberately absent from the result.  Callers that
    need a raw-joule pooled estimate must first use ``assert_compatible`` and a
    separate within-boundary analysis; this comparator never pools them.
    """

    effect_list = tuple(effects)
    if len(effect_list) < 2:
        raise AnalysisError("replication comparison needs at least two effects")
    if standardized_heterogeneity_tolerance < 0:
        raise AnalysisError("heterogeneity tolerance must be non-negative")
    first = effect_list[0]
    if not math.isclose(first.confidence, 0.95, rel_tol=0.0, abs_tol=1e-12):
        raise AnalysisError(
            "replication comparison requires two-sided 95% confidence intervals"
        )
    for effect in effect_list[1:]:
        if (
            effect.treatment_arm != first.treatment_arm
            or effect.control_arm != first.control_arm
        ):
            raise AnalysisError("replications must use the same arm contrast")
        if effect.estimand != first.estimand:
            raise AnalysisError("replications must use the same estimand")
        if effect.context != first.context:
            raise AnalysisError("replications must use the same workload context")
        if effect.policy != first.policy:
            raise AnalysisError("replications must use the same analysis policy")
        if not math.isclose(effect.confidence, 0.95, rel_tol=0.0, abs_tol=1e-12):
            raise AnalysisError(
                "replication comparison requires two-sided 95% confidence intervals"
            )
    if any(effect.standardized_effect is None for effect in effect_list):
        raise AnalysisError(
            "replication comparison needs non-zero paired variance in every effect"
        )
    standardized = tuple(
        float(effect.standardized_effect)
        for effect in effect_list
        if effect.standardized_effect is not None
    )
    directions = tuple(
        1
        if effect.mean_difference_joules > 0
        else -1
        if effect.mean_difference_joules < 0
        else 0
        for effect in effect_list
    )
    all_claim_eligible = all(effect.claim_eligible for effect in effect_list)
    sign_agreement = (
        all_claim_eligible and len(set(directions)) == 1 and directions[0] != 0
    )
    heterogeneity = max(standardized) - min(standardized)
    return CrossBoundaryReplicationComparison(
        boundary_ids=tuple(effect.boundary.boundary_id for effect in effect_list),
        backends=tuple(effect.boundary.backend for effect in effect_list),
        effect_directions=directions,
        standardized_effects=standardized,
        sign_agreement=sign_agreement,
        standardized_heterogeneity=heterogeneity,
        heterogeneity_tolerance=standardized_heterogeneity_tolerance,
        standardized_effect_agreement=(
            sign_agreement and heterogeneity <= standardized_heterogeneity_tolerance
        ),
        all_claim_eligible=all_claim_eligible,
    )


@dataclass(frozen=True)
class StudyAcceptanceResult:
    """Top-level confirmatory acceptance/falsification decision."""

    supported: bool
    fixed_horizon_supported: bool
    equal_work_supported: bool
    placebo_supported: bool
    independent_day_replication_supported: bool
    mechanism_required: bool
    mechanism_supported: bool | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "fixed_horizon_supported": self.fixed_horizon_supported,
            "equal_work_supported": self.equal_work_supported,
            "placebo_supported": self.placebo_supported,
            "independent_day_replication_supported": (
                self.independent_day_replication_supported
            ),
            "mechanism_required": self.mechanism_required,
            "mechanism_supported": self.mechanism_supported,
            "reasons": list(self.reasons),
        }


def evaluate_study_acceptance(
    *,
    fixed_horizon_reversal: RankingReversalResult,
    equal_work_reversal: RankingReversalResult,
    placebo_checks: Iterable[PlaceboZeroCheck],
    independent_day_reversals: Mapping[str, Sequence[RankingReversalResult]],
    mechanism_attenuation: MechanismAttenuationResult | None = None,
    require_mechanism: bool = False,
) -> StudyAcceptanceResult:
    """Apply the plan's confirmatory acceptance gates as one decision.

    Each independent day must contain supported fixed-horizon and equal-work
    reversal results.  Output equivalence, work completion, and SLO checks are
    inherited from each reversal's claim-eligible paired effects.
    """

    fixed = fixed_horizon_reversal
    equal = equal_work_reversal

    def contract(
        reversal: RankingReversalResult, expected_estimand: Estimand
    ) -> tuple[str, str, str, str, str]:
        isolated = reversal.isolated_effect
        cotenanted = reversal.cotenanted_effect
        isolated.boundary.assert_compatible(cotenanted.boundary)
        if (
            isolated.estimand != expected_estimand
            or cotenanted.estimand != expected_estimand
        ):
            raise AnalysisError(
                f"{expected_estimand.value} reversal has the wrong estimand"
            )
        if (
            isolated.treatment_arm,
            isolated.control_arm,
        ) != (cotenanted.treatment_arm, cotenanted.control_arm):
            raise AnalysisError("reversal effects do not use the same arm contrast")
        if isolated.context == cotenanted.context:
            raise AnalysisError(
                "reversal does not distinguish isolated and co-tenanted contexts"
            )
        if isolated.policy != cotenanted.policy:
            raise AnalysisError("reversal effects do not use the same host policy")
        return (
            isolated.treatment_arm,
            isolated.control_arm,
            isolated.context,
            cotenanted.context,
            isolated.policy,
        )

    fixed_contract = contract(fixed, Estimand.FIXED_HORIZON)
    equal_contract = contract(equal, Estimand.EQUAL_WORK)
    fixed.isolated_effect.boundary.assert_compatible(equal.isolated_effect.boundary)
    if fixed_contract != equal_contract:
        raise AnalysisError(
            "fixed-horizon and equal-work reversals must use the same arms, "
            "contexts, and host policy"
        )

    reasons: list[str] = []
    if not fixed.supported:
        reasons.append("fixed-horizon ranking reversal is not supported")
    if not equal.supported:
        reasons.append("equal-work ranking reversal is not supported")
    placebo_list = tuple(placebo_checks)
    placebo_supported = bool(placebo_list) and all(
        check.centered_on_zero for check in placebo_list
    )
    if not placebo_supported:
        reasons.append("placebo estimates are not all centered on zero")

    day_supported = len(independent_day_reversals) >= 2
    if not day_supported:
        reasons.append("fewer than two independent replication days were supplied")
    block_owners: dict[tuple[Estimand, str], str] = {}
    for day, reversals in independent_day_reversals.items():
        if not day.strip():
            raise AnalysisError("independent-day labels must not be empty")
        day_results = tuple(reversals)
        estimands = {result.isolated_effect.estimand for result in day_results}
        if estimands != {Estimand.FIXED_HORIZON, Estimand.EQUAL_WORK}:
            day_supported = False
            reasons.append(
                f"day {day!r} lacks fixed-horizon and equal-work reversal results"
            )
        if any(not result.supported for result in day_results):
            day_supported = False
            reasons.append(f"day {day!r} has an unsupported reversal")
        for result in day_results:
            fixed.isolated_effect.boundary.assert_compatible(
                result.isolated_effect.boundary
            )
            expected_contract = (
                fixed_contract
                if result.isolated_effect.estimand is Estimand.FIXED_HORIZON
                else equal_contract
            )
            if contract(result, result.isolated_effect.estimand) != expected_contract:
                raise AnalysisError(
                    f"day {day!r} reversal does not match the primary study contract"
                )
            for effect in (result.isolated_effect, result.cotenanted_effect):
                for block_id in effect.block_ids:
                    key = (effect.estimand, block_id)
                    owner = block_owners.setdefault(key, day)
                    if owner != day:
                        day_supported = False
                        reasons.append(
                            f"days {owner!r} and {day!r} reuse matched block "
                            f"{block_id!r} for {effect.estimand.value}"
                        )

    mechanism_supported: bool | None = (
        None if mechanism_attenuation is None else mechanism_attenuation.supported
    )
    if mechanism_attenuation is not None:
        mechanism = mechanism_attenuation.estimate
        reference = fixed if mechanism.estimand is Estimand.FIXED_HORIZON else equal
        reference_effect = reference.cotenanted_effect
        reference_effect.boundary.assert_compatible(mechanism.boundary)
        if (
            mechanism.treatment_arm,
            mechanism.control_arm,
        ) != (reference_effect.treatment_arm, reference_effect.control_arm):
            raise AnalysisError("mechanism result uses a different arm contrast")
        if mechanism.context != reference_effect.context:
            raise AnalysisError("mechanism result uses a different workload context")
        if mechanism.baseline_policy != reference_effect.policy:
            raise AnalysisError(
                "mechanism baseline policy does not match the primary reversal"
            )
    if require_mechanism and mechanism_attenuation is None:
        reasons.append("mechanism attenuation is required but missing")
    elif mechanism_attenuation is not None and not mechanism_attenuation.supported:
        reasons.append("mechanism attenuation is not supported")

    # Surface claim-eligibility failures explicitly even when a caller built a
    # RankingReversalResult manually rather than through detect_ranking_reversal.
    for label, reversal in (("fixed-horizon", fixed), ("equal-work", equal)):
        for effect in (reversal.isolated_effect, reversal.cotenanted_effect):
            if not effect.claim_eligible:
                reasons.append(
                    f"{label} result is claim-ineligible: "
                    + ", ".join(effect.ineligibility_reasons)
                )

    return StudyAcceptanceResult(
        supported=not reasons,
        fixed_horizon_supported=fixed.supported,
        equal_work_supported=equal.supported,
        placebo_supported=placebo_supported,
        independent_day_replication_supported=day_supported,
        mechanism_required=require_mechanism,
        mechanism_supported=mechanism_supported,
        reasons=tuple(dict.fromkeys(reasons)),
    )
