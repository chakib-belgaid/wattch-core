"""CLI: python -m pywattch.energytest {discover,run} --root DIR [--json]"""

from __future__ import annotations

import argparse
import json
import sys

from ..client import default_socket_path
from .discovery import discover_tests
from .runner import STATUS_PASSED, run_tests, summarize

SCHEMA_VERSION = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pywattch.energytest",
        description="Discover and run energy tests through a Wattch daemon.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="list energy tests")
    discover.add_argument("--root", default=".", help="directory to scan")
    discover.add_argument("--json", action="store_true", help="machine-readable output")

    run = sub.add_parser("run", help="run energy tests")
    run.add_argument("--root", default=".", help="directory to scan")
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.add_argument(
        "--socket",
        default=None,
        help="wattch daemon socket (default: auto-detected, see docs/powerd.md)",
    )
    run.add_argument("--interval-ms", type=int, default=10, help="sampling interval")
    run.add_argument("--repeat", type=int, default=None, help="override per-test repeat")
    run.add_argument(
        "--test",
        action="append",
        default=None,
        metavar="FILE::NAME",
        help="run only these test ids (repeatable)",
    )

    args = parser.parse_args(argv)
    if args.command == "run" and args.socket is None:
        # Resolve now (not at import time) so --json output reports the
        # actual path probed/used, not a stale snapshot.
        args.socket = default_socket_path()
    tests, errors = discover_tests(args.root)

    if args.command == "discover":
        if args.json:
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "root": args.root,
                        "tests": [t.to_dict() for t in tests],
                        "errors": [e.to_dict() for e in errors],
                    }
                )
            )
        else:
            for test in tests:
                budget = (
                    f" (budget {test.budget_joules} J)" if test.budget_joules else ""
                )
                print(f"{test.test_id}{budget}")
            for error in errors:
                print(f"ERROR {error.file}: {error.message}", file=sys.stderr)
        return 0 if not errors else 1

    if args.test:
        wanted = set(args.test)
        tests = [t for t in tests if t.test_id in wanted]
        missing = wanted - {t.test_id for t in tests}
        if missing:
            print(f"unknown test ids: {sorted(missing)}", file=sys.stderr)
            return 2

    results = run_tests(
        tests,
        socket_path=args.socket,
        interval_ms=args.interval_ms,
        repeat_override=args.repeat,
    )
    summary = summarize(results)

    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "root": args.root,
                    "socket": args.socket,
                    "results": [r.to_dict() for r in results],
                    "discovery_errors": [e.to_dict() for e in errors],
                    "summary": summary,
                }
            )
        )
    else:
        for result in results:
            mark = "PASS" if result.status == STATUS_PASSED else result.status.upper()
            print(
                f"{mark:<12} {result.test_id}  "
                f"{result.total_joules:.6f} J  {result.wall_time_s * 1000:.1f} ms"
            )
            if result.error:
                print(f"    {result.error.strip().splitlines()[-1]}")
        print(
            f"\n{summary['total']} tests: {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['error']} error, "
            f"{summary['over_budget']} over budget"
        )

    failed = summary["failed"] + summary["error"] + summary["over_budget"]
    return 0 if failed == 0 and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
