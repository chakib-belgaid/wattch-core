#!/usr/bin/env python3
"""Fail-closed classification of one Wattch run and primary source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evidence_contract import qualify_run


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--source-uid")
    parser.add_argument(
        "--trial-record",
        type=Path,
        help="Coordinator-owned trial binding for this exact artifact",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = qualify_run(
        args.run,
        requested_source_uid=args.source_uid,
        trial_record=args.trial_record,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
