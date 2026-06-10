"""Demo energy tests for the VSCode Energy Tests extension.

Run them with a daemon up, e.g.:

    cargo run -p wattch-powerd -- --backend synthetic
    python -m pywattch.energytest run --root examples/energy-tests --json
"""

import time

from pywattch.energytest import energy_test


def energy_test_sorting():
    data = list(range(50_000, 0, -1))
    for _ in range(20):
        sorted(data)


@energy_test(repeat=3)
def string_building():
    chunks = []
    for i in range(200_000):
        chunks.append(str(i))
    "".join(chunks)


@energy_test(budget_joules=50.0)
def busy_loop_within_budget():
    deadline = time.perf_counter() + 0.2
    while time.perf_counter() < deadline:
        pass
