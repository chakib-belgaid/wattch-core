"""Updated pyJoules-style energy measurement API.

Same concepts as pyJoules (meter, decorator, context manager, handlers) but
energy comes from a Wattch daemon socket instead of direct RAPL file reads,
so no elevated privileges are needed in the Python process.

Semantics: the meter snapshots the most recent daemon sample at each marker
(`start`/`record`/`stop`), so tag boundaries are quantized to the sampling
interval (`interval_ms`, default 10 ms). Cumulative energy counters become
wrap-corrected joule deltas; instant power sources (watts) are integrated
with the trapezoidal rule and reported as estimates.
"""

from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ._messages import RawSampleBatch, SourceDescriptor, StopCaptureAck
from .client import WattchClient
from .errors import WattchError

_ENERGY_UNIT_TO_JOULES = {"JOULE": 1.0, "MICROJOULE": 1e-6}


def cumulative_delta(start: float, end: float, max_value: float | None) -> float:
    """Wrap-corrected delta of a cumulative counter."""
    delta = end - start
    if delta < 0:
        if max_value is None or max_value <= 0:
            raise WattchError(
                f"cumulative counter went backwards ({start} -> {end}) "
                "and the source declares no max_value for wrap correction"
            )
        delta += max_value
    return delta


def trapezoidal_joules(points: list[tuple[int, float]]) -> float:
    """Integrate (monotonic_ns, watts) points into joules."""
    total = 0.0
    for (t0, w0), (t1, w1) in zip(points, points[1:]):
        total += (w0 + w1) / 2.0 * (t1 - t0) / 1e9
    return total


@dataclass(frozen=True)
class EnergySample:
    """Energy attributed to one tagged phase."""

    tag: str
    duration_s: float
    energy: dict[str, float]
    """Joules per energy domain (source_path) from cumulative counters."""
    estimated_energy: dict[str, float] = field(default_factory=dict)
    """Joules per domain integrated from instant power sources (estimates)."""

    @property
    def total_joules(self) -> float:
        return sum(self.energy.values())


@dataclass(frozen=True)
class EnergyTrace:
    samples: tuple[EnergySample, ...]

    def __iter__(self):
        return iter(self.samples)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        return self.samples[index]

    @property
    def total_joules(self) -> float:
        return sum(sample.total_joules for sample in self.samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": [
                {
                    "tag": s.tag,
                    "duration_s": s.duration_s,
                    "energy_joules": s.energy,
                    "estimated_energy_joules": s.estimated_energy,
                    "total_joules": s.total_joules,
                }
                for s in self.samples
            ],
            "total_joules": self.total_joules,
        }


@dataclass
class _Marker:
    tag: str
    wall_s: float
    cumulative: dict[int, float]
    monotonic_ns: int
    instant_joules: dict[int, float]


class EnergyMeter:
    """Measure energy of tagged code phases through a Wattch daemon.

    Usage::

        meter = EnergyMeter()  # auto-detects the daemon socket
        meter.start(tag="setup")
        ...
        meter.record(tag="compute")
        ...
        meter.stop()
        trace = meter.get_trace()
    """

    def __init__(
        self,
        socket_path: str | None = None,
        interval_ms: int = 10,
        source_ids: list[int] | None = None,
        first_sample_timeout_s: float = 5.0,
    ):
        self.socket_path = socket_path
        self.interval_ms = interval_ms
        self.source_ids = source_ids
        self.first_sample_timeout_s = first_sample_timeout_s

        self._client: WattchClient | None = None
        self._reader: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._first_batch = threading.Event()
        self._lock = threading.Lock()
        self._reader_error: Exception | None = None
        self._stop_ack: StopCaptureAck | None = None

        self._sources: dict[int, SourceDescriptor] = {}
        self._latest: dict[int, float] = {}
        self._latest_ns: int = 0
        self._instant_last: dict[int, tuple[int, float]] = {}
        self._instant_joules: dict[int, float] = {}
        self._markers: list[_Marker] = []
        self._trace: EnergyTrace | None = None
        self._run_id = ""

    # -- lifecycle ----------------------------------------------------------

    def start(self, tag: str = "start") -> None:
        if self._client is not None:
            raise WattchError("meter already started")
        self._trace = None
        self._markers = []
        self._stop_requested.clear()
        self._first_batch.clear()
        self._reader_error = None
        self._stop_ack = None
        self._latest = {}
        self._latest_ns = 0
        self._run_id = f"pywattch-meter-{time.time_ns()}"

        self._client = WattchClient(self.socket_path, client_name="pywattch-meter")
        try:
            self._sources = {s.local_source_id: s for s in self._client.sources}
            self._instant_last = {}
            self._instant_joules = {
                sid: 0.0
                for sid, s in self._sources.items()
                if s.temporality == "INSTANT" and s.unit == "WATT"
            }
            self._client.start_capture(self._run_id, self.interval_ms, self.source_ids)
        except BaseException:
            self._client.close()
            self._client = None
            raise
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        if not self._first_batch.wait(self.first_sample_timeout_s):
            self.stop()
            raise WattchError(
                f"no samples from daemon within {self.first_sample_timeout_s}s"
            )
        self._mark(tag)

    def record(self, tag: str) -> None:
        if self._client is None:
            raise WattchError("meter is not running")
        self._mark(tag)

    def stop(self) -> None:
        if self._client is None:
            return
        # Wait for one sample taken after this instant so the final marker
        # covers all work done before stop(). Without this, phases shorter
        # than the sampling interval would report 0 J; with it they may
        # include up to one extra interval of idle energy instead.
        self._wait_for_fresh_sample()
        self._mark("__stop__")
        self._stop_requested.set()
        if self._reader is not None:
            self._reader.join(timeout=10)
            if self._reader.is_alive():
                self._client.close()
                self._reader.join(timeout=1)
                if self._reader_error is None:
                    self._reader_error = WattchError(
                        "capture reader did not stop before the deadline"
                    )
        self._client.close()
        self._client = None
        self._reader = None
        if self._reader_error is not None:
            error = self._reader_error
            self._reader_error = None
            raise WattchError(f"capture reader failed: {error}") from error

    def __enter__(self) -> "EnergyMeter":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- results -------------------------------------------------------------

    def get_trace(self) -> EnergyTrace:
        if self._client is not None:
            raise WattchError("stop the meter before reading the trace")
        if self._trace is None:
            self._trace = self._build_trace()
        return self._trace

    @property
    def domains(self) -> list[str]:
        return [
            s.source_path
            for s in self._sources.values()
            if s.temporality == "CUMULATIVE" and s.unit in _ENERGY_UNIT_TO_JOULES
        ]

    # -- internals -----------------------------------------------------------

    def _wait_for_fresh_sample(self) -> None:
        with self._lock:
            seen_ns = self._latest_ns
        deadline = time.monotonic() + max(3 * self.interval_ms / 1000.0, 0.1)
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest_ns > seen_ns:
                    return
            time.sleep(self.interval_ms / 2000.0)

    def _mark(self, tag: str) -> None:
        with self._lock:
            self._markers.append(
                _Marker(
                    tag=tag,
                    wall_s=time.perf_counter(),
                    cumulative=dict(self._latest),
                    monotonic_ns=self._latest_ns,
                    instant_joules=dict(self._instant_joules),
                )
            )

    def _read_loop(self) -> None:
        assert self._client is not None
        client = self._client
        try:
            stop_sent = False
            while True:
                if self._stop_requested.is_set() and not stop_sent:
                    ack, drained = client.stop_capture(self._run_id)
                    for batch in drained:
                        self._ingest(batch)
                    self._stop_ack = ack
                    return
                message = client.read_batch()
                if isinstance(message, StopCaptureAck):
                    self._stop_ack = message
                    return
                self._ingest(message)
                self._first_batch.set()
        except Exception as exc:  # surfaced on stop()
            self._reader_error = exc
            self._first_batch.set()

    def _ingest(self, batch: RawSampleBatch) -> None:
        with self._lock:
            for sample in batch.samples:
                if sample.source_id not in self._sources:
                    raise WattchError(
                        f"daemon sent sample for unknown source id {sample.source_id}"
                    )
                value = sample.numeric_value
                self._latest[sample.source_id] = value
                self._latest_ns = max(self._latest_ns, sample.monotonic_ns)
                if sample.source_id in self._instant_joules:
                    previous = self._instant_last.get(sample.source_id)
                    if previous is not None:
                        previous_ns, previous_watts = previous
                        self._instant_joules[sample.source_id] += (
                            (previous_watts + value)
                            / 2.0
                            * (sample.monotonic_ns - previous_ns)
                            / 1e9
                        )
                    self._instant_last[sample.source_id] = (
                        sample.monotonic_ns,
                        value,
                    )

    def _build_trace(self) -> EnergyTrace:
        samples: list[EnergySample] = []
        for begin, end in zip(self._markers, self._markers[1:]):
            energy: dict[str, float] = {}
            for source_id, descriptor in self._sources.items():
                if descriptor.temporality != "CUMULATIVE":
                    continue
                scale = _ENERGY_UNIT_TO_JOULES.get(descriptor.unit)
                if scale is None:
                    continue
                start_value = begin.cumulative.get(source_id)
                end_value = end.cumulative.get(source_id)
                if start_value is None or end_value is None:
                    continue
                delta = cumulative_delta(start_value, end_value, descriptor.max_value)
                energy[descriptor.source_path] = delta * scale

            estimated: dict[str, float] = {}
            for source_id in self._instant_joules:
                descriptor = self._sources[source_id]
                begin_joules = begin.instant_joules.get(source_id, 0.0)
                end_joules = end.instant_joules.get(source_id, begin_joules)
                if end_joules > begin_joules:
                    estimated[descriptor.source_path] = end_joules - begin_joules

            samples.append(
                EnergySample(
                    tag=begin.tag,
                    duration_s=end.wall_s - begin.wall_s,
                    energy=energy,
                    estimated_energy=estimated,
                )
            )
        return EnergyTrace(samples=tuple(samples))


class EnergyContext:
    """Context manager mirroring pyJoules' EnergyContext.

    Usage::

        with EnergyContext(handler=PrintHandler(), tag="phase1") as ctx:
            work_one()
            ctx.record(tag="phase2")
            work_two()
    """

    def __init__(
        self,
        handler: Callable[[EnergyTrace], None] | Any = None,
        tag: str = "start",
        socket_path: str | None = None,
        interval_ms: int = 10,
    ):
        self.handler = handler
        self.tag = tag
        self.meter = EnergyMeter(socket_path=socket_path, interval_ms=interval_ms)

    def record(self, tag: str) -> None:
        self.meter.record(tag)

    def __enter__(self) -> "EnergyContext":
        self.meter.start(tag=self.tag)
        return self

    def __exit__(self, *exc_info) -> None:
        self.meter.stop()
        if self.handler is not None:
            trace = self.meter.get_trace()
            process = getattr(self.handler, "process", self.handler)
            process(trace)


def measure_energy(
    func: Callable | None = None,
    *,
    handler: Any = None,
    socket_path: str | None = None,
    interval_ms: int = 10,
    tag: str | None = None,
) -> Callable:
    """Decorator mirroring pyJoules' measure_energy.

    Usage::

        @measure_energy
        def work(): ...

        @measure_energy(handler=CSVHandler("energy.csv"))
        def work(): ...
    """

    def decorate(target: Callable) -> Callable:
        @functools.wraps(target)
        def wrapper(*args, **kwargs):
            meter = EnergyMeter(socket_path=socket_path, interval_ms=interval_ms)
            meter.start(tag=tag or target.__name__)
            try:
                return target(*args, **kwargs)
            finally:
                meter.stop()
                trace = meter.get_trace()
                if handler is not None:
                    process = getattr(handler, "process", handler)
                    process(trace)
                else:
                    from .handlers import PrintHandler

                    PrintHandler().process(trace)

        return wrapper

    if func is not None:
        return decorate(func)
    return decorate


def domains_of(sources: Iterable[SourceDescriptor]) -> list[str]:
    """Energy domains (cumulative joule/microjoule sources) of a daemon."""
    return [
        s.source_path
        for s in sources
        if s.temporality == "CUMULATIVE" and s.unit in _ENERGY_UNIT_TO_JOULES
    ]
