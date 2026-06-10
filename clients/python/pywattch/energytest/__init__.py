"""Energy tests: unit-test-shaped checks that measure (and bound) joules.

A test is a zero-argument callable that either:

- lives in a file matching ``energy_test_*.py`` or ``*_energy_test.py`` and
  is named ``energy_test_*``, or
- is decorated with :func:`energy_test` (any name, same file patterns).

A test *passes* when it raises nothing and, if it declares
``budget_joules``, its median measured energy stays within that budget.
That budget is what turns a measurement into an assertion.

Run them with ``python -m pywattch.energytest run --root <dir> --json``.
"""

from typing import Callable

ATTR_MARK = "_wattch_energy_test"
ATTR_BUDGET = "_wattch_budget_joules"
ATTR_REPEAT = "_wattch_repeat"


def energy_test(
    func: Callable | None = None,
    *,
    budget_joules: float | None = None,
    repeat: int = 1,
) -> Callable:
    """Mark a callable as an energy test.

    Usage::

        @energy_test
        def parse_corpus(): ...

        @energy_test(budget_joules=0.5, repeat=3)
        def hot_path(): ...
    """

    def mark(target: Callable) -> Callable:
        setattr(target, ATTR_MARK, True)
        setattr(target, ATTR_BUDGET, budget_joules)
        setattr(target, ATTR_REPEAT, repeat)
        return target

    if func is not None:
        return mark(func)
    return mark


from .discovery import DiscoveredTest, discover_tests  # noqa: E402
from .runner import TestResult, run_tests  # noqa: E402

__all__ = [
    "DiscoveredTest",
    "TestResult",
    "discover_tests",
    "energy_test",
    "run_tests",
]
