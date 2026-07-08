# pywattch

Energy measurement for Python, backed by a Wattch daemon. This is the
updated pyJoules-style client: the API keeps pyJoules' concepts (meter,
decorator, context manager, handlers) but reads energy through the Wattch
protocol socket instead of touching RAPL files, so Python code never needs
elevated privileges.

```python
from pywattch import EnergyMeter, EnergyContext, measure_energy
from pywattch.handlers import PrintHandler, CSVHandler

# Explicit meter
meter = EnergyMeter()
meter.start(tag="setup")
load_data()
meter.record(tag="compute")
compute()
meter.stop()
for sample in meter.get_trace():
    print(sample.tag, sample.total_joules)

# Decorator
@measure_energy(handler=CSVHandler("energy.csv"))
def train_epoch(): ...

# Context manager
with EnergyContext(handler=PrintHandler(), tag="phase1") as ctx:
    work_one()
    ctx.record(tag="phase2")
    work_two()
```

Extras:

- `pywattch.profiler.EnergyProfiler` — pyinstrument call-tree profiling with
  proportional energy attribution (install with `pip install pywattch[profiler]`).
- `python -m pywattch.energytest` — discover and run energy tests
  (unit tests that assert on joules), used by the VSCode Energy Tests
  extension.

## Daemon

Start one of the Wattch daemons first:

```bash
wattch-powerd --backend synthetic       # dev
sudo wattch-powerd --backend linux-rapl # Linux RAPL
```

## Semantics

- Tag boundaries are quantized to the sampling interval (default 10 ms).
- Cumulative counters (RAPL-style) become wrap-corrected joule deltas —
  these are the `energy` values.
- Instant power sources (watts) are integrated trapezoidally and reported
  separately as `estimated_energy`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,profiler]'
.venv/bin/python -m pytest tests
```
