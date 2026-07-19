# Polling versus batching workload

`polling_batching_worker.py` is Wattch's first runnable experimental workload.
It does not encode or imply an energy result. Its only intended treatment
difference is the consumer strategy:

- `--mode polling` makes non-blocking pipe reads in a tight loop and processes
  each arrival immediately.
- `--mode batching` blocks in `select`, accumulates `--batch-size` arrivals,
  and then processes that batch.

Both modes generate the same task set and the same logical arrival trace from
`WATTCH_INPUT_ID` and `WATTCH_REPLAY_SEED`. A small, explicitly implemented
SplitMix64 shuffle makes the trace reproducible without depending on Python's
random-module implementation. The task computation is pure SHA-256 work. The
final results are sorted by task id before serialization, so arrival order does
not change the program output.

The arrival producer is a separate POSIX process, not a Python thread. A busy
polling consumer therefore cannot delay it through the GIL. Each record carries
both its scheduled and actual publication time. The metrics include
producer-lateness percentiles and `arrival_trace_valid`; `slo_met` is false
when the maximum lateness exceeds `--producer-lateness-limit-ms`. A trial
harness must reject such a run instead of treating a delayed offered trace as
equivalent demand.

The checked-in case declares this as an arrival-paced `BATCH` workload. The
producer remains inside the launched process tree and inherits its placement
and priority, so it is not evidence of an independently driven open-loop
service. A future service experiment must use an external request generator
with separately frozen placement and must still validate offered-arrival
lateness.

`--mode sham` is available for `C0`. It uses the same producer and blocking
arrival drain but skips the SHA-256 task computation. Its stdout intentionally
differs from the two real variants and must not be included in their output
equivalence assertion. This control keeps launcher and synthetic-arrival
harness costs present without pretending that `C0` executed either program.

## Output and metrics contract

Stdout contains one canonical JSON record followed by `\n`. It contains only
the deterministic work result. The exact bytes from the polling and batching
commands are identical, and their SHA-256 is suitable for
`expected_output_sha256`.

Runtime observations are excluded from stdout. By default, stderr contains one
compact JSON record followed by `\n`; Wattch preserves that as the per-trial
`target.stderr.log`. During a Wattch trial, the runner sets
`WATTCH_METRICS_PATH` to the unique trial-local `target.metrics.json`, and the
worker creates that sidecar exclusively. An explicit `--metrics-path` takes
precedence over the environment. The sealed record maps into `WorkloadMetrics`
as follows:

```text
completed_work    -> completed_work
work_unit         -> work_unit
throughput_per_s  -> throughput_per_s
latency_ms        -> latency_ms
backlog_at_end    -> backlog_at_end
slo_met           -> slo_met
```

The record also preserves `arrival_trace_sha256`, `arrival_trace_valid`,
producer-lateness evidence, `output_sha256`, runtime, mode, read calls,
processed batches, and empty poll count. Supplying `--metrics-path PATH` (or
setting `WATTCH_METRICS_PATH`) writes that record to a new file; the command
fails rather than overwriting an existing file.

Example smoke run:

```bash
WATTCH_INPUT_ID=wattch-polling-batching-v1 \
WATTCH_REPLAY_SEED=20260719 \
python3 examples/workloads/polling_batching_worker.py \
  --mode polling --items 64 --arrival-interval-ms 1 --work-rounds 8
```

The checked-in benchmark uses a longer but bounded trace. Its observed timing
and energy must be established through calibrated, randomized Wattch trials;
the workload itself makes no comparative claim.

Run the focused tests with:

```bash
python3 -m unittest discover -s examples/workloads/tests -v
```
