"""Decision-complete configuration for matched Wattch experiments.

The types in this module describe experimental intent only.  They do not
integrate samples, attribute energy to a process, or interpret an effect.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..analysis.boundary import (
    AggregationRule,
    BoundarySourceSignature,
    DISJOINT_GROUP_LABEL,
    DISJOINT_MEMBER_LABEL,
    MeasurementBoundary,
)

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ExperimentArm(str, Enum):
    """The three arms in one matched block."""

    C0 = "C0"
    C1 = "C1"
    C2 = "C2"


class WorkloadMode(str, Enum):
    BATCH = "BATCH"
    SERVICE = "SERVICE"


class MechanismPolicy(str, Enum):
    """Host power-state policy declared for one matched block.

    The experiment runner never implements these privileged settings itself.
    Non-default policies require an injected host-policy controller that
    applies and verifies the requested state before acquisition starts.
    """

    NORMAL_DYNAMIC = "NORMAL_DYNAMIC"
    FIXED_NON_TURBO = "FIXED_NON_TURBO"
    RESTRICTED_DEEP_IDLE = "RESTRICTED_DEEP_IDLE"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_finite_non_negative(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")


def _freeze_string_mapping(
    value: Mapping[str, str], field_name: str
) -> Mapping[str, str]:
    copied: dict[str, str] = {}
    for key, item in value.items():
        _require_text(key, f"{field_name} key")
        if not isinstance(item, str):
            raise ValueError(f"{field_name} values must be strings")
        copied[key] = item
    return MappingProxyType(copied)


def _portable_id(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if len(value.encode("utf-8")) > 128 or not _PORTABLE_ID.fullmatch(value):
        raise ValueError(
            f"{field_name} must be at most 128 UTF-8 bytes and contain only "
            "letters, digits, '.', '_' or '-'"
        )


@dataclass(frozen=True)
class WorkloadCommand:
    """A shell-free command declaration suitable for reproducible trials."""

    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("workload argv must not be empty")
        for argument in self.argv:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError("workload argv entries must be NUL-free strings")
        if not self.argv[0]:
            raise ValueError("workload executable must not be empty")
        if self.cwd is not None:
            _require_text(self.cwd, "workload cwd")
            if "\x00" in self.cwd:
                raise ValueError("workload cwd must not contain NUL")
        object.__setattr__(self, "env", _freeze_string_mapping(self.env, "env"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "env": dict(sorted(self.env.items())),
        }


@dataclass(frozen=True)
class ArmConfig:
    """The command and equivalence metadata for one matched arm."""

    arm: ExperimentArm
    label: str
    command: WorkloadCommand
    expected_output_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.label, "arm label")
        if self.expected_output_sha256 is not None:
            digest = self.expected_output_sha256
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(
                    "expected_output_sha256 must be 64 lowercase hex digits"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm.value,
            "label": self.label,
            "command": self.command.to_dict(),
            "expected_output_sha256": self.expected_output_sha256,
        }


@dataclass(frozen=True)
class FixedHorizon:
    """Observe every arm for the same target-launch-to-recovery duration."""

    duration_s: float
    pre_window_s: float = 0.0
    workload_mode: WorkloadMode = WorkloadMode.BATCH
    arrival_trace_id: str | None = None
    tolerance_s: float = 0.0001

    def __post_init__(self) -> None:
        _require_finite_non_negative(self.duration_s, "duration_s")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if round(self.duration_s * 1_000_000_000) <= 0:
            raise ValueError("duration_s must resolve to at least one nanosecond")
        _require_finite_non_negative(self.pre_window_s, "pre_window_s")
        _require_finite_non_negative(self.tolerance_s, "tolerance_s")
        if self.tolerance_s <= 0:
            raise ValueError("tolerance_s must be positive")
        tolerance_ns = round(self.tolerance_s * 1_000_000_000)
        if tolerance_ns <= 0:
            raise ValueError("tolerance_s must resolve to at least one nanosecond")
        horizon_ns = round(self.duration_s * 1_000_000_000)
        maximum_tolerance_ns = max(1, math.floor(horizon_ns * 0.01))
        if tolerance_ns > maximum_tolerance_ns:
            raise ValueError(
                "tolerance_s must not exceed 1% of the fixed horizon "
                "(subject to one-nanosecond resolution)"
            )
        if not isinstance(self.workload_mode, WorkloadMode):
            raise ValueError("workload_mode must be a WorkloadMode")
        if self.workload_mode is WorkloadMode.SERVICE:
            if self.arrival_trace_id is None:
                raise ValueError(
                    "service fixed-horizon trials require arrival_trace_id"
                )
            _require_text(self.arrival_trace_id, "arrival_trace_id")
        elif self.arrival_trace_id is not None:
            _require_text(self.arrival_trace_id, "arrival_trace_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimand": "FIXED_HORIZON",
            "duration_s": float(self.duration_s),
            "pre_window_s": float(self.pre_window_s),
            "tolerance_s": float(self.tolerance_s),
            "workload_mode": self.workload_mode.value,
            "arrival_trace_id": self.arrival_trace_id,
        }


@dataclass(frozen=True)
class EqualWork:
    """Run to declared equal work, then drain and observe recovery."""

    target_work: float
    work_unit: str
    workload_mode: WorkloadMode
    timeout_s: float
    drain_timeout_s: float = 0.0
    recovery_tail_s: float = 0.0
    arrival_trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_finite_non_negative(self.target_work, "target_work")
        if self.target_work <= 0:
            raise ValueError("target_work must be positive")
        _require_text(self.work_unit, "work_unit")
        _require_finite_non_negative(self.timeout_s, "timeout_s")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        _require_finite_non_negative(self.drain_timeout_s, "drain_timeout_s")
        _require_finite_non_negative(self.recovery_tail_s, "recovery_tail_s")
        if self.workload_mode is WorkloadMode.SERVICE:
            if self.arrival_trace_id is None:
                raise ValueError("service equal-work trials require arrival_trace_id")
            _require_text(self.arrival_trace_id, "arrival_trace_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimand": "EQUAL_WORK",
            "target_work": float(self.target_work),
            "work_unit": self.work_unit,
            "workload_mode": self.workload_mode.value,
            "timeout_s": float(self.timeout_s),
            "drain_timeout_s": float(self.drain_timeout_s),
            "recovery_tail_s": float(self.recovery_tail_s),
            "arrival_trace_id": self.arrival_trace_id,
        }


ObservationRule = FixedHorizon | EqualWork


@dataclass(frozen=True)
class ExperimentConfig:
    """Configuration shared by every trial in a randomized matched study."""

    experiment_id: str
    boundary: MeasurementBoundary
    observation: ObservationRule
    c0: ArmConfig
    c1: ArmConfig
    c2: ArmConfig
    blocks: int = 12
    seed: int = 0
    interval_ms: int = 50
    trial_washout_s: float = 0.0
    block_washout_s: float = 0.0
    background: WorkloadCommand | None = None
    background_workload_id: str = "idle"
    background_nice: int = 0
    target_nice_levels: tuple[int, ...] = (0,)
    mechanism_policies: tuple[MechanismPolicy, ...] = (MechanismPolicy.NORMAL_DYNAMIC,)
    input_id: str = "default-input"
    confirmatory: bool = False
    cpu_affinity: tuple[int, ...] = ()
    resource_limits: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _portable_id(self.experiment_id, "experiment_id")
        if not isinstance(self.boundary, MeasurementBoundary):
            raise ValueError("boundary must be a canonical MeasurementBoundary")
        if not isinstance(self.observation, (FixedHorizon, EqualWork)):
            raise ValueError("observation must be FixedHorizon or EqualWork")
        expected_arms = (
            (self.c0, ExperimentArm.C0),
            (self.c1, ExperimentArm.C1),
            (self.c2, ExperimentArm.C2),
        )
        for arm_config, expected in expected_arms:
            if arm_config.arm is not expected:
                raise ValueError(
                    f"{expected.value.lower()} must configure arm {expected.value}"
                )
        if isinstance(self.blocks, bool) or not isinstance(self.blocks, int):
            raise ValueError("blocks must be a positive integer")
        if self.blocks <= 0:
            raise ValueError("blocks must be a positive integer")
        if isinstance(self.interval_ms, bool) or not isinstance(self.interval_ms, int):
            raise ValueError("interval_ms must be a positive integer")
        if self.interval_ms <= 0:
            raise ValueError("interval_ms must be a positive integer")
        _require_finite_non_negative(self.trial_washout_s, "trial_washout_s")
        _require_finite_non_negative(self.block_washout_s, "block_washout_s")
        if (
            isinstance(self.seed, bool)
            or not isinstance(self.seed, int)
            or not 0 <= self.seed < 2**64
        ):
            raise ValueError("seed must be an unsigned 64-bit integer")
        _require_text(self.background_workload_id, "background_workload_id")
        self._validate_nice(self.background_nice, "background_nice")
        if not self.target_nice_levels:
            raise ValueError("target_nice_levels must not be empty")
        for nice in self.target_nice_levels:
            self._validate_nice(nice, "target_nice_level")
        if len(set(self.target_nice_levels)) != len(self.target_nice_levels):
            raise ValueError("target_nice_levels must not contain duplicates")
        if not self.mechanism_policies:
            raise ValueError("mechanism_policies must not be empty")
        for policy in self.mechanism_policies:
            if not isinstance(policy, MechanismPolicy):
                raise ValueError(
                    "mechanism_policies entries must be MechanismPolicy values"
                )
        if len(set(self.mechanism_policies)) != len(self.mechanism_policies):
            raise ValueError("mechanism_policies must not contain duplicates")
        _require_text(self.input_id, "input_id")
        if not isinstance(self.confirmatory, bool):
            raise ValueError("confirmatory must be a boolean")
        if self.confirmatory:
            c1_digest = self.c1.expected_output_sha256
            c2_digest = self.c2.expected_output_sha256
            if c1_digest is None or c1_digest != c2_digest:
                raise ValueError(
                    "confirmatory experiments require C1 and C2 to share the "
                    "same non-null expected_output_sha256"
                )
        for cpu in self.cpu_affinity:
            if isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0:
                raise ValueError("cpu_affinity entries must be non-negative integers")
        if len(set(self.cpu_affinity)) != len(self.cpu_affinity):
            raise ValueError("cpu_affinity must not contain duplicates")
        object.__setattr__(
            self,
            "resource_limits",
            _freeze_string_mapping(self.resource_limits, "resource_limits"),
        )
        object.__setattr__(
            self, "labels", _freeze_string_mapping(self.labels, "labels")
        )

    @staticmethod
    def _validate_nice(value: int, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 19
        ):
            raise ValueError(f"{field_name} must be an integer from 0 through 19")

    @property
    def arms(self) -> tuple[ArmConfig, ArmConfig, ArmConfig]:
        return self.c0, self.c1, self.c2

    def arm(self, arm: ExperimentArm) -> ArmConfig:
        return {
            ExperimentArm.C0: self.c0,
            ExperimentArm.C1: self.c1,
            ExperimentArm.C2: self.c2,
        }[arm]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "measurement_boundary": self.boundary.to_dict(),
            "observation": self.observation.to_dict(),
            "arms": [arm.to_dict() for arm in self.arms],
            "blocks": self.blocks,
            "seed": self.seed,
            "interval_ms": self.interval_ms,
            "trial_washout_s": float(self.trial_washout_s),
            "block_washout_s": float(self.block_washout_s),
            "background": None
            if self.background is None
            else self.background.to_dict(),
            "background_workload_id": self.background_workload_id,
            "background_nice": self.background_nice,
            "target_nice_levels": list(self.target_nice_levels),
            "mechanism_policies": [policy.value for policy in self.mechanism_policies],
            "input_id": self.input_id,
            "confirmatory": self.confirmatory,
            "output_equivalence": (
                "VERIFIED_BY_DIGEST" if self.confirmatory else "UNVERIFIED_EXPLORATORY"
            ),
            "cpu_affinity": list(self.cpu_affinity),
            "resource_limits": dict(sorted(self.resource_limits.items())),
            "labels": dict(sorted(self.labels.items())),
        }


def command_from_sequence(
    argv: Sequence[str], cwd: Path | str | None = None
) -> WorkloadCommand:
    """Convenience helper that still preserves the shell-free command contract."""

    return WorkloadCommand(
        argv=tuple(argv),
        cwd=None if cwd is None else str(cwd),
    )


__all__ = [
    "AggregationRule",
    "ArmConfig",
    "EqualWork",
    "ExperimentArm",
    "ExperimentConfig",
    "FixedHorizon",
    "MeasurementBoundary",
    "MechanismPolicy",
    "ObservationRule",
    "BoundarySourceSignature",
    "DISJOINT_GROUP_LABEL",
    "DISJOINT_MEMBER_LABEL",
    "WorkloadCommand",
    "WorkloadMode",
    "command_from_sequence",
]
