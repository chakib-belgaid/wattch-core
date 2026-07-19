#!/usr/bin/env python3
"""Collect and seal an exploratory physical-meter calibration.

Calibration is deliberately staged.  ``instrumented`` captures immutable raw
Wattch evidence while the privileged collector is running.  ``baseline`` must
run only after the collector and daemon are stopped.  ``finalize`` pairs those
records offline and publishes schema-v1 calibration only when every strict
quality threshold passes.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "clients" / "python"))

import physical_study  # noqa: E402
from physical_calibration_workload import fixed_work  # noqa: E402
from pywattch._frames import FrameKind  # noqa: E402
from pywattch._messages import SourceDescriptor  # noqa: E402
from pywattch.analysis.boundary import MeasurementBoundary  # noqa: E402
from pywattch.analysis.calibration import (  # noqa: E402
    SamplingIntervalObservation,
    calculate_measurement_overhead,
    select_fastest_sampling_interval,
    summarize_source_quality,
    write_meter_calibration_artifact,
)
from pywattch.analysis.outcomes import Estimand, compute_energy_outcome  # noqa: E402
from pywattch.analysis.replay import _read_trace  # noqa: E402
from summarize_run import select_window  # noqa: E402


SCHEMA_VERSION = 1
INSTRUMENTED_KIND = "wattch-meter-calibration-instrumented"
BASELINE_KIND = "wattch-meter-calibration-baseline"
ASSESSMENT_KIND = "wattch-meter-calibration-assessment"
REGIMES = ("idle", "fixed-load", "aa")
DEFAULT_PHASE_FRACTIONS = (0.0, 0.25, 0.5, 0.75)
CALIBRATION_BOUNDARY_ID = physical_study.PHYSICAL_BOUNDARY_ID


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


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def parse_phase_fractions(value: str) -> tuple[float, ...]:
    try:
        fractions = tuple(float(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("phase fractions must be numeric") from error
    if (
        len(fractions) < 2
        or len(set(fractions)) != len(fractions)
        or any(not math.isfinite(item) or item < 0 or item >= 1 for item in fractions)
    ):
        raise argparse.ArgumentTypeError(
            "use at least two unique phase fractions in the range [0, 1)"
        )
    return fractions


def _workload_command(
    plan: dict[str, Any],
    *,
    regime: str,
    idle_seconds: float,
    fixed_iterations: int,
    seed: int,
) -> list[str]:
    workload = plan["workload"]
    prefix = [
        str(Path(workload["sandbox_exec_path"]).resolve(strict=True)),
        "-p",
        physical_study.NO_FORK_SANDBOX_PROFILE,
        str(Path(workload["python_path"]).resolve(strict=True)),
        "-I",
        "-B",
        str((HERE / "physical_calibration_workload.py").resolve(strict=True)),
    ]
    if regime == "idle":
        return [*prefix, "idle", "--duration-s", str(idle_seconds)]
    return [
        *prefix,
        "fixed",
        "--iterations",
        str(fixed_iterations),
        "--seed",
        str(seed),
    ]


def _expected_stdout(
    *, regime: str, idle_seconds: float, fixed_result: dict[str, object]
) -> bytes:
    value = (
        {"duration_s": idle_seconds, "mode": "idle"}
        if regime == "idle"
        else fixed_result
    )
    return canonical_json_bytes(value)


def _schedule(
    *, repetitions: int, phase_fractions: tuple[float, ...], seed: int
) -> list[dict[str, object]]:
    entries = [
        {
            "regime": regime,
            "phase_index": phase_index,
            "phase_fraction": phase_fraction,
            "repetition": repetition,
        }
        for repetition in range(1, repetitions + 1)
        for phase_index, phase_fraction in enumerate(phase_fractions)
        for regime in REGIMES
    ]
    random.Random(seed).shuffle(entries)
    for index, entry in enumerate(entries, start=1):
        entry["execution_order"] = index
        entry["observation_id"] = (
            f"cal-{entry['regime']}-p{entry['phase_index']}-r{entry['repetition']}"
        )
    return entries


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    names = (
        "manifest.json",
        "sources.json",
        "raw.trace.wch",
        "events.trace.wch",
        "command.json",
        "command.lifecycle.jsonl",
        "target.stdout.log",
        "target.stderr.log",
    )
    return {
        name: hash_file(run_dir / name)
        for name in names
        if (run_dir / name).is_file() and not (run_dir / name).is_symlink()
    }


def _verify_file_bindings(bindings: dict[Path, str]) -> None:
    for path, expected_sha256 in bindings.items():
        if hash_file(path) != expected_sha256:
            raise ValueError(f"calibration input changed during collection: {path}")


def _analyze_run(
    run_dir: Path,
    *,
    source_uid: str,
    boundary: MeasurementBoundary,
    expected_daemon: dict[str, Any],
) -> dict[str, object]:
    manifest = read_object(run_dir / "manifest.json")
    daemon_identity = {
        "clock_instance_id": manifest.get("clock_instance_id"),
        "node_id": manifest.get("node_id"),
        "protocol_version": manifest.get("protocol_version"),
    }
    expected_identity = {
        "clock_instance_id": expected_daemon["clock_instance_id"],
        "node_id": expected_daemon["node_id"],
        "protocol_version": expected_daemon["protocol_version"],
    }
    if daemon_identity != expected_identity:
        raise ValueError("daemon identity changed during calibration collection")
    descriptors = tuple(
        SourceDescriptor.from_dict(item)
        for item in json.loads((run_dir / "sources.json").read_text("utf-8"))
    )
    selected = boundary.validate_descriptors(descriptors)
    if tuple(source.source_uid for source in selected) != (source_uid,):
        raise ValueError("capture does not preserve the calibration boundary")
    raw_batches = _read_trace(run_dir / "raw.trace.wch", FrameKind.RAW_SAMPLE_BATCH)
    samples = tuple(sample for batch in raw_batches for sample in batch.samples)
    event_batches = _read_trace(run_dir / "events.trace.wch", FrameKind.EVENT_BATCH)
    start_kind, end_kind, start_ns, end_ns = select_window(event_batches)
    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=descriptors,
        samples=samples,
        start_monotonic_ns=start_ns,
        end_monotonic_ns=end_ns,
        estimand=Estimand.EQUAL_WORK,
    )
    quality = {
        item.source_uid: item
        for item in summarize_source_quality(descriptors, samples)
    }
    by_uid = {item.source_uid: item for item in descriptors}
    source = by_uid[source_uid]
    required_uids = [source_uid]
    if source.temporality == "INTERVAL_AVERAGE":
        duration_uid = source.labels.get("wattch.interval_duration_source_uid")
        if not duration_uid or duration_uid not in quality:
            raise ValueError("interval-average source lacks captured duration evidence")
        required_uids.append(duration_uid)
    missing_ratios = {
        uid: quality[uid].missing_sample_ratio for uid in required_uids
    }
    if any(value is None for value in missing_ratios.values()):
        raise ValueError("capture has indeterminate source missingness")
    return {
        "artifact_sha256": _artifact_hashes(run_dir),
        "daemon_identity": daemon_identity,
        "end_marker": end_kind,
        "energy_outcome": outcome.to_dict(),
        "marker_duration_s": (end_ns - start_ns) / 1e9,
        "marker_start_monotonic_ns": start_ns,
        "required_source_missing_sample_ratios": missing_ratios,
        "source_quality": {uid: quality[uid].to_dict() for uid in required_uids},
        "start_marker": start_kind,
    }


def collect_instrumented(args: argparse.Namespace) -> dict[str, object]:
    plan_path = args.plan.resolve(strict=True)
    plan = read_object(plan_path)
    wattch = args.wattch.resolve(strict=True)
    coordinator_path = Path(__file__).resolve(strict=True)
    workload_path = (HERE / "physical_calibration_workload.py").resolve(strict=True)
    bindings = {
        plan_path: hash_file(plan_path),
        wattch: hash_file(wattch),
        coordinator_path: hash_file(coordinator_path),
        workload_path: hash_file(workload_path),
    }
    readiness = physical_study.preflight(
        plan, socket=args.socket.resolve(), calibration_path=None
    )
    _verify_file_bindings(bindings)
    source_uid = str(readiness["selected_source"]["source_uid"])
    boundary = MeasurementBoundary.from_dict(readiness["measurement_boundary"])
    interval_ns = int(plan["acquisition"]["interval_ms"]) * 1_000_000
    minimum_duration_s = float(plan["acquisition"]["minimum_target_duration_s"])
    output_root = args.out.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    runs_root = output_root / "runs"
    observations_root = output_root / "observations"
    runs_root.mkdir()
    observations_root.mkdir()
    fixed_result = fixed_work(args.fixed_iterations, args.workload_seed)
    schedule = _schedule(
        repetitions=args.repetitions,
        phase_fractions=args.phase_fractions,
        seed=args.schedule_seed,
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    observations: list[dict[str, object]] = []
    for item in schedule:
        _verify_file_bindings(bindings)
        regime = str(item["regime"])
        observation_id = str(item["observation_id"])
        command = _workload_command(
            plan,
            regime=regime,
            idle_seconds=args.idle_seconds,
            fixed_iterations=args.fixed_iterations,
            seed=args.workload_seed,
        )
        expected_stdout = _expected_stdout(
            regime=regime,
            idle_seconds=args.idle_seconds,
            fixed_result=fixed_result,
        )
        if args.cooldown_s:
            time.sleep(args.cooldown_s)
        phase_delay_s = float(item["phase_fraction"]) * interval_ns / 1e9
        if phase_delay_s:
            time.sleep(phase_delay_s)
        wattch_command = [
            str(wattch),
            "run",
            "--socket",
            str(args.socket.resolve()),
            "--out",
            str(runs_root),
            "--interval-ms",
            str(plan["acquisition"]["poll_interval_ms"]),
            "--timeout-ms",
            str(args.timeout_ms),
            "--run-id",
            observation_id,
            "--label",
            "wattch.calibration=true",
            "--label",
            f"wattch.calibration.regime={regime}",
            "--json",
            "--",
            *command,
        ]
        completed = subprocess.run(
            wattch_command,
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            timeout=args.timeout_ms / 1000 + 30,
        )
        _verify_file_bindings(bindings)
        try:
            wrapper = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(
                f"{observation_id} returned invalid Wattch JSON"
            ) from error
        run_dir = runs_root / observation_id
        if (
            completed.returncode != 0
            or not isinstance(wrapper, dict)
            or wrapper.get("exit_code") != 0
            or (run_dir / "target.stdout.log").read_bytes() != expected_stdout
            or (run_dir / "target.stderr.log").read_bytes() != b""
            or read_object(run_dir / "command.json").get("argv") != command
        ):
            raise ValueError(f"{observation_id} failed its exact workload gates")
        try:
            analysis = _analyze_run(
                run_dir,
                source_uid=source_uid,
                boundary=boundary,
                expected_daemon=readiness["daemon"],
            )
        except Exception as error:
            write_new(
                observations_root / f"{observation_id}.failure.json",
                {
                    **item,
                    "artifact_sha256": _artifact_hashes(run_dir),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "run_dir": str(run_dir),
                },
            )
            raise
        if float(analysis["marker_duration_s"]) < minimum_duration_s:
            raise ValueError(f"{observation_id} is shorter than the calibrated window")
        selected_quality = analysis["source_quality"][source_uid]
        if (
            int(selected_quality["observed_samples"])
            < plan["acquisition"]["minimum_selected_source_samples"]
        ):
            raise ValueError(f"{observation_id} has too few selected-source samples")
        actual_phase_fraction = (
            int(analysis["marker_start_monotonic_ns"]) % interval_ns
        ) / interval_ns
        observation = {
            **item,
            "actual_marker_phase_fraction": actual_phase_fraction,
            "command": command,
            "expected_stdout_sha256": hashlib.sha256(expected_stdout).hexdigest(),
            "phase_delay_s": phase_delay_s,
            "run_dir": str(run_dir),
            **analysis,
        }
        write_new(observations_root / f"{observation_id}.json", observation)
        observations.append(observation)
    document = {
        "schema_version": SCHEMA_VERSION,
        "record_type": INSTRUMENTED_KIND,
        "created_unix_ns": time.time_ns(),
        "plan_path": str(plan_path),
        "plan_sha256": bindings[plan_path],
        "wattch_path": str(wattch),
        "wattch_sha256": bindings[wattch],
        "coordinator_path": str(coordinator_path),
        "coordinator_sha256": bindings[coordinator_path],
        "workload_path": str(workload_path),
        "workload_sha256": bindings[workload_path],
        "daemon": readiness["daemon"],
        "selected_source": readiness["selected_source"],
        "measurement_boundary": boundary.to_dict(),
        "interval_ns": interval_ns,
        "parameters": {
            "cooldown_s": args.cooldown_s,
            "fixed_iterations": args.fixed_iterations,
            "idle_seconds": args.idle_seconds,
            "phase_fractions": list(args.phase_fractions),
            "poll_interval_ms": plan["acquisition"]["poll_interval_ms"],
            "repetitions": args.repetitions,
            "schedule_seed": args.schedule_seed,
            "workload_seed": args.workload_seed,
        },
        "observations": observations,
    }
    write_new(output_root / "instrumented.json", document)
    return document


def _assert_no_collector(socket: Path) -> dict[str, object]:
    if socket.exists():
        raise ValueError(f"collector socket still exists: {socket}")
    completed = subprocess.run(
        ["/bin/ps", "-axo", "pid=,comm=,args="],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError("cannot verify that the privileged collector is stopped")
    listing = completed.stdout.decode("utf-8", errors="replace")
    conflicts: list[str] = []
    for line in listing.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        command_name = Path(fields[1]).name
        argv0 = Path(fields[2].split(None, 1)[0]).name if len(fields) == 3 else ""
        if command_name in {"powermetrics", "wattch-powerd"} or argv0 in {
            "powermetrics",
            "wattch-powerd",
        }:
            conflicts.append(line.strip())
    if conflicts:
        raise ValueError(
            "no-collector baseline requires wattch-powerd and powermetrics to be "
            "stopped; found: " + "; ".join(conflicts)
        )
    return {
        "socket_absent": True,
        "process_listing_command": ["/bin/ps", "-axo", "pid=,comm=,args="],
        "process_listing_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "collector_processes_found": [],
    }


def collect_baseline(args: argparse.Namespace) -> dict[str, object]:
    instrumented_path = args.instrumented.resolve(strict=True)
    instrumented = read_object(instrumented_path)
    if instrumented.get("record_type") != INSTRUMENTED_KIND:
        raise ValueError("instrumented input has the wrong record type")
    plan_path = Path(instrumented["plan_path"]).resolve(strict=True)
    if hash_file(plan_path) != instrumented["plan_sha256"]:
        raise ValueError("physical plan changed after instrumented collection")
    physical_study.verify_plan(read_object(plan_path))
    if (
        hash_file(Path(instrumented["coordinator_path"]))
        != instrumented["coordinator_sha256"]
    ):
        raise ValueError(
            "calibration coordinator changed after instrumented collection"
        )
    if (
        hash_file(Path(instrumented["workload_path"]))
        != instrumented["workload_sha256"]
    ):
        raise ValueError("calibration workload changed after instrumented collection")
    collector_check = _assert_no_collector(args.socket.resolve())
    cooldown_s = (
        float(instrumented["parameters"]["cooldown_s"])
        if args.cooldown_s is None
        else args.cooldown_s
    )
    output_root = args.out.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    observations: list[dict[str, object]] = []
    for item in instrumented["observations"]:
        if cooldown_s:
            time.sleep(cooldown_s)
        phase_delay_s = float(item["phase_delay_s"])
        if phase_delay_s:
            time.sleep(phase_delay_s)
        started_ns = time.monotonic_ns()
        completed = subprocess.run(
            item["command"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            timeout=args.timeout_ms / 1000,
        )
        ended_ns = time.monotonic_ns()
        if (
            completed.returncode != 0
            or hashlib.sha256(completed.stdout).hexdigest()
            != item["expected_stdout_sha256"]
            or completed.stderr != b""
        ):
            raise ValueError(f"baseline {item['observation_id']} failed exact gates")
        observations.append(
            {
                "observation_id": item["observation_id"],
                "regime": item["regime"],
                "phase_index": item["phase_index"],
                "repetition": item["repetition"],
                "duration_s": (ended_ns - started_ns) / 1e9,
                "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
            }
        )
    document = {
        "schema_version": SCHEMA_VERSION,
        "record_type": BASELINE_KIND,
        "created_unix_ns": time.time_ns(),
        "instrumented_path": str(instrumented_path),
        "instrumented_sha256": hash_file(instrumented_path),
        "collector_check": collector_check,
        "cooldown_s": cooldown_s,
        "observations": observations,
    }
    write_new(output_root / "baseline.json", document)
    return document


def phase_sensitivity_ratio(observations: Iterable[dict[str, Any]]) -> float:
    """Return relative spread across phase-specific mean energy outcomes."""

    by_phase: dict[int, list[float]] = defaultdict(list)
    for item in observations:
        energy = float(item["energy_outcome"]["total_joules"])
        if not math.isfinite(energy) or energy <= 0:
            raise ValueError("phase sensitivity requires positive finite energy")
        by_phase[int(item["phase_index"])].append(energy)
    if len(by_phase) < 2:
        raise ValueError("phase sensitivity requires at least two phase groups")
    phase_means = [statistics.fmean(values) for values in by_phase.values()]
    center = statistics.fmean(phase_means)
    return (max(phase_means) - min(phase_means)) / center


def finalize(args: argparse.Namespace) -> dict[str, object]:
    instrumented_path = args.instrumented.resolve(strict=True)
    baseline_path = args.baseline.resolve(strict=True)
    instrumented = read_object(instrumented_path)
    baseline = read_object(baseline_path)
    if instrumented.get("record_type") != INSTRUMENTED_KIND:
        raise ValueError("instrumented input has the wrong record type")
    if baseline.get("record_type") != BASELINE_KIND:
        raise ValueError("baseline input has the wrong record type")
    if baseline.get("instrumented_sha256") != hash_file(instrumented_path):
        raise ValueError("baseline is not bound to this instrumented record")
    instrumented_by_id = {
        item["observation_id"]: item for item in instrumented["observations"]
    }
    baseline_by_id = {
        item["observation_id"]: item for item in baseline["observations"]
    }
    if set(instrumented_by_id) != set(baseline_by_id):
        raise ValueError("baseline and instrumented observation ids differ")
    interval_ns = int(instrumented["interval_ns"])
    calibration_observations: list[SamplingIntervalObservation] = []
    regime_details: dict[str, object] = {}
    for regime in REGIMES:
        measured = [
            item
            for item in instrumented["observations"]
            if item["regime"] == regime
        ]
        if not measured:
            raise ValueError(f"calibration lacks {regime} observations")
        reference = [baseline_by_id[item["observation_id"]] for item in measured]
        overhead = calculate_measurement_overhead(
            baseline_durations_s=(item["duration_s"] for item in reference),
            instrumented_durations_s=(item["marker_duration_s"] for item in measured),
        )
        missing = max(
            float(value)
            for item in measured
            for value in item["required_source_missing_sample_ratios"].values()
        )
        phase = phase_sensitivity_ratio(measured)
        calibration_observations.append(
            SamplingIntervalObservation(
                interval_ns=interval_ns,
                regime=regime,
                measurement_overhead_ratio=overhead.overhead_ratio,
                missing_sample_ratio=missing,
                phase_sensitivity_ratio=phase,
            )
        )
        regime_details[regime] = {
            "measurement_overhead": overhead.to_dict(),
            "missing_sample_ratio": missing,
            "phase_sensitivity_ratio": phase,
        }
    selection = select_fastest_sampling_interval(calibration_observations)
    assessment_path = args.assessment.resolve()
    assessment = {
        "schema_version": SCHEMA_VERSION,
        "record_type": ASSESSMENT_KIND,
        "created_unix_ns": time.time_ns(),
        "instrumented_path": str(instrumented_path),
        "instrumented_sha256": hash_file(instrumented_path),
        "baseline_path": str(baseline_path),
        "baseline_sha256": hash_file(baseline_path),
        "regimes": regime_details,
        "selection": selection.to_dict(),
        "eligible": selection.selected_interval_ns is not None,
    }
    write_new(assessment_path, assessment)
    result: dict[str, object] = {
        "assessment_path": str(assessment_path),
        "assessment_sha256": hash_file(assessment_path),
        "eligible": assessment["eligible"],
    }
    if assessment["eligible"] is True:
        boundary = MeasurementBoundary.from_dict(instrumented["measurement_boundary"])
        sealed = write_meter_calibration_artifact(
            args.out.resolve(),
            calibration_id=args.calibration_id,
            boundary=boundary,
            selection=selection,
        )
        result.update(
            {
                "calibration_path": str(sealed.path),
                "calibration_sha256": sealed.sha256,
            }
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    instrumented = subparsers.add_parser("instrumented")
    instrumented.add_argument("--plan", type=Path, required=True)
    instrumented.add_argument("--socket", type=Path, required=True)
    instrumented.add_argument("--wattch", type=Path, required=True)
    instrumented.add_argument("--out", type=Path, required=True)
    instrumented.add_argument("--repetitions", type=int, default=2)
    instrumented.add_argument(
        "--phase-fractions",
        type=parse_phase_fractions,
        default=DEFAULT_PHASE_FRACTIONS,
    )
    instrumented.add_argument("--idle-seconds", type=float, default=5.5)
    instrumented.add_argument("--fixed-iterations", type=int, default=45_000_000)
    instrumented.add_argument("--workload-seed", type=int, default=20260720)
    instrumented.add_argument("--schedule-seed", type=int, default=20260720)
    instrumented.add_argument("--cooldown-s", type=float, default=5.0)
    instrumented.add_argument("--timeout-ms", type=int, default=120_000)

    baseline = subparsers.add_parser("baseline")
    baseline.add_argument("--instrumented", type=Path, required=True)
    baseline.add_argument("--socket", type=Path, required=True)
    baseline.add_argument("--out", type=Path, required=True)
    baseline.add_argument("--cooldown-s", type=float)
    baseline.add_argument("--timeout-ms", type=int, default=120_000)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--instrumented", type=Path, required=True)
    finalize_parser.add_argument("--baseline", type=Path, required=True)
    finalize_parser.add_argument("--assessment", type=Path, required=True)
    finalize_parser.add_argument("--out", type=Path, required=True)
    finalize_parser.add_argument("--calibration-id", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "instrumented":
        if (
            args.repetitions <= 0
            or args.idle_seconds <= 0
            or args.fixed_iterations <= 0
            or args.cooldown_s < 0
            or args.timeout_ms <= 0
        ):
            raise ValueError("instrumented calibration arguments are invalid")
        result = collect_instrumented(args)
    elif args.command == "baseline":
        if (
            (args.cooldown_s is not None and args.cooldown_s < 0)
            or args.timeout_ms <= 0
        ):
            raise ValueError("baseline calibration arguments are invalid")
        result = collect_baseline(args)
    else:
        result = finalize(args)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if args.command == "finalize" and result["eligible"] is not True:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
