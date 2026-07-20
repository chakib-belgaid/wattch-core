"""Fail-closed Wattch artifact/source qualification for agent workflows.

This module classifies evidence. It deliberately does not integrate energy or
turn one run into a comparison claim.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any


REQUIRED_SOURCE_FIELDS = {
    "local_source_id",
    "source_uid",
    "node_id",
    "backend",
    "kind",
    "unit",
    "temporality",
    "evidence_class",
    "source_path",
    "clock_domain",
    "sampling_mode",
}
ENERGY_KINDS = {
    "CPU_PACKAGE_ENERGY",
    "PLATFORM_ENERGY",
    "CPU_CORE_ENERGY",
    "DRAM_ENERGY",
}
POWER_KINDS = {"CPU_POWER", "GPU_POWER", "ANE_POWER", "WALL_POWER"}
SOURCE_KINDS = ENERGY_KINDS | POWER_KINDS | {
    "C_STATE_RESIDENCY",
    "P_STATE_FREQUENCY",
    "WAKEUP_COUNTER",
    "CONTEXT_SWITCH_COUNTER",
    "INTERRUPT_COUNTER",
    "SAMPLE_INTERVAL_DURATION",
    "BENCHMARK_EVENT",
    "AI_SPAN_EVENT",
    "OTHER",
}
UNITS = {
    "JOULE",
    "WATT",
    "MILLIWATT",
    "MICROJOULE",
    "NANOSECOND",
    "PERCENT",
    "COUNTER",
    "HERTZ",
    "CELSIUS",
    "BYTE",
    "UNKNOWN",
}
TEMPORALITIES = {
    "CUMULATIVE",
    "INSTANT",
    "INTERVAL_AVERAGE",
    "DELTA",
    "GAUGE",
    "EVENT",
}
EVIDENCE_CLASSES = {
    "RAW_HARDWARE_COUNTER",
    "RAW_SENSOR_READING",
    "OS_TELEMETRY",
    "TOOL_DERIVED_ESTIMATE",
    "MODEL_DERIVED_ESTIMATE",
    "BENCHMARK_MARKER",
}
SAMPLING_MODES = {"PERIODIC", "EVENT", "POLLED", "IMPORTED", "SYNTHETIC"}
MIN_NATIVE_UPDATES = 10
MAX_FRAME_PAYLOAD_BYTES = 16 * 1024 * 1024
FRAME_HEADER = struct.Struct("<4sHHHHI")
INTERVAL_DURATION_SOURCE_UID_LABEL = "wattch.interval_duration_source_uid"
INTERVAL_ANCHOR_LABEL = "wattch.interval_anchor"
INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP = (
    "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP"
)
INTERVAL_TIMELINE_RECONSTRUCTION_LABEL = (
    "wattch.interval_timeline_reconstruction"
)
INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1 = (
    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"
)
INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V2 = (
    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V2"
)
SYNTHETIC_BACKENDS = {"synthetic", "testd", "wattch-testd", "deterministic"}
LOCAL_RAW_CLASSES = {"RAW_HARDWARE_COUNTER", "RAW_SENSOR_READING"}
CORE_ARTIFACT_FILES = {
    "manifest.json",
    "sources.json",
    "raw.trace.wch",
    "events.trace.wch",
}
CLI_BOUND_FILES = CORE_ARTIFACT_FILES | {
    "command.json",
    "command.lifecycle.jsonl",
    "target.stdout.log",
    "target.stderr.log",
}
PAIRED_DIFFERENTIAL_MODE = "PAIRED_DIFFERENTIAL"
DIFFERENTIAL_READINESS_KIND = "wattch-agent-differential-readiness-diagnostic"


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _framed_json(path: Path, expected_kind: int):
    """Yield structurally decoded JSON frames without depending on repo imports."""

    with path.open("rb") as trace:
        while True:
            header = trace.read(FRAME_HEADER.size)
            if not header:
                return
            if len(header) != FRAME_HEADER.size:
                raise ValueError("truncated frame header")
            magic, major, minor, kind, flags, payload_len = FRAME_HEADER.unpack(header)
            if (
                magic != b"WCH0"
                or major != 0
                or minor not in {1, 2}
                or kind != expected_kind
                or (minor < 2 and expected_kind == 11)
                or flags != 0
                or payload_len > MAX_FRAME_PAYLOAD_BYTES
            ):
                raise ValueError("invalid trace frame envelope")
            payload = trace.read(payload_len)
            if len(payload) != payload_len:
                raise ValueError("truncated frame payload")
            document = json.loads(payload.decode("utf-8"))
            if not isinstance(document, dict):
                raise ValueError("trace payload must be one JSON object")
            yield document


def _raw_value_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"type", "value"}:
        return False
    kind = value.get("type")
    payload = value.get("value")
    if kind == "f64":
        return (
            not isinstance(payload, bool)
            and isinstance(payload, (int, float))
            and math.isfinite(float(payload))
        )
    if kind == "i64":
        return (
            isinstance(payload, int)
            and not isinstance(payload, bool)
            and -(2**63) <= payload < 2**63
        )
    if kind == "bytes":
        return isinstance(payload, list) and all(
            isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= 255
            for item in payload
        )
    return False


def _trace_gate(
    run: Path,
    manifest: dict[str, Any],
    sources: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any], list[str]]:
    """Decode both traces and derive the authoritative measurement window."""

    known_ids = {
        source.get("local_source_id")
        for source in sources
        if _nonnegative_int(source.get("local_source_id"))
    }
    counts = {int(source_id): 0 for source_id in known_ids}
    window_counts = {int(source_id): 0 for source_id in known_ids}
    sequence_gaps = {int(source_id): 0 for source_id in known_ids}
    evidence: dict[str, Any] = {
        "counts": counts,
        "window_counts": window_counts,
        "sequence_gaps": sequence_gaps,
        "marker_duration_s": None,
        "marker_pair": None,
    }
    run_id = manifest.get("run_id")
    node_id = manifest.get("node_id")
    try:
        event_frames = 0
        event_ids: set[str] = set()
        last_event_ns: int | None = None
        last_event_batch: int | None = None
        markers: dict[str, list[int]] = {}
        for batch in _framed_json(run / "events.trace.wch", 11):
            event_frames += 1
            batch_sequence = batch.get("batch_sequence_no")
            events = batch.get("events")
            if (
                batch.get("run_id") != run_id
                or batch.get("node_id") != node_id
                or not _nonnegative_int(batch_sequence)
                or (
                    last_event_batch is not None
                    and int(batch_sequence) != last_event_batch + 1
                )
                or not isinstance(events, list)
                or not events
            ):
                raise ValueError("invalid event batch")
            last_event_batch = int(batch_sequence)
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("event must be an object")
                event_id = event.get("event_id")
                monotonic_ns = event.get("monotonic_ns")
                labels = event.get("labels")
                kind = event.get("kind")
                if (
                    event.get("run_id") != run_id
                    or event.get("node_id") != node_id
                    or not isinstance(event_id, str)
                    or not event_id
                    or event_id in event_ids
                    or not _nonnegative_int(monotonic_ns)
                    or (last_event_ns is not None and int(monotonic_ns) < last_event_ns)
                    or not isinstance(kind, str)
                    or not isinstance(labels, dict)
                    or not all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in labels.items()
                    )
                ):
                    raise ValueError("invalid event")
                event_ids.add(event_id)
                last_event_ns = int(monotonic_ns)
                markers.setdefault(kind, []).append(int(monotonic_ns))
        if event_frames == 0:
            raise ValueError("event trace contains no frames")
        pairs = []
        for start_kind, end_kind in (
            ("TARGET_SPAWN", "TARGET_EXIT"),
            ("WATTCH_WINDOW_START", "WATTCH_WINDOW_END"),
        ):
            starts = markers.get(start_kind, [])
            ends = markers.get(end_kind, [])
            if len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
                pairs.append((start_kind, end_kind, starts[0], ends[0]))
            elif starts or ends:
                raise ValueError("measurement markers are incomplete or duplicated")
        if len(pairs) != 1:
            raise ValueError("trace must contain exactly one measurement marker pair")
        start_kind, end_kind, start_ns, end_ns = pairs[0]
        evidence["marker_pair"] = [start_kind, end_kind]
        evidence["marker_duration_s"] = (end_ns - start_ns) / 1e9

        raw_frames = 0
        last_batch_sequence: int | None = None
        last_sample: dict[int, tuple[int, int]] = {}
        for batch in _framed_json(run / "raw.trace.wch", 10):
            raw_frames += 1
            batch_sequence = batch.get("batch_sequence_no")
            samples = batch.get("samples")
            if (
                batch.get("run_id") != run_id
                or batch.get("node_id") != node_id
                or not _nonnegative_int(batch_sequence)
                or (
                    last_batch_sequence is not None
                    and int(batch_sequence) <= last_batch_sequence
                )
                or not isinstance(samples, list)
                or len(samples) > len(known_ids)
            ):
                raise ValueError("invalid raw sample batch")
            last_batch_sequence = int(batch_sequence)
            seen_ids: set[int] = set()
            for sample in samples:
                if not isinstance(sample, dict):
                    raise ValueError("raw sample must be an object")
                source_id = sample.get("source_id")
                sequence_no = sample.get("sequence_no")
                monotonic_ns = sample.get("monotonic_ns")
                value = sample.get("value")
                if (
                    source_id not in known_ids
                    or source_id in seen_ids
                    or not _nonnegative_int(sequence_no)
                    or not _nonnegative_int(monotonic_ns)
                    or not _raw_value_valid(value)
                ):
                    raise ValueError("invalid raw sample")
                position = last_sample.get(int(source_id))
                if position is not None and (
                    int(sequence_no) <= position[0]
                    or int(monotonic_ns) < position[1]
                ):
                    raise ValueError("raw sample order regressed")
                if position is not None:
                    sequence_gaps[int(source_id)] += int(sequence_no) - position[0] - 1
                seen_ids.add(int(source_id))
                last_sample[int(source_id)] = (int(sequence_no), int(monotonic_ns))
                counts[int(source_id)] += 1
                if start_ns <= int(monotonic_ns) <= end_ns:
                    window_counts[int(source_id)] += 1
        if raw_frames == 0:
            raise ValueError("raw trace contains no frames")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False, evidence, ["TRACE_DECODE_FAILED"]
    return True, evidence, []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def _upper(value: object) -> str:
    return value.upper() if isinstance(value, str) else ""


def _source_semantics(source: dict[str, Any]) -> tuple[bool, str]:
    kind = source.get("kind") if isinstance(source.get("kind"), str) else ""
    unit = source.get("unit") if isinstance(source.get("unit"), str) else ""
    temporality = (
        source.get("temporality")
        if isinstance(source.get("temporality"), str)
        else ""
    )
    if kind in ENERGY_KINDS:
        return unit in {"JOULE", "MICROJOULE"} and temporality == "CUMULATIVE", "energy-counter"
    if kind in POWER_KINDS:
        return unit in {"WATT", "MILLIWATT"} and temporality in {
            "INSTANT",
            "INTERVAL_AVERAGE",
        }, "power"
    return False, "non-energy"


def classify_source(source: object) -> dict[str, object]:
    blockers: list[str] = []
    if not isinstance(source, dict):
        return {
            "valid": False,
            "source_uid": None,
            "evidence_tier": "INVALID",
            "outcome_semantics_supported": False,
            "physical_source_candidate": False,
            "blockers": ["INVALID_SOURCE_DESCRIPTOR"],
        }
    missing = sorted(REQUIRED_SOURCE_FIELDS - set(source))
    if missing:
        blockers.append("INVALID_SOURCE_DESCRIPTOR")
    for name in ("source_uid", "node_id", "backend", "source_path", "clock_domain"):
        if not isinstance(source.get(name), str) or not source.get(name):
            blockers.append("INVALID_SOURCE_DESCRIPTOR")
    if not isinstance(source.get("local_source_id"), int) or isinstance(
        source.get("local_source_id"), bool
    ):
        blockers.append("INVALID_SOURCE_DESCRIPTOR")
    for name, allowed in (
        ("kind", SOURCE_KINDS),
        ("unit", UNITS),
        ("temporality", TEMPORALITIES),
        ("evidence_class", EVIDENCE_CLASSES),
        ("sampling_mode", SAMPLING_MODES),
    ):
        value = source.get(name)
        if not isinstance(value, str) or value not in allowed:
            blockers.append("INVALID_SOURCE_DESCRIPTOR")

    semantics_supported, semantics = _source_semantics(source)
    if not semantics_supported:
        blockers.append("UNSUPPORTED_ENERGY_SEMANTICS")
    backend = str(source.get("backend", "")).strip().lower()
    sampling = source.get("sampling_mode")
    evidence_class = source.get("evidence_class")
    synthetic = sampling == "SYNTHETIC" or backend in SYNTHETIC_BACKENDS
    if synthetic:
        tier = "PLUMBING_ONLY"
        blockers.append("SYNTHETIC_SOURCE")
    elif evidence_class in LOCAL_RAW_CLASSES:
        tier = "RAW_LOCAL_MEASUREMENT"
    elif evidence_class == "TOOL_DERIVED_ESTIMATE":
        tier = "LOCAL_TOOL_ESTIMATE"
    elif evidence_class == "MODEL_DERIVED_ESTIMATE":
        tier = "MODEL_ESTIMATE"
        blockers.append("MODEL_DERIVED_SOURCE")
    elif evidence_class in {"OS_TELEMETRY", "BENCHMARK_MARKER"}:
        tier = "NON_ENERGY"
        blockers.append("UNSUPPORTED_EVIDENCE_CLASS")
    else:
        tier = "INVALID"
        blockers.append("UNSUPPORTED_EVIDENCE_CLASS")
    physical_candidate = semantics_supported and tier in {
        "RAW_LOCAL_MEASUREMENT",
        "LOCAL_TOOL_ESTIMATE",
    }
    return {
        "valid": not missing and "INVALID_SOURCE_DESCRIPTOR" not in blockers,
        "source_uid": source.get("source_uid"),
        "kind": source.get("kind"),
        "unit": source.get("unit"),
        "temporality": source.get("temporality"),
        "evidence_class": source.get("evidence_class"),
        "sampling_mode": source.get("sampling_mode"),
        "backend": source.get("backend"),
        "evidence_tier": tier,
        "outcome_semantics": semantics,
        "outcome_semantics_supported": semantics_supported,
        "physical_source_candidate": physical_candidate,
        "blockers": sorted(set(blockers)),
    }


def _descriptor_set(sources: object) -> tuple[list[dict[str, Any]], list[str]]:
    blockers: list[str] = []
    if not isinstance(sources, list) or not sources:
        return [], ["EMPTY_SOURCE_SET"]
    if not all(isinstance(source, dict) for source in sources):
        return [], ["INVALID_SOURCE_DESCRIPTOR"]
    typed = list(sources)
    local_ids = [source.get("local_source_id") for source in typed]
    uids = [source.get("source_uid") for source in typed]
    if len(local_ids) != len(set(map(str, local_ids))) or len(uids) != len(set(uids)):
        blockers.append("INVALID_SOURCE_DESCRIPTOR")
    uid_set = set(uids)
    for source in typed:
        classified = classify_source(source)
        if not classified["valid"]:
            blockers.append("INVALID_SOURCE_DESCRIPTOR")
        parent = source.get("parent_source_uid")
        if parent is not None and parent not in uid_set:
            blockers.append("INVALID_SOURCE_DESCRIPTOR")
        if _upper(source.get("temporality")) == "INTERVAL_AVERAGE":
            if _interval_duration_source(typed, source) is None:
                blockers.append("INTERVAL_DURATION_UNBOUND")
    return typed, sorted(set(blockers))


def _interval_duration_source(
    sources: list[dict[str, Any]], source: dict[str, Any]
) -> dict[str, Any] | None:
    """Mirror the canonical MeasurementBoundary interval-average contract."""

    labels = source.get("labels")
    if not isinstance(labels, dict):
        return None
    duration_uid = labels.get(INTERVAL_DURATION_SOURCE_UID_LABEL)
    if (
        not isinstance(duration_uid, str)
        or not duration_uid
        or labels.get(INTERVAL_ANCHOR_LABEL)
        != INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP
    ):
        return None
    reconstruction = labels.get(INTERVAL_TIMELINE_RECONSTRUCTION_LABEL)
    if (
        reconstruction is not None
        and reconstruction
        not in {
            INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1,
            INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V2,
        }
    ):
        return None
    matches = [item for item in sources if item.get("source_uid") == duration_uid]
    if len(matches) != 1:
        return None
    duration = matches[0]
    if (
        duration.get("kind") != "SAMPLE_INTERVAL_DURATION"
        or duration.get("unit") != "NANOSECOND"
        or duration.get("temporality") != "DELTA"
        or duration.get("node_id") != source.get("node_id")
        or duration.get("backend") != source.get("backend")
        or duration.get("clock_domain") != source.get("clock_domain")
        or duration.get("evidence_class") != source.get("evidence_class")
    ):
        return None
    return duration


def _select_source(
    sources: list[dict[str, Any]], requested_uid: str | None
) -> tuple[dict[str, Any] | None, list[str]]:
    if requested_uid is not None:
        matches = [source for source in sources if source.get("source_uid") == requested_uid]
        return (matches[0], []) if len(matches) == 1 else (None, ["PRIMARY_SOURCE_NOT_FOUND"])
    candidates = [
        source
        for source in sources
        if classify_source(source)["outcome_semantics_supported"]
        and source.get("parent_source_uid") is None
    ]
    if len(candidates) != 1:
        return None, ["AMBIGUOUS_PRIMARY_SOURCE"]
    return candidates[0], []


def _mcp_lifecycle(run: Path, manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    start_path = run / "mcp-capture.json"
    terminal_path = run / "mcp-capture-terminal.json"
    labels = manifest.get("labels") if isinstance(manifest.get("labels"), dict) else {}
    capture_id = labels.get("wattch.mcp.capture_id")
    managed = capture_id is not None or start_path.exists() or terminal_path.exists()
    if not managed:
        return False, []
    blockers: list[str] = []
    if not capture_id or not start_path.is_file() or not terminal_path.is_file():
        return False, ["MCP_STATUS_UNBOUND"]
    try:
        start = _json(start_path)
        terminal = _json(terminal_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ["MCP_STATUS_UNBOUND"]
    run_id = manifest.get("run_id")
    for record in (start, terminal):
        if not isinstance(record, dict) or record.get("capture_id") != capture_id or record.get("run_id") != run_id:
            blockers.append("MCP_STATUS_UNBOUND")
    if not isinstance(terminal, dict) or terminal.get("state") != "complete":
        blockers.append("CAPTURE_INCOMPLETE")
    if terminal.get("tail_complete") is not True:
        blockers.append("TAIL_INCOMPLETE")
    incomplete = terminal.get("tail_incomplete_source_ids")
    if incomplete not in ([], None):
        blockers.append("TAIL_INCOMPLETE")
    target = terminal.get("tail_target_monotonic_ns")
    last = terminal.get("tail_last_monotonic_ns")
    if not isinstance(target, int) or not isinstance(last, int) or last < target:
        blockers.append("TAIL_INCOMPLETE")
    return True, sorted(set(blockers))


def _cli_lifecycle(run: Path, manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    command_path = run / "command.json"
    if not command_path.is_file():
        return False, []
    path = run / "command.lifecycle.jsonl"
    try:
        records = [
            json.loads(line)
            for line in path.read_text("utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True, ["CAPTURE_INCOMPLETE"]
    run_id = manifest.get("run_id")
    if (
        len(records) != 2
        or not all(isinstance(item, dict) for item in records)
        or records[0].get("state") != "STARTED"
        or records[0].get("run_id") != run_id
        or records[1].get("state") != "COMPLETE"
        or records[1].get("run_id") != run_id
        or not isinstance(records[1].get("exit_code"), int)
        or isinstance(records[1].get("exit_code"), bool)
    ):
        return True, ["CAPTURE_INCOMPLETE"]
    return True, []


def _scope_matches(scope: object, candidate_sha: object) -> bool:
    return (
        isinstance(scope, dict)
        and scope.get("valid") is True
        and scope.get("unexpected_changes") == []
        and scope.get("candidate_sha256") == candidate_sha
        and scope.get("candidate_path_matches", True) is True
        and scope.get("candidate_hash_matches", True) is True
    )


def _trial_binding(
    run: Path,
    trial_path: Path | None,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    if trial_path is None:
        return None, ["CORRECTNESS_UNBOUND"]
    try:
        trial = _json(trial_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["CORRECTNESS_UNBOUND"]
    if not isinstance(trial, dict) or trial.get("record_type") != "wattch-agent-trial":
        return None, ["CORRECTNESS_UNBOUND"]
    blockers: list[str] = []
    artifact = trial.get("artifact")
    if (
        not isinstance(artifact, dict)
        or Path(str(artifact.get("run_dir", ""))).resolve() != run
        or artifact.get("run_id") != manifest.get("run_id")
    ):
        blockers.append("COMMAND_UNBOUND")
    acquisition = trial.get("acquisition")
    if (
        not isinstance(acquisition, dict)
        or acquisition.get("complete") is not True
        or acquisition.get("inspect_passed") is not True
    ):
        blockers.append("ARTIFACT_INVALID")
    correctness = trial.get("correctness")
    if (
        not isinstance(correctness, dict)
        or correctness.get("correct") is not True
        or correctness.get("completed_work") is not True
    ):
        blockers.append("CORRECTNESS_UNBOUND")
    else:
        for name in ("stdout_exact", "stderr_exact", "command_exact", "cwd_exact", "exit_exact"):
            if correctness.get(name) is not True:
                blockers.append("OUTPUT_MISMATCH")
    candidate = trial.get("candidate")
    candidate_sha = candidate.get("sha256") if isinstance(candidate, dict) else None
    mutation_scope = trial.get("mutation_scope")
    scope_before = (
        candidate.get("scope_before") if isinstance(candidate, dict) else None
    )
    scope_after = (
        candidate.get("scope_after") if isinstance(candidate, dict) else None
    )
    if scope_before is None:
        scope_before = mutation_scope
    if scope_after is None:
        scope_after = mutation_scope
    if (
        not isinstance(candidate, dict)
        or candidate.get("unchanged_during_trial") is not True
        or not _scope_matches(scope_before, candidate_sha)
        or not _scope_matches(scope_after, candidate_sha)
    ):
        blockers.append("MUTATION_SCOPE_UNBOUND")
    else:
        candidate_path_value = candidate.get("path")
        try:
            candidate_path = Path(str(candidate_path_value))
            if candidate_path.is_symlink():
                raise OSError("candidate is a symlink")
            candidate_path = candidate_path.resolve(strict=True)
            if (
                not candidate_path.is_file()
                or not isinstance(candidate_sha, str)
                or _sha256(candidate_path) != candidate_sha
            ):
                blockers.append("CANDIDATE_UNBOUND")
        except OSError:
            blockers.append("CANDIDATE_UNBOUND")
    manifest_labels = manifest.get("labels")
    if (
        not isinstance(candidate_sha, str)
        or not isinstance(manifest_labels, dict)
        or manifest_labels.get("candidate_sha256") != candidate_sha
    ):
        blockers.append("CANDIDATE_UNBOUND")
    files = artifact.get("files") if isinstance(artifact, dict) else None
    if not isinstance(files, dict):
        blockers.append("CHECKSUM_MISMATCH")
    else:
        required_files = CLI_BOUND_FILES if (run / "command.json").is_file() else CORE_ARTIFACT_FILES
        if not required_files.issubset(files):
            blockers.append("CHECKSUM_MISMATCH")
        for relative, expected in files.items():
            path = run / relative
            if not isinstance(expected, str) or not path.is_file() or _sha256(path) != expected:
                blockers.append("CHECKSUM_MISMATCH")
    return trial, sorted(set(blockers))


def _quality_gate(
    run: Path,
    source: dict[str, Any],
    trial: dict[str, Any] | None,
    trace_evidence: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    allow_unevaluated_overhead: bool = False,
) -> tuple[bool, list[str]]:
    path = run / "quality.json"
    if not path.is_file():
        return False, ["QUALITY_MISSING"]
    try:
        quality = _json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ["QUALITY_NOT_ACCEPTED"]
    blockers: list[str] = []
    disposition = quality.get("disposition") if isinstance(quality, dict) else None
    issues = quality.get("issues") if isinstance(quality, dict) else None
    overhead_review_only = (
        allow_unevaluated_overhead
        and disposition == "REVIEW_REQUIRED"
        and isinstance(issues, list)
        and bool(issues)
        and all(
            isinstance(issue, dict)
            and issue.get("severity") == "WARNING"
            and issue.get("code") == "OVERHEAD_NOT_EVALUATED"
            for issue in issues
        )
        and quality.get("measurement_overhead_ratio") is None
    )
    if not isinstance(quality, dict) or (
        disposition != "ACCEPTED" and not overhead_review_only
    ):
        blockers.append("QUALITY_NOT_ACCEPTED")
    if not isinstance(issues, list) or any(
        isinstance(issue, dict) and _upper(issue.get("severity")) == "ERROR"
        for issue in issues
    ):
        blockers.append("QUALITY_NOT_ACCEPTED")
    summaries = quality.get("sources") if isinstance(quality, dict) else None
    required_sources = [source]
    if _upper(source.get("temporality")) == "INTERVAL_AVERAGE":
        duration = _interval_duration_source(sources, source)
        if duration is None:
            blockers.append("INTERVAL_DURATION_UNBOUND")
        else:
            required_sources.append(duration)

    trace_counts = trace_evidence.get("counts")
    derived_gaps = trace_evidence.get("sequence_gaps")
    window_counts = trace_evidence.get("window_counts")
    marker_duration_s = trace_evidence.get("marker_duration_s")
    for required_source in required_sources:
        source_uid = required_source.get("source_uid")
        selected = [
            item
            for item in summaries or []
            if isinstance(item, dict) and item.get("source_uid") == source_uid
        ]
        if len(selected) != 1:
            blockers.append("QUALITY_NOT_ACCEPTED")
            continue
        item = selected[0]
        observed_samples = item.get("observed_samples")
        local_source_id = required_source.get("local_source_id")
        trace_count = (
            trace_counts.get(local_source_id)
            if isinstance(trace_counts, dict) and _nonnegative_int(local_source_id)
            else None
        )
        if (
            isinstance(observed_samples, bool)
            or not isinstance(observed_samples, int)
            or observed_samples < MIN_NATIVE_UPDATES
            or observed_samples != trace_count
        ):
            blockers.append("QUALITY_NOT_ACCEPTED")
        sequence_gaps = item.get("sequence_gaps")
        derived_gap_count = (
            derived_gaps.get(local_source_id)
            if isinstance(derived_gaps, dict) and _nonnegative_int(local_source_id)
            else None
        )
        if (
            isinstance(sequence_gaps, bool)
            or not isinstance(sequence_gaps, int)
            or sequence_gaps != 0
            or sequence_gaps != derived_gap_count
        ):
            blockers.append("QUALITY_NOT_ACCEPTED")
        nominal_period_ns = required_source.get("nominal_period_ns")
        window_count = (
            window_counts.get(local_source_id)
            if isinstance(window_counts, dict)
            else None
        )
        if (
            isinstance(nominal_period_ns, bool)
            or not isinstance(nominal_period_ns, int)
            or nominal_period_ns <= 0
            or isinstance(marker_duration_s, bool)
            or not isinstance(marker_duration_s, (int, float))
            or not math.isfinite(float(marker_duration_s))
            or marker_duration_s <= 0
        ):
            blockers.append("NATIVE_CADENCE_UNBOUND")
        elif float(marker_duration_s) * 1e9 < MIN_NATIVE_UPDATES * nominal_period_ns:
            blockers.append("NATIVE_CADENCE_UNDERQUALIFIED")
        if (
            isinstance(window_count, bool)
            or not isinstance(window_count, int)
            or window_count < MIN_NATIVE_UPDATES
        ):
            blockers.append("NATIVE_CADENCE_UNDERQUALIFIED")

    acquisition = trial.get("acquisition") if isinstance(trial, dict) else None
    reported_marker_duration_s = (
        acquisition.get("marker_duration_s")
        if isinstance(acquisition, dict)
        else None
    )
    if (
        isinstance(marker_duration_s, bool)
        or not isinstance(marker_duration_s, (int, float))
        or not math.isfinite(float(marker_duration_s))
        or marker_duration_s <= 0
    ):
        blockers.append("NATIVE_CADENCE_UNBOUND")
    elif (
        isinstance(reported_marker_duration_s, bool)
        or not isinstance(reported_marker_duration_s, (int, float))
        or not math.isfinite(float(reported_marker_duration_s))
        or not math.isclose(
            float(reported_marker_duration_s),
            float(marker_duration_s),
            rel_tol=1e-12,
            abs_tol=1e-9,
        )
    ):
        blockers.append("MARKER_DURATION_MISMATCH")
    files = trial.get("artifact", {}).get("files", {}) if trial else {}
    if files.get("quality.json") != _sha256(path):
        blockers.append("CHECKSUM_MISMATCH")
    return not blockers, sorted(set(blockers))


def _calibration_gate(
    run: Path,
    source: dict[str, Any],
    trial: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> tuple[bool, list[str]]:
    if not isinstance(trial, dict) or trial.get("calibration_bound") is not True:
        return False, ["CALIBRATION_MISSING"]
    reference = trial.get("calibration")
    if not isinstance(reference, dict):
        return False, ["CALIBRATION_MISSING"]
    try:
        raw_path = Path(reference["path"])
        if raw_path.is_symlink() or not raw_path.is_file():
            return False, ["CALIBRATION_MISMATCH"]
        path = raw_path.resolve(strict=True)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False, ["CALIBRATION_MISSING"]
    expected_sha = reference.get("sha256")
    if not isinstance(expected_sha, str) or _sha256(path) != expected_sha:
        return False, ["CALIBRATION_MISMATCH"]
    try:
        document = _json(path)
        quality = _json(run / "quality.json")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ["CALIBRATION_MISMATCH"]
    if not isinstance(document, dict) or not isinstance(quality, dict):
        return False, ["CALIBRATION_MISMATCH"]
    if document.get("schema_version") != 1 or document.get("kind") != "METER_CALIBRATION":
        return False, ["CALIBRATION_MISMATCH"]
    regimes = document.get("required_regimes")
    if (
        not isinstance(regimes, list)
        or not all(isinstance(item, str) for item in regimes)
        or not {"IDLE", "FIXED_LOAD", "AA"}.issubset(regimes)
    ):
        return False, ["CALIBRATION_MISMATCH"]
    boundary = document.get("measurement_boundary")
    if isinstance(boundary, dict):
        boundary_bytes = json.dumps(
            boundary,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        if hashlib.sha256(boundary_bytes).hexdigest() != document.get(
            "measurement_boundary_sha256"
        ):
            return False, ["CALIBRATION_MISMATCH"]
    signatures = boundary.get("required_sources") if isinstance(boundary, dict) else None
    matching_signatures = [
        item
        for item in signatures or []
        if isinstance(item, dict)
        and item.get("source_uid") == source.get("source_uid")
    ]
    signature = matching_signatures[0] if len(matching_signatures) == 1 else None
    labels = source.get("labels") if isinstance(source.get("labels"), dict) else {}
    expected_signature = {
        "source_uid": source.get("source_uid"),
        "kind": source.get("kind"),
        "unit": source.get("unit"),
        "temporality": source.get("temporality"),
        "evidence_class": source.get("evidence_class"),
        "parent_source_uid": source.get("parent_source_uid"),
        "clock_domain": source.get("clock_domain"),
        "disjoint_group": labels.get("wattch.disjoint_group"),
        "disjoint_member": labels.get("wattch.disjoint_member"),
    }
    duration_uid = labels.get("wattch.interval_duration_source_uid")
    if duration_uid is not None:
        expected_signature["interval_duration_source_uid"] = duration_uid
    if (
        not isinstance(boundary, dict)
        or boundary.get("backend") != source.get("backend")
        or boundary.get("aggregation_rule") != "SINGLE_SOURCE"
        or boundary.get("primary_source_uids") != [source.get("source_uid")]
        or signature != expected_signature
    ):
        return False, ["CALIBRATION_MISMATCH"]
    manifest_labels = manifest.get("labels")
    selected_interval_ns = document.get("selected_interval_ns")
    nominal_period_ns = source.get("nominal_period_ns")
    try:
        labeled_interval_ns = int(manifest_labels["interval_ms"]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return False, ["CALIBRATION_MISMATCH"]
    if (
        not isinstance(manifest_labels, dict)
        or manifest_labels.get("measurement_overhead_calibration_sha256")
        != expected_sha
        or selected_interval_ns != labeled_interval_ns
        or isinstance(nominal_period_ns, bool)
        or not isinstance(nominal_period_ns, int)
        or nominal_period_ns <= 0
        or selected_interval_ns != nominal_period_ns
    ):
        return False, ["CALIBRATION_MISMATCH"]
    selected_metrics = document.get("selected_metrics")
    thresholds = document.get("thresholds")
    if not isinstance(selected_metrics, dict) or not isinstance(thresholds, dict):
        return False, ["CALIBRATION_MISMATCH"]
    for metric_name, threshold_name, hard_limit in (
        ("measurement_overhead_ratio", "max_measurement_overhead_ratio", 0.01),
        ("missing_sample_ratio", "max_missing_sample_ratio", 0.001),
        ("phase_sensitivity_ratio", "max_phase_sensitivity_ratio", 0.01),
    ):
        metric = selected_metrics.get(metric_name)
        threshold = thresholds.get(threshold_name)
        if (
            isinstance(metric, bool)
            or not isinstance(metric, (int, float))
            or not math.isfinite(float(metric))
            or float(metric) < 0
            or isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or float(threshold) <= 0
            or float(metric) >= float(threshold)
            or float(threshold) > hard_limit
        ):
            return False, ["CALIBRATION_MISMATCH"]
    calibrated_overhead = (
        selected_metrics.get("measurement_overhead_ratio")
    )
    quality_overhead = quality.get("measurement_overhead_ratio")
    if (
        isinstance(calibrated_overhead, bool)
        or not isinstance(calibrated_overhead, (int, float))
        or isinstance(quality_overhead, bool)
        or not isinstance(quality_overhead, (int, float))
        or not math.isclose(
            float(calibrated_overhead),
            float(quality_overhead),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        return False, ["CALIBRATION_MISMATCH"]
    return True, []


def _differential_readiness_gate(
    source: dict[str, Any],
    trial: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Bind a rejected absolute meter to an explicit paired-only estimand."""

    if (
        not isinstance(trial, dict)
        or trial.get("comparison_mode") != PAIRED_DIFFERENTIAL_MODE
        or trial.get("differential_readiness_bound") is not True
        or trial.get("calibration_bound") is not False
    ):
        return False, ["DIFFERENTIAL_READINESS_MISSING"]
    reference = trial.get("differential_readiness")
    if not isinstance(reference, dict):
        return False, ["DIFFERENTIAL_READINESS_MISSING"]
    try:
        raw_path = Path(reference["path"])
        if raw_path.is_symlink() or not raw_path.is_file():
            return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
        path = raw_path.resolve(strict=True)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return False, ["DIFFERENTIAL_READINESS_MISSING"]
    expected_sha = reference.get("sha256")
    if not isinstance(expected_sha, str) or _sha256(path) != expected_sha:
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    try:
        document = _json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("record_type") != DIFFERENTIAL_READINESS_KIND
        or document.get("eligible_for_comparison") is not False
        or document.get("method")
        != "pywattch.analysis.calibration.compare_sham_trials"
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    blockers = document.get("blockers")
    if (
        not isinstance(blockers, list)
        or not {
            "METER_PHASE_SENSITIVITY_REJECTED",
            "MEASUREMENT_OVERHEAD_NOT_EVALUATED",
        }.issubset(blockers)
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    meaningful = document.get("meaningful_effect")
    threshold = (
        meaningful.get("meaningful_effect_joules")
        if isinstance(meaningful, dict)
        else None
    )
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) <= 0
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    instrumented_value = document.get("instrumented_path")
    if not isinstance(instrumented_value, str) or not instrumented_value:
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    instrumented_path = Path(instrumented_value)
    if not instrumented_path.is_absolute():
        instrumented_path = path.parent / instrumented_path
    try:
        if instrumented_path.is_symlink() or not instrumented_path.is_file():
            return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
        instrumented_path = instrumented_path.resolve(strict=True)
    except OSError:
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    instrumented_sha = document.get("instrumented_sha256")
    if (
        not isinstance(instrumented_sha, str)
        or _sha256(instrumented_path) != instrumented_sha
        or reference.get("instrumented_sha256") != instrumented_sha
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    try:
        instrumented = _json(instrumented_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    if (
        not isinstance(instrumented, dict)
        or instrumented.get("schema_version") != 1
        or instrumented.get("record_type")
        != "wattch-meter-calibration-instrumented"
        or not isinstance(instrumented.get("observations"), list)
        or not instrumented["observations"]
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    boundary = instrumented.get("measurement_boundary")
    labels = source.get("labels") if isinstance(source.get("labels"), dict) else {}
    signatures = boundary.get("required_sources") if isinstance(boundary, dict) else None
    matching = [
        item
        for item in signatures or []
        if isinstance(item, dict)
        and item.get("source_uid") == source.get("source_uid")
    ]
    if len(matching) != 1:
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    signature = matching[0]
    expected_fields = {
        "source_uid": source.get("source_uid"),
        "kind": source.get("kind"),
        "unit": source.get("unit"),
        "temporality": source.get("temporality"),
        "evidence_class": source.get("evidence_class"),
        "parent_source_uid": source.get("parent_source_uid"),
        "clock_domain": source.get("clock_domain"),
        "interval_duration_source_uid": labels.get(
            INTERVAL_DURATION_SOURCE_UID_LABEL
        ),
        "interval_timeline_reconstruction": labels.get(
            INTERVAL_TIMELINE_RECONSTRUCTION_LABEL
        ),
    }
    if (
        not isinstance(boundary, dict)
        or boundary.get("aggregation_rule") != "SINGLE_SOURCE"
        or boundary.get("backend") != source.get("backend")
        or boundary.get("primary_source_uids") != [source.get("source_uid")]
        or any(signature.get(key) != value for key, value in expected_fields.items())
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    manifest_labels = manifest.get("labels")
    nominal_period_ns = source.get("nominal_period_ns")
    try:
        labeled_interval_ns = int(manifest_labels["interval_ms"]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    if (
        not isinstance(manifest_labels, dict)
        or manifest_labels.get("comparison_mode") != PAIRED_DIFFERENTIAL_MODE
        or manifest_labels.get("differential_readiness_sha256") != expected_sha
        or manifest_labels.get("measurement_evidence_sha256") != expected_sha
        or instrumented.get("interval_ns") != labeled_interval_ns
        or nominal_period_ns != labeled_interval_ns
    ):
        return False, ["DIFFERENTIAL_READINESS_MISMATCH"]
    return True, []


def qualify_run(
    run_dir: Path,
    *,
    requested_source_uid: str | None = None,
    trial_record: Path | None = None,
) -> dict[str, object]:
    """Return explicit gates; every physical/comparison flag fails closed."""

    run = run_dir.resolve()
    blockers: list[str] = []
    try:
        sources_value = _json(run / "sources.json")
        manifest = _json(run / "manifest.json")
    except (OSError, UnicodeError, json.JSONDecodeError):
        sources_value, manifest = [], {}
        blockers.append("ARTIFACT_INVALID")
    sources, descriptor_blockers = _descriptor_set(sources_value)
    blockers.extend(descriptor_blockers)
    if not isinstance(manifest, dict) or manifest.get("sources") != sources_value:
        blockers.append("ARTIFACT_INVALID")
    selected, selection_blockers = _select_source(sources, requested_source_uid)
    blockers.extend(selection_blockers)
    source_result = classify_source(selected)
    blockers.extend(source_result["blockers"])

    core_files_present = all(
        (run / relative).is_file() and (run / relative).stat().st_size > 0
        for relative in CORE_ARTIFACT_FILES
    )
    if not core_files_present:
        blockers.append("ARTIFACT_INVALID")
    trace_ok = False
    trace_evidence: dict[str, Any] = {}
    if core_files_present and isinstance(manifest, dict):
        trace_ok, trace_evidence, trace_blockers = _trace_gate(
            run, manifest, sources
        )
        blockers.extend(trace_blockers)
    managed, lifecycle_blockers = _mcp_lifecycle(run, manifest if isinstance(manifest, dict) else {})
    blockers.extend(lifecycle_blockers)
    cli_managed, cli_lifecycle_blockers = _cli_lifecycle(
        run, manifest if isinstance(manifest, dict) else {}
    )
    blockers.extend(cli_lifecycle_blockers)
    trial, binding_blockers = _trial_binding(
        run,
        trial_record,
        manifest if isinstance(manifest, dict) else {},
    )
    blockers.extend(binding_blockers)
    if not managed and not cli_managed:
        blockers.append("CAPTURE_NOT_FINALIZED")
    if cli_managed and not all(
        (run / relative).is_file()
        and ((run / relative).stat().st_size > 0 or relative == "target.stderr.log")
        for relative in CLI_BOUND_FILES
    ):
        blockers.append("ARTIFACT_INVALID")

    source_uid = source_result.get("source_uid")
    differential_requested = (
        isinstance(trial, dict)
        and trial.get("comparison_mode") == PAIRED_DIFFERENTIAL_MODE
        and trial.get("differential_readiness_bound") is True
    )
    quality_ok = False
    if isinstance(source_uid, str):
        quality_ok, quality_blockers = _quality_gate(
            run,
            selected,
            trial,
            trace_evidence,
            sources,
            allow_unevaluated_overhead=differential_requested,
        )
        blockers.extend(quality_blockers)
    else:
        blockers.append("QUALITY_MISSING")
    calibration_bound = False
    differential_readiness_bound = False
    if differential_requested:
        differential_readiness_bound, evidence_blockers = (
            _differential_readiness_gate(
                selected if isinstance(selected, dict) else {},
                trial,
                manifest if isinstance(manifest, dict) else {},
            )
        )
    else:
        calibration_bound, evidence_blockers = _calibration_gate(
            run,
            selected if isinstance(selected, dict) else {},
            trial,
            manifest if isinstance(manifest, dict) else {},
        )
    blockers.extend(evidence_blockers)

    gates = {
        "descriptor_set_valid": not descriptor_blockers,
        "single_primary_source": selected is not None,
        "source_semantics_supported": bool(source_result["outcome_semantics_supported"]),
        "source_is_local_physical_candidate": bool(source_result["physical_source_candidate"]),
        "core_artifact_files_present": core_files_present,
        "trace_artifacts_decodable": trace_ok,
        "capture_lifecycle_complete": (managed and not lifecycle_blockers)
        or (cli_managed and not cli_lifecycle_blockers),
        "correctness_and_work_bound": trial is not None and not binding_blockers,
        "quality_accepted_and_bound": quality_ok,
        "calibration_bound": calibration_bound,
        "differential_readiness_bound": differential_readiness_bound,
    }
    common_gate_names = (
        "descriptor_set_valid",
        "single_primary_source",
        "source_semantics_supported",
        "source_is_local_physical_candidate",
        "core_artifact_files_present",
        "trace_artifacts_decodable",
        "capture_lifecycle_complete",
        "correctness_and_work_bound",
        "quality_accepted_and_bound",
    )
    common_eligible = all(gates[name] for name in common_gate_names)
    descriptive = common_eligible and calibration_bound and not blockers
    differential = (
        common_eligible and differential_readiness_bound and not blockers
    )
    # Schema v1 binds a local exploratory calibration, but does not carry an
    # independently verified factory-calibration attestation.
    confirmatory_calibration_verified = False
    physical = descriptive and confirmatory_calibration_verified
    warnings = [
        "Local Wattch telemetry does not observe remote model inference or datacenter energy.",
        "One run cannot establish a model or implementation comparison.",
    ]
    if source_result["evidence_tier"] == "LOCAL_TOOL_ESTIMATE":
        warnings.append("The selected local-domain outcome is a tool-derived estimate.")
    if calibration_bound:
        warnings.append(
            "Schema-v1 calibration supports an exploratory local estimate only; "
            "it is not factory-verified confirmatory calibration."
        )
    if differential_readiness_bound:
        warnings.extend(
            [
                "The rejected absolute meter authorizes only an A/A-thresholded paired differential analysis.",
                "The continuously running collector is part of this instrumented-system estimand; absolute energy remains ineligible.",
            ]
        )
    return {
        "schema_version": 1,
        "run": str(run),
        "selected_source_uid": source_uid,
        "source": source_result,
        "gates": gates,
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "outcome_computable": False,
        "descriptive_local_outcome_eligible": descriptive,
        "descriptive_local_run_eligible": descriptive,
        "differential_local_outcome_eligible": differential,
        "differential_local_run_eligible": differential,
        "confirmatory_calibration_verified": confirmatory_calibration_verified,
        "physical_run_claim_eligible": physical,
        "physical_comparison_claim_eligible": False,
        "remote_model_energy_claim_eligible": False,
    }
