---
name: wattch-energy-optimize
description: Use when an agent must measure and optimize a local code workload with Wattch through MCP or CLI, preserve auditable raw evidence, and compare correctness-preserving implementations without overstating synthetic, shared-system, or remote model-inference energy.
---

# Wattch Energy Optimize

Improve one local workload through measured, correctness-preserving iterations. Treat raw artifacts and source semantics as the evidence; never infer a stronger boundary than Wattch observed.

## Establish the contract

Before editing, freeze:

- workload command, inputs, useful-work count, and correctness verifier;
- repository revision and mutable files;
- daemon socket, sampling interval, output root, and run-id pattern;
- one primary source boundary and the requested repetitions;
- exact candidate SHA-256 plus valid before/after mutation-scope records;
- the baseline/candidate order or a recorded counterbalancing seed.

Do not let the code under test choose the authoritative measurement window.
Before launching a comparison matrix, capture one source-qualification pilot.
Proceed only when the selected source, native cadence, marker coverage, and
quality gates are usable for the frozen workload.

## Qualify the source

Call `wattch_sources` first. If MCP is unavailable, run `wattch sources` and retain `sources.json` from the first artifact.

Inspect `backend`, `sampling_mode`, `evidence_class`, `unit`, `temporality`, source identity, and parent topology. Resolve this skill's directory, then run `python3 <skill-directory>/scripts/check_evidence.py RUN_DIR` after capture. Add `--source-uid` and the coordinator-owned `--trial-record` when they exist.

- `synthetic` or test-daemon values prove plumbing only. Do not rank implementations by physical energy from them.
- Local RAPL or powermetrics sources observe shared domains, not a process-private meter.
- Local Codex capture excludes Luna/Terra inference energy in the provider datacenter.
- Never sum nested RAPL domains or CPU/GPU/ANE unless a declared boundary proves a disjoint aggregate.

Read [references/measurement-integrity.md](references/measurement-integrity.md) before making a comparative claim.

## Measure, change, repeat

1. Run the correctness verifier and one warm-up.
2. Capture a baseline with the frozen command.
3. Make one narrow optimization whose mechanism you can explain.
4. Run the same verifier before inspecting performance.
5. Capture the candidate with the same command, input, source, and interval.
6. Make each measured window span at least ten native source updates; faster
   client polling does not create independent hardware samples.
7. Repeat sequentially and counterbalance order; do not overlap shared-domain trials.
8. Preserve failed runs and raw artifacts instead of silently excluding them.

Use one of the exact workflows in [references/cli-mcp.md](references/cli-mcp.md). Always call `wattch_capture_stop` in a cleanup path after a successful start. Treat `wattch_inspect` as integrity evidence, not a joule calculator.

## Decide and report

Correctness is a gate. If outputs or useful work differ, do not call the candidate an optimization.

Prefer repeated single-source joules only when every explicit source, artifact, correctness, quality, calibration, trace, and hash gate is eligible. Schema-v1 calibration authorizes only an exploratory `descriptive_local_run_eligible` result; it never sets physical or confirmatory claim flags. Eligibility fails closed when a gate is absent. Otherwise report duration, correctness, raw artifact paths, and plumbing status without inventing energy. Separate agent-workflow evidence from generated-code workload evidence.

Use [references/result-contract.md](references/result-contract.md) and [templates/result.md](templates/result.md). Include run IDs, source UID, boundary, backend, evidence class, marker window, repetitions, statistic, raw values, uncertainty, failures, and limitations.
