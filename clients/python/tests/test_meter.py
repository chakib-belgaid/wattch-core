import time

import pytest

from pywattch import EnergyContext, EnergyMeter, WattchError, measure_energy
from pywattch.meter import cumulative_delta, trapezoidal_joules


class TestCumulativeDelta:
    def test_plain_delta(self):
        assert cumulative_delta(100.0, 350.0, max_value=1000.0) == 250.0

    def test_wrap_correction(self):
        # Counter wrapped: 950 -> 50 with max 1000 means 100 consumed.
        assert cumulative_delta(950.0, 50.0, max_value=1000.0) == pytest.approx(100.0)

    def test_backwards_without_max_value_raises(self):
        with pytest.raises(WattchError, match="wrap"):
            cumulative_delta(950.0, 50.0, max_value=None)


class TestTrapezoidalJoules:
    def test_constant_power(self):
        # 10 W for 1 second = 10 J.
        points = [(0, 10.0), (500_000_000, 10.0), (1_000_000_000, 10.0)]
        assert trapezoidal_joules(points) == pytest.approx(10.0)

    def test_ramp(self):
        # 0 -> 10 W over 1 s = 5 J.
        points = [(0, 0.0), (1_000_000_000, 10.0)]
        assert trapezoidal_joules(points) == pytest.approx(5.0)

    def test_too_few_points(self):
        assert trapezoidal_joules([(0, 10.0)]) == 0.0


class TestEnergyMeter:
    def test_multi_tag_trace(self, fake_daemon):
        meter = EnergyMeter(fake_daemon.socket_path, interval_ms=5)
        meter.start(tag="phase1")
        time.sleep(0.05)
        meter.record(tag="phase2")
        time.sleep(0.05)
        meter.stop()

        trace = meter.get_trace()
        assert [s.tag for s in trace] == ["phase1", "phase2"]
        for sample in trace:
            assert sample.duration_s > 0.02
            # The fake package counter advances 1000 uJ per 5ms tick, so each
            # ~50ms phase must see energy in the right ballpark.
            assert sample.energy["cpu/package/0/energy"] > 0
            assert sample.energy["cpu/package/0/dram/energy"] > 0
            # GPU instant power (10 W constant) integrates to roughly
            # 10 W x duration.
            gpu = sample.estimated_energy.get("gpu/0/power")
            assert gpu is not None and gpu > 0

        # Total equals the sum over phases (no double counting).
        assert trace.total_joules == pytest.approx(
            sum(s.total_joules for s in trace)
        )

    def test_meter_domains(self, fake_daemon):
        meter = EnergyMeter(fake_daemon.socket_path, interval_ms=5)
        meter.start()
        meter.stop()
        assert set(meter.domains) == {
            "cpu/package/0/energy",
            "cpu/package/0/dram/energy",
        }

    def test_trace_requires_stop(self, fake_daemon):
        meter = EnergyMeter(fake_daemon.socket_path, interval_ms=5)
        meter.start()
        try:
            with pytest.raises(WattchError, match="stop"):
                meter.get_trace()
        finally:
            meter.stop()

    def test_trace_to_dict_shape(self, fake_daemon):
        with EnergyMeter(fake_daemon.socket_path, interval_ms=5) as meter:
            time.sleep(0.03)
        data = meter.get_trace().to_dict()
        assert "total_joules" in data
        assert data["samples"][0].keys() == {
            "tag",
            "duration_s",
            "energy_joules",
            "estimated_energy_joules",
            "total_joules",
        }


class TestDecoratorAndContext:
    def test_measure_energy_decorator_invokes_handler(self, fake_daemon):
        traces = []

        @measure_energy(handler=traces.append, socket_path=fake_daemon.socket_path, interval_ms=5)
        def busy():
            time.sleep(0.04)
            return 42

        assert busy() == 42
        assert len(traces) == 1
        sample = traces[0][0]
        assert sample.tag == "busy"
        assert sample.energy["cpu/package/0/energy"] > 0

    def test_energy_context_records_tags(self, fake_daemon):
        traces = []
        with EnergyContext(
            handler=traces.append,
            tag="setup",
            socket_path=fake_daemon.socket_path,
            interval_ms=5,
        ) as ctx:
            time.sleep(0.04)
            ctx.record(tag="work")
            time.sleep(0.04)

        assert [s.tag for s in traces[0]] == ["setup", "work"]
