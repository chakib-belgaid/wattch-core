"""Integration against the real Rust wattch-testd, when a binary exists."""

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from pywattch import EnergyMeter, WattchClient

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTD = REPO_ROOT / "target" / "debug" / "wattch-testd"
SCENARIO = REPO_ROOT / "examples" / "scenarios" / "laptop_cpu_gpu_nested.json"

pytestmark = pytest.mark.skipif(
    not TESTD.exists(), reason="build wattch-testd first: cargo build -p wattch-testd"
)


@pytest.fixture
def testd():
    socket_path = f"/tmp/wattch-testd-pytest-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    process = subprocess.Popen(
        [str(TESTD), "--scenario", str(SCENARIO), "--socket", socket_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not os.path.exists(socket_path):
            assert time.monotonic() < deadline, "testd did not bind its socket"
            time.sleep(0.02)
        yield socket_path
    finally:
        process.terminate()
        process.wait(timeout=5)
        if os.path.exists(socket_path):
            os.unlink(socket_path)


def test_python_client_speaks_to_rust_daemon(testd):
    with WattchClient(testd) as client:
        assert client.hello.daemon_name == "wattch-testd"
        assert len(client.sources) == 6
        batches = client.capture(duration_s=0.1, interval_ms=10)
    assert batches
    assert all(len(batch.samples) == 6 for batch in batches)


def test_abandoned_stream_leaves_connection_usable(testd):
    with WattchClient(testd) as client:
        stream = client.stream("run-abandoned", interval_ms=10)
        assert next(stream).samples
        stream.close()  # abandon mid-capture: must stop and drain

        batches = client.capture(duration_s=0.05, interval_ms=10)
    assert batches


def test_energy_meter_against_rust_daemon(testd):
    with EnergyMeter(testd, interval_ms=10) as meter:
        time.sleep(0.08)
    trace = meter.get_trace()
    assert len(trace) == 1
    # The scenario's package counter ramps 0.42 J per tick, so energy is
    # strictly positive over any window.
    assert trace[0].energy["cpu/package/0/energy"] > 0
