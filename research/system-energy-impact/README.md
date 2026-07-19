# Wattch context-dependent energy-impact paper

This directory contains the paper, its generated figures, and the preliminary
evidence used in the manuscript.

## Evidence status

- The deterministic scenarios verify estimand calculations and fault
  observability against planted truths.
- The polling-versus-batching pilot verifies output equivalence, work
  completion, backlog drain, schedule balance, and distinct wait behavior.
- No real meter-backed energy result is present. The local Apple
  `powermetrics` backend required a privileged daemon that was not available
  non-interactively.

The artifacts therefore do not confirm or refute the physical ranking-reversal
hypothesis.

## Reproduce the preliminary checks

Build the deterministic daemon, then run the driver. By default, each rerun is
written to a fresh timestamped directory below `results/reruns/`.

```bash
cargo build -p wattch-testd
clients/python/.venv/bin/python \
  research/system-energy-impact/experiments/run_preliminary_validation.py \
  --blocks 12
```

To choose a destination explicitly, pass a directory that does not already
exist:

```bash
clients/python/.venv/bin/python \
  research/system-energy-impact/experiments/run_preliminary_validation.py \
  --blocks 12 \
  --output-dir /tmp/wattch-preliminary-rerun
```

## Build the paper

The builder reads `paper.md` and the checked-in JSON summaries, regenerates all
three figures, and writes the final DOCX.

```bash
/Users/chakib/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  research/system-energy-impact/build_paper.py
```

Primary artifacts:

- `Wattch_Context_Dependent_Energy_Impact.docx`: formatted manuscript.
- `paper.md`: editable manuscript source.
- `results/deterministic_validation.json`: planted-truth verification.
- `results/workload_pilot.json`: 72-trial workload-admissibility pilot.
- `results/live_protocol/deterministic-live-reversal/`: socket capture smoke
  artifact.

The next empirical stage requires a separately launched privileged
`wattch-powerd` daemon, a frozen RAPL or `powermetrics` measurement boundary,
meter calibration, a discarded pilot, sealed preregistration, and independent
confirmatory blocks.
