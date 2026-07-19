#!/usr/bin/env python3
"""Frozen optimization target for the Wattch agent comparison.

The implementation is intentionally straightforward rather than efficient.
Agents may change this file only.  The CLI, generated event stream, public
``summarize`` result, and stdout bytes are the correctness contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence


EVENT_KINDS = ("read", "write", "cache_hit", "cache_miss")
MASK_64 = (1 << 64) - 1
Event = tuple[int, int, int]


def generate_events(count: int, accounts: int, seed: int) -> list[Event]:
    """Generate a deterministic stream of ``(account, kind, value)`` events."""
    state = seed & MASK_64
    events: list[Event] = []
    for _ in range(count):
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK_64
        account = (state >> 17) % accounts
        kind = (state >> 41) % len(EVENT_KINDS)
        value = 1 + ((state >> 7) % 997)
        events.append((account, kind, value))
    return events


def summarize(events: Sequence[Event]) -> list[dict[str, object]]:
    """Return stable per-account aggregates while preserving event order."""
    account_ids = sorted({event[0] for event in events})
    result: list[dict[str, object]] = []

    for account_id in account_ids:
        account_events = [event for event in events if event[0] == account_id]
        kind_counts = {
            EVENT_KINDS[kind]: sum(1 for event in account_events if event[1] == kind)
            for kind in range(len(EVENT_KINDS))
        }
        kind_values = {
            EVENT_KINDS[kind]: sum(
                event[2] for event in account_events if event[1] == kind
            )
            for kind in range(len(EVENT_KINDS))
        }
        rolling = 0
        for _, kind, value in account_events:
            rolling = (rolling * 1_000_003 + kind * 1_009 + value) & MASK_64

        result.append(
            {
                "account": account_id,
                "count": len(account_events),
                "kind_counts": kind_counts,
                "kind_values": kind_values,
                "rolling": rolling,
                "total_value": sum(event[2] for event in account_events),
            }
        )

    return result


def canonical_bytes(summary: list[dict[str, object]]) -> bytes:
    return json.dumps(
        summary,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def run(count: int, accounts: int, seed: int, repeat: int) -> dict[str, object]:
    events = generate_events(count, accounts, seed)
    digest = hashlib.sha256()
    summary: list[dict[str, object]] = []
    for _ in range(repeat):
        summary = summarize(events)
        digest.update(canonical_bytes(summary))
    return {
        "accounts": len(summary),
        "events": count,
        "repeat": repeat,
        "result_sha256": digest.hexdigest(),
        "schema_version": 1,
        "seed": seed,
    }


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=positive_int, default=300_000)
    parser.add_argument("--accounts", type=positive_int, default=512)
    parser.add_argument("--seed", type=int, default=20260719)
    parser.add_argument("--repeat", type=positive_int, default=6)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.accounts > args.events:
        raise SystemExit("--accounts must not exceed --events")
    output = run(args.events, args.accounts, args.seed, args.repeat)
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
