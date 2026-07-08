import pytest

from pywattch import WattchClient, WattchConnectionError, WattchDaemonError


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


def test_abandoned_stream_leaves_connection_usable(fake_daemon):
    with WattchClient(fake_daemon.socket_path) as client:
        stream = client.stream("run-abandoned", interval_ms=5)
        assert next(stream).samples
        stream.close()  # abandon mid-capture: must stop and drain

        batches = client.capture(duration_s=0.05, interval_ms=5)
    assert batches
