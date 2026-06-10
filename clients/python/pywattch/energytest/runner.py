"""Execute energy tests and report joules per test."""

from __future__ import annotations

import statistics
import traceback
from dataclasses import dataclass, field

from ..client import DEFAULT_SOCKET_PATH
from ..meter import EnergyMeter
from .discovery import DiscoveredTest

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"
STATUS_OVER_BUDGET = "over_budget"


@dataclass
class RunMeasurement:
    wall_time_s: float
    energy_joules: dict[str, float]
    total_joules: float

    def to_dict(self) -> dict:
        return {
            "wall_time_s": self.wall_time_s,
            "energy_joules": self.energy_joules,
            "total_joules": self.total_joules,
        }


@dataclass
class TestResult:
    test_id: str
    file: str
    name: str
    line: int
    status: str
    budget_joules: float | None
    repeats: int
    wall_time_s: float = 0.0
    energy_joules: dict[str, float] = field(default_factory=dict)
    total_joules: float = 0.0
    error: str | None = None
    runs: list[RunMeasurement] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.test_id,
            "file": self.file,
            "name": self.name,
            "line": self.line,
            "status": self.status,
            "budget_joules": self.budget_joules,
            "repeats": self.repeats,
            "wall_time_s": self.wall_time_s,
            "energy_joules": self.energy_joules,
            "total_joules": self.total_joules,
            "error": self.error,
            "runs": [r.to_dict() for r in self.runs],
        }


def _median_run(runs: list[RunMeasurement]) -> RunMeasurement:
    """The run with the median total energy (ties: lower index)."""
    totals = [r.total_joules for r in runs]
    median = statistics.median_low(totals)
    return runs[totals.index(median)]


def run_one(
    test: DiscoveredTest,
    socket_path: str = DEFAULT_SOCKET_PATH,
    interval_ms: int = 10,
    repeat_override: int | None = None,
) -> TestResult:
    repeats = repeat_override if repeat_override is not None else test.repeat
    result = TestResult(
        test_id=test.test_id,
        file=test.file,
        name=test.name,
        line=test.line,
        status=STATUS_PASSED,
        budget_joules=test.budget_joules,
        repeats=repeats,
    )

    for _ in range(repeats):
        meter = EnergyMeter(socket_path=socket_path, interval_ms=interval_ms)
        try:
            meter.start(tag=test.name)
        except Exception:
            result.status = STATUS_ERROR
            result.error = traceback.format_exc(limit=10)
            return result

        try:
            test.function()
        except AssertionError:
            result.status = STATUS_FAILED
            result.error = traceback.format_exc(limit=10)
        except Exception:
            result.status = STATUS_ERROR
            result.error = traceback.format_exc(limit=10)
        finally:
            try:
                meter.stop()
            except Exception:
                if result.status == STATUS_PASSED:
                    result.status = STATUS_ERROR
                    result.error = traceback.format_exc(limit=10)

        if result.status in (STATUS_FAILED, STATUS_ERROR):
            return result

        trace = meter.get_trace()
        energy: dict[str, float] = {}
        for sample in trace:
            for domain, joules in sample.energy.items():
                energy[domain] = energy.get(domain, 0.0) + joules
        result.runs.append(
            RunMeasurement(
                wall_time_s=sum(s.duration_s for s in trace),
                energy_joules=energy,
                total_joules=sum(energy.values()),
            )
        )

    median = _median_run(result.runs)
    result.wall_time_s = median.wall_time_s
    result.energy_joules = median.energy_joules
    result.total_joules = median.total_joules

    if test.budget_joules is not None and result.total_joules > test.budget_joules:
        result.status = STATUS_OVER_BUDGET
        result.error = (
            f"median energy {result.total_joules:.6f} J exceeds "
            f"budget {test.budget_joules:.6f} J"
        )
    return result


def run_tests(
    tests: list[DiscoveredTest],
    socket_path: str = DEFAULT_SOCKET_PATH,
    interval_ms: int = 10,
    repeat_override: int | None = None,
) -> list[TestResult]:
    return [
        run_one(test, socket_path, interval_ms, repeat_override) for test in tests
    ]


def summarize(results: list[TestResult]) -> dict:
    summary = {
        "total": len(results),
        STATUS_PASSED: 0,
        STATUS_FAILED: 0,
        STATUS_ERROR: 0,
        STATUS_OVER_BUDGET: 0,
    }
    for result in results:
        summary[result.status] += 1
    return summary
