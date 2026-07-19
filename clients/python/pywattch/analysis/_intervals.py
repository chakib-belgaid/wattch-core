"""Offline synchronization for native interval-average telemetry.

Collectors such as Apple ``powermetrics`` report an average and a native
elapsed duration for each frame.  Wattch preserves the frame's daemon receipt
timestamp as a synchronization anchor.  Receipt latency is not part of the
native averaging interval, so independently subtracting every duration from
every receipt would manufacture tiny gaps or overlaps whenever pipe delivery
jitters.

This module keeps acquisition evidence untouched.  It reconstructs the
contiguous native elapsed-time axis offline, fits one affine mapping from that
axis to all daemon receipt anchors, and rejects mappings whose drift or
residuals are too large to support a defensible alignment.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

from .boundary import AnalysisError


# The collector and daemon clocks are on one host. A two-percent scale error is
# far larger than ordinary oscillator drift while still allowing a short trace
# to absorb bounded receipt scheduling jitter. Large scale disagreement is
# usually a dropped native frame or a broken duration interpretation.
MAX_AFFINE_DRIFT_PPM = 20_000.0

# Individual receipt anchors may jitter. Permit at most five percent of the
# median native interval, with practical lower/upper bounds. These constants
# are intentionally explicit and versioned in the reconstruction method name;
# calibration can reject a backend/interval before confirmatory use even when
# a trace remains inside this structural validity envelope.
MAX_RESIDUAL_FRACTION = 0.05
MIN_MAX_RESIDUAL_NS = 2_000_000.0
MAX_MAX_RESIDUAL_NS = 50_000_000.0
AFFINE_RECONSTRUCTION_METHOD = "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"


@dataclass(frozen=True)
class SynchronizedInterval:
    """One native interval mapped onto the daemon monotonic clock."""

    start_ns: int
    end_ns: int
    receipt_ns: int
    duration_ns: int
    value: float


@dataclass(frozen=True)
class IntervalSynchronization:
    """Mapped intervals plus auditable affine synchronization diagnostics."""

    intervals: tuple[SynchronizedInterval, ...]
    slope: float
    intercept_ns: float
    max_abs_residual_ns: float
    allowed_max_residual_ns: float
    drift_ppm: float
    method: str = AFFINE_RECONSTRUCTION_METHOD


def _positive_integer_duration(raw: float) -> int:
    if not math.isfinite(raw) or raw <= 0 or not raw.is_integer():
        raise AnalysisError(
            "interval duration must be a positive integer number of nanoseconds"
        )
    return int(raw)


def synchronize_interval_averages(
    power_points: Sequence[tuple[int, float]],
    duration_points: Sequence[tuple[int, float]],
) -> IntervalSynchronization:
    """Map contiguous native intervals to daemon receipt time.

    ``power_points`` and ``duration_points`` must contain the same frames. Their
    timestamps are the raw daemon receipt anchors; durations remain the native
    collector evidence. For two or more frames, ordinary least squares maps
    cumulative native interval ends to receipt time. A single frame retains the
    historical unit-slope interpretation and ends exactly at its receipt.
    """

    if not power_points:
        raise AnalysisError("interval-average synchronization needs at least one frame")
    power_receipts = tuple(timestamp for timestamp, _ in power_points)
    duration_receipts = tuple(timestamp for timestamp, _ in duration_points)
    if power_receipts != duration_receipts:
        raise AnalysisError(
            "interval-average power needs same-frame duration evidence at every receipt"
        )
    if any(
        right <= left for left, right in zip(power_receipts, power_receipts[1:])
    ):
        raise AnalysisError(
            "interval-average receipt timestamps must strictly increase"
        )

    durations = tuple(_positive_integer_duration(raw) for _, raw in duration_points)
    values = tuple(float(raw) for _, raw in power_points)
    if any(not math.isfinite(value) for value in values):
        raise AnalysisError("interval-average values must be finite")

    native_ends: list[int] = []
    elapsed = 0
    for duration in durations:
        elapsed += duration
        native_ends.append(elapsed)

    if len(native_ends) == 1:
        slope = 1.0
        intercept = float(power_receipts[0] - native_ends[0])
        residuals = (0.0,)
    else:
        mean_native = statistics.fmean(native_ends)
        mean_receipt = statistics.fmean(power_receipts)
        denominator = sum((value - mean_native) ** 2 for value in native_ends)
        if denominator <= 0:
            raise AnalysisError("native interval timeline has no synchronization span")
        slope = sum(
            (native - mean_native) * (receipt - mean_receipt)
            for native, receipt in zip(native_ends, power_receipts)
        ) / denominator
        intercept = mean_receipt - slope * mean_native
        residuals = tuple(
            receipt - (intercept + slope * native)
            for native, receipt in zip(native_ends, power_receipts)
        )

    if not math.isfinite(slope) or slope <= 0 or not math.isfinite(intercept):
        raise AnalysisError("interval-average affine synchronization is invalid")
    drift_ppm = abs(slope - 1.0) * 1_000_000.0
    if drift_ppm > MAX_AFFINE_DRIFT_PPM:
        raise AnalysisError(
            "interval-average affine synchronization drift "
            f"{drift_ppm:.1f} ppm exceeds {MAX_AFFINE_DRIFT_PPM:.1f} ppm"
        )

    median_duration = float(statistics.median(durations))
    allowed_residual = min(
        MAX_MAX_RESIDUAL_NS,
        max(MIN_MAX_RESIDUAL_NS, median_duration * MAX_RESIDUAL_FRACTION),
    )
    max_abs_residual = max(abs(value) for value in residuals)
    if max_abs_residual > allowed_residual:
        raise AnalysisError(
            "interval-average affine synchronization receipt residual "
            f"{max_abs_residual:.0f} ns exceeds {allowed_residual:.0f} ns"
        )

    # Round each shared native boundary once. Adjacent intervals reuse the same
    # integer endpoint, so rounding can never introduce a gap or double count.
    native_boundaries = (0, *native_ends)
    mapped_boundaries = tuple(
        round(intercept + slope * boundary) for boundary in native_boundaries
    )
    if any(
        right <= left
        for left, right in zip(mapped_boundaries, mapped_boundaries[1:])
    ):
        raise AnalysisError(
            "interval-average affine synchronization collapsed an interval"
        )

    intervals = tuple(
        SynchronizedInterval(
            start_ns=mapped_boundaries[index],
            end_ns=mapped_boundaries[index + 1],
            receipt_ns=power_receipts[index],
            duration_ns=durations[index],
            value=values[index],
        )
        for index in range(len(values))
    )
    return IntervalSynchronization(
        intervals=intervals,
        slope=slope,
        intercept_ns=intercept,
        max_abs_residual_ns=max_abs_residual,
        allowed_max_residual_ns=allowed_residual,
        drift_ppm=drift_ppm,
    )
