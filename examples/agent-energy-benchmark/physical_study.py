#!/usr/bin/env python3
"""Prepare and run the physical follow-up for the four frozen submissions.

This coordinator remains outside the daemon.  It freezes candidate bytes and a
balanced schedule, executes the common amplified workload through ``wattch
run``, derives quality metadata offline, and reports only the selected local
source boundary.  It never claims to observe remote model inference.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import random
import signal
import shutil
import statistics
import subprocess
import sys
import time
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "clients" / "python"))
sys.path.insert(
    0,
    str(REPO_ROOT / "skills" / "wattch-energy-optimize" / "scripts"),
)

import coordinator  # noqa: E402
from evidence_contract import classify_source  # noqa: E402
from judge import expected_output, sha256_bytes  # noqa: E402
from physical_workload import aggregate_result  # noqa: E402
from pywattch._frames import FrameKind  # noqa: E402
from pywattch._messages import SourceDescriptor  # noqa: E402
from pywattch.analysis.boundary import MeasurementBoundary  # noqa: E402
from pywattch.analysis.calibration import (  # noqa: E402
    build_trial_quality_metadata,
    summarize_source_quality,
)
from pywattch.analysis.replay import _read_trace  # noqa: E402
from pywattch.client import WattchClient  # noqa: E402
from pywattch.errors import WattchError  # noqa: E402
from summarize_run import select_window  # noqa: E402


SCHEMA_VERSION = 1
PLAN_KIND = "wattch-agent-physical-study-plan"
RESULT_KIND = "wattch-agent-physical-study-result"
CANONICAL_CONDITIONS = (
    "luna-control",
    "luna-skill",
    "terra-control",
    "terra-skill",
)
WILLIAMS_ROWS = (
    (0, 1, 3, 2),
    (1, 2, 0, 3),
    (2, 3, 1, 0),
    (3, 0, 2, 1),
)
DEFAULT_INTERVAL_MS = 250
DEFAULT_ITERATIONS = 48
DEFAULT_BLOCKS = 8
DEFAULT_COOLDOWN_S = 5.0
DEFAULT_MIN_DURATION_S = 5.0
DEFAULT_MIN_SAMPLES = 20
DEFAULT_TARGET_TIMEOUT_MS = 120_000
PHYSICAL_BOUNDARY_ID = "agent-energy-apple-cpu"
NO_FORK_SANDBOX_PROFILE = "(version 1) (allow default) (deny process-fork)"
NO_FORK_SANDBOX_PROBE = (
    "import errno, subprocess, sys\n"
    "try:\n"
    " subprocess.run([sys.executable, '-c', 'pass'], check=False)\n"
    "except OSError as error:\n"
    " raise SystemExit(0 if error.errno == errno.EPERM else 2)\n"
    "raise SystemExit(1)\n"
)
CANONICAL_CONDITION_METADATA = {
    "luna-control": {
        "id": "luna-control",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "skill_enabled": False,
    },
    "luna-skill": {
        "id": "luna-skill",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "skill_enabled": True,
    },
    "terra-control": {
        "id": "terra-control",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "skill_enabled": False,
    },
    "terra-skill": {
        "id": "terra-skill",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "skill_enabled": True,
    },
}
EXPLORATORY_CALIBRATION_FIELDS = {
    "schema_version",
    "kind",
    "calibration_id",
    "measurement_boundary",
    "measurement_boundary_sha256",
    "selected_interval_ns",
    "required_regimes",
    "selected_metrics",
    "thresholds",
}


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(canonical_json_bytes(value))
        output.flush()
        os.fsync(output.fileno())


def safe_component(value: str) -> str:
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in value):
        raise ValueError("plan id must use lowercase letters, digits, dot, or hyphen")
    return value


def build_schedule(
    condition_ids: Iterable[str], *, blocks: int, seed: int, plan_id: str
) -> list[dict[str, object]]:
    condition_ids = list(condition_ids)
    if len(condition_ids) != 4 or len(set(condition_ids)) != 4:
        raise ValueError("schedule requires four unique conditions")
    if blocks <= 0 or blocks % 4:
        raise ValueError("blocks must be a positive multiple of four")
    rng = random.Random(seed)
    mapping = list(condition_ids)
    rng.shuffle(mapping)
    schedule: list[dict[str, object]] = []
    execution_order = 0
    for cycle in range(blocks // 4):
        row_order = list(range(4))
        rng.shuffle(row_order)
        for row_index in row_order:
            block = len(schedule) // 4 + 1
            for position, mapped_index in enumerate(WILLIAMS_ROWS[row_index], start=1):
                execution_order += 1
                condition = mapping[mapped_index]
                schedule.append(
                    {
                        "block": block,
                        "cycle": cycle + 1,
                        "position": position,
                        "execution_order": execution_order,
                        "condition": condition,
                        "run_id": f"{plan_id}-{condition}-b{block:02d}",
                    }
                )
    validate_schedule(schedule, condition_ids=condition_ids, blocks=blocks)
    return schedule


def validate_schedule(
    schedule: object, *, condition_ids: Iterable[str], blocks: int
) -> None:
    condition_ids = tuple(condition_ids)
    if len(condition_ids) != 4 or len(set(condition_ids)) != 4:
        raise ValueError("schedule requires four unique conditions")
    if blocks <= 0 or blocks % 4:
        raise ValueError("blocks must be a positive multiple of four")
    if not isinstance(schedule, list) or len(schedule) != blocks * 4:
        raise ValueError("schedule does not contain four trials per block")
    if not all(isinstance(item, dict) for item in schedule):
        raise ValueError("schedule entries must be objects")
    for index, item in enumerate(schedule):
        if (
            item.get("execution_order") != index + 1
            or item.get("block") != index // 4 + 1
            or item.get("position") != index % 4 + 1
        ):
            raise ValueError("schedule list order does not match its execution labels")
    positions: Counter[tuple[str, int]] = Counter()
    carryover: Counter[tuple[str, str]] = Counter()
    run_ids: set[str] = set()
    expected_order = 1
    for block in range(1, blocks + 1):
        rows = [item for item in schedule if isinstance(item, dict) and item.get("block") == block]
        if len(rows) != 4 or {item.get("condition") for item in rows} != set(condition_ids):
            raise ValueError(f"block {block} is incomplete or duplicates a condition")
        rows.sort(key=lambda item: int(item["position"]))
        if [item["position"] for item in rows] != [1, 2, 3, 4]:
            raise ValueError(f"block {block} positions are invalid")
        for item in rows:
            if item.get("execution_order") != expected_order:
                raise ValueError("schedule execution order is not contiguous")
            expected_order += 1
            run_id = item.get("run_id")
            if not isinstance(run_id, str) or run_id in run_ids:
                raise ValueError("schedule run ids must be unique strings")
            run_ids.add(run_id)
            positions[(str(item["condition"]), int(item["position"]))] += 1
        carryover.update(
            (str(left["condition"]), str(right["condition"]))
            for left, right in zip(rows, rows[1:])
        )
    expected_position_count = blocks // 4
    if any(
        positions[(condition, position)] != expected_position_count
        for condition in condition_ids
        for position in range(1, 5)
    ):
        raise ValueError("schedule is not position balanced")
    expected_pair_count = blocks // 4
    if any(
        carryover[(left, right)] != expected_pair_count
        for left in condition_ids
        for right in condition_ids
        if left != right
    ):
        raise ValueError("schedule is not first-order carryover balanced within blocks")


def _condition_record(state_path: Path) -> dict[str, object]:
    state_path = state_path.resolve(strict=True)
    state = read_json(state_path)
    condition = state["condition"]
    condition_id = str(condition["id"])
    identity = {
        key: condition.get(key)
        for key in ("id", "model", "reasoning_effort", "skill_enabled")
    }
    trial_order = condition.get("trial_order")
    if (
        identity != CANONICAL_CONDITION_METADATA.get(condition_id)
        or isinstance(trial_order, bool)
        or not isinstance(trial_order, int)
        or trial_order <= 0
    ):
        raise ValueError(f"{condition_id} condition metadata is not canonical")
    checkout = Path(state["checkout"]).resolve(strict=True)
    mutation = coordinator.verify_mutation(state)
    if mutation.get("valid") is not True or not mutation.get("candidate_sha256"):
        raise ValueError(f"{condition_id} checkout mutation is not valid: {mutation}")
    candidate = checkout / state["candidate"]["relative_path"]
    agent_path = state_path.parent / f"{condition_id}-agent-result.json"
    condition_path = state_path.parent / f"{condition_id}-result.json"
    agent = read_json(agent_path)
    result = read_json(condition_path)
    if agent.get("condition") != condition or result.get("condition") != condition:
        raise ValueError(f"{condition_id} result condition metadata does not match state")
    if agent.get("successful") is not True:
        raise ValueError(f"{condition_id} agent workflow was not successful")
    if result.get("all_measured_trials_correct") is not True or result.get("api_probe_passed") is not True:
        raise ValueError(f"{condition_id} did not pass correctness gates")
    candidate_sha = str(mutation["candidate_sha256"])
    if result.get("candidate_sha256") != candidate_sha:
        raise ValueError(f"{condition_id} candidate differs from its frozen result")
    return {
        "id": condition_id,
        "model": condition["model"],
        "reasoning_effort": condition["reasoning_effort"],
        "skill_enabled": condition["skill_enabled"],
        "agent_trial_order": trial_order,
        "state_path": str(state_path),
        "state_sha256": hash_file(state_path),
        "checkout": str(checkout),
        "candidate_path": str(candidate.resolve(strict=True)),
        "candidate_sha256": candidate_sha,
        "agent_result_path": str(agent_path.resolve(strict=True)),
        "agent_result_sha256": hash_file(agent_path),
        "condition_result_path": str(condition_path.resolve(strict=True)),
        "condition_result_sha256": hash_file(condition_path),
    }


def expected_workload_aggregate(workload: dict[str, Any]) -> dict[str, object]:
    return aggregate_result(
        (
            expected_output(
                workload["events"],
                workload["accounts"],
                workload["seed"] + iteration,
                workload["repeat"],
            )
            for iteration in range(workload["iterations"])
        ),
        events=workload["events"],
        accounts=workload["accounts"],
        seed=workload["seed"],
        repeat=workload["repeat"],
        iterations=workload["iterations"],
    )


def prepare_plan(args: argparse.Namespace) -> dict[str, object]:
    plan_id = safe_component(args.plan_id)
    state_paths = [path.resolve(strict=True) for path in args.state]
    comparison = coordinator.compare_states(state_paths)
    if comparison["valid"] is not True:
        raise ValueError(f"condition states do not share one frozen contract: {comparison}")
    conditions = [_condition_record(path) for path in state_paths]
    by_id = {str(item["id"]): item for item in conditions}
    if set(by_id) != set(CANONICAL_CONDITIONS):
        raise ValueError("the four canonical conditions are required exactly once")
    if {item["agent_trial_order"] for item in conditions} != {1, 2, 3, 4}:
        raise ValueError("agent trial order must be a permutation of 1 through 4")
    driver = args.driver.resolve(strict=True)
    python = args.python.resolve(strict=True)
    sandbox_exec = args.sandbox_exec.resolve(strict=True)
    workload = {
        "events": args.events,
        "accounts": args.accounts,
        "seed": args.workload_seed,
        "repeat": args.repeat,
        "iterations": args.iterations,
        "target_timeout_ms": args.target_timeout_ms,
    }
    aggregate = expected_workload_aggregate(workload)
    expected_stdout = canonical_json_bytes(aggregate)
    schedule = build_schedule(
        CANONICAL_CONDITIONS,
        blocks=args.blocks,
        seed=args.schedule_seed,
        plan_id=plan_id,
    )
    warmup_order = list(CANONICAL_CONDITIONS)
    random.Random(args.schedule_seed ^ 0x57545443).shuffle(warmup_order)
    pilot = {
        "block": 0,
        "cycle": 0,
        "position": 0,
        "execution_order": 0,
        "condition": "luna-control",
        "run_id": f"{plan_id}-qualification-pilot",
    }
    plan: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": PLAN_KIND,
        "plan_id": plan_id,
        "created_unix_ns": time.time_ns(),
        "common": {
            "base_commit": comparison["base_commit"],
            "base_tree": comparison["base_tree"],
            "prompt_sha256": comparison["prompt_sha256"],
        },
        "conditions": [by_id[name] for name in CANONICAL_CONDITIONS],
        "workload": {
            **workload,
            "driver_path": str(driver),
            "driver_sha256": hash_file(driver),
            "python_path": str(python),
            "python_sha256": hash_file(python),
            "sandbox_exec_path": str(sandbox_exec),
            "sandbox_exec_sha256": hash_file(sandbox_exec),
            "coordinator_path": str(Path(__file__).resolve(strict=True)),
            "coordinator_sha256": hash_file(Path(__file__).resolve(strict=True)),
            "summarizer_path": str((HERE / "summarize_run.py").resolve(strict=True)),
            "summarizer_sha256": hash_file(
                (HERE / "summarize_run.py").resolve(strict=True)
            ),
            "evidence_contract_path": str(
                (
                    REPO_ROOT
                    / "skills"
                    / "wattch-energy-optimize"
                    / "scripts"
                    / "evidence_contract.py"
                ).resolve(strict=True)
            ),
            "evidence_contract_sha256": hash_file(
                (
                    REPO_ROOT
                    / "skills"
                    / "wattch-energy-optimize"
                    / "scripts"
                    / "evidence_contract.py"
                ).resolve(strict=True)
            ),
            "expected_stdout_bytes": len(expected_stdout),
            "expected_stdout_sha256": sha256_bytes(expected_stdout),
        },
        "acquisition": {
            "backend": "apple-powermetrics",
            "source_kind": "CPU_POWER",
            "unit": "MILLIWATT",
            "temporality": "INTERVAL_AVERAGE",
            "evidence_class": "TOOL_DERIVED_ESTIMATE",
            "interval_ms": args.interval_ms,
            "minimum_target_duration_s": args.minimum_target_duration_s,
            "minimum_selected_source_samples": args.minimum_samples,
            "cooldown_s": args.cooldown_s,
        },
        "design": {
            "blocks": args.blocks,
            "schedule_seed": args.schedule_seed,
            "warmup_order": warmup_order,
            "qualification_pilot": pilot,
            "schedule": schedule,
        },
        "claim_scope": {
            "local_generated_code_source_domain_only": True,
            "remote_model_inference_observed": False,
            "whole_machine_energy_observed": False,
        },
    }
    _validate_plan_bounds(plan)
    write_new(args.out.resolve(), plan)
    return plan


def _validate_plan_bounds(plan: dict[str, Any]) -> None:
    workload = plan.get("workload")
    acquisition = plan.get("acquisition")
    design = plan.get("design")
    if not all(isinstance(item, dict) for item in (workload, acquisition, design)):
        raise ValueError("plan workload, acquisition, and design must be objects")
    for name in ("events", "accounts", "repeat", "iterations", "target_timeout_ms"):
        value = workload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"workload {name} must be a positive integer")
    seed = workload.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("workload seed must be an integer")
    if workload["accounts"] > workload["events"]:
        raise ValueError("workload accounts must not exceed events")

    interval_ms = acquisition.get("interval_ms")
    minimum_samples = acquisition.get("minimum_selected_source_samples")
    if (
        isinstance(interval_ms, bool)
        or not isinstance(interval_ms, int)
        or interval_ms <= 0
        or isinstance(minimum_samples, bool)
        or not isinstance(minimum_samples, int)
        or minimum_samples < 10
    ):
        raise ValueError("acquisition must require at least ten native samples")
    minimum_duration = acquisition.get("minimum_target_duration_s")
    cooldown = acquisition.get("cooldown_s")
    if (
        isinstance(minimum_duration, bool)
        or not isinstance(minimum_duration, (int, float))
        or not math.isfinite(float(minimum_duration))
        or float(minimum_duration) <= 0
        or float(minimum_duration) < interval_ms * 10 / 1000
        or isinstance(cooldown, bool)
        or not isinstance(cooldown, (int, float))
        or not math.isfinite(float(cooldown))
        or float(cooldown) < 0
    ):
        raise ValueError("acquisition duration and cooldown bounds are invalid")
    if workload["target_timeout_ms"] <= float(minimum_duration) * 1000:
        raise ValueError("target timeout must exceed the minimum measurement window")

    blocks = design.get("blocks")
    schedule_seed = design.get("schedule_seed")
    if (
        isinstance(blocks, bool)
        or not isinstance(blocks, int)
        or blocks <= 0
        or blocks % 4
        or isinstance(schedule_seed, bool)
        or not isinstance(schedule_seed, int)
    ):
        raise ValueError("study design bounds are invalid")


def verify_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("record_type") != PLAN_KIND:
        raise ValueError("unsupported physical study plan")
    _validate_plan_bounds(plan)
    conditions = plan.get("conditions")
    if (
        not isinstance(conditions, list)
        or len(conditions) != len(CANONICAL_CONDITIONS)
        or {item.get("id") for item in conditions if isinstance(item, dict)}
        != set(CANONICAL_CONDITIONS)
    ):
        raise ValueError("plan conditions are incomplete")
    state_paths = [Path(condition["state_path"]).resolve(strict=True) for condition in conditions]
    comparison = coordinator.compare_states(state_paths)
    if comparison.get("valid") is not True or plan.get("common") != {
        "base_commit": comparison.get("base_commit"),
        "base_tree": comparison.get("base_tree"),
        "prompt_sha256": comparison.get("prompt_sha256"),
    }:
        raise ValueError("plan common contract differs from its bound states")
    for condition in conditions:
        if _condition_record(Path(condition["state_path"])) != condition:
            raise ValueError("plan condition record differs from its bound state")
        for path_key, sha_key in (
            ("state_path", "state_sha256"),
            ("candidate_path", "candidate_sha256"),
            ("agent_result_path", "agent_result_sha256"),
            ("condition_result_path", "condition_result_sha256"),
        ):
            path = Path(condition[path_key]).resolve(strict=True)
            if hash_file(path) != condition[sha_key]:
                raise ValueError(f"plan-bound file changed: {path}")
    if {condition.get("agent_trial_order") for condition in conditions} != {1, 2, 3, 4}:
        raise ValueError("agent trial order must be a permutation of 1 through 4")
    workload = plan["workload"]
    driver = Path(workload["driver_path"]).resolve(strict=True)
    if hash_file(driver) != workload["driver_sha256"]:
        raise ValueError("physical workload driver changed after plan creation")
    for path_key, sha_key in (
        ("python_path", "python_sha256"),
        ("sandbox_exec_path", "sandbox_exec_sha256"),
        ("coordinator_path", "coordinator_sha256"),
        ("summarizer_path", "summarizer_sha256"),
        ("evidence_contract_path", "evidence_contract_sha256"),
    ):
        path = Path(workload[path_key]).resolve(strict=True)
        if hash_file(path) != workload[sha_key]:
            raise ValueError(f"plan-bound runtime changed: {path}")
    aggregate = expected_workload_aggregate(workload)
    expected = canonical_json_bytes(aggregate)
    if len(expected) != workload["expected_stdout_bytes"] or sha256_bytes(expected) != workload["expected_stdout_sha256"]:
        raise ValueError("plan expected stdout contract is invalid")
    validate_schedule(
        plan["design"]["schedule"],
        condition_ids=CANONICAL_CONDITIONS,
        blocks=plan["design"]["blocks"],
    )
    design = plan["design"]
    if set(design.get("warmup_order", [])) != set(CANONICAL_CONDITIONS) or len(
        design.get("warmup_order", [])
    ) != len(CANONICAL_CONDITIONS):
        raise ValueError("warmup order must contain every condition exactly once")
    pilot = design.get("qualification_pilot")
    if (
        not isinstance(pilot, dict)
        or pilot.get("condition") not in CANONICAL_CONDITIONS
        or not isinstance(pilot.get("run_id"), str)
        or pilot.get("run_id")
        in {item["run_id"] for item in design["schedule"]}
    ):
        raise ValueError("qualification pilot is invalid")


def verify_runtime_bindings(
    plan: dict[str, Any],
    *,
    plan_path: Path,
    plan_sha256: str,
    wattch_path: Path,
    wattch_sha256: str,
) -> None:
    """Hash-check coordinator-owned bytes without running benchmark work."""

    if hash_file(plan_path) != plan_sha256:
        raise ValueError("physical study plan changed during execution")
    if hash_file(wattch_path) != wattch_sha256:
        raise ValueError("wattch executable changed during execution")
    for condition in plan["conditions"]:
        for path_key, sha_key in (
            ("state_path", "state_sha256"),
            ("candidate_path", "candidate_sha256"),
            ("agent_result_path", "agent_result_sha256"),
            ("condition_result_path", "condition_result_sha256"),
        ):
            path = Path(condition[path_key]).resolve(strict=True)
            if hash_file(path) != condition[sha_key]:
                raise ValueError(f"plan-bound file changed during execution: {path}")
    workload = plan["workload"]
    for path_key, sha_key in (
        ("driver_path", "driver_sha256"),
        ("python_path", "python_sha256"),
        ("sandbox_exec_path", "sandbox_exec_sha256"),
        ("coordinator_path", "coordinator_sha256"),
        ("summarizer_path", "summarizer_sha256"),
        ("evidence_contract_path", "evidence_contract_sha256"),
    ):
        path = Path(workload[path_key]).resolve(strict=True)
        if hash_file(path) != workload[sha_key]:
            raise ValueError(f"plan-bound runtime changed during execution: {path}")


def select_physical_source(
    descriptors: Iterable[SourceDescriptor], acquisition: dict[str, Any]
) -> SourceDescriptor:
    descriptors = tuple(descriptors)
    matches = [
        descriptor
        for descriptor in descriptors
        if descriptor.backend == acquisition["backend"]
        and descriptor.kind == acquisition["source_kind"]
        and descriptor.unit == acquisition["unit"]
        and descriptor.temporality == acquisition["temporality"]
        and descriptor.evidence_class == acquisition["evidence_class"]
    ]
    if len(matches) != 1:
        raise ValueError(
            "preflight requires exactly one Apple CPU power source; "
            f"found {len(matches)}"
        )
    selected = matches[0]
    classification = classify_source(selected.to_dict())
    if classification["physical_source_candidate"] is not True:
        raise ValueError(f"selected source is not a physical candidate: {classification}")
    if selected.nominal_period_ns != acquisition["interval_ms"] * 1_000_000:
        raise ValueError("selected source native cadence does not match the physical plan")
    MeasurementBoundary.from_descriptors(
        descriptors,
        [selected.source_uid],
        boundary_id=PHYSICAL_BOUNDARY_ID,
    )
    return selected


def validate_calibration(
    path: Path,
    *,
    source: SourceDescriptor,
    descriptors: Iterable[SourceDescriptor],
    interval_ms: int,
) -> dict[str, Any]:
    descriptors = tuple(descriptors)
    if path.is_symlink() or not path.is_file():
        raise ValueError("calibration must be one regular, non-symlink file")
    path = path.resolve(strict=True)
    calibration = read_json(path)
    if calibration.get("schema_version") != 1 or calibration.get("kind") != "METER_CALIBRATION":
        raise ValueError("calibration is not a sealed METER_CALIBRATION document")
    if set(calibration) != EXPLORATORY_CALIBRATION_FIELDS:
        raise ValueError("exploratory calibration fields are incomplete or unexpected")
    if calibration.get("selected_interval_ns") != interval_ms * 1_000_000:
        raise ValueError("calibration interval does not match the physical plan")
    regimes = calibration.get("required_regimes")
    if (
        not isinstance(regimes, list)
        or not all(isinstance(item, str) for item in regimes)
        or not {"IDLE", "FIXED_LOAD", "AA"}.issubset(regimes)
    ):
        raise ValueError("calibration lacks idle, fixed-load, or A/A evidence")
    boundary = calibration.get("measurement_boundary")
    if not isinstance(boundary, dict) or boundary.get("primary_source_uids") != [source.source_uid]:
        raise ValueError("calibration boundary does not select this source exactly once")
    parsed_boundary = MeasurementBoundary.from_dict(boundary)
    expected_boundary = MeasurementBoundary.from_descriptors(
        descriptors,
        [source.source_uid],
        boundary_id=PHYSICAL_BOUNDARY_ID,
    )
    if parsed_boundary != expected_boundary:
        raise ValueError("calibration boundary differs from the frozen study boundary")
    selected = parsed_boundary.validate_descriptors(descriptors)
    if selected != (source,):
        raise ValueError("calibration source semantics do not match the live daemon")
    encoded_boundary = json.dumps(
        boundary,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if hashlib.sha256(encoded_boundary).hexdigest() != calibration.get("measurement_boundary_sha256"):
        raise ValueError("calibration boundary fingerprint is invalid")
    metrics = calibration.get("selected_metrics")
    thresholds = calibration.get("thresholds")
    if not isinstance(metrics, dict) or not isinstance(thresholds, dict):
        raise ValueError("calibration metrics or thresholds are missing")
    checks = (
        ("measurement_overhead_ratio", "max_measurement_overhead_ratio", 0.01),
        ("missing_sample_ratio", "max_missing_sample_ratio", 0.001),
        ("phase_sensitivity_ratio", "max_phase_sensitivity_ratio", 0.01),
    )
    for metric_name, threshold_name, hard_limit in checks:
        metric = metrics.get(metric_name)
        threshold = thresholds.get(threshold_name)
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(float(metric))
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or float(metric) < 0
            or float(threshold) <= 0
            or float(metric) >= float(threshold)
            or float(threshold) > hard_limit
        ):
            raise ValueError(f"calibration {metric_name} is not eligible")
    return {
        "path": str(path),
        "sha256": hash_file(path),
        "document": calibration,
        "evidence_level": "EXPLORATORY_CALIBRATION",
    }


def preflight(
    plan: dict[str, Any], *, socket: Path, calibration_path: Path | None
) -> dict[str, object]:
    verify_plan(plan)
    verify_no_fork_sandbox(plan)
    with WattchClient(str(socket.resolve()), client_name="wattch-physical-study") as client:
        descriptors = tuple(client.sources)
        source = select_physical_source(descriptors, plan["acquisition"])
        boundary = MeasurementBoundary.from_descriptors(
            descriptors,
            [source.source_uid],
            boundary_id=PHYSICAL_BOUNDARY_ID,
        )
        daemon = {
            "name": client.hello.daemon_name,
            "version": client.hello.daemon_version,
            "node_id": client.node_id,
            "clock_instance_id": client.clock_instance_id,
            "protocol_version": client.protocol_version,
        }
    calibration = (
        validate_calibration(
            calibration_path,
            source=source,
            descriptors=descriptors,
            interval_ms=plan["acquisition"]["interval_ms"],
        )
        if calibration_path is not None
        else None
    )
    return {
        "source_ready": True,
        "descriptive_comparison_ready": calibration is not None,
        "confirmatory_comparison_ready": False,
        "daemon": daemon,
        "selected_source": source.to_dict(),
        "measurement_boundary": boundary.to_dict(),
        "calibration": calibration,
        "limitations": [
            "The boundary is a local Apple CPU-domain tool estimate, not whole-machine energy.",
            "Local capture does not observe Luna or Terra inference in the provider datacenter.",
            "Schema-v1 calibration supports exploratory estimates, not a confirmatory claim.",
            "Williams carryover balance is within blocks; cooldown is not a verified thermal reset.",
        ],
    }


def workload_command(plan: dict[str, Any], condition: dict[str, Any]) -> list[str]:
    workload = plan["workload"]
    return [
        str(Path(workload["sandbox_exec_path"]).resolve()),
        "-p",
        NO_FORK_SANDBOX_PROFILE,
        str(Path(workload["python_path"]).resolve()),
        "-I",
        "-B",
        str(Path(workload["driver_path"]).resolve()),
        "--candidate",
        str(Path(condition["candidate_path"]).resolve()),
        "--candidate-sha256",
        condition["candidate_sha256"],
        "--events",
        str(workload["events"]),
        "--accounts",
        str(workload["accounts"]),
        "--seed",
        str(workload["seed"]),
        "--repeat",
        str(workload["repeat"]),
        "--iterations",
        str(workload["iterations"]),
        "--timeout-ms",
        str(workload["target_timeout_ms"]),
    ]


def verify_no_fork_sandbox(plan: dict[str, Any]) -> None:
    """Require an OS-enforced process-spawn denial before untrusted work."""

    workload = plan["workload"]
    command = [
        str(Path(workload["sandbox_exec_path"]).resolve()),
        "-p",
        NO_FORK_SANDBOX_PROFILE,
        str(Path(workload["python_path"]).resolve()),
        "-I",
        "-B",
        "-c",
        NO_FORK_SANDBOX_PROBE,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            "the no-fork process sandbox is unavailable; run the physical study "
            f"from a macOS host context that permits sandbox-exec ({detail})"
        )


def verify_condition_scope(condition: dict[str, Any]) -> dict[str, object]:
    state_path = Path(condition["state_path"]).resolve(strict=True)
    if hash_file(state_path) != condition["state_sha256"]:
        raise ValueError(f"condition state changed: {state_path}")
    state = read_json(state_path)
    mutation = coordinator.verify_mutation(state)
    candidate = (
        Path(state["checkout"]) / state["candidate"]["relative_path"]
    )
    candidate_path_matches = (
        mutation.get("valid") is True
        and not candidate.is_symlink()
        and candidate.resolve(strict=True) == Path(condition["candidate_path"])
    )
    candidate_hash_matches = (
        mutation.get("candidate_sha256") == condition["candidate_sha256"]
    )
    return {
        **mutation,
        "candidate_path_matches": candidate_path_matches,
        "candidate_hash_matches": candidate_hash_matches,
        "valid": mutation.get("valid") is True
        and candidate_path_matches
        and candidate_hash_matches,
    }


def _expected_stdout(plan: dict[str, Any]) -> bytes:
    workload = plan["workload"]
    return canonical_json_bytes(expected_workload_aggregate(workload))


def judge_artifact(
    run_dir: Path,
    *,
    expected_command: list[str],
    expected_cwd: Path,
    child_exit_code: object,
    expected_stdout: bytes,
) -> dict[str, object]:
    command = read_json(run_dir / "command.json")
    stdout = (run_dir / "target.stdout.log").read_bytes()
    stderr = (run_dir / "target.stderr.log").read_bytes()
    gates = {
        "stdout_exact": stdout == expected_stdout,
        "stderr_exact": stderr == b"",
        "command_exact": command.get("argv") == expected_command,
        "cwd_exact": command.get("cwd") == str(expected_cwd.resolve()),
        "exit_exact": child_exit_code == 0,
    }
    return {
        **gates,
        "correct": all(gates.values()),
        "completed_work": all(gates.values()),
        "actual_stdout_sha256": sha256_bytes(stdout),
        "expected_stdout_sha256": sha256_bytes(expected_stdout),
    }


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (
        "manifest.json",
        "sources.json",
        "raw.trace.wch",
        "events.trace.wch",
        "command.json",
        "command.lifecycle.jsonl",
        "target.stdout.log",
        "target.stderr.log",
        "quality.json",
    ):
        path = run_dir / name
        if path.is_file() and not path.is_symlink():
            result[name] = hash_file(path)
    return result


def _write_quality(
    run_dir: Path,
    *,
    run_id: str,
    source_uid: str,
    measurement_overhead_ratio: float,
    max_overhead_ratio: float,
    max_missing_sample_ratio: float,
) -> dict[str, object]:
    descriptors = [
        SourceDescriptor.from_dict(item)
        for item in json.loads((run_dir / "sources.json").read_text("utf-8"))
    ]
    batches = _read_trace(run_dir / "raw.trace.wch", FrameKind.RAW_SAMPLE_BATCH)
    samples = [sample for batch in batches for sample in batch.samples]
    summaries = summarize_source_quality(descriptors, samples)
    by_uid = {descriptor.source_uid: descriptor for descriptor in descriptors}
    selected = by_uid.get(source_uid)
    if selected is None:
        raise ValueError(f"selected source {source_uid!r} is missing from the trace")
    required_source_uids = [source_uid]
    if selected.temporality == "INTERVAL_AVERAGE":
        duration_uid = selected.labels.get("wattch.interval_duration_source_uid")
        if not duration_uid or duration_uid not in by_uid:
            raise ValueError("selected interval average lacks its duration source")
        required_source_uids.append(duration_uid)
    quality = build_trial_quality_metadata(
        trial_id=run_id,
        summaries=summaries,
        measurement_overhead_ratio=measurement_overhead_ratio,
        required_source_uids=required_source_uids,
        max_overhead_ratio=max_overhead_ratio,
        max_missing_sample_ratio=max_missing_sample_ratio,
    )
    write_new(run_dir / "quality.json", quality)
    return quality


def _condition_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in plan["conditions"]}


def _labels(
    plan: dict[str, Any],
    item: dict[str, Any],
    condition: dict[str, Any],
    *,
    calibration_sha256: str,
    trial_role: str,
) -> list[str]:
    values = {
        "study": plan["plan_id"],
        "stage": (
            "physical-source-qualification"
            if trial_role == "qualification-pilot"
            else "physical-generated-code"
        ),
        "trial_role": trial_role,
        "condition": condition["id"],
        "model": condition["model"],
        "reasoning_effort": condition["reasoning_effort"],
        "skill": "on" if condition["skill_enabled"] else "off",
        "prompt_sha256": plan["common"]["prompt_sha256"],
        "candidate_sha256": condition["candidate_sha256"],
        "block": str(item["block"]),
        "position": str(item["position"]),
        "execution_order": str(item["execution_order"]),
        "interval_ms": str(plan["acquisition"]["interval_ms"]),
        "measurement_overhead_calibration_sha256": calibration_sha256,
    }
    result: list[str] = []
    for key, value in sorted(values.items()):
        result.extend(("--label", f"{key}={value}"))
    return result


def inspection_blockers(
    inspection: dict[str, Any],
    *,
    source: dict[str, Any],
    minimum_samples: int,
) -> list[str]:
    blockers: list[str] = []
    lifecycle = inspection.get("command_lifecycle")
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("status") != "COMPLETE"
        or not isinstance(lifecycle.get("exit_code"), int)
        or isinstance(lifecycle.get("exit_code"), bool)
    ):
        blockers.append("COMMAND_LIFECYCLE_INCOMPLETE")
    if inspection.get("unknown_source_samples") != 0:
        blockers.append("UNKNOWN_SOURCE_SAMPLES")
    per_source = inspection.get("per_source")
    stats = (
        per_source.get(str(source["local_source_id"]), {})
        if isinstance(per_source, dict)
        else {}
    )
    samples = stats.get("samples") if isinstance(stats, dict) else None
    if (
        isinstance(samples, bool)
        or not isinstance(samples, int)
        or samples < minimum_samples
    ):
        blockers.append("TOO_FEW_NATIVE_SAMPLES")
    if not isinstance(stats, dict) or stats.get("sequence_gaps") != 0:
        blockers.append("SEQUENCE_GAPS")
    return blockers


def _run_warmups(
    plan: dict[str, Any],
    output_root: Path,
    *,
    runtime_guard,
) -> list[dict[str, object]]:
    expected = _expected_stdout(plan)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    results: list[dict[str, object]] = []
    conditions = _condition_map(plan)
    for condition_id in plan["design"]["warmup_order"]:
        runtime_guard()
        condition = conditions[condition_id]
        scope_before = verify_condition_scope(condition)
        command = workload_command(plan, condition)
        started = time.perf_counter()
        completed, timed_out, cleanup = _run_process_tree(
            command,
            cwd=condition["checkout"],
            env=environment,
            timeout_s=plan["workload"]["target_timeout_ms"] / 1000 + 5.0,
        )
        runtime_guard()
        result = {
            "condition": condition["id"],
            "returncode": completed.returncode,
            "duration_s": time.perf_counter() - started,
            "stdout_sha256": sha256_bytes(completed.stdout),
            "stderr_sha256": sha256_bytes(completed.stderr),
            "timed_out": timed_out,
            "process_group_cleanup": cleanup,
            "scope_before": scope_before,
            "scope_after": verify_condition_scope(condition),
            "correct": completed.returncode == 0
            and not timed_out
            and cleanup["clean"] is True
            and cleanup["term_sent"] is False
            and completed.stdout == expected
            and completed.stderr == b"",
        }
        result["correct"] = (
            result["correct"]
            and result["scope_before"]["valid"] is True
            and result["scope_after"]["valid"] is True
        )
        write_new(output_root / "warmups" / f"{condition['id']}.json", result)
        results.append(result)
    if not all(result["correct"] for result in results):
        raise ValueError("one or more unmeasured physical-study warmups failed")
    return results


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_process_group(
    process: subprocess.Popen[bytes],
    *,
    term_grace_s: float = 0.5,
    kill_grace_s: float = 0.5,
) -> dict[str, bool]:
    """Bound cleanup of the isolated process tree, including descendants."""

    process_group_id = process.pid
    result = {"term_sent": False, "kill_sent": False, "clean": True}

    def wait_until_gone(grace_s: float) -> bool:
        deadline = time.monotonic() + grace_s
        while True:
            process.poll()
            if not _process_group_exists(process_group_id):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    if not _process_group_exists(process_group_id):
        process.poll()
        return result
    try:
        os.killpg(process_group_id, signal.SIGTERM)
        result["term_sent"] = True
    except ProcessLookupError:
        process.poll()
        return result
    if wait_until_gone(term_grace_s):
        return result
    try:
        os.killpg(process_group_id, signal.SIGKILL)
        result["kill_sent"] = True
    except ProcessLookupError:
        process.poll()
        return result
    result["clean"] = wait_until_gone(kill_grace_s)
    return result


def _run_process_tree(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[subprocess.CompletedProcess[bytes], bool, dict[str, bool]]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup = _cleanup_process_group(process)
        stdout, stderr = process.communicate(timeout=2.0)
    else:
        cleanup = _cleanup_process_group(process)
    return (
        subprocess.CompletedProcess(command, process.returncode, stdout, stderr),
        timed_out,
        cleanup,
    )


def run_study(args: argparse.Namespace) -> dict[str, object]:
    plan_path = args.plan.resolve(strict=True)
    plan_bytes = plan_path.read_bytes()
    plan = json.loads(plan_bytes)
    if not isinstance(plan, dict):
        raise ValueError("physical study plan must contain one JSON object")
    plan_sha256 = sha256_bytes(plan_bytes)
    wattch_path = args.wattch.resolve(strict=True)
    wattch_sha256 = hash_file(wattch_path)
    runtime_guard = lambda: verify_runtime_bindings(
        plan,
        plan_path=plan_path,
        plan_sha256=plan_sha256,
        wattch_path=wattch_path,
        wattch_sha256=wattch_sha256,
    )
    runtime_guard()
    readiness = preflight(plan, socket=args.socket, calibration_path=args.calibration)
    if readiness["descriptive_comparison_ready"] is not True:
        raise ValueError("physical study requires an eligible exploratory calibration")
    output_root = args.out.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    write_new(
        output_root / "study.json",
        {
            "plan_path": str(plan_path),
            "plan_sha256": plan_sha256,
            "wattch_path": str(wattch_path),
            "wattch_sha256": wattch_sha256,
            "readiness": readiness,
        },
    )
    _run_warmups(plan, output_root, runtime_guard=runtime_guard)
    conditions = _condition_map(plan)
    expected = _expected_stdout(plan)
    source = readiness["selected_source"]
    calibration = readiness["calibration"]
    overhead = float(calibration["document"]["selected_metrics"]["measurement_overhead_ratio"])
    calibration_thresholds = calibration["document"]["thresholds"]
    summaries: list[dict[str, object]] = []
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    runs_root = output_root / "runs"
    runs_root.mkdir()
    execution_items = [
        (plan["design"]["qualification_pilot"], True),
        *((item, False) for item in plan["design"]["schedule"]),
    ]
    for item, is_pilot in execution_items:
        runtime_guard()
        condition = conditions[item["condition"]]
        scope_before = verify_condition_scope(condition)
        if scope_before["valid"] is not True:
            raise ValueError(f"condition scope changed before {item['run_id']}")
        if plan["acquisition"]["cooldown_s"]:
            time.sleep(float(plan["acquisition"]["cooldown_s"]))
        command = workload_command(plan, condition)
        wattch_command = [
            str(wattch_path),
            "run",
            "--socket",
            str(args.socket.resolve()),
            "--out",
            str(runs_root),
            "--interval-ms",
            str(plan["acquisition"]["interval_ms"]),
            "--timeout-ms",
            str(plan["workload"]["target_timeout_ms"]),
            "--run-id",
            item["run_id"],
            *_labels(
                plan,
                item,
                condition,
                calibration_sha256=calibration["sha256"],
                trial_role=(
                    "qualification-pilot" if is_pilot else "comparison-matrix"
                ),
            ),
            "--json",
            "--",
            *command,
        ]
        completed = subprocess.run(
            wattch_command,
            cwd=condition["checkout"],
            env=environment,
            capture_output=True,
            check=False,
        )
        runtime_guard()
        wrapper = None
        try:
            wrapper = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        run_dir = runs_root / item["run_id"]
        judgement: dict[str, object] = {"correct": False, "completed_work": False}
        inspect_value: dict[str, Any] | None = None
        quality: dict[str, object] | None = None
        blockers: list[str] = []
        required_files = ("command.json", "target.stdout.log", "target.stderr.log")
        if all((run_dir / name).is_file() for name in required_files):
            judgement = judge_artifact(
                run_dir,
                expected_command=command,
                expected_cwd=Path(condition["checkout"]),
                child_exit_code=wrapper.get("exit_code") if isinstance(wrapper, dict) else None,
                expected_stdout=expected,
            )
        if judgement["correct"] is not True:
            blockers.append("CORRECTNESS_OR_USEFUL_WORK_FAILED")
        scope_after = verify_condition_scope(condition)
        if scope_after["valid"] is not True:
            blockers.append("MUTATION_SCOPE_CHANGED")
        runtime_guard()
        inspection = subprocess.run(
            [str(wattch_path), "inspect", "--run", str(run_dir), "--json"],
            capture_output=True,
            check=False,
        )
        runtime_guard()
        if inspection.returncode == 0:
            try:
                inspect_value = json.loads(inspection.stdout)
            except (json.JSONDecodeError, UnicodeDecodeError):
                blockers.append("INSPECT_JSON_INVALID")
        else:
            blockers.append("INSPECT_FAILED")
        descriptors = (
            json.loads((run_dir / "sources.json").read_text("utf-8"))
            if (run_dir / "sources.json").is_file()
            else []
        )
        matches = [item for item in descriptors if item.get("source_uid") == source["source_uid"]]
        if matches != [source]:
            blockers.append("SOURCE_DESCRIPTOR_DRIFT")
        if inspect_value is not None:
            blockers.extend(
                inspection_blockers(
                    inspect_value,
                    source=source,
                    minimum_samples=plan["acquisition"][
                        "minimum_selected_source_samples"
                    ],
                )
            )
        try:
            event_batches = _read_trace(run_dir / "events.trace.wch", FrameKind.EVENT_BATCH)
            _start, _end, start_ns, end_ns = select_window(event_batches)
            marker_duration_s = (end_ns - start_ns) / 1e9
            if marker_duration_s < plan["acquisition"]["minimum_target_duration_s"]:
                blockers.append("TARGET_WINDOW_TOO_SHORT")
        except (OSError, ValueError, SystemExit) as error:
            marker_duration_s = None
            blockers.append(f"MARKER_WINDOW_INVALID:{error}")
        if not blockers:
            quality = _write_quality(
                run_dir,
                run_id=item["run_id"],
                source_uid=source["source_uid"],
                measurement_overhead_ratio=overhead,
                max_overhead_ratio=float(
                    calibration_thresholds["max_measurement_overhead_ratio"]
                ),
                max_missing_sample_ratio=float(
                    calibration_thresholds["max_missing_sample_ratio"]
                ),
            )
            if quality["disposition"] != "ACCEPTED":
                blockers.append("QUALITY_NOT_ACCEPTED")
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "wattch-agent-trial",
            "plan_sha256": plan_sha256,
            "condition": {
                "id": condition["id"],
                "model": condition["model"],
                "reasoning_effort": condition["reasoning_effort"],
                "skill_enabled": condition["skill_enabled"],
            },
            "prompt": {"sha256": plan["common"]["prompt_sha256"]},
            "trial": {
                **item,
                "index": item["block"],
                "outcome_type": "physical-generated-code-workload",
                "role": (
                    "qualification-pilot" if is_pilot else "comparison-matrix"
                ),
            },
            "candidate": {
                "path": condition["candidate_path"],
                "sha256": condition["candidate_sha256"],
                "unchanged_during_trial": hash_file(Path(condition["candidate_path"]))
                == condition["candidate_sha256"],
                "scope_before": scope_before,
                "scope_after": scope_after,
            },
            "command": {
                "argv": command,
                "cwd": condition["checkout"],
                "argv_sha256": sha256_bytes(canonical_json_bytes(command)),
            },
            "artifact": {
                "run_id": item["run_id"],
                "run_dir": str(run_dir.resolve()),
                "files": _artifact_hashes(run_dir),
            },
            "acquisition": {
                "complete": isinstance(inspect_value, dict)
                and isinstance(inspect_value.get("command_lifecycle"), dict)
                and inspect_value["command_lifecycle"].get("status") == "COMPLETE",
                "inspect_passed": inspect_value is not None,
                "wrapper": wrapper,
                "marker_duration_s": marker_duration_s,
                "selected_source": source,
            },
            "correctness": judgement,
            "quality": quality,
            "calibration_bound": True,
            "calibration": {key: calibration[key] for key in ("path", "sha256")},
            "descriptive_local_run_eligible": not blockers,
            "physical_claim_eligible": False,
            "run_blockers": blockers,
        }
        record_path = (
            output_root / "pilot" / "trial.json"
            if is_pilot
            else output_root / "trials" / f"{item['run_id']}.json"
        )
        write_new(record_path, record)
        runtime_guard()
        summary_process = subprocess.run(
            [
                str(Path(plan["workload"]["python_path"]).resolve()),
                str(HERE / "summarize_run.py"),
                "--run",
                str(run_dir),
                "--source-uid",
                source["source_uid"],
                "--trial-record",
                str(record_path),
            ],
            capture_output=True,
            check=False,
            env=environment,
        )
        runtime_guard()
        summary_value: dict[str, object] = {
            "claim_eligible": False,
            "descriptive_local_run_eligible": False,
            "error": summary_process.stderr.decode("utf-8", errors="replace"),
        }
        if summary_process.returncode == 0:
            try:
                summary_value = json.loads(summary_process.stdout)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                summary_value = {
                    "claim_eligible": False,
                    "descriptive_local_run_eligible": False,
                    "error": f"invalid summary JSON: {error}",
                }
        summary_path = (
            output_root / "pilot" / "summary.json"
            if is_pilot
            else output_root / "summaries" / f"{item['run_id']}.json"
        )
        write_new(summary_path, summary_value)
        if is_pilot:
            pilot_eligible = (
                record["descriptive_local_run_eligible"] is True
                and summary_value.get("descriptive_local_run_eligible") is True
            )
            write_new(
                output_root / "pilot" / "RESULT.json",
                {
                    "qualification_passed": pilot_eligible,
                    "run_id": item["run_id"],
                    "record_path": str(record_path),
                    "summary_path": str(summary_path),
                },
            )
            if not pilot_eligible:
                raise ValueError(
                    "measured source-qualification pilot failed; matrix was not started"
                )
            continue
        summaries.append({"schedule": item, "record": record, "summary": summary_value})
    result = aggregate_study(plan, readiness=readiness, trials=summaries)
    write_new(output_root / "RESULTS.json", result)
    return result


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(probability * (len(ordered) - 1))))
    return ordered[index]


def bootstrap_mean_ci(values: list[float], *, seed: int, samples: int = 10_000) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = [
        statistics.fmean(rng.choice(values) for _ in values) for _ in range(samples)
    ]
    return [_percentile(means, 0.025), _percentile(means, 0.975)]


def aggregate_study(
    plan: dict[str, Any], *, readiness: dict[str, Any], trials: list[dict[str, Any]]
) -> dict[str, object]:
    by_condition: dict[str, list[dict[str, Any]]] = {name: [] for name in CANONICAL_CONDITIONS}
    by_block: dict[int, dict[str, float]] = {}
    expected_schedule = plan["design"].get("schedule")
    schedule_valid = False
    try:
        validate_schedule(
            expected_schedule,
            condition_ids=CANONICAL_CONDITIONS,
            blocks=plan["design"].get("blocks"),
        )
        schedule_valid = True
    except (TypeError, ValueError):
        pass
    expected_rows = (
        [canonical_json_bytes(item) for item in expected_schedule]
        if isinstance(expected_schedule, list)
        else []
    )
    observed_rows = [canonical_json_bytes(item["schedule"]) for item in trials]
    matrix_complete = (
        schedule_valid
        and len(expected_rows) == plan["design"]["blocks"] * 4
        and len(expected_rows) > 0
        and Counter(expected_rows) == Counter(observed_rows)
        and all(count == 1 for count in Counter(observed_rows).values())
        and readiness.get("descriptive_comparison_ready") is True
    )
    all_eligible = matrix_complete
    for item in trials:
        condition = item["schedule"]["condition"]
        summary = item["summary"]
        record = item["record"]
        eligible = (
            record["descriptive_local_run_eligible"] is True
            and summary.get("descriptive_local_run_eligible") is True
        )
        all_eligible = all_eligible and eligible
        energy = summary.get("outcome", {}).get("total_joules") if eligible else None
        duration = summary.get("outcome", {}).get("duration_s") if eligible else None
        row = {
            "block": item["schedule"]["block"],
            "execution_order": item["schedule"]["execution_order"],
            "run_id": item["schedule"]["run_id"],
            "descriptive_local_run_eligible": eligible,
            "energy_joules": energy,
            "duration_s": duration,
            "blockers": record["run_blockers"],
        }
        by_condition[condition].append(row)
        if eligible:
            by_block.setdefault(int(row["block"]), {})[condition] = float(energy)
    cells: list[dict[str, object]] = []
    for condition in CANONICAL_CONDITIONS:
        rows = by_condition[condition]
        energies = [float(row["energy_joules"]) for row in rows if row["energy_joules"] is not None]
        cells.append(
            {
                "condition": condition,
                "trials": rows,
                "retained": len(energies),
                "median_energy_joules": statistics.median(energies) if energies else None,
                "mean_energy_joules": statistics.fmean(energies) if energies else None,
                "raw_energy_joules": energies,
            }
        )
    contrast_specs = {
        "luna_skill_minus_control": ("luna-skill", "luna-control"),
        "terra_skill_minus_control": ("terra-skill", "terra-control"),
        "control_terra_minus_luna": ("terra-control", "luna-control"),
        "skill_terra_minus_luna": ("terra-skill", "luna-skill"),
    }
    contrasts: dict[str, object] = {}
    for index, (name, (left, right)) in enumerate(contrast_specs.items()):
        values = [
            block[left] - block[right]
            for block in by_block.values()
            if left in block and right in block
        ]
        contrasts[name] = {
            "paired_differences_joules": values,
            "mean_difference_joules": statistics.fmean(values) if values else None,
            "median_difference_joules": statistics.median(values) if values else None,
            "bootstrap_95_ci_mean_joules": bootstrap_mean_ci(
                values, seed=int(plan["design"]["schedule_seed"]) + index
            ),
        }
    interaction = []
    for block in by_block.values():
        if set(CANONICAL_CONDITIONS).issubset(block):
            interaction.append(
                (block["terra-skill"] - block["terra-control"])
                - (block["luna-skill"] - block["luna-control"])
            )
    contrasts["model_by_skill_interaction"] = {
        "paired_differences_joules": interaction,
        "mean_difference_joules": statistics.fmean(interaction) if interaction else None,
        "median_difference_joules": statistics.median(interaction) if interaction else None,
        "bootstrap_95_ci_mean_joules": bootstrap_mean_ci(
            interaction, seed=int(plan["design"]["schedule_seed"]) + 99
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RESULT_KIND,
        "plan_id": plan["plan_id"],
        "source": readiness["selected_source"],
        "cells": cells,
        "paired_contrasts": contrasts,
        "claims": {
            "descriptive_local_comparison_eligible": all_eligible,
            "physical_comparison_claim_eligible": False,
            "confirmatory_comparison_claim_eligible": False,
            "local_generated_code_source_domain_only": True,
            "remote_model_energy_claim_eligible": False,
            "whole_machine_energy_claim_eligible": False,
        },
        "matrix_complete": matrix_complete,
        "limitations": readiness["limitations"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--state", type=Path, nargs=4, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--plan-id", required=True)
    prepare.add_argument("--driver", type=Path, default=HERE / "physical_workload.py")
    prepare.add_argument("--python", type=Path, default=Path(sys.executable))
    prepare.add_argument(
        "--sandbox-exec", type=Path, default=Path("/usr/bin/sandbox-exec")
    )
    prepare.add_argument("--events", type=int, default=300_000)
    prepare.add_argument("--accounts", type=int, default=512)
    prepare.add_argument("--workload-seed", type=int, default=20260719)
    prepare.add_argument("--repeat", type=int, default=6)
    prepare.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    prepare.add_argument(
        "--target-timeout-ms", type=int, default=DEFAULT_TARGET_TIMEOUT_MS
    )
    prepare.add_argument("--blocks", type=int, default=DEFAULT_BLOCKS)
    prepare.add_argument("--schedule-seed", type=int, default=20260719)
    prepare.add_argument("--interval-ms", type=int, default=DEFAULT_INTERVAL_MS)
    prepare.add_argument("--cooldown-s", type=float, default=DEFAULT_COOLDOWN_S)
    prepare.add_argument("--minimum-target-duration-s", type=float, default=DEFAULT_MIN_DURATION_S)
    prepare.add_argument("--minimum-samples", type=int, default=DEFAULT_MIN_SAMPLES)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--plan", type=Path, required=True)
    preflight_parser.add_argument("--socket", type=Path, required=True)
    preflight_parser.add_argument("--calibration", type=Path)

    run = subparsers.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--socket", type=Path, required=True)
    run.add_argument("--calibration", type=Path, required=True)
    run.add_argument("--wattch", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "prepare":
        if min(
            args.events,
            args.accounts,
            args.repeat,
            args.iterations,
            args.target_timeout_ms,
            args.interval_ms,
            args.minimum_samples,
        ) <= 0:
            raise ValueError("workload and acquisition counts must be positive")
        if args.accounts > args.events:
            raise ValueError("accounts must not exceed events")
        if args.cooldown_s < 0 or args.minimum_target_duration_s <= 0:
            raise ValueError("cooldown and duration bounds are invalid")
        minimum_native_window_s = args.interval_ms * 10 / 1000
        if args.minimum_samples < 10:
            raise ValueError("minimum samples must cover at least ten native updates")
        if args.minimum_target_duration_s < minimum_native_window_s:
            raise ValueError(
                "minimum target duration must cover at least ten native periods"
            )
        result = prepare_plan(args)
    elif args.command == "verify":
        result = read_json(args.plan.resolve(strict=True))
        verify_plan(result)
        result = {"valid": True, "plan_id": result["plan_id"]}
    elif args.command == "preflight":
        plan = read_json(args.plan.resolve(strict=True))
        result = preflight(plan, socket=args.socket, calibration_path=args.calibration)
    else:
        result = run_study(args)
    print(canonical_json_bytes(result).decode("ascii"), end="")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, WattchError) as error:
        print(
            canonical_json_bytes(
                {
                    "error": type(error).__name__,
                    "message": str(error),
                }
            ).decode("ascii"),
            file=sys.stderr,
            end="",
        )
        raise SystemExit(1) from None
