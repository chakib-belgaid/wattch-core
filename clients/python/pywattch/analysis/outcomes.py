"""Boundary-aware energy outcomes computed from immutable raw samples."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .._messages import RawSample, SourceDescriptor
from ..meter import cumulative_delta
from .boundary import (
    INTERVAL_DURATION_SOURCE_UID_LABEL,
    AnalysisError,
    MeasurementBoundary,
)
from ._intervals import synchronize_interval_averages


_ENERGY_TO_JOULES = {"JOULE": 1.0, "MICROJOULE": 1e-6}
_POWER_TO_WATTS = {"WATT": 1.0, "MILLIWATT": 1e-3}


class Estimand(str, Enum):
    """Observation rule used to delimit an energy outcome."""

    FIXED_HORIZON = "FIXED_HORIZON"
    EQUAL_WORK = "EQUAL_WORK"


@dataclass(frozen=True)
class EnergyOutcome:
    """Energy observed inside one declared measurement boundary."""

    boundary: MeasurementBoundary
    estimand: Estimand
    start_monotonic_ns: int
    end_monotonic_ns: int
    per_source_joules: dict[str, float]
    integration_methods: dict[str, str]

    def __post_init__(self) -> None:
        if self.end_monotonic_ns <= self.start_monotonic_ns:
            raise AnalysisError("energy outcome end must be after start")
        expected = set(self.boundary.primary_source_uids)
        if set(self.per_source_joules) != expected:
            raise AnalysisError(
                "energy outcome must contain exactly the boundary's primary sources"
            )
        if set(self.integration_methods) != expected:
            raise AnalysisError(
                "energy outcome must record one method per primary source"
            )
        for uid, joules in self.per_source_joules.items():
            if not math.isfinite(joules) or joules < 0:
                raise AnalysisError(
                    f"energy for source {uid!r} must be finite and non-negative"
                )

    @property
    def duration_s(self) -> float:
        return (self.end_monotonic_ns - self.start_monotonic_ns) / 1e9

    @property
    def total_joules(self) -> float:
        """The declared single source or explicitly disjoint sum."""

        return sum(self.per_source_joules.values())

    @property
    def estimated(self) -> bool:
        return any(
            method in {"TRAPEZOIDAL_POWER", "INTERVAL_AVERAGE_POWER_OVERLAP"}
            for method in self.integration_methods.values()
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "measurement_boundary": self.boundary.to_dict(),
            "estimand": self.estimand.value,
            "start_monotonic_ns": self.start_monotonic_ns,
            "end_monotonic_ns": self.end_monotonic_ns,
            "duration_s": self.duration_s,
            "per_source_joules": dict(self.per_source_joules),
            "total_joules": self.total_joules,
            "integration_methods": dict(self.integration_methods),
            "estimated": self.estimated,
        }


def _numeric_points(
    samples: Iterable[RawSample], *, minimum_points: int = 2
) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    previous_sequence: int | None = None
    previous_ns: int | None = None
    for sample in samples:
        if previous_sequence is not None and sample.sequence_no <= previous_sequence:
            raise AnalysisError("source sample sequence numbers must strictly increase")
        if previous_ns is not None and sample.monotonic_ns <= previous_ns:
            raise AnalysisError("source sample timestamps must strictly increase")
        if isinstance(sample.value, bool):
            raise AnalysisError("boolean raw samples are not numeric telemetry")
        if isinstance(sample.value, bytes):
            raise AnalysisError("byte raw samples are not numeric telemetry")
        value = float(sample.value)
        if not math.isfinite(value):
            raise AnalysisError("source sample values must be finite")
        points.append((sample.monotonic_ns, value))
        previous_sequence = sample.sequence_no
        previous_ns = sample.monotonic_ns
    if len(points) < minimum_points:
        raise AnalysisError(
            f"energy outcome requires at least {minimum_points} sample(s) per source"
        )
    return points


def _interpolate(points: list[tuple[int, float]], at_ns: int) -> float:
    if at_ns < points[0][0] or at_ns > points[-1][0]:
        raise AnalysisError(
            f"source samples do not bracket requested timestamp {at_ns}"
        )
    for index, (timestamp_ns, value) in enumerate(points):
        if timestamp_ns == at_ns:
            return value
        if timestamp_ns > at_ns:
            previous_ns, previous_value = points[index - 1]
            ratio = (at_ns - previous_ns) / (timestamp_ns - previous_ns)
            return previous_value + ratio * (value - previous_value)
    return points[-1][1]


def _clip_points(
    points: list[tuple[int, float]], start_ns: int, end_ns: int
) -> list[tuple[int, float]]:
    clipped = [(start_ns, _interpolate(points, start_ns))]
    clipped.extend((t, value) for t, value in points if start_ns < t < end_ns)
    clipped.append((end_ns, _interpolate(points, end_ns)))
    return clipped


def _cumulative_joules(
    descriptor: SourceDescriptor,
    points: list[tuple[int, float]],
    start_ns: int,
    end_ns: int,
) -> float:
    scale = _ENERGY_TO_JOULES.get(descriptor.unit)
    if scale is None:
        raise AnalysisError(
            f"cumulative source {descriptor.source_uid!r} uses unsupported "
            f"energy unit {descriptor.unit!r}"
        )

    # Unwrap first, then interpolate.  Interpolating the raw values across a
    # rollover would manufacture a large backwards jump.
    unwrapped: list[tuple[int, float]] = [(points[0][0], 0.0)]
    total = 0.0
    for (previous_ns, previous), (timestamp_ns, value) in zip(points, points[1:]):
        del previous_ns  # timestamp retained in the output point below
        try:
            total += cumulative_delta(previous, value, descriptor.max_value)
        except Exception as exc:
            raise AnalysisError(
                f"cannot unwrap cumulative source {descriptor.source_uid!r}: {exc}"
            ) from exc
        unwrapped.append((timestamp_ns, total))
    start_value = _interpolate(unwrapped, start_ns)
    end_value = _interpolate(unwrapped, end_ns)
    joules = (end_value - start_value) * scale
    if joules < 0:
        raise AnalysisError(
            f"cumulative source {descriptor.source_uid!r} produced negative energy"
        )
    return joules


def _instant_power_joules(
    descriptor: SourceDescriptor,
    points: list[tuple[int, float]],
    start_ns: int,
    end_ns: int,
) -> float:
    scale = _POWER_TO_WATTS.get(descriptor.unit)
    if scale is None:
        raise AnalysisError(
            f"instant source {descriptor.source_uid!r} uses unsupported power unit "
            f"{descriptor.unit!r}"
        )
    clipped = _clip_points(points, start_ns, end_ns)
    joules = 0.0
    for (time_0, raw_0), (time_1, raw_1) in zip(clipped, clipped[1:]):
        if raw_0 < 0 or raw_1 < 0:
            raise AnalysisError(
                f"instant power source {descriptor.source_uid!r} is negative"
            )
        watts_0 = raw_0 * scale
        watts_1 = raw_1 * scale
        joules += (watts_0 + watts_1) / 2.0 * (time_1 - time_0) / 1e9
    return joules


def _interval_average_power_joules(
    descriptor: SourceDescriptor,
    power_points: list[tuple[int, float]],
    duration_points: list[tuple[int, float]],
    start_ns: int,
    end_ns: int,
) -> float:
    """Integrate native interval averages without inventing point samples."""

    scale = _POWER_TO_WATTS.get(descriptor.unit)
    if scale is None:
        raise AnalysisError(
            f"interval-average source {descriptor.source_uid!r} uses unsupported "
            f"power unit {descriptor.unit!r}"
        )
    synchronized = synchronize_interval_averages(power_points, duration_points)

    joules = 0.0
    covered_segments: list[tuple[int, int]] = []
    for interval in synchronized.intervals:
        raw_power = interval.value
        if raw_power < 0:
            raise AnalysisError(
                f"interval-average power source {descriptor.source_uid!r} is negative"
            )
        overlap_start_ns = max(start_ns, interval.start_ns)
        overlap_end_ns = min(end_ns, interval.end_ns)
        if overlap_end_ns <= overlap_start_ns:
            continue
        joules += (
            raw_power
            * scale
            * (overlap_end_ns - overlap_start_ns)
            / 1e9
        )
        covered_segments.append((overlap_start_ns, overlap_end_ns))

    if not covered_segments:
        raise AnalysisError(
            f"interval-average source {descriptor.source_uid!r} does not overlap "
            "the requested outcome window"
        )
    covered_until = start_ns
    for segment_start, segment_end in covered_segments:
        if segment_start > covered_until:
            raise AnalysisError(
                f"interval-average source {descriptor.source_uid!r} has an "
                "unmeasured gap inside the requested outcome window"
            )
        if segment_start < covered_until:
            raise AnalysisError(
                f"interval-average source {descriptor.source_uid!r} has "
                "overlapping synchronized native intervals"
            )
        covered_until = segment_end
    if covered_until < end_ns:
        raise AnalysisError(
            f"interval-average source {descriptor.source_uid!r} does not cover "
            "the end of the requested outcome window"
        )
    return joules


def compute_energy_outcome(
    *,
    boundary: MeasurementBoundary,
    descriptors: Iterable[SourceDescriptor],
    samples: Iterable[RawSample],
    start_monotonic_ns: int,
    end_monotonic_ns: int,
    estimand: Estimand | str,
) -> EnergyOutcome:
    """Compute a boundary-aware outcome without interpreting it in a daemon.

    Cumulative energy counters are wrap-corrected and linearly interpolated at
    the requested window. Instant point power remains trapezoidally integrated
    for backward compatibility. Native interval-average power is reconstructed
    on its contiguous native-duration timeline, affinely synchronized to its
    daemon receipt anchors, and multiplied by its exact overlap with the
    requested window; it is never converted into or interpolated as point data.
    The boundary constructor requires matching descriptor disjoint-group
    evidence, unique member identities, and no nested parent/child pair before
    permitting a sum.
    """

    if start_monotonic_ns < 0:
        raise AnalysisError("start_monotonic_ns must be non-negative")
    if end_monotonic_ns <= start_monotonic_ns:
        raise AnalysisError("end_monotonic_ns must be after start_monotonic_ns")
    descriptor_list = tuple(descriptors)
    selected = boundary.validate_descriptors(descriptor_list)
    descriptor_by_uid = {
        descriptor.source_uid: descriptor for descriptor in descriptor_list
    }
    local_ids = [source.local_source_id for source in selected]
    if len(local_ids) != len(set(local_ids)):
        raise AnalysisError("primary sources have duplicate local source ids")

    relevant_ids = set(local_ids)
    for descriptor in selected:
        if descriptor.temporality == "INTERVAL_AVERAGE":
            duration_uid = descriptor.labels[INTERVAL_DURATION_SOURCE_UID_LABEL]
            relevant_ids.add(descriptor_by_uid[duration_uid].local_source_id)
    grouped: dict[int, list[RawSample]] = {
        source_id: [] for source_id in relevant_ids
    }
    for sample in samples:
        if sample.source_id in grouped:
            grouped[sample.source_id].append(sample)

    energy: dict[str, float] = {}
    methods: dict[str, str] = {}
    for descriptor in selected:
        if descriptor.temporality == "CUMULATIVE":
            energy[descriptor.source_uid] = _cumulative_joules(
                descriptor,
                _numeric_points(grouped[descriptor.local_source_id]),
                start_monotonic_ns,
                end_monotonic_ns,
            )
            methods[descriptor.source_uid] = "COUNTER_DELTA"
        elif descriptor.temporality == "INSTANT":
            energy[descriptor.source_uid] = _instant_power_joules(
                descriptor,
                _numeric_points(grouped[descriptor.local_source_id]),
                start_monotonic_ns,
                end_monotonic_ns,
            )
            methods[descriptor.source_uid] = "TRAPEZOIDAL_POWER"
        elif descriptor.temporality == "INTERVAL_AVERAGE":
            duration_uid = descriptor.labels[INTERVAL_DURATION_SOURCE_UID_LABEL]
            duration_descriptor = descriptor_by_uid[duration_uid]
            energy[descriptor.source_uid] = _interval_average_power_joules(
                descriptor,
                _numeric_points(
                    grouped[descriptor.local_source_id], minimum_points=1
                ),
                _numeric_points(
                    grouped[duration_descriptor.local_source_id], minimum_points=1
                ),
                start_monotonic_ns,
                end_monotonic_ns,
            )
            methods[descriptor.source_uid] = "INTERVAL_AVERAGE_POWER_OVERLAP"
        else:
            raise AnalysisError(
                f"primary source {descriptor.source_uid!r} has unsupported "
                f"temporality {descriptor.temporality!r}"
            )

    return EnergyOutcome(
        boundary=boundary,
        estimand=Estimand(estimand),
        start_monotonic_ns=start_monotonic_ns,
        end_monotonic_ns=end_monotonic_ns,
        per_source_joules=energy,
        integration_methods=methods,
    )
