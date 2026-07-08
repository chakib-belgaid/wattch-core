"""Blocking Wattch protocol v0.1 client over a Unix socket."""

from __future__ import annotations

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

DEFAULT_SOCKET_PATH = "/tmp/wattch-powerd.sock"


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
        socket_path: str = DEFAULT_SOCKET_PATH,
        client_name: str = "pywattch",
        connect_timeout_s: float = 5.0,
    ):
        self.socket_path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(connect_timeout_s)
        try:
            self._sock.connect(socket_path)
        except OSError as exc:
            self._sock.close()
            raise WattchConnectionError(
                f"failed to connect to wattch daemon at {socket_path}: {exc}"
            ) from exc
        self._sock.settimeout(None)

        write_frame(
            self._sock,
            FrameKind.HELLO_REQUEST,
            encode_hello_request(client_name, __version__),
        )
        hello = self._read_expected(FrameKind.HELLO_RESPONSE, "HelloResponse")
        assert isinstance(hello, HelloResponse)
        self.hello: HelloResponse = hello

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
        write_frame(
            self._sock,
            FrameKind.START_CAPTURE,
            encode_start_capture(run_id, interval_ms, source_ids or []),
        )
        ack = self._read_expected(FrameKind.START_CAPTURE_ACK, "StartCaptureAck")
        assert isinstance(ack, StartCaptureAck)
        return ack

    def read_batch(self) -> RawSampleBatch | StopCaptureAck:
        """Read the next data-plane message during an active capture."""
        kind, payload = read_frame(self._sock)
        message = decode_payload(kind, payload)
        if isinstance(message, ErrorMessage):
            raise WattchDaemonError(message.code, message.message)
        if isinstance(message, (RawSampleBatch, StopCaptureAck)):
            return message
        raise WattchProtocolError(
            f"unexpected {kind.name} during capture, expected RawSampleBatch or StopCaptureAck"
        )

    def stop_capture(self, run_id: str) -> tuple[StopCaptureAck, list[RawSampleBatch]]:
        """Request stop and drain in-flight batches until the ack arrives."""
        write_frame(self._sock, FrameKind.STOP_CAPTURE, encode_stop_capture(run_id))
        drained: list[RawSampleBatch] = []
        while True:
            message = self.read_batch()
            if isinstance(message, StopCaptureAck):
                return message, drained
            drained.append(message)

    def stream(
        self,
        run_id: str,
        interval_ms: int = 50,
        source_ids: list[int] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[RawSampleBatch]:
        """Yield batches until `should_stop()` is true, then stop cleanly.

        Abandoning the iterator early (``break``, ``close()``) also stops the
        capture and drains in-flight batches, so the connection stays usable.
        """
        self.start_capture(run_id, interval_ms, source_ids)
        stopping = False
        try:
            while True:
                if not stopping and should_stop is not None and should_stop():
                    write_frame(
                        self._sock, FrameKind.STOP_CAPTURE, encode_stop_capture(run_id)
                    )
                    stopping = True
                message = self.read_batch()
                if isinstance(message, StopCaptureAck):
                    return
                yield message
        except GeneratorExit:
            if not stopping:
                write_frame(
                    self._sock, FrameKind.STOP_CAPTURE, encode_stop_capture(run_id)
                )
            while not isinstance(self.read_batch(), StopCaptureAck):
                pass
            raise

    def capture(
        self,
        duration_s: float,
        interval_ms: int = 50,
        source_ids: list[int] | None = None,
        run_id: str | None = None,
    ) -> list[RawSampleBatch]:
        """Capture for roughly `duration_s` and return all batches."""
        run_id = run_id or f"pywattch-{time.time_ns()}"
        deadline = time.monotonic() + duration_s
        batches: list[RawSampleBatch] = []
        for batch in self.stream(
            run_id,
            interval_ms,
            source_ids,
            should_stop=lambda: time.monotonic() >= deadline,
        ):
            batches.append(batch)
        return batches

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

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


__all__ = ["WattchClient", "DEFAULT_SOCKET_PATH", "PROTOCOL_VERSION"]
