"""JouleHunter-style energy profiling: pyinstrument + Wattch.

`EnergyProfiler` runs a pyinstrument sampling profiler and a Wattch
`EnergyMeter` over the same interval, then attributes the measured joules
across the call tree proportionally to time. The attribution is an
estimate (MODEL_DERIVED_ESTIMATE in Wattch terms): time share is a model,
not a per-function hardware measurement — output is labelled accordingly.

Requires the `profiler` extra: ``pip install pywattch[profiler]``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .errors import WattchError
from .meter import EnergyMeter

ATTRIBUTION_LABEL = "time_share_estimate"


@dataclass
class FrameEnergy:
    """One call-tree node with attributed energy."""

    function: str
    location: str
    time_s: float
    self_time_s: float
    children: list["FrameEnergy"] = field(default_factory=list)
    self_joules: float = 0.0
    total_joules: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "location": self.location,
            "time_s": self.time_s,
            "self_time_s": self.self_time_s,
            "self_joules": self.self_joules,
            "total_joules": self.total_joules,
            "children": [c.to_dict() for c in self.children],
        }


def attribute_energy(root: FrameEnergy, total_joules: float) -> None:
    """Distribute `total_joules` over the tree proportionally to time.

    Every node gets `total_joules * time / root.time` as its subtree energy
    and `total_joules * self_time / root.time` as its own share.
    """
    total_time = root.time_s
    if total_time <= 0:
        return

    def visit(node: FrameEnergy) -> None:
        node.total_joules = total_joules * node.time_s / total_time
        node.self_joules = total_joules * node.self_time_s / total_time
        for child in node.children:
            visit(child)

    visit(root)


def render_frame_tree(root: FrameEnergy, min_fraction: float = 0.001) -> str:
    """pyinstrument-style tree with joules per frame."""
    lines: list[str] = []
    threshold = root.total_joules * min_fraction

    def visit(node: FrameEnergy, depth: int) -> None:
        if root.total_joules > 0 and node.total_joules < threshold:
            return
        percent = (
            100.0 * node.total_joules / root.total_joules
            if root.total_joules > 0
            else 0.0
        )
        indent = "  " * depth
        lines.append(
            f"{node.total_joules:10.4f} J {percent:5.1f}%  "
            f"{indent}{node.function}  ({node.location})"
        )
        for child in sorted(
            node.children, key=lambda c: c.total_joules, reverse=True
        ):
            visit(child, depth + 1)

    visit(root, 0)
    return "\n".join(lines)


def _convert_pyinstrument_frame(frame) -> FrameEnergy:
    children = [
        _convert_pyinstrument_frame(child)
        for child in frame.children
        if not child.is_synthetic
    ]
    location = (
        f"{frame.file_path_short}:{frame.line_no}"
        if frame.file_path_short
        else "<unknown>"
    )
    return FrameEnergy(
        function=frame.function,
        location=location,
        time_s=frame.time,
        self_time_s=frame.total_self_time,
        children=children,
    )


class EnergyProfiler:
    """Profile Python code and attribute measured energy to its call tree.

    Usage::

        with EnergyProfiler() as ep:  # auto-detects the daemon socket
            workload()
        print(ep.render_text())
        report = ep.to_dict()
    """

    def __init__(
        self,
        socket_path: str | None = None,
        interval_ms: int = 10,
        profiler_interval_s: float = 0.001,
    ):
        try:
            from pyinstrument import Profiler
        except ImportError as exc:
            raise WattchError(
                "pyinstrument is required for EnergyProfiler; "
                "install with: pip install pywattch[profiler]"
            ) from exc

        self._profiler = Profiler(interval=profiler_interval_s, async_mode="disabled")
        self._meter = EnergyMeter(socket_path=socket_path, interval_ms=interval_ms)
        self._root: FrameEnergy | None = None
        self._energy_by_domain: dict[str, float] = {}
        self._duration_s = 0.0
        self._started_at = 0.0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._root = None
        self._meter.start(tag="profile")
        self._started_at = time.perf_counter()
        self._profiler.start()

    def stop(self) -> None:
        self._profiler.stop()
        self._duration_s = time.perf_counter() - self._started_at
        self._meter.stop()

        trace = self._meter.get_trace()
        self._energy_by_domain = {}
        for sample in trace:
            for domain, joules in sample.energy.items():
                self._energy_by_domain[domain] = (
                    self._energy_by_domain.get(domain, 0.0) + joules
                )

        session = self._profiler.last_session
        root_frame = session.root_frame() if session else None
        if root_frame is None:
            self._root = FrameEnergy(
                function="<no samples>",
                location="<none>",
                time_s=self._duration_s,
                self_time_s=self._duration_s,
            )
        else:
            self._root = _convert_pyinstrument_frame(root_frame)
        attribute_energy(self._root, self.total_joules)

    def __enter__(self) -> "EnergyProfiler":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- results ---------------------------------------------------------------

    @property
    def total_joules(self) -> float:
        return sum(self._energy_by_domain.values())

    @property
    def energy_by_domain(self) -> dict[str, float]:
        return dict(self._energy_by_domain)

    @property
    def root(self) -> FrameEnergy:
        if self._root is None:
            raise WattchError("profiler has not finished a run yet")
        return self._root

    def render_text(self, min_fraction: float = 0.001) -> str:
        header = (
            f"Energy profile: {self.total_joules:.4f} J over "
            f"{self._duration_s:.3f} s "
            f"(attribution: {ATTRIBUTION_LABEL}, not a per-function measurement)\n"
            + "\n".join(
                f"  {domain}: {joules:.4f} J"
                for domain, joules in sorted(self._energy_by_domain.items())
            )
        )
        return header + "\n\n" + render_frame_tree(self.root, min_fraction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_joules": self.total_joules,
            "duration_s": self._duration_s,
            "energy_by_domain": self.energy_by_domain,
            "attribution": ATTRIBUTION_LABEL,
            "root": self.root.to_dict(),
        }
