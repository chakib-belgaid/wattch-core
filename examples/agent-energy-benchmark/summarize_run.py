#!/usr/bin/env python3
"""Compute one explicitly bounded energy outcome from a Wattch run artifact.

This is an offline client-layer helper. It never mutates the raw artifact and
never aggregates multiple domains. Synthetic values are returned for plumbing
verification but are marked as ineligible for physical energy comparisons.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "clients" / "python"))

from pywattch._frames import FrameKind  # noqa: E402
from pywattch._messages import SourceDescriptor  # noqa: E402
from pywattch.analysis.boundary import MeasurementBoundary  # noqa: E402
from pywattch.analysis.outcomes import Estimand, compute_energy_outcome  # noqa: E402
from pywattch.analysis.replay import _read_trace  # noqa: E402


DEFAULT_KIND_ORDER = ("CPU_PACKAGE_ENERGY", "PLATFORM_ENERGY", "CPU_POWER")
MARKER_PAIRS = (
    ("TARGET_SPAWN", "TARGET_EXIT"),
    ("WATTCH_WINDOW_START", "WATTCH_WINDOW_END"),
)


def _qualify_run(run_dir: Path, *, source_uid: str, trial_record: Path | None):
    """Load the optional skill qualifier only when an outcome is summarized."""

    script_roots = (
        REPO_ROOT / ".agents/skills/wattch-energy-optimize/scripts",
        REPO_ROOT / "skills/wattch-energy-optimize/scripts",
    )
    for root in script_roots:
        if root.is_dir():
            sys.path.insert(0, str(root))
            break
    try:
        from evidence_contract import qualify_run
    except ModuleNotFoundError as error:
        if error.name != "evidence_contract":
            raise
        raise SystemExit(
            "Wattch evidence qualifier is unavailable; install the "
            "wattch-energy-optimize skill or run from its source checkout"
        ) from error
    return qualify_run(
        run_dir,
        requested_source_uid=source_uid,
        trial_record=trial_record,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--source-uid")
    parser.add_argument(
        "--trial-record",
        type=Path,
        help="Coordinator-owned binding for this exact captured execution",
    )
    return parser.parse_args()


def select_source(
    descriptors: list[SourceDescriptor], requested_uid: str | None
) -> SourceDescriptor:
    if requested_uid is not None:
        matches = [source for source in descriptors if source.source_uid == requested_uid]
        if len(matches) != 1:
            raise SystemExit(f"source uid {requested_uid!r} is not present exactly once")
        return matches[0]

    for kind in DEFAULT_KIND_ORDER:
        matches = [
            source
            for source in descriptors
            if source.kind == kind and source.parent_source_uid is None
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SystemExit(
                f"multiple root {kind} sources; choose one with --source-uid"
            )
    raise SystemExit("no unambiguous CPU/platform energy or CPU power source found")


def select_window(event_batches):
    events = [event for batch in event_batches for event in batch.events]
    by_kind: dict[str, list[object]] = {}
    for event in events:
        by_kind.setdefault(event.kind, []).append(event)
    for start_kind, end_kind in MARKER_PAIRS:
        starts = by_kind.get(start_kind, [])
        ends = by_kind.get(end_kind, [])
        if len(starts) == 1 and len(ends) == 1:
            start = starts[0]
            end = ends[0]
            if end.monotonic_ns <= start.monotonic_ns:
                raise SystemExit(f"{end_kind} does not follow {start_kind}")
            return start_kind, end_kind, start.monotonic_ns, end.monotonic_ns
    raise SystemExit("artifact lacks one unambiguous supported start/end marker pair")


def main() -> None:
    args = parse_args()
    run_dir = args.run.resolve()
    descriptors = [
        SourceDescriptor.from_dict(item)
        for item in json.loads((run_dir / "sources.json").read_text("utf-8"))
    ]
    source = select_source(descriptors, args.source_uid)
    raw_batches = _read_trace(run_dir / "raw.trace.wch", FrameKind.RAW_SAMPLE_BATCH)
    event_batches = _read_trace(run_dir / "events.trace.wch", FrameKind.EVENT_BATCH)
    start_kind, end_kind, start_ns, end_ns = select_window(event_batches)
    boundary = MeasurementBoundary.from_descriptors(
        descriptors,
        [source.source_uid],
        boundary_id=f"single:{source.source_uid}",
    )
    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=descriptors,
        samples=(sample for batch in raw_batches for sample in batch.samples),
        start_monotonic_ns=start_ns,
        end_monotonic_ns=end_ns,
        estimand=Estimand.EQUAL_WORK,
    )
    qualification = _qualify_run(
        run_dir,
        source_uid=source.source_uid,
        trial_record=args.trial_record,
    )
    qualification["outcome_computable"] = True
    result = {
        "claim_eligible": qualification["physical_run_claim_eligible"],
        "physical_comparison_claim_eligible": qualification[
            "physical_comparison_claim_eligible"
        ],
        "remote_model_energy_claim_eligible": qualification[
            "remote_model_energy_claim_eligible"
        ],
        "end_marker": end_kind,
        "evidence_status": qualification["source"]["evidence_tier"],
        "outcome": outcome.to_dict(),
        "qualification": qualification,
        "run_dir": str(run_dir),
        "source": source.to_dict(),
        "start_marker": start_kind,
        "warnings": qualification["warnings"],
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
