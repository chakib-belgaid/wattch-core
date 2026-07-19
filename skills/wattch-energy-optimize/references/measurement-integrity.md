# Measurement integrity

## Claim boundary

Wattch records system/domain telemetry. It does not allocate private energy to one process. A local Codex workflow includes app, network wait, shell, compiler, tests, and other activity inside the selected domain. It excludes remote model inference.

Choose exactly one primary source unless descriptors explicitly authorize a disjoint sum. Preserve its `source_uid`, kind, unit, temporality, evidence class, clock domain, and ancestry.

## Evidence classes

- Synthetic/testd: deterministic integration evidence only. Never a physical ranking.
- Raw hardware counter: local domain measurement, still subject to calibration and shared-host interference.
- Tool-derived estimate: report as an estimate and retain the native interval/duration evidence.
- Model-derived estimate: name the model and do not present it as direct measurement.

## Trial discipline

- Run shared-domain trials sequentially after background build/test activity is idle.
- Freeze inputs, useful work, output digest, dependencies, and timeout.
- Bind the candidate SHA-256 in the manifest and require matching, clean
  before/after mutation scopes. Recheck coordinator, plan, analyzer, skill, and
  Wattch hashes around every untrusted workload.
- Run one source-qualification pilot before the comparison. Require the marker
  window to cover at least ten native source updates; a shorter window or a
  faster client poll interval is not a substitute for native observations.
- Derive the marker duration and in-window sample count from decoded traces;
  never trust a trial-authored duration. Canonical quality metadata uses
  `sequence_gaps` and must equal the gaps derived from raw sample sequences.
- Warm up separately, then alternate or counterbalance baseline/candidate order.
- Keep failures in the outcome table. Report pass rate beside conditional energy results.
- One run per condition is a smoke demonstration. Comparative claims need repeated blocks and uncertainty.
- A lower-level schema-v1 meter calibration supports exploratory local-domain
  estimates only. It does not authorize a confirmatory physical claim; that
  requires replay-verified calibration and the repository's verified study path.
- Do not call joules carbon, whole-machine energy, or datacenter energy without a separately validated boundary.

## Agent comparisons

Separate two outcomes:

1. Local agent-workflow evidence from launch to submission.
2. Generated-code evidence from an external judge running the submitted patch on identical inputs.

The second outcome answers whether the implementation is more efficient. Neither outcome measures the remote Luna/Terra inference footprint.
