import socket
import threading
import time

import pytest

from pywattch._frames import (
    HEADER_LEN,
    MAX_PAYLOAD_LEN,
    FrameKind,
    decode_header,
    encode_frame,
    read_frame,
)
from pywattch.errors import WattchConnectionError, WattchProtocolError


def test_frame_layout_is_byte_compatible_with_rust():
    # Golden bytes: WCH0, wire 0.1 LE, kind=1 LE, flags=0, len=3 LE, payload.
    frame = encode_frame(FrameKind.HELLO_REQUEST, b"abc")
    assert frame == bytes.fromhex("57434830" "0000" "0100" "0100" "0000" "03000000") + b"abc"
    assert len(frame) == HEADER_LEN + 3


def test_header_round_trip_all_kinds():
    for kind in FrameKind:
        frame = encode_frame(kind, b"{}")
        decoded_kind, flags, payload_len = decode_header(frame)
        assert decoded_kind == kind
        assert flags == 0
        assert payload_len == 2


def test_kind_values_match_protocol_v01():
    assert FrameKind.HELLO_REQUEST == 1
    assert FrameKind.HELLO_RESPONSE == 2
    assert FrameKind.START_CAPTURE == 3
    assert FrameKind.STOP_CAPTURE == 4
    assert FrameKind.START_CAPTURE_ACK == 5
    assert FrameKind.STOP_CAPTURE_ACK == 6
    assert FrameKind.RAW_SAMPLE_BATCH == 10
    assert FrameKind.EVENT_BATCH == 11
    assert FrameKind.ERROR == 255


def test_rejects_bad_magic():
    frame = bytearray(encode_frame(FrameKind.HELLO_REQUEST, b""))
    frame[0] = ord("X")
    with pytest.raises(WattchProtocolError, match="magic"):
        decode_header(bytes(frame))


def test_rejects_unknown_kind():
    frame = bytearray(encode_frame(FrameKind.HELLO_REQUEST, b""))
    frame[8:10] = (42).to_bytes(2, "little")
    with pytest.raises(WattchProtocolError, match="unknown frame kind"):
        decode_header(bytes(frame))


def test_rejects_wrong_wire_version():
    frame = bytearray(encode_frame(FrameKind.HELLO_REQUEST, b""))
    frame[4:6] = (9).to_bytes(2, "little")
    with pytest.raises(WattchProtocolError, match="wire version"):
        decode_header(bytes(frame))


def test_rejects_oversized_payload_header():
    frame = bytearray(encode_frame(FrameKind.HELLO_REQUEST, b""))
    frame[12:16] = (MAX_PAYLOAD_LEN + 1).to_bytes(4, "little")
    with pytest.raises(WattchProtocolError, match="too large"):
        decode_header(bytes(frame))


def test_read_deadline_is_absolute_across_trickled_bytes():
    reader, writer = socket.socketpair()
    reader.settimeout(0.08)
    frame = encode_frame(FrameKind.HELLO_RESPONSE, b"{}")

    def trickle() -> None:
        try:
            for byte in frame:
                writer.send(bytes([byte]))
                time.sleep(0.03)
        except OSError:
            pass

    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    started = time.monotonic()
    with pytest.raises(WattchConnectionError):
        read_frame(reader)
    assert time.monotonic() - started < 0.2
    reader.close()
    writer.close()
    thread.join(timeout=0.2)
