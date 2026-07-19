"""Immutable raw-evidence artifacts for matched Wattch trials."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .._frames import FrameKind, encode_frame
from .._messages import RawSampleBatch, SourceDescriptor
from .config import FixedHorizon, MeasurementBoundary
from .schedule import ExperimentSchedule, TrialSpec

EXPERIMENT_FILE = "experiment.json"
SCHEDULE_FILE = "schedule.json"
TRIAL_FILE = "trial.json"
MANIFEST_FILE = "manifest.json"
SOURCES_FILE = "sources.json"
RAW_TRACE_FILE = "raw.trace.wch"
EVENTS_TRACE_FILE = "events.trace.wch"
QUALITY_FILE = "quality.json"
CHECKSUMS_FILE = "checksums.json"
ACQUISITION_FILE = "acquisition.json"


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _compact_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _exclusive_write(path: Path, contents: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("xb") as handle:
        handle.write(_json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _calculate_checksums(root: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == CHECKSUMS_FILE:
            continue
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        files[relative] = {"sha256": digest.hexdigest(), "bytes": size}
    return files


def _canonical_quality_document(
    value: Mapping[str, Any], *, expected_trial_id: str
) -> dict[str, Any]:
    canonical_fields = {
        "trial_id",
        "disposition",
        "measurement_overhead_ratio",
        "sources",
        "issues",
    }
    if set(value) != canonical_fields:
        raise ValueError(
            "quality metadata must contain exactly the canonical "
            f"TrialQualityMetadata fields {sorted(canonical_fields)}"
        )
    document = dict(value)
    if document["trial_id"] != expected_trial_id:
        raise ValueError("quality metadata trial_id does not match the trial")
    if document["disposition"] not in {
        "ACCEPTED",
        "REVIEW_REQUIRED",
        "REJECTED",
    }:
        raise ValueError("quality disposition is not canonical")
    overhead = document["measurement_overhead_ratio"]
    if overhead is not None and (
        isinstance(overhead, bool)
        or not isinstance(overhead, (int, float))
        or not math.isfinite(float(overhead))
        or float(overhead) < 0
    ):
        raise ValueError("measurement_overhead_ratio must be finite and non-negative")

    sources = document["sources"]
    if not isinstance(sources, list):
        raise ValueError("quality sources must be a list")
    seen_uids: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError("quality sources must contain objects")
        required = {
            "source_uid",
            "observed_samples",
            "sequence_gaps",
            "missing_sample_ratio",
            "max_timestamp_jitter_ns",
        }
        if not required.issubset(source):
            raise ValueError("quality source summary is missing canonical fields")
        source_uid = source["source_uid"]
        if not isinstance(source_uid, str) or not source_uid.strip():
            raise ValueError("quality source_uid must be a non-empty string")
        if source_uid in seen_uids:
            raise ValueError("quality source_uids must be unique")
        seen_uids.add(source_uid)
        for name in ("observed_samples", "sequence_gaps"):
            item = source[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"quality {name} must be a non-negative integer")
        missing_ratio = source["missing_sample_ratio"]
        if (
            isinstance(missing_ratio, bool)
            or not isinstance(missing_ratio, (int, float))
            or not math.isfinite(float(missing_ratio))
            or not 0 <= float(missing_ratio) <= 1
        ):
            raise ValueError("quality missing_sample_ratio must be between zero and one")
        jitter = source["max_timestamp_jitter_ns"]
        if jitter is not None and (
            isinstance(jitter, bool) or not isinstance(jitter, int) or jitter < 0
        ):
            raise ValueError(
                "quality max_timestamp_jitter_ns must be a non-negative integer or null"
            )

    issues = document["issues"]
    if not isinstance(issues, list):
        raise ValueError("quality issues must be a list")
    for issue in issues:
        if not isinstance(issue, Mapping) or not {
            "code",
            "severity",
            "message",
            "source_uid",
        }.issubset(issue):
            raise ValueError("quality issues must contain canonical fields")
        if issue["severity"] not in {"WARNING", "ERROR"}:
            raise ValueError("quality issue severity is not canonical")
        if not isinstance(issue["code"], str) or not issue["code"]:
            raise ValueError("quality issue code must be a non-empty string")
        if not isinstance(issue["message"], str) or not issue["message"]:
            raise ValueError("quality issue message must be a non-empty string")
        if issue["source_uid"] is not None and not isinstance(
            issue["source_uid"], str
        ):
            raise ValueError("quality issue source_uid must be a string or null")
    return document


def _as_dict(value: object, field_name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    raise TypeError(f"{field_name} must be a mapping or provide to_dict()")


def _event_dict(value: object) -> dict[str, Any]:
    event = _as_dict(value, "event")
    required = {"event_id", "run_id", "node_id", "monotonic_ns", "kind", "labels"}
    missing = required - event.keys()
    if missing:
        raise ValueError(f"event is missing fields {sorted(missing)}")
    if not isinstance(event["event_id"], str) or not event["event_id"]:
        raise ValueError("event_id must be a non-empty string")
    return event


def prepare_experiment_layout(
    output_root: str | Path, schedule: ExperimentSchedule
) -> Path:
    """Create the experiment-level metadata and empty trial/analysis roots."""

    experiment_dir = Path(output_root) / schedule.experiment.experiment_id
    experiment_dir.mkdir(parents=True, exist_ok=False)
    _exclusive_write(
        experiment_dir / EXPERIMENT_FILE,
        _json_bytes(schedule.experiment.to_dict()),
    )
    _exclusive_write(experiment_dir / SCHEDULE_FILE, _json_bytes(schedule.to_dict()))
    (experiment_dir / "trials").mkdir()
    # Derived data belongs here and is intentionally not fabricated by the
    # acquisition runner.  Offline analysis creates files only when available.
    (experiment_dir / "analysis").mkdir()
    return experiment_dir


class TrialArtifactWriter:
    """Streaming writer for one trial's raw samples and daemon-stamped events."""

    def __init__(
        self,
        experiment_dir: str | Path,
        trial: TrialSpec,
        boundary: MeasurementBoundary,
        sources: Sequence[SourceDescriptor],
        *,
        node_id: str,
        protocol_version: str,
        clock_instance_id: str,
        wattch_version: str = "pywattch/0.1.0",
        created_unix_ns: int | None = None,
    ):
        if not node_id.strip():
            raise ValueError("node_id must not be empty")
        if not protocol_version.strip():
            raise ValueError("protocol_version must not be empty")
        if not clock_instance_id.strip():
            raise ValueError("clock_instance_id must not be empty")
        if not wattch_version.strip():
            raise ValueError("wattch_version must not be empty")
        if created_unix_ns is None:
            created_unix_ns = time.time_ns()
        if (
            isinstance(created_unix_ns, bool)
            or not isinstance(created_unix_ns, int)
            or created_unix_ns < 0
        ):
            raise ValueError("created_unix_ns must be a non-negative integer")
        selected = boundary.validate_descriptors(sources)

        self.trial = trial
        self.boundary = boundary
        self.sources = tuple(sources)
        self.selected_sources = selected
        self.node_id = node_id
        self.protocol_version = protocol_version
        self.clock_instance_id = clock_instance_id
        self.trial_dir = Path(experiment_dir) / "trials" / trial.trial_id
        self.trial_dir.mkdir(parents=False, exist_ok=False)

        # This file is exactly wattch_core::research::TrialMetadata.  Runtime
        # status belongs in acquisition.json so Rust can deserialize it without
        # depending on Python-specific fields.
        self._trial_document = trial.to_dict()
        _exclusive_write(self.trial_dir / TRIAL_FILE, _json_bytes(self._trial_document))

        source_documents = [source.to_dict() for source in self.sources]
        manifest_labels = {
            "experiment_id": trial.experiment_id,
            "trial_id": trial.trial_id,
            "interval_ms": str(trial.interval_ms),
            "measurement_boundary_id": boundary.boundary_id,
            "wattch.clock_instance_id": clock_instance_id,
        }
        if isinstance(trial.observation, FixedHorizon):
            manifest_labels["fixed_horizon_tolerance_ns"] = str(
                round(trial.observation.tolerance_s * 1_000_000_000)
            )
        manifest = {
            "run_id": trial.run_id,
            "node_id": node_id,
            "protocol_version": protocol_version,
            "wattch_version": wattch_version,
            "created_unix_ns": created_unix_ns,
            "clock_instance_id": clock_instance_id,
            "sources": source_documents,
            "labels": manifest_labels,
        }
        _exclusive_write(self.trial_dir / MANIFEST_FILE, _json_bytes(manifest))
        _exclusive_write(self.trial_dir / SOURCES_FILE, _json_bytes(source_documents))

        quality = {
            "trial_id": trial.trial_id,
            "disposition": "REVIEW_REQUIRED",
            "measurement_overhead_ratio": None,
            "sources": [],
            "issues": [
                {
                    "code": "NOT_EVALUATED",
                    "severity": "WARNING",
                    "message": "raw evidence quality has not been evaluated offline",
                    "source_uid": None,
                }
            ],
        }
        _exclusive_write(self.trial_dir / QUALITY_FILE, _json_bytes(quality))
        self._quality = quality

        self._raw_handle = (self.trial_dir / RAW_TRACE_FILE).open("xb")
        self._events_handle = (self.trial_dir / EVENTS_TRACE_FILE).open("xb")
        self._raw_batches = 0
        self._raw_samples = 0
        self._event_batches = 0
        self._events = 0
        self._event_ids: set[str] = set()
        self._fallback_event_batch_sequence = 0
        self._finished = False
        self._lock = threading.RLock()

    def append_raw_batch(self, batch: RawSampleBatch) -> None:
        with self._lock:
            self._require_open()
            if batch.run_id != self.trial.run_id:
                raise ValueError(
                    f"raw batch run_id {batch.run_id!r} does not match {self.trial.run_id!r}"
                )
            if batch.node_id != self.node_id:
                raise ValueError(
                    f"raw batch node_id {batch.node_id!r} does not match {self.node_id!r}"
                )
            payload = _compact_json_bytes(batch.to_dict())
            self._raw_handle.write(encode_frame(FrameKind.RAW_SAMPLE_BATCH, payload))
            self._raw_batches += 1
            self._raw_samples += len(batch.samples)

    def append_event_batch(self, batch: object) -> None:
        """Append one daemon EventBatch mapping/message without interpretation."""

        with self._lock:
            self._require_open()
            document = _as_dict(batch, "event batch")
            if document.get("run_id") != self.trial.run_id:
                raise ValueError("event batch run_id does not match the trial")
            if document.get("node_id") != self.node_id:
                raise ValueError("event batch node_id does not match the manifest")
            raw_events = document.get("events")
            if not isinstance(raw_events, (list, tuple)):
                raise ValueError("event batch events must be a list or tuple")
            events = [_event_dict(event) for event in raw_events]
            duplicates = [
                event["event_id"]
                for event in events
                if event["event_id"] in self._event_ids
            ]
            if duplicates:
                # The v0.2 contract emits EventBatch before RecordEventAck.  A
                # session adapter may therefore deliver the same authoritative
                # event through both paths; never duplicate it in the artifact.
                if len(duplicates) == len(events):
                    return
                raise ValueError("event batch mixes duplicate and new event_ids")
            self._events_handle.write(
                encode_frame(FrameKind.EVENT_BATCH, _compact_json_bytes(document))
            )
            self._event_ids.update(event["event_id"] for event in events)
            self._event_batches += 1
            self._events += len(events)
            sequence = document.get("batch_sequence_no")
            if (
                isinstance(sequence, int)
                and sequence >= self._fallback_event_batch_sequence
            ):
                self._fallback_event_batch_sequence = sequence + 1

    def append_daemon_event(self, event: object) -> None:
        """Persist an ack's daemon-stamped event if no EventBatch callback did."""

        with self._lock:
            document = _event_dict(event)
            if document["event_id"] in self._event_ids:
                return
            if document["run_id"] != self.trial.run_id:
                raise ValueError("event run_id does not match the trial")
            if document["node_id"] != self.node_id:
                raise ValueError("event node_id does not match the manifest")
            batch = {
                "run_id": self.trial.run_id,
                "node_id": self.node_id,
                "batch_sequence_no": self._fallback_event_batch_sequence,
                "events": [document],
            }
            self.append_event_batch(batch)

    def finish(
        self,
        *,
        result: Mapping[str, Any] | None = None,
        status: str = "COMPLETED",
    ) -> Path:
        """Seal traces and bind provisional raw evidence with SHA-256.

        Quality is deliberately not accepted from a daemon capture response.
        It can only be computed offline and applied with
        :func:`finalize_trial_quality` after this initial seal verifies.
        """

        self._require_open()
        if not status.strip():
            raise ValueError("status must not be empty")
        self._flush_and_close_traces()

        acquisition = {
            "schema_version": 1,
            "status": status,
            "result": None if result is None else dict(result),
            "calibration_thresholds": {
                "max_collection_overhead_fraction": 0.01,
                "max_missing_sample_rate": 0.001,
            },
            "integrity": {
                "raw_batches": self._raw_batches,
                "raw_samples": self._raw_samples,
                "event_batches": self._event_batches,
                "events": self._events,
            },
        }
        _exclusive_write(self.trial_dir / ACQUISITION_FILE, _json_bytes(acquisition))

        checksum_document = {
            "schema_version": 1,
            "algorithm": "sha256",
            "files": self._checksums(),
        }
        _exclusive_write(
            self.trial_dir / CHECKSUMS_FILE,
            _json_bytes(checksum_document),
        )
        self._finished = True
        return self.trial_dir

    def abort(self, error: BaseException) -> Path:
        """Seal partial evidence and record a failed acquisition outcome."""

        return self.finish(
            result={"error_type": type(error).__name__, "error": str(error)},
            status="FAILED",
        )

    def _checksums(self) -> dict[str, dict[str, Any]]:
        return _calculate_checksums(self.trial_dir)

    def _flush_and_close_traces(self) -> None:
        for handle in (self._raw_handle, self._events_handle):
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()

    def _require_open(self) -> None:
        if self._finished or self._raw_handle.closed or self._events_handle.closed:
            raise RuntimeError("trial artifact writer is already finished")


def verify_checksums(trial_dir: str | Path) -> list[str]:
    """Return relative paths whose size or SHA-256 no longer matches."""

    root = Path(trial_dir)
    document = json.loads((root / CHECKSUMS_FILE).read_text(encoding="utf-8"))
    failures: list[str] = []
    for relative, expected in document["files"].items():
        path = root / relative
        if not path.is_file():
            failures.append(relative)
            continue
        contents = path.read_bytes()
        if len(contents) != expected["bytes"]:
            failures.append(relative)
            continue
        if hashlib.sha256(contents).hexdigest() != expected["sha256"]:
            failures.append(relative)
    return failures


def finalize_trial_quality(
    trial_dir: str | Path, quality_metadata: Mapping[str, Any]
) -> Path:
    """Finalize provisional quality metadata and reseal artifact checksums.

    Capture writes immutable raw/event traces and a conservative
    ``REVIEW_REQUIRED`` placeholder.  Offline calibration may replace that
    placeholder exactly once.  Existing checksums are verified before any
    mutation, and the trace files are never rewritten.
    """

    root = Path(trial_dir)
    required_files = {
        TRIAL_FILE,
        MANIFEST_FILE,
        SOURCES_FILE,
        RAW_TRACE_FILE,
        EVENTS_TRACE_FILE,
        QUALITY_FILE,
        ACQUISITION_FILE,
        CHECKSUMS_FILE,
    }
    missing = sorted(name for name in required_files if not (root / name).is_file())
    if missing:
        raise ValueError("trial artifact is missing files: " + ", ".join(missing))
    failures = verify_checksums(root)
    if failures:
        raise ValueError(
            "trial artifact checksum verification failed: " + ", ".join(failures)
        )

    trial = json.loads((root / TRIAL_FILE).read_text(encoding="utf-8"))
    trial_id = trial.get("trial_id")
    if not isinstance(trial_id, str) or not trial_id:
        raise ValueError("trial.json has no valid trial_id")
    if root.name != trial_id:
        raise ValueError("trial directory name does not match trial_id")
    current = json.loads((root / QUALITY_FILE).read_text(encoding="utf-8"))
    placeholder_issue = current.get("issues") == [
        {
            "code": "NOT_EVALUATED",
            "message": "raw evidence quality has not been evaluated offline",
            "severity": "WARNING",
            "source_uid": None,
        }
    ]
    if current.get("disposition") != "REVIEW_REQUIRED" or not placeholder_issue:
        raise ValueError("trial quality was already finalized")

    canonical = _canonical_quality_document(
        quality_metadata,
        expected_trial_id=trial_id,
    )
    if canonical["disposition"] == "ACCEPTED":
        overhead = canonical["measurement_overhead_ratio"]
        if overhead is None or float(overhead) >= 0.01:
            raise ValueError("ACCEPTED quality requires collection overhead below 1%")
        boundary = trial.get("measurement_boundary")
        primary_uids = (
            boundary.get("primary_source_uids")
            if isinstance(boundary, Mapping)
            else None
        )
        if not isinstance(primary_uids, list) or not all(
            isinstance(uid, str) for uid in primary_uids
        ):
            raise ValueError("trial measurement boundary has invalid primary sources")
        by_uid = {source["source_uid"]: source for source in canonical["sources"]}
        missing_quality = sorted(set(primary_uids) - set(by_uid))
        if missing_quality:
            raise ValueError(
                "ACCEPTED quality omits primary sources: "
                + ", ".join(missing_quality)
            )
        for uid in primary_uids:
            source = by_uid[uid]
            if source["observed_samples"] < 2:
                raise ValueError(
                    "ACCEPTED quality requires at least two samples per primary source"
                )
            if float(source["missing_sample_ratio"]) >= 0.001:
                raise ValueError(
                    "ACCEPTED quality requires missing samples below 0.1%"
                )
        if any(issue["severity"] == "ERROR" for issue in canonical["issues"]):
            raise ValueError("ACCEPTED quality cannot contain ERROR issues")

    _replace_json(root / QUALITY_FILE, canonical)
    checksum_document = {
        "schema_version": 1,
        "algorithm": "sha256",
        "files": _calculate_checksums(root),
    }
    _replace_json(root / CHECKSUMS_FILE, checksum_document)
    return root / QUALITY_FILE


__all__ = [
    "CHECKSUMS_FILE",
    "EVENTS_TRACE_FILE",
    "MANIFEST_FILE",
    "QUALITY_FILE",
    "RAW_TRACE_FILE",
    "SOURCES_FILE",
    "TRIAL_FILE",
    "TrialArtifactWriter",
    "finalize_trial_quality",
    "prepare_experiment_layout",
    "verify_checksums",
]
