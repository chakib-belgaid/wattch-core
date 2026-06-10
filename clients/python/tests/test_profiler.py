import time

import pytest

from pywattch.profiler import (
    EnergyProfiler,
    FrameEnergy,
    attribute_energy,
    render_frame_tree,
)


def synthetic_tree() -> FrameEnergy:
    return FrameEnergy(
        function="main",
        location="app.py:1",
        time_s=1.0,
        self_time_s=0.0,
        children=[
            FrameEnergy(
                function="hot_loop", location="app.py:10", time_s=0.6, self_time_s=0.6
            ),
            FrameEnergy(
                function="io_wait", location="app.py:20", time_s=0.4, self_time_s=0.4
            ),
        ],
    )


class TestAttribution:
    def test_proportional_to_time_share(self):
        root = synthetic_tree()
        attribute_energy(root, total_joules=10.0)

        assert root.total_joules == pytest.approx(10.0)
        assert root.self_joules == pytest.approx(0.0)
        hot, io = root.children
        assert hot.total_joules == pytest.approx(6.0)
        assert hot.self_joules == pytest.approx(6.0)
        assert io.total_joules == pytest.approx(4.0)
        # Children account for everything the parent did not do itself.
        assert hot.total_joules + io.total_joules == pytest.approx(
            root.total_joules - root.self_joules
        )

    def test_zero_duration_attributes_nothing(self):
        root = FrameEnergy(function="f", location="x:1", time_s=0.0, self_time_s=0.0)
        attribute_energy(root, total_joules=5.0)
        assert root.total_joules == 0.0


class TestRenderTree:
    def test_renders_joules_and_percentages(self):
        root = synthetic_tree()
        attribute_energy(root, total_joules=10.0)
        text = render_frame_tree(root)
        lines = text.splitlines()
        assert "main" in lines[0] and "100.0%" in lines[0]
        # Children sorted by joules: hot_loop first.
        assert "hot_loop" in lines[1] and "60.0%" in lines[1]
        assert "io_wait" in lines[2] and "40.0%" in lines[2]

    def test_prunes_negligible_frames(self):
        root = synthetic_tree()
        root.children.append(
            FrameEnergy(function="noise", location="x:1", time_s=0.0001, self_time_s=0.0001)
        )
        root.time_s = 1.0001
        attribute_energy(root, total_joules=10.0)
        assert "noise" not in render_frame_tree(root, min_fraction=0.01)


def busy_wait(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        pass


class TestEnergyProfilerIntegration:
    def test_profiles_against_fake_daemon(self, fake_daemon):
        with EnergyProfiler(fake_daemon.socket_path, interval_ms=5) as ep:
            busy_wait(0.08)

        assert ep.total_joules > 0
        assert set(ep.energy_by_domain) == {
            "cpu/package/0/energy",
            "cpu/package/0/dram/energy",
        }
        assert ep.root.total_joules == pytest.approx(ep.total_joules)

        text = ep.render_text()
        assert "busy_wait" in text
        assert "time_share_estimate" in text

        report = ep.to_dict()
        assert report["attribution"] == "time_share_estimate"
        assert report["total_joules"] == pytest.approx(ep.total_joules)
        assert report["root"]["children"], "expected at least one profiled frame"
