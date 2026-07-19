"""Fail-closed Wattch artifact/source qualification for agent workflows.

This module classifies evidence. It deliberately does not integrate energy or
turn one run into a comparison claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
    "target.stdout.log",
    "target.stderr.log",
}


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
    kind = _upper(source.get("kind"))
    unit = _upper(source.get("unit"))
    temporality = _upper(source.get("temporality"))
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

    semantics_supported, semantics = _source_semantics(source)
    if not semantics_supported:
        blockers.append("UNSUPPORTED_ENERGY_SEMANTICS")
    backend = str(source.get("backend", "")).strip().lower()
    sampling = _upper(source.get("sampling_mode"))
    evidence_class = _upper(source.get("evidence_class"))
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
            labels = source.get("labels")
            duration_uid = (
                labels.get("wattch.interval_duration_source_uid")
                if isinstance(labels, dict)
                else None
            )
            matches = [item for item in typed if item.get("source_uid") == duration_uid]
            if len(matches) != 1:
                blockers.append("INTERVAL_DURATION_UNBOUND")
            else:
                duration = matches[0]
                if (
                    _upper(duration.get("kind")) != "SAMPLE_INTERVAL_DURATION"
                    or _upper(duration.get("unit")) != "NANOSECOND"
                    or _upper(duration.get("temporality")) != "DELTA"
                    or duration.get("node_id") != source.get("node_id")
                    or duration.get("clock_domain") != source.get("clock_domain")
                ):
                    blockers.append("INTERVAL_DURATION_UNBOUND")
    return typed, sorted(set(blockers))


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


def _trial_binding(run: Path, trial_path: Path | None) -> tuple[dict[str, Any] | None, list[str]]:
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
    if not isinstance(artifact, dict) or Path(str(artifact.get("run_dir", ""))).resolve() != run:
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
    files = artifact.get("files") if isinstance(artifact, dict) else None
    if not isinstance(files, dict):
        blockers.append("CHECKSUM_MISMATCH")
    else:
        for relative, expected in files.items():
            path = run / relative
            if not isinstance(expected, str) or not path.is_file() or _sha256(path) != expected:
                blockers.append("CHECKSUM_MISMATCH")
    return trial, sorted(set(blockers))


def _quality_gate(run: Path, source_uid: str, trial: dict[str, Any] | None) -> tuple[bool, list[str]]:
    path = run / "quality.json"
    if not path.is_file():
        return False, ["QUALITY_MISSING"]
    try:
        quality = _json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, ["QUALITY_NOT_ACCEPTED"]
    blockers: list[str] = []
    if not isinstance(quality, dict) or quality.get("disposition") != "ACCEPTED":
        blockers.append("QUALITY_NOT_ACCEPTED")
    issues = quality.get("issues") if isinstance(quality, dict) else None
    if not isinstance(issues, list) or any(
        isinstance(issue, dict) and _upper(issue.get("severity")) == "ERROR"
        for issue in issues
    ):
        blockers.append("QUALITY_NOT_ACCEPTED")
    summaries = quality.get("sources") if isinstance(quality, dict) else None
    selected = [item for item in summaries or [] if isinstance(item, dict) and item.get("source_uid") == source_uid]
    if len(selected) != 1:
        blockers.append("QUALITY_NOT_ACCEPTED")
    else:
        item = selected[0]
        if not isinstance(item.get("observed_samples"), int) or item["observed_samples"] < 2:
            blockers.append("QUALITY_NOT_ACCEPTED")
        if item.get("sequence_gap_count") not in (0, None):
            blockers.append("QUALITY_NOT_ACCEPTED")
    files = trial.get("artifact", {}).get("files", {}) if trial else {}
    if files.get("quality.json") != _sha256(path):
        blockers.append("CHECKSUM_MISMATCH")
    return not blockers, sorted(set(blockers))


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
    managed, lifecycle_blockers = _mcp_lifecycle(run, manifest if isinstance(manifest, dict) else {})
    blockers.extend(lifecycle_blockers)
    trial, binding_blockers = _trial_binding(run, trial_record)
    blockers.extend(binding_blockers)
    cli_managed = (run / "command.json").is_file()
    if not managed and not cli_managed:
        blockers.append("CAPTURE_NOT_FINALIZED")
    if cli_managed and not all(
        (run / relative).is_file()
        and ((run / relative).stat().st_size > 0 or relative == "target.stderr.log")
        for relative in CLI_BOUND_FILES
    ):
        blockers.append("ARTIFACT_INVALID")

    source_uid = source_result.get("source_uid")
    quality_ok = False
    if isinstance(source_uid, str):
        quality_ok, quality_blockers = _quality_gate(run, source_uid, trial)
        blockers.extend(quality_blockers)
    else:
        blockers.append("QUALITY_MISSING")
    calibration_bound = bool(trial and trial.get("calibration_bound") is True)
    if not calibration_bound:
        blockers.append("CALIBRATION_MISSING")

    gates = {
        "descriptor_set_valid": not descriptor_blockers,
        "single_primary_source": selected is not None,
        "source_semantics_supported": bool(source_result["outcome_semantics_supported"]),
        "source_is_local_physical_candidate": bool(source_result["physical_source_candidate"]),
        "core_artifact_files_present": core_files_present,
        "capture_lifecycle_complete": (managed and not lifecycle_blockers)
        or (cli_managed and trial is not None and not binding_blockers),
        "correctness_and_work_bound": trial is not None and not binding_blockers,
        "quality_accepted_and_bound": quality_ok,
        "calibration_bound": calibration_bound,
    }
    physical = all(gates.values()) and not blockers
    warnings = [
        "Local Wattch telemetry does not observe remote model inference or datacenter energy.",
        "One run cannot establish a model or implementation comparison.",
    ]
    if source_result["evidence_tier"] == "LOCAL_TOOL_ESTIMATE":
        warnings.append("The selected local-domain outcome is a tool-derived estimate.")
    return {
        "schema_version": 1,
        "run": str(run),
        "selected_source_uid": source_uid,
        "source": source_result,
        "gates": gates,
        "blockers": sorted(set(blockers)),
        "warnings": warnings,
        "outcome_computable": False,
        "descriptive_local_outcome_eligible": bool(
            gates["source_semantics_supported"]
            and gates["core_artifact_files_present"]
            and source_result["evidence_tier"]
            in {"RAW_LOCAL_MEASUREMENT", "LOCAL_TOOL_ESTIMATE"}
        ),
        "physical_run_claim_eligible": physical,
        "physical_comparison_claim_eligible": False,
        "remote_model_energy_claim_eligible": False,
    }
