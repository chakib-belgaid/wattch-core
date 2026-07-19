#!/usr/bin/env python3
"""Deterministic polling-versus-batching workload for Wattch experiments.

The two modes consume the same generated arrival trace and perform the same
pure work.  Only their waiting/dispatch strategy differs:

* ``polling`` repeatedly performs non-blocking reads with no sleep.
* ``batching`` blocks for input and processes complete batches.

The optional ``sham`` mode blocks on the same arrivals and drains them without
performing the task computation.  It exists only as a three-arm launcher and
arrival-harness control; it is not one of the equivalent program variants.

Canonical result JSON is written to stdout.  Runtime metrics are deliberately
kept out of stdout and written as one JSON object to stderr, or to the path
selected with ``--metrics-path``.  This lets Wattch use stdout's SHA-256 as an
output-equivalence check without pretending that runtime observations are
deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence


DEFAULT_INPUT_ID = "wattch-polling-batching-v1"
DEFAULT_REPLAY_SEED = 20260719
_MASK_64 = (1 << 64) - 1


@dataclass(frozen=True)
class LogicalArrival:
    sequence: int
    task_id: int
    due_offset_ns: int


@dataclass(frozen=True)
class ReceivedTask:
    task_id: int
    arrival_ns: int
    due_ns: int


class SplitMix64:
    """Tiny explicitly specified PRNG used only to permute arrival order."""

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK_64

    def next_u64(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK_64
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK_64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK_64
        return (value ^ (value >> 31)) & _MASK_64


def _stable_shuffle(values: list[int], seed: int) -> None:
    generator = SplitMix64(seed)
    for index in range(len(values) - 1, 0, -1):
        other = generator.next_u64() % (index + 1)
        values[index], values[other] = values[other], values[index]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_milliseconds(value: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a non-negative number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number")
    nanoseconds = parsed * Decimal(1_000_000)
    if nanoseconds != nanoseconds.to_integral_value():
        raise argparse.ArgumentTypeError(
            "must resolve to a whole number of nanoseconds"
        )
    return int(nanoseconds)


def _positive_seconds(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return parsed


def _environment_text(name: str, fallback: str) -> str:
    value = os.environ.get(name, fallback)
    if not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty NUL-free string")
    return value


def _environment_seed() -> int:
    raw = os.environ.get("WATTCH_REPLAY_SEED", str(DEFAULT_REPLAY_SEED))
    try:
        return int(raw, 10) & _MASK_64
    except ValueError as exc:
        raise ValueError("WATTCH_REPLAY_SEED must be a base-10 integer") from exc


def build_arrival_trace(
    *, items: int, interval_ns: int, replay_seed: int
) -> tuple[LogicalArrival, ...]:
    order = list(range(items))
    _stable_shuffle(order, replay_seed)
    return tuple(
        LogicalArrival(
            sequence=sequence,
            task_id=task_id,
            due_offset_ns=sequence * interval_ns,
        )
        for sequence, task_id in enumerate(order)
    )


def arrival_trace_sha256(
    trace: Iterable[LogicalArrival], *, input_id: str, arrival_trace_id: str
) -> str:
    digest = hashlib.sha256()
    digest.update(b"wattch-arrival-trace-v1\0")
    digest.update(input_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(arrival_trace_id.encode("utf-8"))
    digest.update(b"\0")
    for arrival in trace:
        digest.update(arrival.sequence.to_bytes(8, "big"))
        digest.update(arrival.task_id.to_bytes(8, "big"))
        digest.update(arrival.due_offset_ns.to_bytes(8, "big"))
    return digest.hexdigest()


def _task_result(task_id: int, *, input_id: str, work_rounds: int) -> bytes:
    task_bytes = task_id.to_bytes(8, "big")
    state = hashlib.sha256(
        b"wattch-work-item-v1\0" + input_id.encode("utf-8") + b"\0" + task_bytes
    ).digest()
    for round_index in range(work_rounds):
        state = hashlib.sha256(
            state + task_bytes + round_index.to_bytes(8, "big")
        ).digest()
    return state


def _canonical_result(
    results: dict[int, bytes], *, input_id: str, work_rounds: int
) -> bytes:
    combined = hashlib.sha256()
    combined.update(b"wattch-work-results-v1\0")
    for task_id in sorted(results):
        combined.update(task_id.to_bytes(8, "big"))
        combined.update(results[task_id])
    document = {
        "algorithm": "sha256-chain-v1",
        "completed_work": len(results),
        "input_id": input_id,
        "result_sha256": combined.hexdigest(),
        "schema_version": 1,
        "work_rounds": work_rounds,
        "work_unit": "items",
    }
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _write_all(file_descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(file_descriptor, payload[offset:])
        if written <= 0:  # pragma: no cover - defensive OS contract guard
            raise OSError("pipe write made no progress")
        offset += written


def _produce(
    file_descriptor: int,
    trace: Sequence[LogicalArrival],
    *,
    origin_ns: int,
) -> None:
    for logical in trace:
        deadline_ns = origin_ns + logical.due_offset_ns
        while True:
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                break
            time.sleep(remaining_ns / 1_000_000_000)
        arrival_ns = time.monotonic_ns()
        _write_all(
            file_descriptor,
            f"{logical.task_id},{arrival_ns},{deadline_ns}\n".encode("ascii"),
        )


def _spawn_producer(
    read_descriptor: int,
    write_descriptor: int,
    trace: Sequence[LogicalArrival],
    *,
    origin_ns: int,
) -> int:
    if not hasattr(os, "fork"):
        raise RuntimeError("this POSIX workload requires os.fork")
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no branch - child process
        try:
            os.close(read_descriptor)
            _produce(write_descriptor, trace, origin_ns=origin_ns)
        except BaseException:
            os._exit(2)
        finally:
            try:
                os.close(write_descriptor)
            except OSError:
                pass
        os._exit(0)
    os.close(write_descriptor)
    return child_pid


def _wait_for_producer(child_pid: int, timeout_s: float = 1.0) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid == child_pid:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= deadline:
            os.kill(child_pid, signal.SIGTERM)
            _, status = os.waitpid(child_pid, 0)
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.001)


def _stop_producer(child_pid: int) -> None:
    try:
        os.kill(child_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass


def _parse_records(buffer: bytearray) -> list[ReceivedTask]:
    records: list[ReceivedTask] = []
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            return records
        raw = bytes(buffer[:newline])
        del buffer[: newline + 1]
        fields = raw.split(b",", 2)
        if len(fields) != 3:
            raise ValueError("producer emitted a malformed task record")
        records.append(
            ReceivedTask(
                task_id=int(fields[0]),
                arrival_ns=int(fields[1]),
                due_ns=int(fields[2]),
            )
        )


def _process_batch(
    tasks: Sequence[ReceivedTask],
    *,
    input_id: str,
    work_rounds: int,
    results: dict[int, bytes],
    latencies_ns: list[int],
    producer_lateness_ns: list[int],
    perform_work: bool = True,
) -> None:
    for task in tasks:
        if task.task_id in results:
            raise ValueError(f"duplicate task id {task.task_id}")
        results[task.task_id] = (
            _task_result(
                task.task_id,
                input_id=input_id,
                work_rounds=work_rounds,
            )
            if perform_work
            else b""
        )
        latencies_ns.append(max(0, time.monotonic_ns() - task.arrival_ns))
        producer_lateness_ns.append(max(0, task.arrival_ns - task.due_ns))


def _consume_polling(
    file_descriptor: int,
    *,
    expected_items: int,
    deadline_ns: int,
    input_id: str,
    work_rounds: int,
) -> tuple[dict[int, bytes], list[int], list[int], int, int, int]:
    os.set_blocking(file_descriptor, False)
    buffer = bytearray()
    results: dict[int, bytes] = {}
    latencies_ns: list[int] = []
    producer_lateness_ns: list[int] = []
    empty_polls = 0
    read_calls = 0
    batches = 0

    while len(results) < expected_items:
        if time.monotonic_ns() >= deadline_ns:
            raise TimeoutError("polling worker exceeded --timeout-s")
        try:
            chunk = os.read(file_descriptor, 65_536)
            read_calls += 1
        except BlockingIOError:
            empty_polls += 1
            continue
        if not chunk:
            break
        buffer.extend(chunk)
        for task in _parse_records(buffer):
            _process_batch(
                (task,),
                input_id=input_id,
                work_rounds=work_rounds,
                results=results,
                latencies_ns=latencies_ns,
                producer_lateness_ns=producer_lateness_ns,
            )
            batches += 1
    if buffer:
        raise ValueError("producer closed with a partial task record")
    return (
        results,
        latencies_ns,
        producer_lateness_ns,
        empty_polls,
        read_calls,
        batches,
    )


def _consume_batching(
    file_descriptor: int,
    *,
    expected_items: int,
    batch_size: int,
    deadline_ns: int,
    input_id: str,
    work_rounds: int,
    perform_work: bool = True,
) -> tuple[dict[int, bytes], list[int], list[int], int, int, int]:
    buffer = bytearray()
    pending: list[ReceivedTask] = []
    results: dict[int, bytes] = {}
    latencies_ns: list[int] = []
    producer_lateness_ns: list[int] = []
    read_calls = 0
    batches = 0
    end_of_stream = False

    while len(results) < expected_items:
        while len(pending) < batch_size and not end_of_stream:
            remaining_s = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining_s <= 0:
                raise TimeoutError("batching worker exceeded --timeout-s")
            readable, _, _ = select.select([file_descriptor], [], [], remaining_s)
            if not readable:
                raise TimeoutError("batching worker exceeded --timeout-s")
            chunk = os.read(file_descriptor, 65_536)
            read_calls += 1
            if not chunk:
                end_of_stream = True
                break
            buffer.extend(chunk)
            pending.extend(_parse_records(buffer))

        if len(pending) >= batch_size:
            batch = pending[:batch_size]
            del pending[:batch_size]
        elif end_of_stream and pending:
            batch = list(pending)
            pending.clear()
        elif end_of_stream:
            break
        else:  # pragma: no cover - loop invariants make this unreachable
            continue

        _process_batch(
            batch,
            input_id=input_id,
            work_rounds=work_rounds,
            results=results,
            latencies_ns=latencies_ns,
            producer_lateness_ns=producer_lateness_ns,
            perform_work=perform_work,
        )
        batches += 1

    if buffer:
        raise ValueError("producer closed with a partial task record")
    return results, latencies_ns, producer_lateness_ns, 0, read_calls, batches


def _percentile_ms(latencies_ns: Sequence[int], percentile: float) -> float:
    if not latencies_ns:
        return 0.0
    ordered = sorted(latencies_ns)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1] / 1_000_000


def _metrics_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _emit_metrics(payload: bytes, path: str | None) -> None:
    if path is None:
        sys.stderr.buffer.write(payload)
        sys.stderr.buffer.flush()
        return
    with Path(path).open("xb") as handle:
        handle.write(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("polling", "batching", "sham"), required=True
    )
    parser.add_argument("--items", type=_positive_int, default=4096)
    parser.add_argument(
        "--arrival-interval-ms",
        type=_non_negative_milliseconds,
        default=_non_negative_milliseconds("2"),
        metavar="MILLISECONDS",
    )
    parser.add_argument("--batch-size", type=_positive_int, default=32)
    parser.add_argument("--work-rounds", type=_positive_int, default=64)
    parser.add_argument("--timeout-s", type=_positive_seconds, default=20.0)
    parser.add_argument(
        "--latency-slo-ms", type=_non_negative_float, default=1000.0
    )
    parser.add_argument(
        "--producer-lateness-limit-ms",
        type=_non_negative_float,
        default=20.0,
        help="invalidate the offered trace when any arrival is later than this",
    )
    parser.add_argument(
        "--metrics-path",
        default=os.environ.get("WATTCH_METRICS_PATH"),
        help=(
            "write metrics JSON here instead of stderr; path must not exist"
        ),
    )
    return parser


def run(arguments: argparse.Namespace) -> int:
    if arguments.metrics_path is not None and (
        not arguments.metrics_path or "\x00" in arguments.metrics_path
    ):
        raise ValueError("metrics path must be a non-empty NUL-free string")
    replay_seed = _environment_seed()
    input_id = _environment_text("WATTCH_INPUT_ID", DEFAULT_INPUT_ID)
    arrival_trace_id = _environment_text(
        "WATTCH_ARRIVAL_TRACE_ID", "generated-splitmix64-v1"
    )
    trace = build_arrival_trace(
        items=arguments.items,
        interval_ns=arguments.arrival_interval_ms,
        replay_seed=replay_seed,
    )
    trace_sha256 = arrival_trace_sha256(
        trace,
        input_id=input_id,
        arrival_trace_id=arrival_trace_id,
    )
    read_descriptor, write_descriptor = os.pipe()
    origin_ns = time.monotonic_ns()
    deadline_ns = origin_ns + round(arguments.timeout_s * 1_000_000_000)
    producer_pid = _spawn_producer(
        read_descriptor,
        write_descriptor,
        trace,
        origin_ns=origin_ns,
    )

    previous_signal_handlers: dict[int, object] = {}

    def stop_process_tree(signum: int, _frame: object) -> None:
        _stop_producer(producer_pid)
        raise SystemExit(128 + signum)

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        previous_signal_handlers[handled_signal] = signal.signal(
            handled_signal, stop_process_tree
        )

    try:
        if arguments.mode == "polling":
            (
                results,
                latencies_ns,
                producer_lateness_ns,
                empty_polls,
                read_calls,
                batches,
            ) = (
                _consume_polling(
                    read_descriptor,
                    expected_items=arguments.items,
                    deadline_ns=deadline_ns,
                    input_id=input_id,
                    work_rounds=arguments.work_rounds,
                )
            )
            wait_strategy = "nonblocking_busy_poll"
        else:
            (
                results,
                latencies_ns,
                producer_lateness_ns,
                empty_polls,
                read_calls,
                batches,
            ) = (
                _consume_batching(
                    read_descriptor,
                    expected_items=arguments.items,
                    batch_size=arguments.batch_size,
                    deadline_ns=deadline_ns,
                    input_id=input_id,
                    work_rounds=arguments.work_rounds,
                    perform_work=arguments.mode == "batching",
                )
            )
            wait_strategy = (
                "blocking_batch"
                if arguments.mode == "batching"
                else "blocking_sham_drain"
            )
    except BaseException:
        _stop_producer(producer_pid)
        raise
    finally:
        try:
            os.close(read_descriptor)
        except OSError:
            pass
        for handled_signal, previous_handler in previous_signal_handlers.items():
            signal.signal(handled_signal, previous_handler)

    producer_returncode = _wait_for_producer(producer_pid)
    if producer_returncode != 0:
        raise RuntimeError(
            f"arrival producer exited with status {producer_returncode}"
        )
    expected_ids = set(range(arguments.items))
    if set(results) != expected_ids:
        missing = len(expected_ids.difference(results))
        raise RuntimeError(f"workload ended with {missing} missing items")

    finished_ns = time.monotonic_ns()
    runtime_s = max((finished_ns - origin_ns) / 1_000_000_000, 1e-9)
    if arguments.mode == "sham":
        stdout = _metrics_bytes(
            {
                "completed_work": len(results),
                "input_id": input_id,
                "role": "sham-arrival-drain",
                "schema_version": 1,
                "work_unit": "items",
            }
        )
    else:
        stdout = _canonical_result(
            results,
            input_id=input_id,
            work_rounds=arguments.work_rounds,
        )
    output_sha256 = hashlib.sha256(stdout).hexdigest()
    p99_ms = _percentile_ms(latencies_ns, 0.99)
    producer_max_lateness_ms = _percentile_ms(producer_lateness_ns, 1.0)
    arrival_trace_valid = (
        producer_max_lateness_ms <= arguments.producer_lateness_limit_ms
    )
    metrics = {
        "arrival_trace_id": arrival_trace_id,
        "arrival_trace_sha256": trace_sha256,
        "arrival_trace_valid": arrival_trace_valid,
        "backlog_at_end": arguments.items - len(results),
        "batch_size": arguments.batch_size if arguments.mode != "polling" else 1,
        "batches_processed": batches,
        "completed_work": len(results),
        "empty_poll_count": empty_polls,
        "input_id": input_id,
        "latency_ms": {
            "max": _percentile_ms(latencies_ns, 1.0),
            "p50": _percentile_ms(latencies_ns, 0.50),
            "p95": _percentile_ms(latencies_ns, 0.95),
            "p99": p99_ms,
        },
        "latency_slo_ms": arguments.latency_slo_ms,
        "mode": arguments.mode,
        "output_sha256": output_sha256,
        "producer_lateness_limit_ms": arguments.producer_lateness_limit_ms,
        "producer_lateness_ms": {
            "max": producer_max_lateness_ms,
            "p50": _percentile_ms(producer_lateness_ns, 0.50),
            "p95": _percentile_ms(producer_lateness_ns, 0.95),
            "p99": _percentile_ms(producer_lateness_ns, 0.99),
        },
        "read_calls": read_calls,
        "replay_seed": replay_seed,
        "runtime_s": runtime_s,
        "schema_version": 1,
        "slo_met": (
            p99_ms <= arguments.latency_slo_ms and arrival_trace_valid
        ),
        "throughput_per_s": len(results) / runtime_s,
        "wait_strategy": wait_strategy,
        "work_rounds": arguments.work_rounds,
        "work_unit": "items",
    }
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    _emit_metrics(_metrics_bytes(metrics), arguments.metrics_path)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return run(arguments)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        error = {
            "error": type(exc).__name__,
            "message": str(exc),
            "schema_version": 1,
        }
        sys.stderr.buffer.write(_metrics_bytes(error))
        sys.stderr.buffer.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
