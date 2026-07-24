"""Blocking Wattch protocol v0.1 client over a Unix socket."""

from __future__ import annotations

import math
import os
import socket
import time
from typing import Callable, Iterator

from ._frames import FrameKind, read_frame, write_frame
from ._messages import (
    PROTOCOL_VERSION,
    ErrorMessage,
    HelloResponse,
    RawSampleBatch,
    SourceDescriptor,
    StartCaptureAck,
    StopCaptureAck,
    decode_payload,
    encode_hello_request,
    encode_start_capture,
    encode_stop_capture,
)
from .errors import WattchConnectionError, WattchDaemonError, WattchProtocolError

__version__ = "0.1.0"

# Fine-grained RAPL energy counters are a documented side channel (PLATYPUS,
# CVE-2020-8694); wattch-powerd's socket -- not the RAPL files -- is the
# actual security boundary, so where it lives matters. Mirrors the Rust
# resolution in wattch_protocol::socket, highest-privilege first:
#   1. /run/wattch/<name>              -- root-owned runtime dir (systemd).
#   2. $XDG_RUNTIME_DIR/wattch/<name>  -- user-private runtime dir.
# Predictable paths in attacker-writable /tmp are intentionally excluded
# from automatic discovery. Development callers can still pass one explicitly.
RUN_WATTCH_SOCKET = "/run/wattch/wattch-powerd.sock"
MAX_RUN_ID_BYTES = 256
CONTROL_TIMEOUT_S = 5.0
STOP_GRACE_S = 5.0
CAPTURE_BATCH_SLACK = 64


def _first_existing(
    candidates: list[str], exists: Callable[[str], bool] = os.path.exists
) -> str:
    """The first candidate for which `exists` is true, else the first one."""
    for candidate in candidates:
        if exists(candidate):
            return candidate
    return candidates[0]


def default_socket_candidates() -> list[str]:
    """Candidate wattch-powerd socket paths, in priority order."""
    candidates = [RUN_WATTCH_SOCKET]
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        candidates.append(os.path.join(xdg_runtime_dir, "wattch", "wattch-powerd.sock"))
    return candidates


def default_socket_path() -> str:
    """The first existing candidate socket path, or the highest-priority one.

    If none of the candidates exist, the highest-priority path is returned
    anyway; connecting to it will raise `WattchConnectionError`, whose
    message includes this path.
    """
    return _first_existing(default_socket_candidates())


# Kept as a public backward-compat export (re-exported from the package root).
# It is a snapshot taken at import time, not env/uid-driven at call time --
# prefer `default_socket_path()` in new code.
DEFAULT_SOCKET_PATH = default_socket_path()


class WattchClient:
    """One connection to a Wattch daemon.

    Usage::

        with WattchClient("/tmp/wattch-testd.sock") as client:
            for source in client.sources:
                print(source.source_path)
            batches = client.capture(duration_s=1.0, interval_ms=50)
    """

    def __init__(
        self,
        socket_path: str | None = None,
        client_name: str = "pywattch",
        connect_timeout_s: float = 5.0,
    ):
        # When the caller relies on the default, remember the candidates so a
        # connection failure can report every path we probed, not just the
        # highest-priority one `default_socket_path()` happens to return.
        if socket_path is None:
            candidates: list[str] | None = default_socket_candidates()
            socket_path = _first_existing(candidates)
        else:
            candidates = None
        self.socket_path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(connect_timeout_s)
        try:
            self._sock.connect(socket_path)
        except OSError as exc:
            self._sock.close()
            detail = f"failed to connect to wattch daemon at {socket_path}"
            if candidates is not None:
                detail += f" (auto-detected; tried {', '.join(candidates)})"
            raise WattchConnectionError(f"{detail}: {exc}") from exc
        self._sock.settimeout(CONTROL_TIMEOUT_S)
        self._active_run_id: str | None = None
        self._accepted_source_ids: frozenset[int] = frozenset()
        self._last_batch_sequence_no: int | None = None
        self._sample_positions: dict[int, tuple[int, int]] = {}
        self._batches_seen = 0
        self._samples_seen = 0

        # Close the socket if the handshake fails midway, so a rejected or
        # malformed Hello doesn't leak the fd on a client we can't return.
        try:
            write_frame(
                self._sock,
                FrameKind.HELLO_REQUEST,
                encode_hello_request(client_name, __version__),
            )
            hello = self._read_expected(FrameKind.HELLO_RESPONSE, "HelloResponse")
            if not isinstance(hello, HelloResponse):
                raise WattchProtocolError(
                    f"unexpected {type(hello).__name__}, expected HelloResponse"
                )
            self.hello: HelloResponse = hello
            self._validate_hello()
        except BaseException:
            self.close()
            raise

    # -- introspection -----------------------------------------------------

    @property
    def sources(self) -> tuple[SourceDescriptor, ...]:
        return self.hello.sources

    @property
    def node_id(self) -> str:
        return self.hello.node_id

    def source_by_id(self, source_id: int) -> SourceDescriptor:
        for source in self.hello.sources:
            if source.local_source_id == source_id:
                return source
        raise KeyError(f"unknown source id {source_id}")

    # -- capture -----------------------------------------------------------

    def start_capture(
        self,
        run_id: str,
        interval_ms: int = 50,
        source_ids: list[int] | None = None,
    ) -> StartCaptureAck:
        self._validate_run_id(run_id)
        if self._active_run_id is not None:
            raise WattchProtocolError("a capture is already active on this connection")
        self._require_uint(interval_ms, 64, "interval_ms")
        if interval_ms == 0:
            raise WattchProtocolError("interval_ms must be positive")
        requested = list(source_ids or [])
        for source_id in requested:
            self._require_uint(source_id, 32, "source_id")
        if len(requested) != len(set(requested)):
            raise WattchProtocolError("source_ids must not contain duplicates")
        advertised = {source.local_source_id for source in self.sources}
        accepted_expected = set(requested) if requested else advertised
        unknown = accepted_expected - advertised
        if unknown:
            raise WattchProtocolError(f"unknown source ids requested: {sorted(unknown)}")
        write_frame(
            self._sock,
            FrameKind.START_CAPTURE,
            encode_start_capture(run_id, interval_ms, requested),
        )
        ack = self._read_expected(FrameKind.START_CAPTURE_ACK, "StartCaptureAck")
        if not isinstance(ack, StartCaptureAck):
            raise WattchProtocolError(
                f"unexpected {type(ack).__name__}, expected StartCaptureAck"
            )
        for source_id in ack.accepted_source_ids:
            self._require_uint(source_id, 32, "accepted_source_id")
        accepted = set(ack.accepted_source_ids)
        if ack.run_id != run_id:
            raise WattchProtocolError(
                f"StartCaptureAck run_id {ack.run_id!r} does not match {run_id!r}"
            )
        if len(accepted) != len(ack.accepted_source_ids) or accepted != accepted_expected:
            raise WattchProtocolError(
                "StartCaptureAck accepted_source_ids do not match the requested source set"
            )
        self._active_run_id = run_id
        self._accepted_source_ids = frozenset(accepted)
        self._last_batch_sequence_no = None
        self._sample_positions = {}
        self._batches_seen = 0
        self._samples_seen = 0
        self._sock.settimeout(max(CONTROL_TIMEOUT_S, 3 * interval_ms / 1000.0))
        return ack

    def read_batch(self) -> RawSampleBatch | StopCaptureAck:
        """Read the next data-plane message during an active capture."""
        kind, payload = read_frame(self._sock)
        message = decode_payload(kind, payload)
        if isinstance(message, ErrorMessage):
            raise WattchDaemonError(message.code, message.message)
        if isinstance(message, RawSampleBatch):
            self._validate_batch(message)
            return message
        if isinstance(message, StopCaptureAck):
            self._validate_stop_ack(message)
            self._clear_capture()
            return message
        raise WattchProtocolError(
            f"unexpected {kind.name} during capture, expected RawSampleBatch or StopCaptureAck"
        )

    def stop_capture(
        self, run_id: str, max_drained_batches: int = CAPTURE_BATCH_SLACK
    ) -> tuple[StopCaptureAck, list[RawSampleBatch]]:
        """Request stop and drain in-flight batches until the ack arrives."""
        if run_id != self._active_run_id:
            raise WattchProtocolError(
                f"cannot stop run {run_id!r}; active run is {self._active_run_id!r}"
            )
        if max_drained_batches < 0:
            raise ValueError("max_drained_batches must not be negative")
        write_frame(self._sock, FrameKind.STOP_CAPTURE, encode_stop_capture(run_id))
        self._sock.settimeout(STOP_GRACE_S)
        drained: list[RawSampleBatch] = []
        while True:
            message = self.read_batch()
            if isinstance(message, StopCaptureAck):
                return message, drained
            if len(drained) >= max_drained_batches:
                self.close()
                raise WattchProtocolError(
                    f"daemon exceeded the post-stop drain limit {max_drained_batches}"
                )
            drained.append(message)

    def stream(
        self,
        run_id: str,
        interval_ms: int = 50,
        source_ids: list[int] | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_batches: int | None = None,
    ) -> Iterator[RawSampleBatch]:
        """Yield batches until `should_stop()` is true, then stop cleanly.

        Abandoning the iterator early (``break``, ``close()``) also stops the
        capture and drains in-flight batches, so the connection stays usable.
        """
        self.start_capture(run_id, interval_ms, source_ids)
        stopping = False
        yielded = 0
        try:
            while True:
                stop_requested = should_stop is not None and should_stop()
                if max_batches is not None and yielded >= max_batches:
                    stop_requested = True
                if not stopping and stop_requested:
                    write_frame(
                        self._sock, FrameKind.STOP_CAPTURE, encode_stop_capture(run_id)
                    )
                    self._sock.settimeout(STOP_GRACE_S)
                    stopping = True
                message = self.read_batch()
                if isinstance(message, StopCaptureAck):
                    return
                yielded += 1
                hard_limit = None if max_batches is None else max_batches + CAPTURE_BATCH_SLACK
                if hard_limit is not None and yielded > hard_limit:
                    self.close()
                    raise WattchProtocolError(
                        f"daemon exceeded capture batch limit {hard_limit}"
                    )
                yield message
        except GeneratorExit:
            if not stopping:
                write_frame(
                    self._sock, FrameKind.STOP_CAPTURE, encode_stop_capture(run_id)
                )
            self._sock.settimeout(STOP_GRACE_S)
            drained = 0
            while not isinstance(self.read_batch(), StopCaptureAck):
                drained += 1
                if drained > CAPTURE_BATCH_SLACK:
                    self.close()
                    raise WattchProtocolError(
                        "daemon exceeded the abandoned-stream drain limit"
                    )
            raise

    def capture(
        self,
        duration_s: float,
        interval_ms: int = 50,
        source_ids: list[int] | None = None,
        run_id: str | None = None,
    ) -> list[RawSampleBatch]:
        """Capture for roughly `duration_s` and return all batches."""
        if not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError("duration_s must be finite and non-negative")
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        run_id = run_id or f"pywattch-{time.time_ns()}"
        deadline = time.monotonic() + duration_s
        expected_batches = max(1, math.ceil(duration_s * 1000.0 / interval_ms))
        max_batches = expected_batches + CAPTURE_BATCH_SLACK
        batches: list[RawSampleBatch] = []
        for batch in self.stream(
            run_id,
            interval_ms,
            source_ids,
            should_stop=lambda: time.monotonic() >= deadline,
            max_batches=max_batches,
        ):
            batches.append(batch)
        return batches

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._clear_capture()

    def __enter__(self) -> "WattchClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _read_expected(self, expected: FrameKind, name: str):
        kind, payload = read_frame(self._sock)
        message = decode_payload(kind, payload)
        if isinstance(message, ErrorMessage):
            raise WattchDaemonError(message.code, message.message)
        if kind != expected:
            raise WattchProtocolError(f"unexpected {kind.name}, expected {name}")
        return message

    def _validate_hello(self) -> None:
        if self.hello.protocol_version != PROTOCOL_VERSION:
            raise WattchProtocolError(
                f"daemon protocol {self.hello.protocol_version!r} does not match "
                f"client protocol {PROTOCOL_VERSION!r}"
            )
        if not self.hello.node_id.strip():
            raise WattchProtocolError("HelloResponse node_id must not be empty")
        ids: set[int] = set()
        uids: set[str] = set()
        for source in self.sources:
            self._require_uint(source.local_source_id, 32, "local_source_id")
            required_text = {
                "source_uid": source.source_uid,
                "node_id": source.node_id,
                "backend": source.backend,
                "kind": source.kind,
                "unit": source.unit,
                "temporality": source.temporality,
                "evidence_class": source.evidence_class,
                "source_path": source.source_path,
                "clock_domain": source.clock_domain,
                "sampling_mode": source.sampling_mode,
            }
            for field_name, value in required_text.items():
                if not isinstance(value, str) or not value.strip():
                    raise WattchProtocolError(
                        f"source descriptor field {field_name} must be a non-empty string"
                    )
            if source.local_source_id in ids:
                raise WattchProtocolError(
                    f"duplicate local source id {source.local_source_id}"
                )
            if source.source_uid in uids:
                raise WattchProtocolError(f"duplicate source uid {source.source_uid!r}")
            if source.node_id != self.hello.node_id:
                raise WattchProtocolError(
                    f"source {source.source_uid!r} belongs to node {source.node_id!r}, "
                    f"not {self.hello.node_id!r}"
                )
            ids.add(source.local_source_id)
            uids.add(source.source_uid)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str):
            raise WattchProtocolError("run_id must be a string")
        if not run_id.strip():
            raise WattchProtocolError("run_id must not be empty")
        try:
            encoded = run_id.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WattchProtocolError("run_id must be valid UTF-8") from exc
        if len(encoded) > MAX_RUN_ID_BYTES:
            raise WattchProtocolError("run_id exceeds 256 UTF-8 bytes")
        if any(ord(character) < 32 or ord(character) == 127 for character in run_id):
            raise WattchProtocolError("run_id must not contain control characters")

    def _validate_batch(self, batch: RawSampleBatch) -> None:
        if self._active_run_id is None:
            raise WattchProtocolError("RawSampleBatch arrived without an active capture")
        if batch.run_id != self._active_run_id:
            raise WattchProtocolError(
                f"batch run_id {batch.run_id!r} does not match {self._active_run_id!r}"
            )
        if batch.node_id != self.node_id:
            raise WattchProtocolError(
                f"batch node_id {batch.node_id!r} does not match {self.node_id!r}"
            )
        self._require_uint(batch.batch_sequence_no, 64, "batch_sequence_no")
        if (
            self._last_batch_sequence_no is not None
            and batch.batch_sequence_no <= self._last_batch_sequence_no
        ):
            raise WattchProtocolError("batch sequence replay or reordering detected")
        if len(batch.samples) > len(self._accepted_source_ids):
            raise WattchProtocolError("batch contains more samples than accepted sources")
        seen: set[int] = set()
        for sample in batch.samples:
            self._require_uint(sample.source_id, 32, "sample.source_id")
            self._require_uint(sample.sequence_no, 64, "sample.sequence_no")
            self._require_uint(sample.monotonic_ns, 64, "sample.monotonic_ns")
            if isinstance(sample.value, bool):
                raise WattchProtocolError("boolean raw sample values are invalid")
            if isinstance(sample.value, int) and not (-(2**63) <= sample.value < 2**63):
                raise WattchProtocolError("integer raw sample value is outside i64 range")
            if isinstance(sample.value, float) and not math.isfinite(sample.value):
                raise WattchProtocolError("floating raw sample value must be finite")
            if sample.source_id not in self._accepted_source_ids:
                raise WattchProtocolError(
                    f"sample references unaccepted source id {sample.source_id}"
                )
            if sample.source_id in seen:
                raise WattchProtocolError(
                    f"batch repeats source id {sample.source_id}"
                )
            seen.add(sample.source_id)
            previous = self._sample_positions.get(sample.source_id)
            if previous is not None:
                sequence_no, monotonic_ns = previous
                if sample.sequence_no <= sequence_no:
                    raise WattchProtocolError(
                        f"source {sample.source_id} sequence replay or reordering detected"
                    )
                if sample.monotonic_ns < monotonic_ns:
                    raise WattchProtocolError(
                        f"source {sample.source_id} monotonic timestamp regressed"
                    )
        for sample in batch.samples:
            self._sample_positions[sample.source_id] = (
                sample.sequence_no,
                sample.monotonic_ns,
            )
        self._last_batch_sequence_no = batch.batch_sequence_no
        self._batches_seen += 1
        self._samples_seen += len(batch.samples)

    def _validate_stop_ack(self, ack: StopCaptureAck) -> None:
        self._require_uint(ack.batches_sent, 64, "batches_sent")
        self._require_uint(ack.samples_sent, 64, "samples_sent")
        if ack.run_id != self._active_run_id:
            raise WattchProtocolError(
                f"StopCaptureAck run_id {ack.run_id!r} does not match "
                f"{self._active_run_id!r}"
            )
        if ack.batches_sent != self._batches_seen or ack.samples_sent != self._samples_seen:
            raise WattchProtocolError(
                "StopCaptureAck counts do not match the batches observed by the client"
            )

    def _clear_capture(self) -> None:
        self._active_run_id = None
        self._accepted_source_ids = frozenset()
        self._last_batch_sequence_no = None
        self._sample_positions = {}
        if self._sock.fileno() >= 0:
            self._sock.settimeout(CONTROL_TIMEOUT_S)

    @staticmethod
    def _require_uint(value: object, bits: int, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value >= 2**bits
        ):
            raise WattchProtocolError(
                f"{field_name} must be an unsigned {bits}-bit integer"
            )


__all__ = ["WattchClient", "DEFAULT_SOCKET_PATH", "PROTOCOL_VERSION"]
