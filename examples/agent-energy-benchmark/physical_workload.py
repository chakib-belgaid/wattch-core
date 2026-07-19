#!/usr/bin/env python3
"""Amplify one frozen candidate implementation for physical measurement.

The driver imports the already-judged candidate by exact content hash and calls
its public ``run`` function repeatedly inside one process.  Wattch owns the
measurement markers outside this program.  A separate trusted judge computes
the expected aggregate stdout; this driver does not decide correctness.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
from typing import Any, Iterable


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_candidate(path: Path, expected_sha256: str) -> Any:
    if path.is_symlink():
        raise ValueError("candidate must be one regular, non-symlink file")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("candidate must be one regular, non-symlink file")
    if sha256_file(path) != expected_sha256:
        raise ValueError("candidate SHA-256 does not match the frozen study plan")

    spec = importlib.util.spec_from_file_location("physical_study_candidate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load candidate {path}")
    module = importlib.util.module_from_spec(spec)
    import_stdout = io.StringIO()
    import_stderr = io.StringIO()
    with contextlib.redirect_stdout(import_stdout), contextlib.redirect_stderr(
        import_stderr
    ):
        spec.loader.exec_module(module)
    if import_stdout.getvalue() or import_stderr.getvalue():
        raise ValueError("candidate import must not write stdout or stderr")
    if not callable(getattr(module, "run", None)):
        raise ValueError("candidate must expose a callable run function")
    return module


def aggregate_result(
    iteration_results: Iterable[object],
    *,
    events: int,
    accounts: int,
    seed: int,
    repeat: int,
    iterations: int,
) -> dict[str, object]:
    digest = hashlib.sha256()
    first_encoded: bytes | None = None
    last_encoded: bytes | None = None
    observed_iterations = 0
    for iteration_result in iteration_results:
        encoded = canonical_json_bytes(iteration_result)
        if first_encoded is None:
            first_encoded = encoded
        last_encoded = encoded
        digest.update(encoded)
        observed_iterations += 1
    if observed_iterations != iterations or first_encoded is None or last_encoded is None:
        raise ValueError("iteration results do not match the declared useful work")
    return {
        "accounts": accounts,
        "aggregate_sha256": digest.hexdigest(),
        "events_per_iteration": events,
        "iteration_result_sha256": hashlib.sha256(first_encoded).hexdigest(),
        "last_iteration_result_sha256": hashlib.sha256(last_encoded).hexdigest(),
        "iterations": iterations,
        "repeat": repeat,
        "schema_version": 1,
        "seed": seed,
        "total_events": events * iterations,
    }


def run_workload(
    candidate: Any,
    *,
    events: int,
    accounts: int,
    seed: int,
    repeat: int,
    iterations: int,
) -> dict[str, object]:
    digest = hashlib.sha256()
    first_encoded: bytes | None = None
    last_encoded: bytes | None = None
    for iteration in range(iterations):
        result = candidate.run(events, accounts, seed + iteration, repeat)
        encoded = canonical_json_bytes(result)
        if first_encoded is None:
            first_encoded = encoded
        last_encoded = encoded
        digest.update(encoded)

    if first_encoded is None or last_encoded is None:
        raise AssertionError("positive iterations produced no result")
    return {
        "accounts": accounts,
        "aggregate_sha256": digest.hexdigest(),
        "events_per_iteration": events,
        "iteration_result_sha256": hashlib.sha256(first_encoded).hexdigest(),
        "last_iteration_result_sha256": hashlib.sha256(last_encoded).hexdigest(),
        "iterations": iterations,
        "repeat": repeat,
        "schema_version": 1,
        "seed": seed,
        "total_events": events * iterations,
    }


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


@contextlib.contextmanager
def wall_clock_timeout(timeout_ms: int):
    """Bound import plus useful work so the capture wrapper can finalize."""

    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"physical workload exceeded {timeout_ms} ms")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_ms / 1000)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--events", type=positive_int, default=300_000)
    parser.add_argument("--accounts", type=positive_int, default=512)
    parser.add_argument("--seed", type=int, default=20260719)
    parser.add_argument("--repeat", type=positive_int, default=6)
    parser.add_argument("--iterations", type=positive_int, default=32)
    parser.add_argument("--timeout-ms", type=positive_int, default=120_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.accounts > args.events:
        raise SystemExit("--accounts must not exceed --events")
    with wall_clock_timeout(args.timeout_ms):
        candidate = load_candidate(args.candidate, args.candidate_sha256)
        result = run_workload(
            candidate,
            events=args.events,
            accounts=args.accounts,
            seed=args.seed,
            repeat=args.repeat,
            iterations=args.iterations,
        )
    print(canonical_json_bytes(result).decode("ascii"))


if __name__ == "__main__":
    main()
