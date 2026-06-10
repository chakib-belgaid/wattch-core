# Energy Tests (VSCode extension)

Unit tests for joules. This extension discovers *energy tests* in your
workspace, runs them through `pywattch` against a local Wattch daemon, and
shows per-test energy consumption in an **Energy Tests** tab — the familiar
test-explorer experience, with energy columns instead of just pass/fail.

## What is an energy test?

A zero-argument Python callable in a file matching `energy_test_*.py` or
`*_energy_test.py`:

```python
# energy_test_parsing.py
import time
from pywattch.energytest import energy_test

def energy_test_parse_corpus():          # by naming convention
    parse(load_corpus())

@energy_test(budget_joules=0.5, repeat=3)  # with an energy budget
def hot_path():
    transform(data)
```

A test passes when it raises nothing and its median measured energy stays
within its (optional) joule budget. Budgets make energy regressions fail CI
the way assertion failures do.

## Requirements

1. A running Wattch daemon:
   - `wattch-powerd --backend synthetic` for development on any OS,
   - `sudo wattch-powerd --backend linux-rapl` for real RAPL on Linux,
   - or `wattch-testd --scenario …` for deterministic values.
2. `pywattch` installed for the configured Python interpreter:
   `pip install pywattch` (or `pip install -e clients/python` from the
   wattch-core repository).

## Usage

- Open the **Energy Tests** view in the activity bar (lightning icon).
- **Run All** or run a single test from its inline button.
- Results open in the **Energy Tests** tab: status, wall time, total joules,
  and per-domain energy (package, DRAM, …) with budget verdicts.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `energyTests.pythonPath` | `python3` | Interpreter with pywattch installed |
| `energyTests.socketPath` | `/tmp/wattch-powerd.sock` | Wattch daemon socket |
| `energyTests.rootDir` | workspace root | Directory scanned for tests |
| `energyTests.intervalMs` | `10` | Sampling interval |
| `energyTests.repeat` | `0` | Override per-test repeat (0 = keep) |

## Development

```bash
npm install
npm run build     # tsc
npm test          # node --test against the runner-JSON model
```

Press F5 in VSCode to launch an Extension Development Host. All measurement
logic lives in Python (`python -m pywattch.energytest`); the extension only
shells out and renders the JSON report.
