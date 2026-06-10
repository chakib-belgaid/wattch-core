# Energy tests

Energy tests are unit-test-shaped checks that measure — and optionally
bound — the energy a piece of Python code consumes. They are the highest
layer of the Wattch stack:

```text
VSCode "Energy Tests" extension
  -> python -m pywattch.energytest (discovery + runner, JSON report)
       -> pywattch.EnergyMeter (pyJoules-style client API)
            -> Wattch daemon socket (wattch-powerd / wattch-testd)
                 -> raw counters (RAPL on Linux, synthetic elsewhere)
```

## Writing tests

Files matching `energy_test_*.py` or `*_energy_test.py`; tests are
zero-argument callables that are either named `energy_test_*` or decorated:

```python
import time
from pywattch.energytest import energy_test

def energy_test_parse_corpus():
    parse(load_corpus())

@energy_test(budget_joules=0.5, repeat=3)
def hot_path():
    transform(data)
```

Statuses:

```text
passed       no exception, within budget (if any)
failed       AssertionError raised
error        any other exception (including daemon unreachable)
over_budget  median energy across repeats exceeds budget_joules
```

`repeat` runs the test N times and reports the run with the median total
energy, which damps scheduler noise on real hardware.

## Running

```bash
# daemon (pick one)
cargo run -p wattch-powerd -- --backend synthetic          # any OS, no privilege
sudo wattch-powerd --backend linux-rapl                    # Linux RAPL
cargo run -p wattch-testd -- --scenario examples/scenarios/laptop_cpu_gpu_nested.json

# tests
python -m pywattch.energytest discover --root examples/energy-tests
python -m pywattch.energytest run --root examples/energy-tests \
    --socket /tmp/wattch-powerd.sock --json
```

The runner exits non-zero when anything failed, errored, or blew its
budget, so it drops into CI as-is.

## Measurement semantics (be honest about them)

- Energy comes from cumulative hardware counters read by the daemon at the
  sampling interval (default 10 ms). Test boundaries are quantized to that
  interval; the meter waits for one post-test sample, so very short tests
  report at most one extra interval of idle energy rather than 0 J.
- On non-Linux dev machines the synthetic backend produces plausible but
  fake values — good for wiring and UI, meaningless for claims.
- Budgets compare the median across repeats. For regression gating on real
  hardware, pin the machine, close the lid, and use repeats >= 3.

## VSCode

The `editors/vscode-energy-tests` extension shows discovered tests in an
**Energy Tests** activity-bar view and renders the runner report in an
**Energy Tests** tab: status, wall time, total joules, per-domain joules,
budget verdict. See `editors/vscode-energy-tests/README.md`.
