import socket

import pytest

from pywattch import (
    WattchClient,
    WattchConnectionError,
    WattchDaemonError,
    WattchProtocolError,
)
from pywattch import client as client_module
from pywattch._messages import HelloResponse, StartCaptureAck


def test_hello_exposes_sources(fake_daemon):
    with WattchClient(fake_daemon.socket_path) as client:
        assert client.node_id == "fake-node"
        assert client.hello.daemon_name == "fake-daemon"
        paths = [s.source_path for s in client.sources]
        assert paths == [
            "cpu/package/0/energy",
            "cpu/package/0/dram/energy",
            "gpu/0/power",
        ]
        package = client.source_by_id(1)
        assert package.unit == "MICROJOULE"
        assert package.temporality == "CUMULATIVE"
        assert package.max_value == 1_000_000.0


def test_capture_returns_batches_with_monotonic_sequences(fake_daemon):
    with WattchClient(fake_daemon.socket_path) as client:
        batches = client.capture(duration_s=0.08, interval_ms=5)

    assert len(batches) >= 2
    last_batch_seq = -1
    last_sample_seq = -1
    for batch in batches:
        assert batch.batch_sequence_no > last_batch_seq
        last_batch_seq = batch.batch_sequence_no
        package = [s for s in batch.samples if s.source_id == 1]
        assert len(package) == 1
        assert package[0].sequence_no > last_sample_seq
        last_sample_seq = package[0].sequence_no


def test_capture_with_source_filter(fake_daemon):
    with WattchClient(fake_daemon.socket_path) as client:
        batches = client.capture(duration_s=0.05, interval_ms=5, source_ids=[1])
    assert batches
    for batch in batches:
        assert {s.source_id for s in batch.samples} == {1}


def test_unknown_source_id_raises_daemon_error(fake_daemon):
    with WattchClient(fake_daemon.socket_path) as client:
        with pytest.raises(WattchDaemonError) as excinfo:
            client.start_capture("run-x", source_ids=[99])
        assert excinfo.value.code == 3


def test_missing_socket_raises_connection_error():
    with pytest.raises(WattchConnectionError, match="failed to connect"):
        WattchClient("/tmp/wattch-missing-socket.sock")


def test_hello_with_wrong_message_type_raises_protocol_error(fake_daemon, monkeypatch):
    # Simulate a decode_payload bug/drift that hands back a message whose
    # type doesn't match the frame kind it was decoded for.
    bogus = StartCaptureAck(run_id="not-a-hello", accepted_source_ids=())
    monkeypatch.setattr(client_module, "decode_payload", lambda kind, payload: bogus)
    with pytest.raises(WattchProtocolError, match="HelloResponse"):
        WattchClient(fake_daemon.socket_path)


def test_autodetect_connection_error_lists_every_candidate(monkeypatch):
    tried = [
        "/tmp/wattch-none-a.sock",
        "/tmp/wattch-none-b.sock",
        "/tmp/wattch-none-c.sock",
    ]
    monkeypatch.setattr(client_module, "default_socket_candidates", lambda: list(tried))
    with pytest.raises(WattchConnectionError) as excinfo:
        WattchClient()  # no explicit socket -> auto-detect
    message = str(excinfo.value)
    assert "auto-detected" in message
    for path in tried:
        assert path in message


def test_handshake_failure_closes_socket(fake_daemon, monkeypatch):
    # A rejected/malformed Hello raised mid-__init__ must not leak the fd.
    created: list[socket.socket] = []
    real_socket = socket.socket

    def tracking_socket(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        created.append(sock)
        return sock

    monkeypatch.setattr(client_module.socket, "socket", tracking_socket)
    bogus = StartCaptureAck(run_id="not-a-hello", accepted_source_ids=())
    monkeypatch.setattr(client_module, "decode_payload", lambda kind, payload: bogus)

    with pytest.raises(WattchProtocolError):
        WattchClient(fake_daemon.socket_path)

    assert created, "expected the client to create a socket"
    assert created[0].fileno() == -1, "socket should be closed after handshake failure"


def test_start_capture_with_wrong_message_type_raises_protocol_error(fake_daemon, monkeypatch):
    with WattchClient(fake_daemon.socket_path) as client:
        bogus = HelloResponse(
            daemon_name="x",
            daemon_version="0",
            protocol_version="0.1.0",
            node_id="fake-node",
            sources=(),
        )
        monkeypatch.setattr(client_module, "decode_payload", lambda kind, payload: bogus)
        with pytest.raises(WattchProtocolError, match="StartCaptureAck"):
            client.start_capture("run-x")


def test_abandoned_stream_leaves_connection_usable(fake_daemon):
    with WattchClient(fake_daemon.socket_path) as client:
        stream = client.stream("run-abandoned", interval_ms=5)
        assert next(stream).samples
        stream.close()  # abandon mid-capture: must stop and drain

        batches = client.capture(duration_s=0.05, interval_ms=5)
    assert batches
