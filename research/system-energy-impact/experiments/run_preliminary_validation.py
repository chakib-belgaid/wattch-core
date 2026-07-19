#!/usr/bin/env python3
"""Run the evidence-safe preliminary validation for the Wattch paper.

This driver deliberately separates two kinds of evidence:

* deterministic Wattch scenarios validate the acquisition/analysis logic against
  planted causal truths;
* polling-versus-batching executions validate output equivalence and workload
  observability, but contain no energy outcome unless a real wattch-powerd meter
  is supplied in a later experiment.

The output must therefore never be cited as empirical confirmation of a real
machine-energy ranking reversal.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TESTD = ROOT / "target" / "debug" / "wattch-testd"
SCENARIOS = ROOT / "examples" / "scenarios"
TRUTH = ROOT / "crates" / "wattch-testd" / "tests" / "fixtures" / "truth"
WORKER = ROOT / "examples" / "workloads" / "polling_batching_worker.py"


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _run_scenario(name: str, raw_dir: Path) -> dict[int, dict[int, tuple[int, float]]]:
    if not TESTD.exists():
        raise RuntimeError(
            f"{TESTD} is missing; run `cargo build -p wattch-testd` first"
        )
    process = subprocess.run(
        [str(TESTD), "--scenario", str(SCENARIOS / name), "--dry-run"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{Path(name).stem}.jsonl").write_text(
        process.stdout, encoding="utf-8"
    )
    samples: dict[int, dict[int, tuple[int, float]]] = {}
    for line in process.stdout.splitlines():
        if not line.startswith("{"):
            continue
        batch = json.loads(line)
        tick = int(batch["batch_sequence_no"])
        for sample in batch["samples"]:
            samples.setdefault(int(sample["source_id"]), {})[tick] = (
                int(sample["monotonic_ns"]),
                float(sample["value"]["value"]),
            )
    return samples


def _value(samples: dict[int, dict[int, tuple[int, float]]], source: int, tick: int) -> float:
    return samples[source][tick][1]


def _delta(
    samples: dict[int, dict[int, tuple[int, float]]],
    source: int,
    window: dict[str, int],
) -> float:
    return _value(samples, source, window["end_tick"]) - _value(
        samples, source, window["start_tick"]
    )


def _rate(
    samples: dict[int, dict[int, tuple[int, float]]],
    source: int,
    window: dict[str, int],
) -> float:
    ticks = window["end_tick"] - window["start_tick"]
    return _delta(samples, source, window) / ticks


def _close_map(observed: dict[str, float], expected: dict[str, float]) -> bool:
    return set(observed) == set(expected) and all(
        math.isclose(observed[key], expected[key], rel_tol=1e-9, abs_tol=1e-9)
        for key in expected
    )


def _evaluate_causal_fixture(
    truth_name: str, raw_dir: Path
) -> dict[str, Any]:
    truth = json.loads((TRUTH / truth_name).read_text(encoding="utf-8"))
    samples = _run_scenario(truth["scenario_file"], raw_dir)
    windows = truth["windows"]
    source = int(truth["primary_source_id"])

    if truth_name == "additive_null.truth.json":
        idle = _rate(samples, source, windows["idle_control"])
        background = _rate(samples, source, windows["background_control"])
        observed = {
            "p1_idle_incremental_joules_per_tick": _rate(
                samples, source, windows["p1_idle"]
            )
            - idle,
            "p1_background_incremental_joules_per_tick": _rate(
                samples, source, windows["p1_background"]
            )
            - background,
            "p2_idle_incremental_joules_per_tick": _rate(
                samples, source, windows["p2_idle"]
            )
            - idle,
            "p2_background_incremental_joules_per_tick": _rate(
                samples, source, windows["p2_background"]
            )
            - background,
        }
    elif truth_name == "interference_reversal.truth.json":
        idle = _rate(samples, source, windows["idle_control"])
        background = _rate(samples, source, windows["background_control"])
        p1_idle = _rate(samples, source, windows["p1_idle"])
        p2_idle = _rate(samples, source, windows["p2_idle"])
        p1_background = _rate(samples, source, windows["p1_background"])
        p2_background = _rate(samples, source, windows["p2_background"])
        observed = {
            "isolated_p1_minus_p2_joules_per_tick": p1_idle - p2_idle,
            "background_p1_minus_p2_joules_per_tick": (
                p1_background - p2_background
            ),
            "p1_background_incremental_joules_per_tick": (
                p1_background - background
            ),
            "p2_background_incremental_joules_per_tick": (
                p2_background - background
            ),
        }
        observed["ranking_reversal_detected"] = (
            observed["isolated_p1_minus_p2_joules_per_tick"] > 0
            and observed["background_p1_minus_p2_joules_per_tick"] < 0
        )
    elif truth_name == "low_priority_dvfs.truth.json":
        work_source = int(truth["work_source_id"])
        observed = {
            "work_units_per_arm": _delta(
                samples, work_source, windows["dynamic_normal"]
            ),
            "dynamic_normal_joules": _delta(
                samples, source, windows["dynamic_normal"]
            ),
            "dynamic_low_priority_joules": _delta(
                samples, source, windows["dynamic_low_priority"]
            ),
            "fixed_normal_joules": _delta(
                samples, source, windows["fixed_normal"]
            ),
            "fixed_low_priority_joules": _delta(
                samples, source, windows["fixed_low_priority"]
            ),
        }
        observed["dynamic_low_priority_saves_energy"] = (
            observed["dynamic_low_priority_joules"]
            < observed["dynamic_normal_joules"]
        )
        observed["fixed_frequency_attenuates_or_reverses"] = (
            observed["fixed_low_priority_joules"]
            >= observed["fixed_normal_joules"]
        )
    elif truth_name == "carryover_confounding.truth.json":
        idle_source = int(truth["idle_source_id"])
        observed = {
            "hot_predecessor_joules": _delta(
                samples, source, windows["hot_predecessor"]
            ),
            "p2_immediate_joules": _delta(
                samples, source, windows["p2_immediate"]
            ),
            "p2_rested_joules": _delta(
                samples, source, windows["p2_rested"]
            ),
            "immediate_deep_idle_percent": _value(
                samples, idle_source, windows["p2_immediate"]["start_tick"]
            ),
            "rested_deep_idle_percent": _value(
                samples, idle_source, windows["p2_rested"]["start_tick"]
            ),
        }
        observed["immediate_minus_rested_joules"] = (
            observed["p2_immediate_joules"] - observed["p2_rested_joules"]
        )
    elif truth_name == "treatment_timing_confounding.truth.json":
        quiet_control = _delta(samples, source, windows["quiet_control"])
        busy_control = _delta(samples, source, windows["busy_control"])
        quiet_treatment = _delta(samples, source, windows["quiet_treatment"])
        busy_treatment = _delta(samples, source, windows["busy_treatment"])
        observed = {
            "quiet_treatment_joules": quiet_treatment,
            "busy_treatment_joules": busy_treatment,
            "naive_busy_minus_quiet_joules": busy_treatment - quiet_treatment,
            "quiet_incremental_joules": quiet_treatment - quiet_control,
            "busy_incremental_joules": busy_treatment - busy_control,
        }
    else:  # pragma: no cover - guarded by the call site
        raise ValueError(f"unsupported truth fixture: {truth_name}")

    numeric_observed = {
        key: float(value)
        for key, value in observed.items()
        if not isinstance(value, bool)
    }
    return {
        "claim": truth["claim"],
        "evidence_class": "DETERMINISTIC_METHODOLOGY_VALIDATION",
        "expected": truth["expected"],
        "observed": observed,
        "passed": _close_map(numeric_observed, truth["expected"]),
        "scenario_file": truth["scenario_file"],
    }


def _evaluate_quality_fixture(raw_dir: Path) -> dict[str, Any]:
    truth = json.loads(
        (TRUTH / "quality_faults.truth.json").read_text(encoding="utf-8")
    )
    samples = _run_scenario(truth["scenario_file"], raw_dir)
    expected = truth["expected"]
    wrapped = int(expected["wrapped_source_id"])
    gapped = int(expected["gapped_source_id"])
    disappearing = int(expected["disappearing_source_id"])
    drifting = int(expected["drifting_source_id"])
    aliased = int(expected["aliased_source_id"])
    observed = {
        "wrapped_prefix": [_value(samples, wrapped, tick) for tick in range(3)],
        "missing_ticks": [tick for tick in range(10) if tick not in samples[gapped]],
        "last_present_tick": max(samples[disappearing]),
        "tick_9_timestamp_ns": samples[drifting][9][0],
        "aliased_observed_value": _value(samples, aliased, 0),
    }
    comparable = {
        key: expected[key]
        for key in observed
    }
    return {
        "claim": truth["claim"],
        "evidence_class": "DETERMINISTIC_METHODOLOGY_VALIDATION",
        "expected": comparable,
        "observed": observed,
        "passed": observed == comparable,
        "scenario_file": truth["scenario_file"],
        "note": (
            "Marker delay is exercised by the live integration test; dry-run "
            "validates wrap, gap, disappearance, drift, and alias observations."
        ),
    }


def run_deterministic_validation(output_dir: Path) -> dict[str, Any]:
    raw_dir = output_dir / "raw_testd"
    cases = {
        "additive_null": _evaluate_causal_fixture(
            "additive_null.truth.json", raw_dir
        ),
        "interference_reversal": _evaluate_causal_fixture(
            "interference_reversal.truth.json", raw_dir
        ),
        "low_priority_dvfs": _evaluate_causal_fixture(
            "low_priority_dvfs.truth.json", raw_dir
        ),
        "thermal_carryover": _evaluate_causal_fixture(
            "carryover_confounding.truth.json", raw_dir
        ),
        "treatment_timing": _evaluate_causal_fixture(
            "treatment_timing_confounding.truth.json", raw_dir
        ),
        "quality_faults": _evaluate_quality_fixture(raw_dir),
    }
    result = {
        "evidence_scope": (
            "Methodology and implementation validation only; scenarios contain "
            "planted effects and are not empirical evidence about real hardware."
        ),
        "cases": cases,
        "all_passed": all(case["passed"] for case in cases.values()),
    }
    _json_dump(output_dir / "deterministic_validation.json", result)
    return result


def _worker_command(mode: str, metrics_path: Path, *, background: bool) -> list[str]:
    return [
        sys.executable,
        str(WORKER),
        "--mode",
        mode,
        "--items",
        "768" if background else "384",
        "--arrival-interval-ms",
        "0.5",
        "--batch-size",
        "16",
        "--work-rounds",
        "128" if background else "48",
        "--timeout-s",
        "5",
        "--latency-slo-ms",
        "1000",
        "--producer-lateness-limit-ms",
        "50",
        "--metrics-path",
        str(metrics_path),
    ]


def _run_workload_arm(
    *,
    output_dir: Path,
    context: str,
    block: int,
    arm: str,
    seed: int,
) -> dict[str, Any]:
    mode = {"C0": "sham", "C1": "batching", "C2": "polling"}[arm]
    trial_id = f"{context}-block-{block:02d}-{arm.lower()}"
    trial_dir = output_dir / "workload_trials" / trial_id
    trial_dir.mkdir(parents=True, exist_ok=False)
    target_metrics = trial_dir / "target.metrics.json"
    environment = dict(os.environ)
    environment.update(
        {
            "WATTCH_INPUT_ID": "wattch-polling-batching-preliminary-v1",
            "WATTCH_REPLAY_SEED": str(seed),
            "WATTCH_ARRIVAL_TRACE_ID": f"target-{context}-{block:02d}",
        }
    )

    background_process: subprocess.Popen[bytes] | None = None
    background_metrics: Path | None = None
    if context == "cotenant":
        background_metrics = trial_dir / "background.metrics.json"
        background_environment = dict(environment)
        background_environment["WATTCH_ARRIVAL_TRACE_ID"] = (
            f"background-{context}-{block:02d}"
        )
        background_process = subprocess.Popen(
            _worker_command("batching", background_metrics, background=True),
            cwd=ROOT,
            env=background_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.02)

    started = time.monotonic()
    process = subprocess.run(
        _worker_command(mode, target_metrics, background=False),
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
    )
    wall_runtime_s = time.monotonic() - started

    background_returncode = None
    if background_process is not None:
        try:
            _, background_stderr = background_process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            background_process.terminate()
            _, background_stderr = background_process.communicate(timeout=2)
        background_returncode = background_process.returncode
        if background_returncode != 0:
            raise RuntimeError(
                f"background failed for {trial_id}: "
                f"{background_stderr.decode('utf-8', errors='replace')}"
            )

    if process.returncode != 0:
        raise RuntimeError(
            f"target failed for {trial_id}: "
            f"{process.stderr.decode('utf-8', errors='replace')}"
        )
    metrics = json.loads(target_metrics.read_text(encoding="ascii"))
    background_document = (
        json.loads(background_metrics.read_text(encoding="ascii"))
        if background_metrics is not None
        else None
    )
    stdout_digest = hashlib.sha256(process.stdout).hexdigest()
    return {
        "arm": arm,
        "background_metrics": background_document,
        "background_returncode": background_returncode,
        "block": block,
        "context": context,
        "mode": mode,
        "seed": seed,
        "stdout_sha256": stdout_digest,
        "target_metrics": metrics,
        "trial_id": trial_id,
        "wall_runtime_s": wall_runtime_s,
    }


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "sample_sd": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def run_workload_pilot(output_dir: Path, *, blocks: int) -> dict[str, Any]:
    permutations = list(itertools.permutations(("C0", "C1", "C2")))
    schedule = [permutations[index % len(permutations)] for index in range(blocks)]
    random.Random(20260719).shuffle(schedule)
    trials: list[dict[str, Any]] = []
    for context in ("isolated", "cotenant"):
        for block, order in enumerate(schedule, start=1):
            seed = 2026071900 + block
            for arm in order:
                trials.append(
                    _run_workload_arm(
                        output_dir=output_dir,
                        context=context,
                        block=block,
                        arm=arm,
                        seed=seed,
                    )
                )

    summaries: dict[str, Any] = {}
    for context in ("isolated", "cotenant"):
        summaries[context] = {}
        for arm in ("C0", "C1", "C2"):
            selected = [
                trial
                for trial in trials
                if trial["context"] == context and trial["arm"] == arm
            ]
            summaries[context][arm] = {
                "runtime_s": _summary(
                    [float(item["target_metrics"]["runtime_s"]) for item in selected]
                ),
                "p99_latency_ms": _summary(
                    [
                        float(item["target_metrics"]["latency_ms"]["p99"])
                        for item in selected
                    ]
                ),
                "throughput_per_s": _summary(
                    [
                        float(item["target_metrics"]["throughput_per_s"])
                        for item in selected
                    ]
                ),
            }

    variant_digests = {
        trial["stdout_sha256"]
        for trial in trials
        if trial["arm"] in {"C1", "C2"}
    }
    result = {
        "blocks_per_context": blocks,
        "evidence_scope": (
            "Workload admissibility and performance pilot only. No energy meter "
            "was available, so these observations cannot validate an energy effect."
        ),
        "output_equivalence": len(variant_digests) == 1,
        "position_balanced": all(
            abs(
                sum(1 for order in schedule if order[position] == arm)
                - blocks / 3
            )
            <= 1
            for position in range(3)
            for arm in ("C0", "C1", "C2")
        ),
        "schedule": [list(order) for order in schedule],
        "summaries": summaries,
        "trials": trials,
        "variant_stdout_sha256": sorted(variant_digests),
    }
    _json_dump(output_dir / "workload_pilot.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "fresh output directory; defaults to a timestamped directory under "
            "research/system-energy-impact/results/reruns"
        ),
    )
    parser.add_argument("--blocks", type=int, default=12)
    parser.add_argument("--skip-workload", action="store_true")
    arguments = parser.parse_args()
    if arguments.blocks <= 0:
        parser.error("--blocks must be positive")
    output_dir = arguments.output_dir
    if output_dir is None:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        output_dir = (
            ROOT
            / "research"
            / "system-energy-impact"
            / "results"
            / "reruns"
            / timestamp
        )
    output_dir.mkdir(parents=True, exist_ok=False)

    deterministic = run_deterministic_validation(output_dir)
    workload = (
        None
        if arguments.skip_workload
        else run_workload_pilot(output_dir, blocks=arguments.blocks)
    )
    summary = {
        "deterministic_all_passed": deterministic["all_passed"],
        "real_meter_available": False,
        "real_meter_blocker": (
            "This macOS host exposes powermetrics only to a privileged daemon; "
            "non-interactive sudo was unavailable in this session."
        ),
        "workload_output_equivalence": (
            workload["output_equivalence"] if workload is not None else None
        ),
        "workload_position_balanced": (
            workload["position_balanced"] if workload is not None else None
        ),
    }
    summary["output_dir"] = os.path.relpath(output_dir, ROOT)
    _json_dump(output_dir / "preliminary_summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if deterministic["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
