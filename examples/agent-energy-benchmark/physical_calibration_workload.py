#!/usr/bin/env python3
"""Trusted fixed-work and idle workloads for physical meter calibration.

The output deliberately excludes elapsed time so matched baseline and
instrumented executions can be checked byte-for-byte.  This program performs
no measurement or interpretation itself.
"""

from __future__ import annotations

import argparse
import json
import time


MASK_64 = (1 << 64) - 1


def fixed_work(iterations: int, seed: int) -> dict[str, object]:
    """Run a deterministic integer workload and return its exact digest."""

    state = seed & MASK_64
    accumulator = 0
    for index in range(iterations):
        state = (state * 6364136223846793005 + 1442695040888963407) & MASK_64
        accumulator = (
            accumulator + ((state >> 17) ^ (state << 13) ^ index)
        ) & MASK_64
    return {
        "digest": f"{accumulator:016x}",
        "iterations": iterations,
        "mode": "fixed",
        "seed": seed,
    }


def idle_work(duration_s: float) -> dict[str, object]:
    """Sleep for a fixed horizon without manufacturing CPU work."""

    time.sleep(duration_s)
    return {"duration_s": duration_s, "mode": "idle"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    idle = subparsers.add_parser("idle")
    idle.add_argument("--duration-s", type=float, required=True)
    fixed = subparsers.add_parser("fixed")
    fixed.add_argument("--iterations", type=int, required=True)
    fixed.add_argument("--seed", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "idle":
        if args.duration_s <= 0:
            raise SystemExit("--duration-s must be greater than zero")
        result = idle_work(args.duration_s)
    else:
        if args.iterations <= 0:
            raise SystemExit("--iterations must be greater than zero")
        result = fixed_work(args.iterations, args.seed)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
