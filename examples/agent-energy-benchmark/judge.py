#!/usr/bin/env python3
"""Trusted correctness oracle for the agent-energy benchmark.

The artifact judge never imports or executes the submitted candidate. It judges
the exact stdout/stderr bytes already captured by ``wattch run`` and verifies
the recorded direct argv/cwd contract. Public-API probes run only in a separate
process and are intended to run after measured trials.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


EVENT_KINDS = ("read", "write", "cache_hit", "cache_miss")
MASK_64 = (1 << 64) - 1
REQUIRED_PUBLIC_CALLS = {
    "generate_events": ("count", "accounts", "seed"),
    "summarize": ("events",),
    "canonical_bytes": ("summary",),
    "run": ("count", "accounts", "seed", "repeat"),
    "positive_int": ("raw",),
    "parse_args": (),
    "main": (),
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def reference_summary(events: list[tuple[int, int, int]]) -> list[dict[str, object]]:
    rows: dict[int, dict[str, object]] = {}
    for account, kind, value in events:
        row = rows.get(account)
        if row is None:
            row = {
                "account": account,
                "count": 0,
                "kind_counts": {name: 0 for name in EVENT_KINDS},
                "kind_values": {name: 0 for name in EVENT_KINDS},
                "rolling": 0,
                "total_value": 0,
            }
            rows[account] = row
        name = EVENT_KINDS[kind]
        row["count"] += 1
        row["kind_counts"][name] += 1
        row["kind_values"][name] += value
        row["rolling"] = (
            row["rolling"] * 1_000_003 + kind * 1_009 + value
        ) & MASK_64
        row["total_value"] += value
    return [rows[key] for key in sorted(rows)]


def reference_events(count: int, accounts: int, seed: int) -> list[tuple[int, int, int]]:
    state = seed & MASK_64
    events: list[tuple[int, int, int]] = []
    for _ in range(count):
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK_64
        events.append(
            (
                (state >> 17) % accounts,
                (state >> 41) % len(EVENT_KINDS),
                1 + ((state >> 7) % 997),
            )
        )
    return events


def expected_output(count: int, accounts: int, seed: int, repeat: int) -> dict[str, object]:
    events = reference_events(count, accounts, seed)
    summary = reference_summary(events) if repeat > 0 else []
    encoded = json.dumps(
        summary,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    digest = hashlib.sha256()
    for _ in range(repeat):
        digest.update(encoded)
    return {
        "accounts": len(summary),
        "events": count,
        "repeat": repeat,
        "result_sha256": digest.hexdigest(),
        "schema_version": 1,
        "seed": seed,
    }


def expected_stdout(count: int, accounts: int, seed: int, repeat: int) -> bytes:
    return (
        json.dumps(
            expected_output(count, accounts, seed, repeat),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def candidate_argv(
    python: Path,
    candidate: Path,
    count: int,
    accounts: int,
    seed: int,
    repeat: int,
) -> list[str]:
    return [
        str(python.resolve()),
        "-I",
        "-B",
        str(candidate.resolve()),
        "--events",
        str(count),
        "--accounts",
        str(accounts),
        "--seed",
        str(seed),
        "--repeat",
        str(repeat),
    ]


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def judge_artifact(
    run_dir: Path,
    *,
    expected_argv: list[str],
    expected_cwd: Path,
    count: int,
    accounts: int,
    seed: int,
    repeat: int,
    child_exit_code: int | None,
) -> dict[str, object]:
    """Judge one already captured execution without loading candidate code."""

    run_dir = run_dir.resolve()
    expected_bytes = expected_stdout(count, accounts, seed, repeat)
    stdout = (run_dir / "target.stdout.log").read_bytes()
    stderr = (run_dir / "target.stderr.log").read_bytes()
    command = _read_json_object(run_dir / "command.json")

    stdout_exact = stdout == expected_bytes
    stderr_exact = stderr == b""
    command_exact = command.get("argv") == expected_argv
    cwd_exact = command.get("cwd") == str(expected_cwd.resolve())
    exit_exact = child_exit_code == 0
    correct = all((stdout_exact, stderr_exact, command_exact, cwd_exact, exit_exact))
    return {
        "schema_version": 1,
        "run_dir": str(run_dir),
        "correct": correct,
        "completed_work": correct,
        "stdout_exact": stdout_exact,
        "stderr_exact": stderr_exact,
        "command_exact": command_exact,
        "cwd_exact": cwd_exact,
        "exit_exact": exit_exact,
        "child_exit_code": child_exit_code,
        "expected_stdout_bytes": len(expected_bytes),
        "actual_stdout_bytes": len(stdout),
        "expected_stdout_sha256": sha256_bytes(expected_bytes),
        "actual_stdout_sha256": sha256_bytes(stdout),
        "expected_stderr_sha256": sha256_bytes(b""),
        "actual_stderr_sha256": sha256_bytes(stderr),
        "useful_work": {
            "events": count,
            "accounts": accounts,
            "seed": seed,
            "repeat": repeat,
        },
    }


def _load_candidate(path: Path):
    spec = importlib.util.spec_from_file_location("judged_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    import_stdout = io.StringIO()
    import_stderr = io.StringIO()
    with contextlib.redirect_stdout(import_stdout), contextlib.redirect_stderr(import_stderr):
        spec.loader.exec_module(module)
    if import_stdout.getvalue() or import_stderr.getvalue():
        raise AssertionError("candidate import must not write stdout or stderr")
    return module


def probe_candidate(path: Path, python: Path) -> dict[str, object]:
    """Probe public behavior in this disposable, unmeasured judge process."""

    path = path.resolve()
    module = _load_candidate(path)
    failures: list[str] = []

    for name, parameters in REQUIRED_PUBLIC_CALLS.items():
        value = getattr(module, name, None)
        if not callable(value):
            failures.append(f"missing public callable {name}")
            continue
        actual_parameters = tuple(inspect.signature(value).parameters)
        if actual_parameters != parameters:
            failures.append(
                f"public signature {name}{actual_parameters!r} != {parameters!r}"
            )

    event_cases = ((1, 1, 0), (257, 31, 0x5A5A), (4097, 97, -11))
    for count, accounts, seed in event_cases:
        expected = reference_events(count, accounts, seed)
        if module.generate_events(count, accounts, seed) != expected:
            failures.append(f"generate_events mismatch for {(count, accounts, seed)!r}")
        if module.summarize(expected) != reference_summary(expected):
            failures.append(f"summarize mismatch for {(count, accounts, seed)!r}")

    sample = reference_summary(reference_events(37, 7, 91))
    encoded = json.dumps(
        sample,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if module.canonical_bytes(sample) != encoded:
        failures.append("canonical_bytes mismatch")

    cli_cases = ((73, 9, 17, 2), (1021, 61, -91, 3))
    run_cases = (*cli_cases, (73, 9, 17, 0), (73, 9, 17, -2))
    for count, accounts, seed, repeat in run_cases:
        expected = expected_output(count, accounts, seed, repeat)
        if module.run(count, accounts, seed, repeat) != expected:
            failures.append(f"run mismatch for {(count, accounts, seed, repeat)!r}")
    for count, accounts, seed, repeat in cli_cases:
        argv = candidate_argv(python, path, count, accounts, seed, repeat)
        completed = subprocess.run(argv, capture_output=True, check=False)
        if completed.returncode != 0:
            failures.append(f"CLI returned {completed.returncode} for {argv[4:]!r}")
        if completed.stdout != expected_stdout(count, accounts, seed, repeat):
            failures.append(f"CLI stdout mismatch for {argv[4:]!r}")
        if completed.stderr != b"":
            failures.append(f"CLI stderr was not empty for {argv[4:]!r}")

    return {
        "schema_version": 1,
        "candidate": str(path),
        "candidate_sha256": sha256_bytes(path.read_bytes()),
        "correct": not failures,
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    artifact = subparsers.add_parser("artifact")
    artifact.add_argument("--run", type=Path, required=True)
    artifact.add_argument("--candidate", type=Path, required=True)
    artifact.add_argument("--python", type=Path, default=Path(sys.executable))
    artifact.add_argument("--cwd", type=Path, required=True)
    artifact.add_argument("--events", type=int, default=300_000)
    artifact.add_argument("--accounts", type=int, default=512)
    artifact.add_argument("--seed", type=int, default=20260719)
    artifact.add_argument("--repeat", type=int, default=6)
    artifact.add_argument("--child-exit-code", type=int, required=True)

    probe = subparsers.add_parser("probe")
    probe.add_argument("--candidate", type=Path, required=True)
    probe.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "artifact":
        argv = candidate_argv(
            args.python,
            args.candidate,
            args.events,
            args.accounts,
            args.seed,
            args.repeat,
        )
        result = judge_artifact(
            args.run,
            expected_argv=argv,
            expected_cwd=args.cwd,
            count=args.events,
            accounts=args.accounts,
            seed=args.seed,
            repeat=args.repeat,
            child_exit_code=args.child_exit_code,
        )
    else:
        result = probe_candidate(args.candidate, args.python)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if result["correct"] else 1)


if __name__ == "__main__":
    main()
