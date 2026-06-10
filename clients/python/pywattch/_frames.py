"""Wattch wire envelope, byte-compatible with crates/wattch-protocol.

Frame layout (little-endian):

    magic            4 bytes  b"WCH0"
    wire major       u16      0
    wire minor       u16      1
    frame kind       u16
    flags            u16
    payload length   u32
    payload          length bytes (JSON in v0.1)
"""

from __future__ import annotations

import socket
import struct
from enum import IntEnum

from .errors import WattchConnectionError, WattchProtocolError

MAGIC = b"WCH0"
HEADER_LEN = 16
WIRE_VERSION_MAJOR = 0
WIRE_VERSION_MINOR = 1
MAX_PAYLOAD_LEN = 16 * 1024 * 1024

_HEADER = struct.Struct("<4sHHHHI")


class FrameKind(IntEnum):
    HELLO_REQUEST = 1
    HELLO_RESPONSE = 2
    START_CAPTURE = 3
    STOP_CAPTURE = 4
    START_CAPTURE_ACK = 5
    STOP_CAPTURE_ACK = 6
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


def decode_header(buf: bytes) -> tuple[FrameKind, int, int]:
    """Return (kind, flags, payload_len) for a 16-byte header."""
    if len(buf) < HEADER_LEN:
        raise WattchProtocolError("buffer too short for frame header")
    magic, major, minor, raw_kind, flags, payload_len = _HEADER.unpack(buf[:HEADER_LEN])
    if magic != MAGIC:
        raise WattchProtocolError("invalid magic bytes")
    if (major, minor) != (WIRE_VERSION_MAJOR, WIRE_VERSION_MINOR):
        raise WattchProtocolError(f"unsupported wire version {major}.{minor}")
    try:
        kind = FrameKind(raw_kind)
    except ValueError as exc:
        raise WattchProtocolError(f"unknown frame kind: {raw_kind}") from exc
    if payload_len > MAX_PAYLOAD_LEN:
        raise WattchProtocolError(f"payload too large: {payload_len} bytes")
    return kind, flags, payload_len


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining > 0:
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
    header = _recv_exact(sock, HEADER_LEN)
    kind, _flags, payload_len = decode_header(header)
    payload = _recv_exact(sock, payload_len) if payload_len else b""
    return kind, payload


def write_frame(sock: socket.socket, kind: FrameKind, payload: bytes, flags: int = 0) -> None:
    try:
        sock.sendall(encode_frame(kind, payload, flags))
    except OSError as exc:
        raise WattchConnectionError(f"socket write failed: {exc}") from exc
