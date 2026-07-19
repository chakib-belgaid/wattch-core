"""Strict, replayable workload-outcome evidence.

The power daemon does not interpret workload behavior.  A target process may
instead write one trial-local JSON sidecar through ``WATTCH_METRICS_PATH``.
Acquisition hashes that file and offline replay reparses it before any causal
claim can use completed-work, output, offered-demand, or SLO fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


WORKLOAD_METRICS_FILENAME = "target.metrics.json"
BACKGROUND_WORKLOAD_METRICS_FILENAME = "background.metrics.json"
WORKLOAD_METRICS_SCHEMA_VERSION = 1
WORKLOAD_METRICS_CONTRACT = "WATTCH_WORKLOAD_METRICS_V1"
MAX_WORKLOAD_METRICS_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WorkloadMetricsError(ValueError):
    """The workload outcome sidecar violates its evidence contract."""


class WorkloadMetricsSource(str, Enum):
    NONE = "NONE"
    EXPLORATORY_PROVIDER = "EXPLORATORY_PROVIDER"
    WORKLOAD_SIDECAR = "WORKLOAD_SIDECAR"


def _finite_non_negative(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise WorkloadMetricsError(f"{name} must be finite and non-negative")
    return float(value)


def _optional_finite_non_negative(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _finite_non_negative(value, name)


@dataclass(frozen=True)
class WorkloadMetrics:
    """Useful-work and service evidence reported by the executed workload."""

    completed_work: float | None = None
    work_unit: str | None = None
    throughput_per_s: float | None = None
    latency_ms: Mapping[str, float] | None = None
    backlog_at_end: float | None = None
    slo_met: bool | None = None
    output_sha256: str | None = None
    arrival_trace_id: str | None = None
    arrival_trace_sha256: str | None = None
    arrival_trace_valid: bool | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "completed_work",
            "throughput_per_s",
            "backlog_at_end",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _finite_non_negative(value, field_name)
        if self.work_unit is not None and (
            not isinstance(self.work_unit, str) or not self.work_unit.strip()
        ):
            raise WorkloadMetricsError("work_unit must not be empty")
        if self.latency_ms is not None:
            copied: dict[str, float] = {}
            for name, value in self.latency_ms.items():
                if not isinstance(name, str) or not name.strip():
                    raise WorkloadMetricsError(
                        "latency_ms names must be non-empty strings"
                    )
                copied[name] = _finite_non_negative(value, f"latency_ms.{name}")
            object.__setattr__(self, "latency_ms", MappingProxyType(copied))
        if self.slo_met is not None and not isinstance(self.slo_met, bool):
            raise WorkloadMetricsError("slo_met must be a boolean or None")
        if self.output_sha256 is not None and (
            not isinstance(self.output_sha256, str)
            or not _SHA256.fullmatch(self.output_sha256)
        ):
            raise WorkloadMetricsError(
                "output_sha256 must be 64 lowercase hexadecimal characters"
            )
        if self.arrival_trace_id is not None and (
            not isinstance(self.arrival_trace_id, str)
            or not self.arrival_trace_id.strip()
        ):
            raise WorkloadMetricsError("arrival_trace_id must not be empty")
        if self.arrival_trace_sha256 is not None and (
            not isinstance(self.arrival_trace_sha256, str)
            or not _SHA256.fullmatch(self.arrival_trace_sha256)
        ):
            raise WorkloadMetricsError(
                "arrival_trace_sha256 must be 64 lowercase hexadecimal characters"
            )
        if self.arrival_trace_valid is not None and not isinstance(
            self.arrival_trace_valid, bool
        ):
            raise WorkloadMetricsError("arrival_trace_valid must be a boolean or None")

    @classmethod
    def from_sidecar_document(cls, document: Mapping[str, Any]) -> "WorkloadMetrics":
        required = {
            "schema_version",
            "completed_work",
            "work_unit",
            "throughput_per_s",
            "latency_ms",
            "backlog_at_end",
            "slo_met",
            "output_sha256",
        }
        missing = sorted(required - set(document))
        if missing:
            raise WorkloadMetricsError(
                "workload metrics sidecar omits fields: " + ", ".join(missing)
            )
        schema_version = document.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or schema_version != WORKLOAD_METRICS_SCHEMA_VERSION
        ):
            raise WorkloadMetricsError(
                "workload metrics sidecar uses an unsupported schema_version"
            )
        latency = document.get("latency_ms")
        if latency is not None and not isinstance(latency, Mapping):
            raise WorkloadMetricsError("latency_ms must be an object or null")
        return cls(
            completed_work=_optional_finite_non_negative(
                document.get("completed_work"), "completed_work"
            ),
            work_unit=document.get("work_unit"),
            throughput_per_s=_optional_finite_non_negative(
                document.get("throughput_per_s"), "throughput_per_s"
            ),
            latency_ms=latency,
            backlog_at_end=_optional_finite_non_negative(
                document.get("backlog_at_end"), "backlog_at_end"
            ),
            slo_met=document.get("slo_met"),
            output_sha256=document.get("output_sha256"),
            arrival_trace_id=document.get("arrival_trace_id"),
            arrival_trace_sha256=document.get("arrival_trace_sha256"),
            arrival_trace_valid=document.get("arrival_trace_valid"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_work": self.completed_work,
            "work_unit": self.work_unit,
            "throughput_per_s": self.throughput_per_s,
            "latency_ms": (
                None
                if self.latency_ms is None
                else dict(sorted(self.latency_ms.items()))
            ),
            "backlog_at_end": self.backlog_at_end,
            "slo_met": self.slo_met,
            "output_sha256": self.output_sha256,
            "arrival_trace_id": self.arrival_trace_id,
            "arrival_trace_sha256": self.arrival_trace_sha256,
            "arrival_trace_valid": self.arrival_trace_valid,
        }


@dataclass(frozen=True)
class WorkloadMetricsEvidence:
    """Acquisition-time provenance for workload metrics."""

    source: WorkloadMetricsSource
    metrics: WorkloadMetrics
    relative_path: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, WorkloadMetricsSource):
            raise WorkloadMetricsError("workload metrics source is invalid")
        if self.source is WorkloadMetricsSource.WORKLOAD_SIDECAR:
            if self.relative_path not in {
                WORKLOAD_METRICS_FILENAME,
                BACKGROUND_WORKLOAD_METRICS_FILENAME,
            }:
                raise WorkloadMetricsError(
                    "verified workload metrics must use a canonical trial-local path"
                )
            if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
                raise WorkloadMetricsError(
                    "verified workload metrics require a valid sidecar SHA-256"
                )
            if self.metrics.output_sha256 is None:
                raise WorkloadMetricsError(
                    "verified workload metrics require output_sha256"
                )
        elif self.relative_path is not None or self.sha256 is not None:
            raise WorkloadMetricsError(
                "non-sidecar workload metrics cannot claim file provenance"
            )

    @classmethod
    def none(cls) -> "WorkloadMetricsEvidence":
        return cls(WorkloadMetricsSource.NONE, WorkloadMetrics())

    @classmethod
    def exploratory(cls, metrics: WorkloadMetrics) -> "WorkloadMetricsEvidence":
        return cls(WorkloadMetricsSource.EXPLORATORY_PROVIDER, metrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": WORKLOAD_METRICS_CONTRACT,
            "source": self.source.value,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "metrics": self.metrics.to_dict(),
        }


def _strict_json_object(contents: bytes) -> Mapping[str, Any]:
    if not contents or len(contents) > MAX_WORKLOAD_METRICS_BYTES:
        raise WorkloadMetricsError(
            "workload metrics sidecar must contain 1 through "
            f"{MAX_WORKLOAD_METRICS_BYTES} bytes"
        )

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise WorkloadMetricsError(
                    f"workload metrics sidecar repeats field {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise WorkloadMetricsError(
            f"workload metrics sidecar contains non-finite value {value}"
        )

    try:
        document = json.loads(
            contents.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkloadMetricsError(
            f"workload metrics sidecar is not strict UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(document, Mapping):
        raise WorkloadMetricsError("workload metrics sidecar must contain one object")
    return document


def load_workload_metrics_sidecar(
    path: Path,
    *,
    expected_stdout_sha256: str,
) -> tuple[WorkloadMetrics, str]:
    """Reopen and validate the exact workload-written sidecar bytes."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WorkloadMetricsError(
            f"cannot stat workload metrics sidecar: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WorkloadMetricsError(
            "workload metrics sidecar must be a regular non-symlink file"
        )
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise WorkloadMetricsError(
            f"cannot read workload metrics sidecar: {exc}"
        ) from exc
    document = _strict_json_object(contents)
    metrics = WorkloadMetrics.from_sidecar_document(document)
    if metrics.output_sha256 != expected_stdout_sha256:
        raise WorkloadMetricsError(
            "workload metrics output_sha256 does not match captured target stdout"
        )
    return metrics, hashlib.sha256(contents).hexdigest()


__all__ = [
    "BACKGROUND_WORKLOAD_METRICS_FILENAME",
    "MAX_WORKLOAD_METRICS_BYTES",
    "WORKLOAD_METRICS_CONTRACT",
    "WORKLOAD_METRICS_FILENAME",
    "WORKLOAD_METRICS_SCHEMA_VERSION",
    "WorkloadMetrics",
    "WorkloadMetricsError",
    "WorkloadMetricsEvidence",
    "WorkloadMetricsSource",
    "load_workload_metrics_sidecar",
]
