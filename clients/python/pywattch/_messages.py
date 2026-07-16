"""Typed protocol v0.1 messages (JSON payloads), mirroring messages.rs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ._frames import FrameKind
from .errors import WattchProtocolError

PROTOCOL_VERSION = "0.1.0"


@dataclass(frozen=True)
class SourceDescriptor:
    local_source_id: int
    source_uid: str
    node_id: str
    backend: str
    kind: str
    unit: str
    temporality: str
    evidence_class: str
    source_path: str
    clock_domain: str
    sampling_mode: str
    parent_source_uid: str | None = None
    nominal_period_ns: int | None = None
    max_value: float | None = None
    resolution: float | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceDescriptor":
        try:
            return cls(
                local_source_id=data["local_source_id"],
                source_uid=data["source_uid"],
                node_id=data["node_id"],
                backend=data["backend"],
                kind=data["kind"],
                unit=data["unit"],
                temporality=data["temporality"],
                evidence_class=data["evidence_class"],
                source_path=data["source_path"],
                clock_domain=data["clock_domain"],
                sampling_mode=data["sampling_mode"],
                parent_source_uid=data.get("parent_source_uid"),
                nominal_period_ns=data.get("nominal_period_ns"),
                max_value=data.get("max_value"),
                resolution=data.get("resolution"),
                labels=dict(data.get("labels") or {}),
            )
        except KeyError as exc:
            raise WattchProtocolError(f"source descriptor missing field {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_source_id": self.local_source_id,
            "source_uid": self.source_uid,
            "node_id": self.node_id,
            "backend": self.backend,
            "kind": self.kind,
            "unit": self.unit,
            "temporality": self.temporality,
            "evidence_class": self.evidence_class,
            "source_path": self.source_path,
            "parent_source_uid": self.parent_source_uid,
            "clock_domain": self.clock_domain,
            "sampling_mode": self.sampling_mode,
            "nominal_period_ns": self.nominal_period_ns,
            "max_value": self.max_value,
            "resolution": self.resolution,
            "labels": self.labels,
        }


@dataclass(frozen=True)
class RawSample:
    source_id: int
    sequence_no: int
    monotonic_ns: int
    value: float | int | bytes

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawSample":
        raw_value = data["value"]
        value_type = raw_value.get("type")
        if value_type == "f64":
            value: float | int | bytes = float(raw_value["value"])
        elif value_type == "i64":
            value = int(raw_value["value"])
        elif value_type == "bytes":
            value = bytes(raw_value["value"])
        else:
            raise WattchProtocolError(f"unknown raw value type {value_type!r}")
        return cls(
            source_id=data["source_id"],
            sequence_no=data["sequence_no"],
            monotonic_ns=data["monotonic_ns"],
            value=value,
        )

    def to_dict(self) -> dict[str, Any]:
        if isinstance(self.value, bool):
            raise WattchProtocolError("boolean raw values are not part of the protocol")
        if isinstance(self.value, int):
            value = {"type": "i64", "value": self.value}
        elif isinstance(self.value, float):
            value = {"type": "f64", "value": self.value}
        else:
            value = {"type": "bytes", "value": list(self.value)}
        return {
            "source_id": self.source_id,
            "sequence_no": self.sequence_no,
            "monotonic_ns": self.monotonic_ns,
            "value": value,
        }

    @property
    def numeric_value(self) -> float:
        if isinstance(self.value, bytes):
            raise WattchProtocolError("bytes sample has no numeric value")
        return float(self.value)


@dataclass(frozen=True)
class RawSampleBatch:
    run_id: str
    node_id: str
    batch_sequence_no: int
    samples: tuple[RawSample, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawSampleBatch":
        return cls(
            run_id=data["run_id"],
            node_id=data["node_id"],
            batch_sequence_no=data["batch_sequence_no"],
            samples=tuple(RawSample.from_dict(s) for s in data["samples"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "batch_sequence_no": self.batch_sequence_no,
            "samples": [s.to_dict() for s in self.samples],
        }


@dataclass(frozen=True)
class HelloResponse:
    daemon_name: str
    daemon_version: str
    protocol_version: str
    node_id: str
    sources: tuple[SourceDescriptor, ...]


@dataclass(frozen=True)
class StartCaptureAck:
    run_id: str
    accepted_source_ids: tuple[int, ...]


@dataclass(frozen=True)
class StopCaptureAck:
    run_id: str
    batches_sent: int
    samples_sent: int


@dataclass(frozen=True)
class ErrorMessage:
    code: int
    message: str


def encode_hello_request(client_name: str, client_version: str) -> bytes:
    return json.dumps(
        {
            "client_name": client_name,
            "client_version": client_version,
            "protocol_version": PROTOCOL_VERSION,
        }
    ).encode()


def encode_start_capture(run_id: str, interval_ms: int, source_ids: list[int]) -> bytes:
    return json.dumps(
        {"run_id": run_id, "interval_ms": interval_ms, "source_ids": source_ids}
    ).encode()


def encode_stop_capture(run_id: str) -> bytes:
    return json.dumps({"run_id": run_id}).encode()


def decode_payload(kind: FrameKind, payload: bytes) -> Any:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WattchProtocolError(f"invalid JSON payload for {kind.name}: {exc}") from exc

    try:
        if kind == FrameKind.HELLO_RESPONSE:
            return HelloResponse(
                daemon_name=data["daemon_name"],
                daemon_version=data["daemon_version"],
                protocol_version=data["protocol_version"],
                node_id=data["node_id"],
                sources=tuple(SourceDescriptor.from_dict(s) for s in data["sources"]),
            )
        if kind == FrameKind.START_CAPTURE_ACK:
            return StartCaptureAck(
                run_id=data["run_id"],
                accepted_source_ids=tuple(data["accepted_source_ids"]),
            )
        if kind == FrameKind.STOP_CAPTURE_ACK:
            return StopCaptureAck(
                run_id=data["run_id"],
                batches_sent=data["batches_sent"],
                samples_sent=data["samples_sent"],
            )
        if kind == FrameKind.RAW_SAMPLE_BATCH:
            return RawSampleBatch.from_dict(data)
        if kind == FrameKind.ERROR:
            return ErrorMessage(code=data["code"], message=data["message"])
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        raise WattchProtocolError(f"malformed {kind.name} payload: {exc}") from exc

    raise WattchProtocolError(f"unexpected frame kind {kind.name} for a client")
