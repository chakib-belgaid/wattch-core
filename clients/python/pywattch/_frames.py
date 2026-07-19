"""Wattch wire envelope, byte-compatible with crates/wattch-protocol.

Frame layout (little-endian):

    magic            4 bytes  b"WCH0"
    wire major       u16      0
    wire minor       u16      2
    frame kind       u16
    flags            u16
    payload length   u32
    payload          length bytes (JSON in v0.1 and v0.2)
"""

from __future__ import annotations

import socket
import struct
import time
from enum import IntEnum

from .errors import WattchConnectionError, WattchProtocolError

MAGIC = b"WCH0"
HEADER_LEN = 16
WIRE_VERSION_MAJOR = 0
WIRE_VERSION_MINOR = 2
MIN_READABLE_WIRE_VERSION_MINOR = 1
MAX_PAYLOAD_LEN = 16 * 1024 * 1024
# Offline migration bound for historical v0.1 trace batches. Live socket I/O
# never uses this larger limit.
MAX_LEGACY_ARTIFACT_PAYLOAD_LEN = MAX_PAYLOAD_LEN * 16

_HEADER = struct.Struct("<4sHHHHI")


class FrameKind(IntEnum):
    HELLO_REQUEST = 1
    HELLO_RESPONSE = 2
    START_CAPTURE = 3
    STOP_CAPTURE = 4
    START_CAPTURE_ACK = 5
    STOP_CAPTURE_ACK = 6
    RECORD_EVENT = 7
    RECORD_EVENT_ACK = 8
    RAW_SAMPLE_BATCH = 10
    EVENT_BATCH = 11
    ERROR = 255


def encode_frame(kind: FrameKind, payload: bytes, flags: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD_LEN:
        raise WattchProtocolError(f"payload too large: {len(payload)} bytes")
    header = _HEADER.pack(
        MAGIC, WIRE_VERSION_MAJOR, WIRE_VERSION_MINOR, int(kind), flags, len(payload)
    )
    return header + payload


def decode_header(
    buf: bytes, *, max_payload_len: int = MAX_PAYLOAD_LEN
) -> tuple[FrameKind, int, int]:
    """Return ``(kind, flags, payload_len)`` for a 16-byte header.

    Live callers use the default 16 MiB bound. Offline legacy-artifact
    readers may pass a larger finite migration bound explicitly.
    """
    if len(buf) < HEADER_LEN:
        raise WattchProtocolError("buffer too short for frame header")
    magic, major, minor, raw_kind, flags, payload_len = _HEADER.unpack(buf[:HEADER_LEN])
    if magic != MAGIC:
        raise WattchProtocolError("invalid magic bytes")
    if major != WIRE_VERSION_MAJOR or not (
        MIN_READABLE_WIRE_VERSION_MINOR <= minor <= WIRE_VERSION_MINOR
    ):
        raise WattchProtocolError(f"unsupported wire version {major}.{minor}")
    try:
        kind = FrameKind(raw_kind)
    except ValueError as exc:
        raise WattchProtocolError(f"unknown frame kind: {raw_kind}") from exc
    if minor < 2 and kind in {
        FrameKind.RECORD_EVENT,
        FrameKind.RECORD_EVENT_ACK,
        FrameKind.EVENT_BATCH,
    }:
        raise WattchProtocolError(f"{kind.name} requires wire version 0.2")
    if payload_len > max_payload_len:
        raise WattchProtocolError(f"payload too large: {payload_len} bytes")
    return kind, flags, payload_len


def _recv_exact(
    sock: socket.socket, length: int, deadline: float | None = None
) -> bytes:
    chunks = []
    remaining = length
    while remaining > 0:
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise WattchConnectionError("socket read deadline exceeded")
            sock.settimeout(max(timeout, 0.001))
        try:
            chunk = sock.recv(remaining)
        except OSError as exc:
            raise WattchConnectionError(f"socket read failed: {exc}") from exc
        if not chunk:
            raise WattchConnectionError("daemon closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket) -> tuple[FrameKind, bytes]:
    original_timeout = sock.gettimeout()
    deadline = (
        None if original_timeout is None else time.monotonic() + original_timeout
    )
    try:
        header = _recv_exact(sock, HEADER_LEN, deadline)
        kind, _flags, payload_len = decode_header(header)
        payload = _recv_exact(sock, payload_len, deadline) if payload_len else b""
        return kind, payload
    finally:
        if sock.fileno() >= 0:
            sock.settimeout(original_timeout)


def write_frame(sock: socket.socket, kind: FrameKind, payload: bytes, flags: int = 0) -> None:
    try:
        sock.sendall(encode_frame(kind, payload, flags))
    except OSError as exc:
        raise WattchConnectionError(f"socket write failed: {exc}") from exc
