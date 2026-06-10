"""Fixture energy tests covering every status."""

import time

from pywattch.energytest import energy_test


def energy_test_by_name():
    time.sleep(0.02)


@energy_test(budget_joules=1000.0)
def decorated_within_budget():
    time.sleep(0.02)


@energy_test(budget_joules=0.0)
def decorated_over_budget():
    time.sleep(0.02)


@energy_test
def decorated_failing():
    assert 1 == 2, "intentional assertion failure"


@energy_test
def decorated_erroring():
    raise RuntimeError("intentional error")


def not_a_test():
    """Neither named energy_test_* nor decorated; must not be collected."""
