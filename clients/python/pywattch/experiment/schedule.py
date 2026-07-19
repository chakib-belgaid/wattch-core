"""Reproducible, position-balanced scheduling for C0/C1/C2 trials."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    EqualWork,
    ExperimentArm,
    ExperimentConfig,
    FixedHorizon,
    MeasurementBoundary,
    MechanismPolicy,
    ObservationRule,
    WorkloadCommand,
)


@dataclass(frozen=True)
class TrialSpec:
    """One concrete arm execution in a randomized matched block."""

    experiment_id: str
    trial_id: str
    block_index: int
    order_position: int
    arm: ExperimentArm
    arm_label: str
    target: WorkloadCommand
    background: WorkloadCommand | None
    target_nice: int
    background_nice: int
    mechanism_policy: MechanismPolicy
    mechanism_position: int
    observation: ObservationRule
    interval_ms: int
    input_id: str
    cpu_affinity: tuple[int, ...]
    resource_limits: dict[str, str]
    labels: dict[str, str]
    random_seed: int
    expected_output_sha256: str | None
    background_workload_id: str
    measurement_boundary: MeasurementBoundary
    confirmatory: bool

    @property
    def run_id(self) -> str:
        return self.trial_id

    def to_dict(self) -> dict[str, Any]:
        """Return the exact Rust `research::TrialMetadata` JSON shape."""

        if self.arm is ExperimentArm.C0:
            arm: dict[str, str] = {"kind": "BACKGROUND_CONTROL"}
        else:
            arm = {"kind": "PROGRAM", "program_id": self.arm_label}
        if isinstance(self.observation, FixedHorizon):
            estimand: dict[str, Any] = {
                "kind": "FIXED_HORIZON",
                "horizon_ns": round(self.observation.duration_s * 1_000_000_000),
            }
        elif isinstance(self.observation, EqualWork):
            estimand = {
                "kind": "EQUAL_WORK",
                "work_amount": float(self.observation.target_work),
                "work_unit": self.observation.work_unit,
            }
        else:  # pragma: no cover - guarded by the config type
            raise TypeError(
                f"unsupported observation {type(self.observation).__name__}"
            )

        resource_limits_document = json.dumps(
            dict(sorted(self.resource_limits.items())),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        labels = {
            **self.labels,
            "arm_code": self.arm.value,
            "order_position": str(self.order_position),
            "target_nice": str(self.target_nice),
            "background_nice": str(self.background_nice),
            "mechanism_policy": self.mechanism_policy.value,
            "mechanism_position": str(self.mechanism_position),
            "interval_ms": str(self.interval_ms),
            "cpu_affinity": ",".join(str(cpu) for cpu in self.cpu_affinity),
            "resource_limits_fingerprint_sha256": hashlib.sha256(
                resource_limits_document.encode("utf-8")
            ).hexdigest(),
            "output_equivalence": (
                "VERIFIED_BY_DIGEST" if self.confirmatory else "UNVERIFIED_EXPLORATORY"
            ),
        }
        if isinstance(self.observation, FixedHorizon):
            labels["pre_window_s"] = repr(float(self.observation.pre_window_s))
            labels["fixed_horizon_tolerance_ns"] = str(
                round(self.observation.tolerance_s * 1_000_000_000)
            )
            labels["workload_mode"] = self.observation.workload_mode.value
            if self.observation.arrival_trace_id is not None:
                labels["arrival_trace_id"] = self.observation.arrival_trace_id
        else:
            labels.update(
                {
                    "workload_mode": self.observation.workload_mode.value,
                    "timeout_s": repr(float(self.observation.timeout_s)),
                    "drain_timeout_s": repr(float(self.observation.drain_timeout_s)),
                    "recovery_tail_s": repr(float(self.observation.recovery_tail_s)),
                }
            )
            if self.observation.arrival_trace_id is not None:
                labels["arrival_trace_id"] = self.observation.arrival_trace_id
        if self.expected_output_sha256 is not None:
            labels["expected_output_sha256"] = self.expected_output_sha256

        return {
            "experiment_id": self.experiment_id,
            "trial_id": self.trial_id,
            "run_id": self.run_id,
            "matched_block_id": (f"{self.experiment_id}-block-{self.block_index:03d}"),
            "arm": arm,
            "background_workload_id": self.background_workload_id,
            "input_id": self.input_id,
            "replay_seed": self.random_seed,
            "estimand": estimand,
            "measurement_boundary": self.measurement_boundary.to_dict(),
            "labels": dict(sorted(labels.items())),
        }

    def to_schedule_dict(self) -> dict[str, Any]:
        """Operational schedule fields kept outside canonical trial.json."""

        return {
            "trial_metadata": self.to_dict(),
            "block_index": self.block_index,
            "order_position": self.order_position,
            "target": self.target.to_dict(),
            "background": None
            if self.background is None
            else self.background.to_dict(),
            "target_nice": self.target_nice,
            "background_nice": self.background_nice,
            "mechanism_policy": self.mechanism_policy.value,
            "mechanism_position": self.mechanism_position,
            "interval_ms": self.interval_ms,
            "cpu_affinity": list(self.cpu_affinity),
            "resource_limits": dict(sorted(self.resource_limits.items())),
        }


@dataclass(frozen=True)
class ExperimentSchedule:
    experiment: ExperimentConfig
    trials: tuple[TrialSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        orders: list[dict[str, Any]] = []
        for block_index in range(1, self.experiment.blocks + 1):
            block = [trial for trial in self.trials if trial.block_index == block_index]
            policy_cells = []
            for policy in self.experiment.mechanism_policies:
                cell = [trial for trial in block if trial.mechanism_policy is policy]
                policy_cells.append(
                    {
                        "mechanism_policy": policy.value,
                        "mechanism_position": cell[0].mechanism_position,
                        "order": [
                            trial.arm.value
                            for trial in sorted(
                                cell, key=lambda item: item.order_position
                            )
                        ],
                    }
                )
            orders.append(
                {
                    "block_index": block_index,
                    "matched_block_id": block[0].to_dict()["matched_block_id"],
                    "replay_seed": block[0].random_seed,
                    "target_nice": block[0].target_nice,
                    "policy_cells": sorted(
                        policy_cells, key=lambda item: item["mechanism_position"]
                    ),
                }
            )
        return {
            "schema_version": 1,
            "experiment": self.experiment.to_dict(),
            "blocks": orders,
            "trials": [trial.to_schedule_dict() for trial in self.trials],
        }

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return destination


def _balanced_arm_orders(
    blocks: int, rng: random.Random
) -> list[tuple[ExperimentArm, ...]]:
    """Return orders balanced at every complete three-block boundary.

    The forward and reverse Latin squares together contain all six possible
    C0/C1/C2 permutations.  Each three-block group places every arm once in
    every position; group and row order are randomized reproducibly.
    """

    forward = [
        (ExperimentArm.C0, ExperimentArm.C1, ExperimentArm.C2),
        (ExperimentArm.C1, ExperimentArm.C2, ExperimentArm.C0),
        (ExperimentArm.C2, ExperimentArm.C0, ExperimentArm.C1),
    ]
    reverse = [
        (ExperimentArm.C0, ExperimentArm.C2, ExperimentArm.C1),
        (ExperimentArm.C2, ExperimentArm.C1, ExperimentArm.C0),
        (ExperimentArm.C1, ExperimentArm.C0, ExperimentArm.C2),
    ]
    orders: list[tuple[ExperimentArm, ...]] = []
    group_index = 0
    while len(orders) < blocks:
        groups = [list(forward), list(reverse)]
        rng.shuffle(groups)
        for group in groups:
            # Rotate the Latin square rather than arbitrarily shuffling rows;
            # this randomizes its starting row while retaining carryover balance.
            offset = rng.randrange(len(group))
            rotated = group[offset:] + group[:offset]
            if group_index % 2:
                rotated.reverse()
            orders.extend(rotated)
            group_index += 1
            if len(orders) >= blocks:
                break
    return orders[:blocks]


def _balanced_priorities(
    blocks: int, levels: tuple[int, ...], rng: random.Random
) -> list[int]:
    priorities: list[int] = []
    while len(priorities) < blocks:
        cycle = list(levels)
        rng.shuffle(cycle)
        priorities.extend(cycle)
    return priorities[:blocks]


def _balanced_policy_orders(
    blocks: int,
    policies: tuple[MechanismPolicy, ...],
    rng: random.Random,
) -> list[tuple[MechanismPolicy, ...]]:
    """Place every policy once per block and balance policy order in cycles."""

    orders: list[tuple[MechanismPolicy, ...]] = []
    while len(orders) < blocks:
        base = list(policies)
        rng.shuffle(base)
        cycle = [tuple(base[offset:] + base[:offset]) for offset in range(len(base))]
        rng.shuffle(cycle)
        orders.extend(cycle)
    return orders[:blocks]


def build_schedule(config: ExperimentConfig) -> ExperimentSchedule:
    """Expand an experiment into deterministic randomized trial specs."""

    rng = random.Random(config.seed)
    # Keep an independent balanced stream for each mechanism policy.  A
    # single stream is balanced globally but can confound policy-specific DiD
    # cells by repeatedly placing one arm in the same launch position.
    arm_orders = {
        policy: iter(_balanced_arm_orders(config.blocks, rng))
        for policy in config.mechanism_policies
    }
    priorities = _balanced_priorities(config.blocks, config.target_nice_levels, rng)
    policy_orders = _balanced_policy_orders(
        config.blocks,
        config.mechanism_policies,
        rng,
    )
    trials: list[TrialSpec] = []

    for block_index, (target_nice, policy_order) in enumerate(
        zip(priorities, policy_orders), start=1
    ):
        block_seed = rng.getrandbits(64)
        for mechanism_position, mechanism_policy in enumerate(policy_order, start=1):
            order = next(arm_orders[mechanism_policy])
            for position, arm in enumerate(order, start=1):
                arm_config = config.arm(arm)
                policy_slug = mechanism_policy.value.lower().replace("_", "-")
                trial_id = (
                    f"{config.experiment_id}-b{block_index:03d}-"
                    f"s{mechanism_position}-m{policy_slug}-"
                    f"p{position}-{arm.value.lower()}-n{target_nice}"
                )
                trials.append(
                    TrialSpec(
                        experiment_id=config.experiment_id,
                        trial_id=trial_id,
                        block_index=block_index,
                        order_position=position,
                        arm=arm,
                        arm_label=arm_config.label,
                        target=arm_config.command,
                        background=config.background,
                        target_nice=target_nice,
                        background_nice=config.background_nice,
                        mechanism_policy=mechanism_policy,
                        mechanism_position=mechanism_position,
                        observation=config.observation,
                        interval_ms=config.interval_ms,
                        input_id=config.input_id,
                        cpu_affinity=config.cpu_affinity,
                        resource_limits=dict(config.resource_limits),
                        labels=dict(config.labels),
                        random_seed=block_seed,
                        expected_output_sha256=arm_config.expected_output_sha256,
                        background_workload_id=config.background_workload_id,
                        measurement_boundary=config.boundary,
                        confirmatory=config.confirmatory,
                    )
                )
    return ExperimentSchedule(experiment=config, trials=tuple(trials))


__all__ = ["ExperimentSchedule", "TrialSpec", "build_schedule"]
