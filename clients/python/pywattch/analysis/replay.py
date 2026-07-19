"""Validated replay from a sealed trial artifact to an analysis observation.

This is the trust boundary between acquisition and causal analysis.  It never
modifies a capture: it verifies the sealed sidecars and framed traces, resolves
the daemon-stamped estimand window, and only accepts evidence whose offline
quality disposition is ``ACCEPTED``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .._frames import (
    HEADER_LEN,
    MAX_LEGACY_ARTIFACT_PAYLOAD_LEN,
    MAX_PAYLOAD_LEN,
    FrameKind,
    decode_header,
)
from .._host_provenance import validate_host_provenance
from .._messages import (
    Event,
    EventBatch,
    RawSample,
    RawSampleBatch,
    SourceDescriptor,
    decode_payload,
)
from .._workload_metrics import (
    BACKGROUND_WORKLOAD_METRICS_FILENAME,
    WORKLOAD_METRICS_CONTRACT,
    WORKLOAD_METRICS_FILENAME,
    WorkloadMetrics,
    WorkloadMetricsError,
    WorkloadMetricsSource,
    load_workload_metrics_sidecar,
)
from .boundary import AnalysisError, MeasurementBoundary
from .calibration import build_trial_quality_metadata, summarize_source_quality
from .effects import TrialObservation
from .outcomes import EnergyOutcome, Estimand, compute_energy_outcome

_REQUIRED_FILES = {
    "trial.json",
    "manifest.json",
    "sources.json",
    "raw.trace.wch",
    "events.trace.wch",
    "quality.json",
    "acquisition.json",
    "checksums.json",
    "target.stdout.log",
    "target.stderr.log",
}

_REQUIRED_MARKERS = (
    "PRE_WINDOW_START",
    "BACKGROUND_ARRIVAL",
    "TARGET_SPAWN",
    "TARGET_EXEC",
    "TARGET_EXIT",
    "RECOVERY_END",
    "BACKGROUND_COMPLETION",
)


@dataclass(frozen=True)
class TrialEvidence:
    """Verified raw evidence and its conservative analysis observation."""

    trial: Mapping[str, Any]
    manifest: Mapping[str, Any]
    quality: Mapping[str, Any]
    acquisition: Mapping[str, Any]
    descriptors: tuple[SourceDescriptor, ...]
    samples: tuple[RawSample, ...]
    events: tuple[Event, ...]
    workload_metrics: WorkloadMetrics | None
    workload_metrics_sha256: str | None
    background_workload_metrics: WorkloadMetrics | None
    background_workload_metrics_sha256: str | None
    outcome: EnergyOutcome
    observation: TrialObservation


_NON_MATCHING_LABELS = {
    "arm_code",
    "order_position",
    "mechanism_policy",
    "mechanism_position",
    "output_equivalence",
    "expected_output_sha256",
    "execution_contract_sha256",
}


def trial_match_fingerprint(trial: Mapping[str, Any]) -> str:
    """Hash the frozen arm-invariant execution contract of a matched block."""

    labels = trial.get("labels")
    estimand = trial.get("estimand")
    if not isinstance(labels, Mapping) or not isinstance(estimand, Mapping):
        raise AnalysisError("trial matching contract needs labels and an estimand")
    required_labels = {
        "target_nice",
        "background_nice",
        "interval_ms",
        "cpu_affinity",
        "resource_limits_fingerprint_sha256",
        "workload_mode",
    }
    missing = sorted(required_labels - set(labels))
    if missing:
        raise AnalysisError(
            "trial matching contract omits labels: " + ", ".join(missing)
        )
    for field_name in (
        "experiment_id",
        "matched_block_id",
        "background_workload_id",
        "input_id",
        "replay_seed",
    ):
        if field_name not in trial:
            raise AnalysisError(f"trial matching contract omits field {field_name!r}")
    stable_labels = {
        str(key): value
        for key, value in labels.items()
        if key not in _NON_MATCHING_LABELS and not str(key).startswith("host_policy_")
    }
    document = {
        "experiment_id": trial["experiment_id"],
        "matched_block_id": trial["matched_block_id"],
        "background_workload_id": trial["background_workload_id"],
        "input_id": trial["input_id"],
        "replay_seed": trial["replay_seed"],
        "estimand": dict(estimand),
        "labels": stable_labels,
    }
    try:
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AnalysisError(
            f"trial matching contract is not strict JSON: {exc}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def evaluate_trial_quality(
    trial_dir: str | Path,
    *,
    measurement_overhead_ratio: float | None,
    max_overhead_ratio: float = 0.01,
    max_missing_sample_ratio: float = 0.001,
) -> dict[str, object]:
    """Evaluate a sealed provisional capture without interpreting its energy.

    The returned canonical document is suitable for
    :func:`pywattch.experiment.finalize_trial_quality`.  Overhead must come from
    a separate matched calibration; it is never inferred from this treatment
    run.
    """

    root = Path(trial_dir)
    missing = sorted(name for name in _REQUIRED_FILES if not (root / name).is_file())
    if missing:
        raise AnalysisError("trial artifact is missing files: " + ", ".join(missing))
    _verify_checksums(root)
    current_quality = _read_json(root / "quality.json")
    if current_quality.get("disposition") != "REVIEW_REQUIRED":
        raise AnalysisError("trial quality is not a provisional REVIEW_REQUIRED record")
    trial = _read_json(root / "trial.json")
    manifest = _read_json(root / "manifest.json")
    source_documents = _read_json(root / "sources.json", object_required=False)
    if (
        not isinstance(source_documents, list)
        or manifest.get("sources") != source_documents
    ):
        raise AnalysisError("manifest sources do not exactly match sources.json")
    try:
        descriptors = tuple(
            SourceDescriptor.from_dict(dict(document))
            for document in source_documents
            if isinstance(document, Mapping)
        )
    except Exception as exc:
        raise AnalysisError(f"invalid source descriptor: {exc}") from exc
    if len(descriptors) != len(source_documents):
        raise AnalysisError("sources.json contains a non-object descriptor")
    trial_id = trial.get("trial_id")
    run_id = trial.get("run_id")
    node_id = manifest.get("node_id")
    if not all(
        isinstance(value, str) and value for value in (trial_id, run_id, node_id)
    ):
        raise AnalysisError("trial, run, or node identity is invalid")
    if root.name != trial_id or manifest.get("run_id") != run_id:
        raise AnalysisError("trial and manifest identities disagree")
    boundary_document = trial.get("measurement_boundary")
    if not isinstance(boundary_document, Mapping):
        raise AnalysisError("trial measurement_boundary must be an object")
    boundary = MeasurementBoundary.from_dict(boundary_document)
    boundary.validate_descriptors(descriptors)
    legacy_v0_1 = _is_legacy_v0_1(manifest.get("protocol_version"))
    raw_batches = _read_trace(
        root / "raw.trace.wch",
        FrameKind.RAW_SAMPLE_BATCH,
        allow_large_legacy_frames=legacy_v0_1,
    )
    if not all(isinstance(batch, RawSampleBatch) for batch in raw_batches):
        raise AnalysisError("raw trace decoded an unexpected message")
    samples = _validate_raw_batches(
        raw_batches,
        run_id=run_id,
        node_id=node_id,
        source_ids={descriptor.local_source_id for descriptor in descriptors},
    )
    event_batches = _read_trace(root / "events.trace.wch", FrameKind.EVENT_BATCH)
    if not all(isinstance(batch, EventBatch) for batch in event_batches):
        raise AnalysisError("event trace decoded an unexpected message")
    events = _validate_event_batches(event_batches, run_id=run_id, node_id=node_id)
    markers = {kind: _one_event(events, kind) for kind in _REQUIRED_MARKERS}
    summaries = summarize_source_quality(
        descriptors,
        samples,
        capture_start_monotonic_ns=markers["TARGET_SPAWN"].monotonic_ns,
        capture_end_monotonic_ns=markers["RECOVERY_END"].monotonic_ns,
        expected_sample_opportunities=_sample_opportunities(descriptors, raw_batches),
    )
    quality = build_trial_quality_metadata(
        trial_id=trial_id,
        summaries=summaries,
        measurement_overhead_ratio=measurement_overhead_ratio,
        required_source_uids=boundary.primary_source_uids,
        max_overhead_ratio=max_overhead_ratio,
        max_missing_sample_ratio=max_missing_sample_ratio,
    )
    batch_gaps = _batch_sequence_gaps(raw_batches)
    if batch_gaps:
        quality["disposition"] = "REJECTED"
        quality["issues"].append(
            {
                "code": "RAW_BATCH_SEQUENCE_GAP",
                "severity": "ERROR",
                "message": f"raw trace is missing {batch_gaps} sample batch(es)",
                "source_uid": None,
            }
        )
    return quality


def _read_json(path: Path, *, object_required: bool = True) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot read {path.name}: {exc}") from exc
    if object_required and not isinstance(value, Mapping):
        raise AnalysisError(f"{path.name} must contain a JSON object")
    return value


def _verify_checksums(root: Path) -> None:
    document = _read_json(root / "checksums.json")
    if document.get("schema_version") != 1 or document.get("algorithm") != "sha256":
        raise AnalysisError("checksums.json uses an unsupported schema or algorithm")
    entries = document.get("files")
    if not isinstance(entries, Mapping):
        raise AnalysisError("checksums.json files must be an object")
    required_sealed = _REQUIRED_FILES - {"checksums.json"}
    if not required_sealed.issubset(entries):
        missing = sorted(required_sealed - set(entries))
        raise AnalysisError("checksums omit required files: " + ", ".join(missing))
    failures: list[str] = []
    for relative, expected in entries.items():
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(expected, Mapping)
        ):
            raise AnalysisError("checksums.json contains an invalid file entry")
        expected_size = expected.get("bytes")
        expected_digest = expected.get("sha256")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_digest, str)
            or len(expected_digest) != 64
        ):
            raise AnalysisError("checksums.json contains invalid size or digest data")
        path = root / relative
        if not path.is_file():
            failures.append(relative)
            continue
        contents = path.read_bytes()
        if len(contents) != expected_size:
            failures.append(relative)
            continue
        if hashlib.sha256(contents).hexdigest() != expected_digest:
            failures.append(relative)
    if failures:
        raise AnalysisError(
            "trial artifact checksum verification failed: "
            + ", ".join(sorted(failures))
        )


def _read_trace(
    path: Path,
    expected_kind: FrameKind,
    *,
    allow_large_legacy_frames: bool = False,
) -> tuple[Any, ...]:
    try:
        contents = path.read_bytes()
    except OSError as exc:
        raise AnalysisError(f"cannot read {path.name}: {exc}") from exc
    offset = 0
    messages: list[Any] = []
    while offset < len(contents):
        if len(contents) - offset < HEADER_LEN:
            raise AnalysisError(f"{path.name} ends with a partial frame header")
        header = contents[offset : offset + HEADER_LEN]
        try:
            wire_minor = int.from_bytes(header[6:8], "little")
            payload_limit = (
                MAX_LEGACY_ARTIFACT_PAYLOAD_LEN
                if allow_large_legacy_frames and wire_minor == 1
                else MAX_PAYLOAD_LEN
            )
            kind, flags, payload_len = decode_header(
                header, max_payload_len=payload_limit
            )
        except Exception as exc:
            raise AnalysisError(f"invalid frame in {path.name}: {exc}") from exc
        if kind is not expected_kind:
            raise AnalysisError(
                f"{path.name} contains {kind.name}, expected {expected_kind.name}"
            )
        if flags != 0:
            raise AnalysisError(f"{path.name} uses unsupported non-zero frame flags")
        payload_start = offset + HEADER_LEN
        payload_end = payload_start + payload_len
        if payload_end > len(contents):
            raise AnalysisError(f"{path.name} ends with a partial frame payload")
        try:
            messages.append(decode_payload(kind, contents[payload_start:payload_end]))
        except Exception as exc:
            raise AnalysisError(f"invalid payload in {path.name}: {exc}") from exc
        offset = payload_end
    if not messages:
        raise AnalysisError(f"{path.name} contains no frames")
    return tuple(messages)


def _is_legacy_v0_1(protocol_version: object) -> bool:
    return isinstance(protocol_version, str) and (
        protocol_version == "0.1" or protocol_version.startswith("0.1.")
    )


def _validate_raw_batches(
    batches: tuple[RawSampleBatch, ...],
    *,
    run_id: str,
    node_id: str,
    source_ids: set[int],
) -> tuple[RawSample, ...]:
    samples: list[RawSample] = []
    previous_batch: int | None = None
    previous_by_source: dict[int, RawSample] = {}
    for batch in batches:
        if batch.run_id != run_id or batch.node_id != node_id:
            raise AnalysisError("raw batch identity does not match trial manifest")
        if previous_batch is not None and batch.batch_sequence_no <= previous_batch:
            raise AnalysisError("raw batch sequence numbers must strictly increase")
        previous_batch = batch.batch_sequence_no
        batch_source_ids: set[int] = set()
        for sample in batch.samples:
            if sample.source_id not in source_ids:
                raise AnalysisError(
                    f"raw trace refers to unknown source id {sample.source_id}"
                )
            if sample.source_id in batch_source_ids:
                raise AnalysisError(f"raw batch repeats source id {sample.source_id}")
            batch_source_ids.add(sample.source_id)
            if sample.sequence_no < 0 or sample.monotonic_ns < 0:
                raise AnalysisError(
                    "raw sample sequence and timestamp must be non-negative"
                )
            previous = previous_by_source.get(sample.source_id)
            if previous is not None:
                if sample.sequence_no <= previous.sequence_no:
                    raise AnalysisError(
                        "per-source sample sequence numbers must strictly increase"
                    )
                if sample.monotonic_ns <= previous.monotonic_ns:
                    raise AnalysisError(
                        "per-source sample timestamps must strictly increase"
                    )
            previous_by_source[sample.source_id] = sample
            samples.append(sample)
    return tuple(samples)


def _batch_sequence_gaps(batches: tuple[RawSampleBatch, ...]) -> int:
    if not batches:
        return 0
    gaps = batches[0].batch_sequence_no
    gaps += sum(
        later.batch_sequence_no - earlier.batch_sequence_no - 1
        for earlier, later in zip(batches, batches[1:])
    )
    return gaps


def _sample_opportunities(
    descriptors: tuple[SourceDescriptor, ...],
    batches: tuple[RawSampleBatch, ...],
) -> dict[int, int]:
    if not batches:
        return {}
    expected_batches = batches[-1].batch_sequence_no + 1
    return {
        descriptor.local_source_id: expected_batches
        for descriptor in descriptors
        if descriptor.sampling_mode == "POLLED" and descriptor.nominal_period_ns is None
    }


def _validate_event_batches(
    batches: tuple[EventBatch, ...], *, run_id: str, node_id: str
) -> tuple[Event, ...]:
    events: list[Event] = []
    event_ids: set[str] = set()
    previous_batch: int | None = None
    previous_timestamp: int | None = None
    for batch in batches:
        if batch.run_id != run_id or batch.node_id != node_id:
            raise AnalysisError("event batch identity does not match trial manifest")
        expected_batch = 0 if previous_batch is None else previous_batch + 1
        if batch.batch_sequence_no != expected_batch:
            raise AnalysisError(
                "event batch sequence numbers must start at zero and be contiguous"
            )
        previous_batch = batch.batch_sequence_no
        for event in batch.events:
            if event.run_id != run_id or event.node_id != node_id:
                raise AnalysisError("event identity does not match trial manifest")
            if not event.event_id or event.event_id in event_ids:
                raise AnalysisError("daemon event IDs must be unique and non-empty")
            if event.monotonic_ns < 0:
                raise AnalysisError("daemon event timestamps must be non-negative")
            if (
                previous_timestamp is not None
                and event.monotonic_ns < previous_timestamp
            ):
                raise AnalysisError("daemon event timestamps must not regress")
            previous_timestamp = event.monotonic_ns
            event_ids.add(event.event_id)
            events.append(event)
    return tuple(events)


def _one_event(events: tuple[Event, ...], kind: str) -> Event:
    matches = [event for event in events if event.kind == kind]
    if len(matches) != 1:
        raise AnalysisError(f"trial requires exactly one daemon event {kind}")
    return matches[0]


def _validate_marker_identity(
    markers: Mapping[str, Event],
    *,
    trial: Mapping[str, Any],
) -> None:
    labels = trial.get("labels")
    if not isinstance(labels, Mapping):
        raise AnalysisError("trial labels are missing")
    expected = {
        "experiment_id": trial.get("experiment_id"),
        "trial_id": trial.get("trial_id"),
        "arm": labels.get("arm_code"),
        "target_nice": labels.get("target_nice"),
        "replay_seed": str(trial.get("replay_seed")),
        "mechanism_policy": labels.get("mechanism_policy"),
    }
    if any(not isinstance(value, str) or not value for value in expected.values()):
        raise AnalysisError("trial marker identity fields are incomplete")
    for kind, event in markers.items():
        for name, value in expected.items():
            if event.labels.get(name) != value:
                raise AnalysisError(
                    f"daemon marker {kind} does not match trial field {name}"
                )


def _validate_host_policy(
    trial: Mapping[str, Any],
    result: Mapping[str, Any],
    markers: Mapping[str, Event],
) -> None:
    labels = trial.get("labels")
    if not isinstance(labels, Mapping):
        raise AnalysisError("trial labels are missing")
    policy = labels.get("mechanism_policy")
    if policy not in {
        "NORMAL_DYNAMIC",
        "FIXED_NON_TURBO",
        "RESTRICTED_DEEP_IDLE",
    }:
        raise AnalysisError("trial mechanism policy is invalid")
    if result.get("mechanism_policy") != policy:
        raise AnalysisError(
            "acquisition mechanism policy disagrees with trial metadata"
        )
    evidence = result.get("host_policy_evidence")
    if evidence is None:
        if policy != "NORMAL_DYNAMIC":
            raise AnalysisError(
                "mechanism intervention has no verified host-policy evidence"
            )
        return
    if not isinstance(evidence, Mapping):
        raise AnalysisError("host-policy evidence must be an object or null")
    settings = evidence.get("settings")
    if (
        evidence.get("policy") != policy
        or evidence.get("verified") is not True
        or not isinstance(evidence.get("controller_id"), str)
        or not evidence.get("controller_id")
        or not isinstance(settings, Mapping)
        or not settings
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in settings.items()
        )
    ):
        raise AnalysisError("host-policy evidence is incomplete or unverified")
    encoded = json.dumps(
        dict(settings),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if evidence.get("settings_fingerprint_sha256") != fingerprint:
        raise AnalysisError("host-policy settings fingerprint is invalid")
    if labels.get("host_policy_settings_fingerprint_sha256") != fingerprint:
        raise AnalysisError("trial labels do not seal the host-policy fingerprint")
    if labels.get("host_policy_verification") != "VERIFIED":
        raise AnalysisError("trial labels do not record verified host policy")
    for kind, event in markers.items():
        if event.labels.get("host_policy_settings_fingerprint_sha256") != fingerprint:
            raise AnalysisError(
                f"daemon marker {kind} does not carry host-policy evidence"
            )
    if result.get("host_policy_restored") is not True:
        raise AnalysisError("host policy was not confirmed restored after capture")


def _validate_quality(
    quality: Mapping[str, Any],
    *,
    trial_id: str,
    boundary: MeasurementBoundary,
    descriptors: tuple[SourceDescriptor, ...],
    samples: tuple[RawSample, ...],
    raw_batches: tuple[RawSampleBatch, ...],
    start_monotonic_ns: int,
    end_monotonic_ns: int,
) -> None:
    if quality.get("trial_id") != trial_id:
        raise AnalysisError("quality metadata trial_id does not match the trial")
    if quality.get("disposition") != "ACCEPTED":
        raise AnalysisError("trial quality disposition is not ACCEPTED")
    if _batch_sequence_gaps(raw_batches):
        raise AnalysisError("accepted raw trace has missing sample batches")
    overhead = quality.get("measurement_overhead_ratio")
    if (
        isinstance(overhead, bool)
        or not isinstance(overhead, (int, float))
        or not math.isfinite(float(overhead))
        or not 0 <= float(overhead) < 0.01
    ):
        raise AnalysisError("accepted trial collection overhead must be below 1%")
    issues = quality.get("issues")
    if not isinstance(issues, list) or any(
        isinstance(issue, Mapping) and issue.get("severity") == "ERROR"
        for issue in issues
    ):
        raise AnalysisError("accepted trial quality contains invalid or error issues")
    summaries = quality.get("sources")
    if not isinstance(summaries, list):
        raise AnalysisError("quality source summaries must be a list")
    by_uid: dict[str, Mapping[str, Any]] = {}
    for item in summaries:
        if not isinstance(item, Mapping):
            raise AnalysisError("quality source summaries must be objects")
        uid = item.get("source_uid")
        if not isinstance(uid, str) or not uid or uid in by_uid:
            raise AnalysisError("quality source UIDs must be unique and non-empty")
        by_uid[uid] = item
    descriptors_by_uid = {
        descriptor.source_uid: descriptor for descriptor in descriptors
    }
    recomputed = {
        summary.source_uid: summary.to_quality_dict()
        for summary in summarize_source_quality(
            descriptors,
            samples,
            capture_start_monotonic_ns=start_monotonic_ns,
            capture_end_monotonic_ns=end_monotonic_ns,
            expected_sample_opportunities=_sample_opportunities(
                descriptors, raw_batches
            ),
        )
    }
    for uid in boundary.primary_source_uids:
        summary = by_uid.get(uid)
        descriptor = descriptors_by_uid[uid]
        if summary is None:
            raise AnalysisError(f"quality metadata omits primary source {uid!r}")
        source_samples = [
            sample
            for sample in samples
            if sample.source_id == descriptor.local_source_id
        ]
        observed = summary.get("observed_samples")
        gaps = summary.get("sequence_gaps")
        missing_ratio = summary.get("missing_sample_ratio")
        if observed != len(source_samples) or len(source_samples) < 2:
            raise AnalysisError(f"quality sample count disagrees for source {uid!r}")
        computed_gaps = sum(
            later.sequence_no - earlier.sequence_no - 1
            for earlier, later in zip(source_samples, source_samples[1:])
        )
        if gaps != computed_gaps:
            raise AnalysisError(
                f"quality sequence gap count disagrees for source {uid!r}"
            )
        expected_summary = recomputed[uid]
        expected_ratio = expected_summary["missing_sample_ratio"]
        if (
            not isinstance(missing_ratio, bool)
            and isinstance(missing_ratio, (int, float))
            and isinstance(expected_ratio, (int, float))
            and not isinstance(expected_ratio, bool)
            and not math.isclose(
                float(missing_ratio),
                float(expected_ratio),
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
        ):
            raise AnalysisError(
                f"quality missingness disagrees with raw timing for source {uid!r}"
            )
        if (
            summary.get("max_timestamp_jitter_ns")
            != expected_summary["max_timestamp_jitter_ns"]
        ):
            raise AnalysisError(
                f"quality timestamp jitter disagrees for source {uid!r}"
            )
        if (
            isinstance(missing_ratio, bool)
            or not isinstance(missing_ratio, (int, float))
            or not math.isfinite(float(missing_ratio))
            or not 0 <= float(missing_ratio) < 0.001
        ):
            raise AnalysisError(
                f"accepted primary source {uid!r} missingness must be below 0.1%"
            )


def _verified_workload_metrics(
    root: Path,
    trial: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    target_stdout_sha256: str,
) -> tuple[WorkloadMetrics | None, str | None]:
    """Reopen outcome evidence; never promote a caller callback to ground truth."""

    evidence = result.get("workload_metrics_evidence")
    if not isinstance(evidence, Mapping):
        raise AnalysisError("acquisition workload metrics evidence is missing")
    if set(evidence) != {
        "contract",
        "source",
        "relative_path",
        "sha256",
        "metrics",
    }:
        raise AnalysisError("workload metrics evidence has a non-canonical shape")
    if evidence.get("contract") != WORKLOAD_METRICS_CONTRACT:
        raise AnalysisError("workload metrics evidence contract is unsupported")
    try:
        source = WorkloadMetricsSource(str(evidence.get("source")))
    except ValueError as exc:
        raise AnalysisError("workload metrics evidence source is invalid") from exc

    if source is not WorkloadMetricsSource.WORKLOAD_SIDECAR:
        if (
            evidence.get("relative_path") is not None
            or evidence.get("sha256") is not None
        ):
            raise AnalysisError("non-sidecar workload metrics claim file provenance")
        if result.get("claim_eligibility") == "ELIGIBLE":
            raise AnalysisError(
                "confirmatory eligibility requires workload-written sidecar evidence"
            )
        return None, None

    labels = trial.get("labels")
    if (
        not isinstance(labels, Mapping)
        or labels.get("workload_metrics_contract") != WORKLOAD_METRICS_CONTRACT
        or labels.get("workload_metrics_path") != WORKLOAD_METRICS_FILENAME
    ):
        raise AnalysisError("trial does not seal the workload metrics contract")
    if evidence.get("relative_path") != WORKLOAD_METRICS_FILENAME:
        raise AnalysisError("workload metrics evidence path is not canonical")
    recorded_digest = evidence.get("sha256")
    if not isinstance(recorded_digest, str):
        raise AnalysisError("workload metrics evidence SHA-256 is missing")
    checksums = _read_json(root / "checksums.json")
    checksum_files = checksums.get("files")
    if (
        not isinstance(checksum_files, Mapping)
        or WORKLOAD_METRICS_FILENAME not in checksum_files
    ):
        raise AnalysisError("checksums omit the workload metrics sidecar")
    try:
        metrics, actual_digest = load_workload_metrics_sidecar(
            root / WORKLOAD_METRICS_FILENAME,
            expected_stdout_sha256=target_stdout_sha256,
        )
    except WorkloadMetricsError as exc:
        raise AnalysisError(str(exc)) from exc
    if actual_digest != recorded_digest:
        raise AnalysisError(
            "acquisition workload metrics digest does not match the sidecar"
        )
    if evidence.get("metrics") != metrics.to_dict():
        raise AnalysisError(
            "acquisition workload metrics fields do not match the sidecar"
        )
    for name, value in metrics.to_dict().items():
        if name == "output_sha256":
            continue
        if result.get(name) != value:
            raise AnalysisError(
                f"acquisition {name} does not match the workload metrics sidecar"
            )
    return metrics, actual_digest


def _verified_background_workload_metrics(
    root: Path,
    trial: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[WorkloadMetrics | None, str | None]:
    """Verify co-tenant progress so target-induced starvation cannot pass."""

    evidence = result.get("background_workload_metrics_evidence")
    if not isinstance(evidence, Mapping):
        raise AnalysisError("background workload metrics evidence is missing")
    if set(evidence) != {
        "contract",
        "source",
        "relative_path",
        "sha256",
        "metrics",
    }:
        raise AnalysisError(
            "background workload metrics evidence has a non-canonical shape"
        )
    if evidence.get("contract") != WORKLOAD_METRICS_CONTRACT:
        raise AnalysisError("background workload metrics contract is unsupported")
    try:
        source = WorkloadMetricsSource(str(evidence.get("source")))
    except ValueError as exc:
        raise AnalysisError("background workload metrics source is invalid") from exc
    execution_contract = result.get("execution_contract")
    has_background = (
        isinstance(execution_contract, Mapping)
        and execution_contract.get("background") is not None
    )
    if source is not WorkloadMetricsSource.WORKLOAD_SIDECAR:
        if (
            evidence.get("relative_path") is not None
            or evidence.get("sha256") is not None
        ):
            raise AnalysisError("unverified background metrics claim file provenance")
        if has_background and result.get("claim_eligibility") == "ELIGIBLE":
            raise AnalysisError(
                "co-tenanted eligibility requires background outcome evidence"
            )
        return None, None
    if not has_background:
        raise AnalysisError("an absent background cannot emit workload metrics")
    labels = trial.get("labels")
    if (
        not isinstance(labels, Mapping)
        or labels.get("background_workload_metrics_path")
        != BACKGROUND_WORKLOAD_METRICS_FILENAME
    ):
        raise AnalysisError("trial does not seal the background metrics path")
    if evidence.get("relative_path") != BACKGROUND_WORKLOAD_METRICS_FILENAME:
        raise AnalysisError("background workload metrics path is not canonical")
    recorded_stdout_digest = result.get("background_stdout_sha256")
    if not isinstance(recorded_stdout_digest, str):
        raise AnalysisError("background stdout digest is missing")
    checksums = _read_json(root / "checksums.json")
    checksum_files = checksums.get("files")
    if (
        not isinstance(checksum_files, Mapping)
        or BACKGROUND_WORKLOAD_METRICS_FILENAME not in checksum_files
    ):
        raise AnalysisError("checksums omit the background workload metrics sidecar")
    background_stdout_path = root / "background.stdout.log"
    if not background_stdout_path.is_file():
        raise AnalysisError("background stdout is missing")
    actual_stdout_digest = hashlib.sha256(
        background_stdout_path.read_bytes()
    ).hexdigest()
    if actual_stdout_digest != recorded_stdout_digest:
        raise AnalysisError("background stdout digest disagrees with acquisition")
    try:
        metrics, actual_digest = load_workload_metrics_sidecar(
            root / BACKGROUND_WORKLOAD_METRICS_FILENAME,
            expected_stdout_sha256=actual_stdout_digest,
        )
    except WorkloadMetricsError as exc:
        raise AnalysisError(str(exc)) from exc
    if evidence.get("sha256") != actual_digest:
        raise AnalysisError(
            "background workload metrics digest does not match the sidecar"
        )
    if evidence.get("metrics") != metrics.to_dict():
        raise AnalysisError(
            "background acquisition fields do not match its metrics sidecar"
        )
    return metrics, actual_digest


def _observation_flags(
    trial: Mapping[str, Any],
    acquisition: Mapping[str, Any],
    *,
    metrics: WorkloadMetrics | None,
    background_metrics: WorkloadMetrics | None,
    target_stdout_sha256: str,
) -> tuple[bool | None, bool | None, bool | None, float | None]:
    result = acquisition.get("result")
    if not isinstance(result, Mapping):
        return None, None, None, None
    completed = (
        acquisition.get("status") == "COMPLETED"
        and result.get("status") == "COMPLETED"
        and result.get("target_timed_out") is False
        and result.get("horizon_terminated") is False
    )
    estimand = trial.get("estimand")
    if not isinstance(estimand, Mapping):
        raise AnalysisError("trial estimand must be an object")
    labels = trial.get("labels")
    labels = labels if isinstance(labels, Mapping) else {}
    execution_contract = result.get("execution_contract")
    has_background = (
        isinstance(execution_contract, Mapping)
        and execution_contract.get("background") is not None
    )
    if not completed:
        recomputed_eligibility = "INELIGIBLE"
    elif (
        estimand.get("kind") == "FIXED_HORIZON"
        and result.get("fixed_horizon_within_tolerance") is not True
    ):
        recomputed_eligibility = "INELIGIBLE"
    elif (
        metrics is None
        or metrics.completed_work is None
        or metrics.throughput_per_s is None
        or metrics.output_sha256 is None
    ):
        recomputed_eligibility = "NOT_EVALUATED"
    elif metrics.arrival_trace_valid is False:
        recomputed_eligibility = "INELIGIBLE"
    elif any(
        value is not None
        for value in (
            metrics.arrival_trace_id,
            metrics.arrival_trace_sha256,
            metrics.arrival_trace_valid,
        )
    ) and (
        metrics.arrival_trace_id is None
        or metrics.arrival_trace_sha256 is None
        or metrics.arrival_trace_valid is not True
        or (
            labels.get("arrival_trace_id") is not None
            and metrics.arrival_trace_id != labels.get("arrival_trace_id")
        )
    ):
        recomputed_eligibility = "NOT_EVALUATED"
    elif has_background and (
        background_metrics is None
        or background_metrics.completed_work is None
        or background_metrics.throughput_per_s is None
        or background_metrics.backlog_at_end is None
        or background_metrics.slo_met is None
        or background_metrics.output_sha256 is None
    ):
        recomputed_eligibility = "NOT_EVALUATED"
    elif (
        has_background
        and background_metrics is not None
        and any(
            value is not None
            for value in (
                background_metrics.arrival_trace_id,
                background_metrics.arrival_trace_sha256,
                background_metrics.arrival_trace_valid,
            )
        )
        and (
            background_metrics.arrival_trace_id is None
            or background_metrics.arrival_trace_sha256 is None
            or background_metrics.arrival_trace_valid is not True
        )
    ):
        recomputed_eligibility = "NOT_EVALUATED"
    elif (
        has_background
        and background_metrics is not None
        and (
            background_metrics.backlog_at_end > 0
            or not background_metrics.slo_met
            or background_metrics.arrival_trace_valid is False
        )
    ):
        recomputed_eligibility = "INELIGIBLE"
    elif estimand.get("kind") == "EQUAL_WORK" and (
        isinstance(estimand.get("work_amount"), bool)
        or not isinstance(estimand.get("work_amount"), (int, float))
        or not math.isclose(
            metrics.completed_work,
            float(estimand["work_amount"]),
            rel_tol=1e-9,
            abs_tol=1e-12,
        )
        or metrics.work_unit != estimand.get("work_unit")
    ):
        recomputed_eligibility = "INELIGIBLE"
    elif labels.get("workload_mode") == "SERVICE" and (
        metrics.latency_ms is None
        or metrics.backlog_at_end is None
        or metrics.slo_met is None
        or metrics.arrival_trace_id != labels.get("arrival_trace_id")
        or metrics.arrival_trace_sha256 is None
        or metrics.arrival_trace_valid is not True
    ):
        recomputed_eligibility = "NOT_EVALUATED"
    elif labels.get("workload_mode") == "SERVICE" and (
        metrics.backlog_at_end > 0 or not metrics.slo_met
    ):
        recomputed_eligibility = "INELIGIBLE"
    elif labels.get("output_equivalence") != "VERIFIED_BY_DIGEST":
        recomputed_eligibility = "NOT_EVALUATED"
    else:
        recomputed_eligibility = "ELIGIBLE"
    if result.get("claim_eligibility") != recomputed_eligibility:
        raise AnalysisError(
            "acquisition claim eligibility disagrees with workload evidence"
        )
    completed_work_value = None if metrics is None else metrics.completed_work

    if estimand.get("kind") == "EQUAL_WORK":
        target = estimand.get("work_amount")
        unit = estimand.get("work_unit")
        work_complete = (
            completed
            and recomputed_eligibility == "ELIGIBLE"
            and isinstance(target, (int, float))
            and not isinstance(target, bool)
            and completed_work_value is not None
            and math.isclose(
                completed_work_value, float(target), rel_tol=1e-9, abs_tol=1e-12
            )
            and metrics is not None
            and metrics.work_unit == unit
        )
    else:
        work_complete = (
            completed
            and recomputed_eligibility == "ELIGIBLE"
            and metrics is not None
            and completed_work_value is not None
            and metrics.throughput_per_s is not None
            and result.get("fixed_horizon_within_tolerance") is True
        )

    arm = trial.get("arm")
    arm_kind = arm.get("kind") if isinstance(arm, Mapping) else None
    recorded_stdout_digest = result.get("target_stdout_sha256")
    if recorded_stdout_digest != target_stdout_sha256:
        raise AnalysisError(
            "acquisition stdout digest does not match the sealed target output"
        )
    if arm_kind == "BACKGROUND_CONTROL":
        output_valid: bool | None = completed
    elif arm_kind == "PROGRAM":
        expected = labels.get("expected_output_sha256")
        actual = result.get("target_stdout_sha256")
        if not isinstance(expected, str):
            output_valid = None
        else:
            output_valid = (
                labels.get("output_equivalence") == "VERIFIED_BY_DIGEST"
                and actual == expected
                and target_stdout_sha256 == expected
            )
    else:
        raise AnalysisError("trial arm kind is invalid")

    workload_mode = labels.get("workload_mode")
    if workload_mode == "BATCH":
        slo_met: bool | None = True
    elif workload_mode == "SERVICE":
        explicit_slo = None if metrics is None else metrics.slo_met
        backlog = None if metrics is None else metrics.backlog_at_end
        latency = None if metrics is None else metrics.latency_ms
        arrival_trace_id = None if metrics is None else metrics.arrival_trace_id
        arrival_trace_sha256 = None if metrics is None else metrics.arrival_trace_sha256
        arrival_trace_valid = None if metrics is None else metrics.arrival_trace_valid
        expected_arrival_trace_id = labels.get("arrival_trace_id")
        if not isinstance(explicit_slo, bool):
            slo_met = None
        else:
            slo_met = (
                explicit_slo
                and isinstance(backlog, (int, float))
                and not isinstance(backlog, bool)
                and float(backlog) == 0
                and isinstance(latency, Mapping)
                and bool(latency)
                and arrival_trace_id == expected_arrival_trace_id
                and isinstance(arrival_trace_sha256, str)
                and len(arrival_trace_sha256) == 64
                and arrival_trace_valid is True
            )
    else:
        slo_met = None if metrics is None else metrics.slo_met
        if not isinstance(slo_met, bool):
            slo_met = None
    return work_complete, output_valid, slo_met, completed_work_value


def _validate_process_priorities(
    trial: Mapping[str, Any],
    result: Mapping[str, Any],
    markers: Mapping[str, Event],
) -> None:
    labels = trial.get("labels")
    if not isinstance(labels, Mapping):
        raise AnalysisError("trial labels are missing")
    try:
        expected_target = int(str(labels["target_nice"]))
        expected_background = int(str(labels["background_nice"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisError("sealed process niceness is invalid") from exc
    actual_target = result.get("actual_target_nice")
    if actual_target != expected_target:
        raise AnalysisError("target nice read-back differs from the schedule")
    target_exec_labels = markers["TARGET_EXEC"].labels
    if target_exec_labels.get("actual_target_nice") != str(expected_target):
        raise AnalysisError("TARGET_EXEC does not seal target nice read-back")
    execution_contract = result.get("execution_contract")
    has_background = (
        isinstance(execution_contract, Mapping)
        and execution_contract.get("background") is not None
    )
    actual_background = result.get("actual_background_nice")
    if has_background:
        if actual_background != expected_background:
            raise AnalysisError("background nice read-back differs from the schedule")
        if target_exec_labels.get("actual_background_nice") != str(expected_background):
            raise AnalysisError("TARGET_EXEC does not seal background nice read-back")
        if markers["BACKGROUND_COMPLETION"].labels.get("actual_background_nice") != str(
            expected_background
        ):
            raise AnalysisError(
                "BACKGROUND_COMPLETION does not seal background nice read-back"
            )
    elif actual_background is not None:
        raise AnalysisError("absent background has a nice read-back")


def _validate_interval_post_window_support(
    *,
    result: Mapping[str, Any],
    trial: Mapping[str, Any],
    boundary: MeasurementBoundary,
    descriptors: tuple[SourceDescriptor, ...],
    samples: tuple[RawSample, ...],
    recovery_end_ns: int,
) -> None:
    """Require enough raw tail evidence to cover an affine-mapped interval."""

    selected = boundary.validate_descriptors(descriptors)
    interval_sources = tuple(
        source for source in selected if source.temporality == "INTERVAL_AVERAGE"
    )
    if not interval_sources:
        return
    bracket = result.get("primary_sample_bracket")
    if not isinstance(bracket, Mapping):
        raise AnalysisError(
            "interval-average trial lacks a sealed primary sample bracket"
        )
    labels = trial.get("labels")
    if not isinstance(labels, Mapping):
        raise AnalysisError("trial labels are missing")
    try:
        fallback_margin_ns = int(str(labels["interval_ms"])) * 1_000_000
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisError("trial interval does not define a tail margin") from exc
    if fallback_margin_ns <= 0:
        raise AnalysisError("trial interval tail margin must be positive")

    for source in interval_sources:
        entry = bracket.get(source.source_uid)
        if not isinstance(entry, Mapping):
            raise AnalysisError(
                f"interval-average source {source.source_uid!r} lacks tail evidence"
            )
        margin_ns = (
            source.nominal_period_ns
            if isinstance(source.nominal_period_ns, int)
            and not isinstance(source.nominal_period_ns, bool)
            and source.nominal_period_ns > 0
            else fallback_margin_ns
        )
        expected_fields = {
            "post_window_sample_ns": entry.get("post_window_sample_ns"),
            "post_window_samples_at_or_after": entry.get(
                "post_window_samples_at_or_after"
            ),
            "post_window_margin_ns": entry.get("post_window_margin_ns"),
            "recovery_end_ns": entry.get("recovery_end_ns"),
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in expected_fields.values()
        ):
            raise AnalysisError(
                f"interval-average source {source.source_uid!r} has malformed tail evidence"
            )
        if (
            expected_fields["post_window_margin_ns"] != margin_ns
            or expected_fields["recovery_end_ns"] != recovery_end_ns
        ):
            raise AnalysisError(
                f"interval-average source {source.source_uid!r} tail contract changed"
            )
        qualifying = sorted(
            sample.monotonic_ns
            for sample in samples
            if sample.source_id == source.local_source_id
            and sample.monotonic_ns >= recovery_end_ns
        )
        recorded_count = expected_fields["post_window_samples_at_or_after"]
        recorded_post_ns = expected_fields["post_window_sample_ns"]
        if (
            recorded_count not in (1, 2)
            or len(qualifying) < recorded_count
            or recorded_post_ns not in qualifying
        ):
            raise AnalysisError(
                f"interval-average source {source.source_uid!r} tail evidence "
                "does not match the raw trace"
            )
        if not (
            len(qualifying) >= 2
            or qualifying[-1] >= recovery_end_ns + margin_ns
        ):
            raise AnalysisError(
                f"interval-average source {source.source_uid!r} lacks a complete "
                "post-window native generation"
            )


def load_trial_evidence(trial_dir: str | Path) -> TrialEvidence:
    """Load one sealed, quality-accepted trial into offline analysis.

    The energy window is always resolved from the daemon-stamped
    ``TARGET_SPAWN`` and ``RECOVERY_END`` events.  Client wall-clock durations
    are never used as the outcome boundary.
    """

    root = Path(trial_dir)
    missing = sorted(name for name in _REQUIRED_FILES if not (root / name).is_file())
    if missing:
        raise AnalysisError("trial artifact is missing files: " + ", ".join(missing))
    _verify_checksums(root)
    trial = _read_json(root / "trial.json")
    manifest = _read_json(root / "manifest.json")
    quality = _read_json(root / "quality.json")
    acquisition = _read_json(root / "acquisition.json")
    source_documents = _read_json(root / "sources.json", object_required=False)
    if not isinstance(source_documents, list):
        raise AnalysisError("sources.json must contain an array")
    if manifest.get("sources") != source_documents:
        raise AnalysisError("manifest sources do not exactly match sources.json")
    try:
        descriptors = tuple(
            SourceDescriptor.from_dict(dict(document))
            for document in source_documents
            if isinstance(document, Mapping)
        )
    except Exception as exc:
        raise AnalysisError(f"invalid source descriptor: {exc}") from exc
    if len(descriptors) != len(source_documents):
        raise AnalysisError("sources.json contains a non-object descriptor")
    local_ids = [descriptor.local_source_id for descriptor in descriptors]
    source_uids = [descriptor.source_uid for descriptor in descriptors]
    if len(local_ids) != len(set(local_ids)) or len(source_uids) != len(
        set(source_uids)
    ):
        raise AnalysisError("source descriptors must have unique local IDs and UIDs")

    trial_id = trial.get("trial_id")
    run_id = trial.get("run_id")
    node_id = manifest.get("node_id")
    clock_id = manifest.get("clock_instance_id")
    if (
        not isinstance(trial_id, str)
        or not trial_id
        or root.name != trial_id
        or not isinstance(run_id, str)
        or not run_id
        or manifest.get("run_id") != run_id
        or not isinstance(node_id, str)
        or not node_id
        or not isinstance(clock_id, str)
        or not clock_id
    ):
        raise AnalysisError("trial, manifest, run, node, or clock identity is invalid")
    labels = manifest.get("labels")
    if not isinstance(labels, Mapping) or labels.get("trial_id") != trial_id:
        raise AnalysisError("manifest labels do not identify the trial")
    if labels.get("wattch.clock_instance_id") != clock_id:
        raise AnalysisError("manifest clock label does not match clock_instance_id")

    boundary_document = trial.get("measurement_boundary")
    if not isinstance(boundary_document, Mapping):
        raise AnalysisError("trial measurement_boundary must be an object")
    boundary = MeasurementBoundary.from_dict(boundary_document)
    boundary.validate_descriptors(descriptors)

    raw_batches = _read_trace(root / "raw.trace.wch", FrameKind.RAW_SAMPLE_BATCH)
    event_batches = _read_trace(root / "events.trace.wch", FrameKind.EVENT_BATCH)
    if not all(isinstance(batch, RawSampleBatch) for batch in raw_batches):
        raise AnalysisError("raw trace decoded an unexpected message")
    if not all(isinstance(batch, EventBatch) for batch in event_batches):
        raise AnalysisError("event trace decoded an unexpected message")
    samples = _validate_raw_batches(
        raw_batches,
        run_id=run_id,
        node_id=node_id,
        source_ids=set(local_ids),
    )
    events = _validate_event_batches(event_batches, run_id=run_id, node_id=node_id)

    integrity = acquisition.get("integrity")
    if not isinstance(integrity, Mapping):
        raise AnalysisError("acquisition integrity summary is missing")
    expected_integrity = {
        "raw_batches": len(raw_batches),
        "raw_samples": len(samples),
        "event_batches": len(event_batches),
        "events": len(events),
    }
    if any(integrity.get(name) != value for name, value in expected_integrity.items()):
        raise AnalysisError("acquisition integrity counts disagree with trace contents")

    markers = {kind: _one_event(events, kind) for kind in _REQUIRED_MARKERS}
    _validate_marker_identity(markers, trial=trial)
    result = acquisition.get("result")
    if not isinstance(result, Mapping):
        raise AnalysisError("acquisition result is missing")
    if result.get("host_provenance") is not None:
        try:
            validate_host_provenance(result.get("host_provenance"))
        except ValueError as exc:
            raise AnalysisError(str(exc)) from exc
    start = markers["TARGET_SPAWN"]
    end = markers["RECOVERY_END"]
    if end.monotonic_ns <= start.monotonic_ns:
        raise AnalysisError("recovery end must follow target spawn")
    common_order = (
        markers["PRE_WINDOW_START"].monotonic_ns
        <= markers["BACKGROUND_ARRIVAL"].monotonic_ns
        <= start.monotonic_ns
        <= markers["TARGET_EXEC"].monotonic_ns
    )
    target_exit_ns = markers["TARGET_EXIT"].monotonic_ns
    if result.get("horizon_terminated") is True:
        lifecycle_ordered = (
            common_order
            and markers["TARGET_EXEC"].monotonic_ns
            <= end.monotonic_ns
            <= target_exit_ns
        )
    else:
        lifecycle_ordered = (
            common_order
            and markers["TARGET_EXEC"].monotonic_ns
            <= target_exit_ns
            <= end.monotonic_ns
        )
    if not lifecycle_ordered:
        raise AnalysisError("daemon lifecycle markers are not causally ordered")
    _validate_interval_post_window_support(
        result=result,
        trial=trial,
        boundary=boundary,
        descriptors=descriptors,
        samples=samples,
        recovery_end_ns=end.monotonic_ns,
    )
    _validate_quality(
        quality,
        trial_id=trial_id,
        boundary=boundary,
        descriptors=descriptors,
        samples=samples,
        raw_batches=raw_batches,
        start_monotonic_ns=start.monotonic_ns,
        end_monotonic_ns=end.monotonic_ns,
    )
    _validate_host_policy(trial, result, markers)
    _validate_process_priorities(trial, result, markers)
    marker_event_ids = result.get("marker_event_ids")
    if not isinstance(marker_event_ids, list) or marker_event_ids != [
        event.event_id for event in events
    ]:
        raise AnalysisError("acquisition marker IDs disagree with the event trace")
    estimand_document = trial.get("estimand")
    if not isinstance(estimand_document, Mapping):
        raise AnalysisError("trial estimand must be an object")
    try:
        estimand = Estimand(str(estimand_document["kind"]))
    except (KeyError, ValueError) as exc:
        raise AnalysisError("trial estimand kind is unsupported") from exc
    background_completion_ns = markers["BACKGROUND_COMPLETION"].monotonic_ns
    if estimand is Estimand.FIXED_HORIZON:
        if background_completion_ns < end.monotonic_ns:
            raise AnalysisError(
                "fixed-horizon background completion must follow recovery end"
            )
    elif not target_exit_ns <= background_completion_ns <= end.monotonic_ns:
        raise AnalysisError(
            "equal-work background completion must occur between target exit "
            "and recovery end"
        )
    if estimand is Estimand.FIXED_HORIZON:
        horizon_ns = estimand_document.get("horizon_ns")
        observed_ns = end.monotonic_ns - start.monotonic_ns
        trial_labels = trial.get("labels")
        if not isinstance(trial_labels, Mapping):
            raise AnalysisError("trial labels are missing")
        trial_tolerance_label = trial_labels.get("fixed_horizon_tolerance_ns")
        manifest_tolerance_label = labels.get("fixed_horizon_tolerance_ns")
        if not isinstance(trial_tolerance_label, str) or not isinstance(
            manifest_tolerance_label, str
        ):
            raise AnalysisError("fixed-horizon tolerance declaration is invalid")
        try:
            tolerance_ns = int(trial_tolerance_label)
            manifest_tolerance_ns = int(manifest_tolerance_label)
        except (TypeError, ValueError) as exc:
            raise AnalysisError(
                "fixed-horizon tolerance declaration is invalid"
            ) from exc
        result_tolerance_ns = result.get("fixed_horizon_tolerance_ns")
        if (
            isinstance(horizon_ns, bool)
            or not isinstance(horizon_ns, int)
            or horizon_ns <= 0
            or trial_tolerance_label != str(tolerance_ns)
            or manifest_tolerance_label != str(manifest_tolerance_ns)
            or tolerance_ns <= 0
            or tolerance_ns >= horizon_ns
            or manifest_tolerance_ns != tolerance_ns
            or isinstance(result_tolerance_ns, bool)
            or not isinstance(result_tolerance_ns, int)
            or result.get("fixed_horizon_target_ns") != horizon_ns
            or result_tolerance_ns != tolerance_ns
            or result.get("fixed_horizon_observed_ns") != observed_ns
            or result.get("fixed_horizon_error_ns") != observed_ns - horizon_ns
        ):
            raise AnalysisError(
                "fixed-horizon acquisition fields or sealed tolerance disagree"
            )
        expected_within_tolerance = abs(observed_ns - horizon_ns) <= tolerance_ns
        if (
            result.get("fixed_horizon_within_tolerance")
            is not expected_within_tolerance
        ):
            raise AnalysisError(
                "fixed-horizon tolerance result disagrees with daemon events"
            )

    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=descriptors,
        samples=samples,
        start_monotonic_ns=start.monotonic_ns,
        end_monotonic_ns=end.monotonic_ns,
        estimand=estimand,
    )
    target_stdout = (root / "target.stdout.log").read_bytes()
    target_stdout_sha256 = hashlib.sha256(target_stdout).hexdigest()
    workload_metrics, workload_metrics_sha256 = _verified_workload_metrics(
        root,
        trial,
        result,
        target_stdout_sha256=target_stdout_sha256,
    )
    (
        background_workload_metrics,
        background_workload_metrics_sha256,
    ) = _verified_background_workload_metrics(root, trial, result)
    work_complete, output_valid, slo_met, completed_work = _observation_flags(
        trial,
        acquisition,
        metrics=workload_metrics,
        background_metrics=background_workload_metrics,
        target_stdout_sha256=target_stdout_sha256,
    )
    trial_labels = trial.get("labels")
    trial_labels = trial_labels if isinstance(trial_labels, Mapping) else {}
    arm = trial_labels.get("arm_code")
    block_id = trial.get("matched_block_id")
    context = trial.get("background_workload_id")
    if not all(isinstance(value, str) and value for value in (arm, block_id, context)):
        raise AnalysisError(
            "trial arm, matched block, or background context is invalid"
        )
    policy = trial_labels.get("mechanism_policy")
    if not isinstance(policy, str) or not policy:
        target_nice = trial_labels.get("target_nice")
        policy = (
            f"nice:{target_nice}" if isinstance(target_nice, str) else "UNSPECIFIED"
        )
    target_nice_label = trial_labels.get("target_nice")
    try:
        target_nice = int(target_nice_label)
    except (TypeError, ValueError) as exc:
        raise AnalysisError("trial target_nice label is invalid") from exc
    if not 0 <= target_nice <= 19:
        raise AnalysisError("trial target_nice label is outside 0 through 19")
    observation = TrialObservation(
        trial_id=trial_id,
        block_id=block_id,
        arm=arm,
        context=context,
        outcome=outcome,
        work_complete=work_complete,
        output_valid=output_valid,
        slo_met=slo_met,
        quality_accepted=True,
        completed_work=completed_work,
        policy=policy,
        target_nice=target_nice,
        match_fingerprint=trial_match_fingerprint(trial),
    )
    return TrialEvidence(
        trial=trial,
        manifest=manifest,
        quality=quality,
        acquisition=acquisition,
        descriptors=descriptors,
        samples=samples,
        events=events,
        workload_metrics=workload_metrics,
        workload_metrics_sha256=workload_metrics_sha256,
        background_workload_metrics=background_workload_metrics,
        background_workload_metrics_sha256=(background_workload_metrics_sha256),
        outcome=outcome,
        observation=observation,
    )


__all__ = [
    "TrialEvidence",
    "evaluate_trial_quality",
    "load_trial_evidence",
    "trial_match_fingerprint",
]
