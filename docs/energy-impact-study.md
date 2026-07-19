# Daemon-Based System Energy Impact Study

## Status and scope

This document defines Wattch's first causal study of software-induced system
energy impact. It deliberately does **not** estimate a process's private energy
consumption. It estimates the change in energy observed at one declared meter
boundary when a program is introduced into an otherwise identical system
context.

All empirical power and energy evidence enters through `wattch-powerd`. RAPL,
`powermetrics`, and future meter backends remain interchangeable acquisition
sources behind the daemon boundary. No wall meter or PDU is required for this
study. Consequently, the result is always named after the domain that was
actually measured; it is not silently promoted to whole-machine electrical
energy.

The first empirical platform is Intel/Linux with a RAPL package or PSys source.
The first case study is polling versus batching. Apple `powermetrics` is a
separate same-method replication, not a source pooled directly with RAPL.

The implementation is split across the same acquisition/interpretation
boundary:

```text
crates/wattch-core/src/research.rs          canonical research records
clients/python/pywattch/experiment/        schedule, runner, raw artifacts
clients/python/pywattch/analysis/          quality, outcomes, effects, model,
                                           and derived artifact publication
examples/scenarios/                        deterministic raw fixtures
crates/wattch-testd/tests/fixtures/truth/  test-only expected effects
```

This repository now contains the study infrastructure, not a completed causal
result. No bundled dataset establishes a polling-versus-batching ranking
reversal on real hardware. The shell-free commands in
`examples/bench/polling_vs_batching.toml` now run the deterministic workload in
`examples/workloads/polling_batching_worker.py`: polling performs tight
non-blocking reads, batching blocks and dispatches batches, and both produce
byte-identical canonical stdout. Runtime work, throughput, backlog, latency,
and SLO evidence is emitted separately as one machine-readable JSON record.
This makes the first case runnable; it does not make it an empirical result.
Privileged live `powermetrics` behavior is also host-dependent; the
deterministic parser and collector tests do not replace per-host meter
calibration.

## Causal question

For program `P`, background context `B`, placement and priority policy `pi`,
hardware and host state `H`, and meter boundary `M`, define:

```text
tau_P(B, pi, H, M) = Y_M(P, B, pi, H) - Y_M(empty, B, pi, H)
```

`Y_M` includes every process and interaction visible inside `M`. The subtraction
is performed offline between randomized matched trials; the daemon never
subtracts a baseline. Operationally, `empty` is `C0`: the same background plus
a sham launcher, so launcher and marker overhead are present in every arm.

For programs `P1` and `P2`, the preregistered ranking reversal is:

```text
Y_M(P1, idle) - Y_M(P2, idle) > 0
Y_M(P1, B)    - Y_M(P2, B)    < 0
```

This asks whether `P1` is heavier when measured in isolation but lighter under
a specified co-tenant workload. A second mechanism hypothesis asks whether a
longer, lower-priority program can avoid a power-state transition and therefore
reduce total energy at `M` despite taking longer.

## Measurement boundary contract

Every experiment freezes a `MeasurementBoundary` before data collection. It
records:

```text
boundary id
backend
primary source uids
permitted aggregation rule
source kind, unit, temporality, evidence class, parent, and clock domain
frozen disjoint-group/member evidence when aggregation is permitted
required source topology
```

Local numeric source ids are capture-local and are not part of boundary
identity. Runs are comparable only if the boundary validator can bind the
declaration to the captured descriptors and the backend, stable source
identity, topology, unit, temporality, evidence class, and clock domain agree.

The permitted aggregation rules are intentionally narrow:

- `SINGLE_SOURCE` selects exactly one primary outcome.
- `SUM_DECLARED_DISJOINT` requires every selected descriptor to carry the same
  non-empty `wattch.disjoint_group` label and a unique non-empty
  `wattch.disjoint_member` label. Both values are frozen in each boundary
  source signature, so removing or changing the evidence makes later runs
  incompatible. Validation also rejects a parent and descendant in the same
  sum.
- Sources that cannot be proven disjoint remain separate outcomes.

Never sum a RAPL package with its core, uncore, or DRAM children. The package
already represents a containing domain. Similarly, do not manufacture a
combined Apple power value unless the captured descriptors establish disjoint
CPU, GPU, and ANE domains and the preregistered boundary permits the sum. The
mere presence of three top-level sources does not authorize that aggregation;
the default is three separate outcomes. A backend must only emit the two
disjointness labels when its own source semantics establish a non-overlapping
partition; experiment code cannot add them merely to make a sum validate.

### Permitted claim language

| Daemon source | Evidence | Permitted outcome wording |
| --- | --- | --- |
| RAPL package | cumulative `MICROJOULE`, `RAW_HARDWARE_COUNTER` | incremental CPU-package energy impact |
| RAPL PSys, when exposed | cumulative `MICROJOULE`, `RAW_HARDWARE_COUNTER` | incremental platform-domain energy impact |
| `powermetrics` CPU/GPU/ANE | interval-average `MILLIWATT` linked to raw frame `elapsed_ns`, `TOOL_DERIVED_ESTIMATE` | incremental measured compute-domain energy impact |
| Future wall/PDU backend | only as described by that backend | whole-machine electrical energy only if the descriptor truly exposes that boundary |

Without external calibration, Wattch makes repeatability and causal-difference
claims. It does not claim absolute meter accuracy. Cross-backend replication
compares effect direction and standardized effect size; it does not compare raw
joules as though RAPL and `powermetrics` measured the same physical boundary.

## Observation rules

Every primary contrast is evaluated with both estimands. One experiment
schedule freezes one observation rule, so fixed-horizon and equal-work trials
are run as separate matched schedules rather than mixed within a block.

### Fixed horizon

Use the same daemon-stamped target-launch-to-recovery-end duration for `C0`,
`C1`, and `C2`. This estimand measures the energy change over an operationally
fixed interval, including interference and tail behavior.

The runner uses the local midpoint around the acknowledged `TARGET_SPAWN`
request only to schedule the intended deadline. The authoritative observed
horizon is `RECOVERY_END.monotonic_ns - TARGET_SPAWN.monotonic_ns`, using the
two daemon-stamped events. It records the target, observed duration, signed
error, and exact preregistered tolerance in `acquisition.json`; the same
tolerance is frozen in `trial.json` and `manifest.json`. It is independent of
the meter sampling interval, must be at least 1 ns, and must be strictly
smaller than the horizon. A horizon outside it is claim-ineligible. If a target
is still running at the horizon, `RECOVERY_END` is stamped before teardown and
the run is also claim-ineligible rather than silently extending the energy
window.
Fixed-horizon trials also declare `BATCH` or `SERVICE`. A service declaration
requires a preregistered open-loop `arrival_trace_id`, which is propagated to
the workload harness, and cannot become claim-eligible without latency,
zero-backlog, and SLO evidence.

### Equal work

- A batch trial runs until the declared amount of work completes, then observes
  the preregistered recovery tail.
- A service trial receives the same open-loop request trace, drains its backlog,
  and then observes the recovery tail.

Every result reports runtime, completed work, throughput, latency, backlog, and
recovery time alongside energy. A negative incremental energy effect is
claim-ineligible when the program merely defers work, leaves a backlog, produces
non-equivalent output, or violates the service-level objective.

## Acquisition and event timing

`wattch-powerd` composes a selected power-meter backend with descriptor-rich raw
OS telemetry and exposes them over one daemon connection. The daemon may remap
child-local numeric ids to make the combined source table unique; stable source
uids and parent relationships remain unchanged.

The Linux collector exposes native or losslessly normalized evidence rather
than derived metrics:

```text
/proc/stat aggregate CPU-time counters       USER_HZ counters
context switches and interrupts              cumulative counters
runnable and blocked tasks                    gauges
per-CPU current frequency                     hertz (sysfs kHz x 1000)
per-CPU idle-state residency                  nanoseconds (sysfs us x 1000)
kernel wakeup-source event_count              cumulative counters, when readable
thermal-zone temperature                      Celsius (sysfs millidegrees / 1000)
CPU throttle counts                           cumulative counters
```

Utilization, rates, counter deltas, power integration, aggregation, and baseline
subtraction are offline operations. Missing optional telemetry is explicit in
the descriptor set and must fail any study boundary or mechanism contract that
requires it.

Protocol v0.2 gives samples and benchmark markers the same daemon-wide monotonic
clock. `HelloResponse.clock_instance_id` identifies that clock epoch for one
daemon lifetime. The runner records these events during an active capture:

```text
PRE_WINDOW_START
BACKGROUND_ARRIVAL
BACKGROUND_COMPLETION
TARGET_SPAWN
TARGET_EXEC
TARGET_EXIT
RECOVERY_END
```

The client supplies an event id, kind, and descriptive labels. The daemon owns
`node_id` and `monotonic_ns`, emits the authoritative event in an `EventBatch`,
and then acknowledges the same event. Client wall-clock send time is never used
as the analysis boundary. See [Protocol v0](protocol-v0.md) for exact ordering
and integrity rules.

The runner does not place a causal marker immediately against an empty meter
buffer. After `StartCapture` it waits until every primary source has emitted at
least one sample, and after `RECOVERY_END` it waits until every primary source
has emitted a sample at or beyond that daemon timestamp before stopping the
capture. An `INTERVAL_AVERAGE` primary additionally requires either a confirmed
second generation at or beyond `RECOVERY_END`, or one receipt at least a declared
nominal period beyond it. This tail does not move the estimand boundary; it only
gives offline affine synchronization enough post-window evidence. Failure of
either bounded barrier fails the trial. Replay independently recomputes the
tail-support rule from raw samples before checking that primary-source evidence
brackets the declared window.

Before opening the trial artifact, the runner also canonicalizes the exact
commands passed to its launcher, including replay environment, working
directory, priority, CPU affinity, and resource limits. Its digest is frozen in
`trial.json` and all marker labels; the full document and digest are sealed in
`acquisition.json`. Confirmatory claim verification compares the reconstructable
fields against the separately sealed randomized schedule.

Useful-work and SLO outcomes use a separate workload-written evidence path.
The runner reserves `target.metrics.json`, injects its absolute path as
`WATTCH_METRICS_PATH`, and never creates the file itself. The executed target
must exclusively create one schema-v1 JSON object. Acquisition binds its
SHA-256 and requires its `output_sha256` to equal the captured stdout bytes;
offline replay reopens and reparses the file instead of trusting copied fields
in `acquisition.json`. A Python `metrics_provider` remains available for
exploration, but its evidence is explicitly `EXPLORATORY_PROVIDER` and cannot
make a confirmatory trial eligible.

When a co-tenant background command exists, the same contract uses
`background.metrics.json`. Confirmatory eligibility requires sealed background
progress evidence with no backlog or reported SLO failure, preventing a target
that suppresses or defers background work from appearing energy-efficient.
Matched-cell verification compares target and background arrival-trace content
digests across C0/C1/C2; a shared trace name alone is insufficient. The runner
also reads back each child process's actual absolute niceness before
`TARGET_EXEC`; requested nice `0` or `+10` is not inferred from a relative
`nice -n` invocation. The default POSIX launcher creates a fresh session for
each target and background command and signals the verified process group on
teardown, escalating from `SIGTERM` to `SIGKILL` when descendants remain. A
workload cannot leave helper processes running into recovery or the next trial.

## Deterministic validation before hardware trials

`wattch-testd` scenarios validate analysis behavior without privileged hardware
or noisy measurements. Expected causal answers belong in test-only truth
sidecars; they are not emitted by the daemon.

The sidecars live under `crates/wattch-testd/tests/fixtures/truth/`, outside the
runtime scenario format.

| Fixture | Intended validation |
| --- | --- |
| `causal_additive_null.json` | additive behavior with no interaction-driven reversal |
| `causal_interference_reversal.json` | opposite isolated and co-tenanted `P1-P2` signs |
| `causal_low_priority_dvfs.json` | equal-work, longer low-priority path with a lower power state |
| `causal_carryover_confounding.json` | thermal and deep-idle-state carryover across insufficient washout |
| `causal_treatment_timing_confounding.json` | a naive timing contrast that disappears after matched-control subtraction |
| `quality_faults.json` | counter wrap, missing readings, disappearance, delayed `RecordEvent`, replay-only clock drift/jitter, and sampling aliasing |

The quality fixture has two deliberately different validation surfaces:

- Counter wraps, omitted readings, source disappearance, and sampling aliasing
  pass through `DeterministicBackend`, the shared daemon runtime, protocol
  framing, and a Unix-socket client in the live integration test.
- A delayed marker is a client-side `record_events` schedule. The fixture
  driver waits until the declared raw batch and sends an ordinary
  `RecordEvent`; the daemon stamps receipt time and emits `EventBatch` plus the
  matching acknowledgement. The daemon never backdates the event to the
  intended benchmark tick.

Per-source `TimestampPattern` offsets, jitter, and drift are intentionally
limited to `--dry-run`/replay batches. They are useful for testing offline
quality rejection, but they do **not** simulate a drifting source clock over a
live socket. Live captures replace those fixture timestamps with the one
daemon-wide monotonic clock, as the protocol requires. A future multi-clock
backend would need explicit synchronization anchors rather than a hidden test
daemon exception.

Together, these fixtures test raw acquisition behavior, event-window
selection, counter unwrapping, quality rejection, and claim logic. They do not
constitute empirical evidence for the research hypothesis.

## Meter calibration

Calibrate each backend and primary source separately before confirmatory runs.

1. Measure native update cadence, resolution, wrap behavior, timestamp jitter,
   missingness, and Wattch collection overhead.
2. Compare candidate capture intervals under idle, fixed load, and A/A trials.
   At every interval, phase-shift otherwise matched workload onsets across one
   native sampling period and record the maximum relative outcome spread.
3. Select the fastest interval for which collection overhead is below `1%` and
   missing samples are below `0.1%`, and phase sensitivity is below its
   preregistered strict limit (the implementation default is `1%`). Sampling
   faster than the meter's update cadence is not additional independent
   evidence and can amplify aliasing.
4. Run sham-versus-sham matched trials to estimate the smallest detectable
   effect.
5. Set the meaningful-effect threshold to:

   ```text
   max(0.05 * mean control energy, 2 * A/A uncertainty)
   ```

The offline quality summary records observed cadence, effective sampling rate,
timestamp jitter, observed resolution, wraps, unexplained backward steps,
sequence and timing gaps, and estimated missingness. A leading or trailing gap
is not inferred without an explicit capture window. An interval is eligible
only when every required idle/fixed-load/A/A regime passes all strict
thresholds; equality with a limit fails. The offline helper writes a strict
`METER_CALIBRATION` artifact containing the full boundary and digest, selected
interval, required regimes, selected worst-case overhead/missingness/phase
metrics, and thresholds. Its content digest is frozen in the trial labels and
acquisition result and must be listed in the sealed preregistration. Claim
verification reopens it and checks the trial boundary, interval, and final
quality overhead value rather than trusting a caller-supplied scalar.

The calibration JSON has exactly these top-level fields:
`schema_version`, `kind`, `calibration_id`, `measurement_boundary`,
`measurement_boundary_sha256`, `selected_interval_ns`, `required_regimes`,
`selected_metrics`, and `thresholds`. `required_regimes` includes `IDLE`,
`FIXED_LOAD`, and `AA`. `selected_metrics` contains
`measurement_overhead_ratio`, `missing_sample_ratio`, and
`phase_sensitivity_ratio`; `thresholds` contains their corresponding `max_*`
values. Every selected value must be strictly below its threshold, and the
overhead and missingness thresholds cannot exceed 1% and 0.1%, respectively.

The study preregistration is itself content-addressed before capture. Its
digest is passed to `TrialRunner` and frozen in each trial's labels and
acquisition result. Verified claim issuance requires that digest to match the
sealed preregistration bundle, so a post-hoc schedule cannot be attached merely
by declaring an earlier timestamp.

Capture initially seals `quality.json` as `REVIEW_REQUIRED` with a
`NOT_EVALUATED` issue. Offline calibration may replace that placeholder with a
canonical `ACCEPTED`, `REVIEW_REQUIRED`, or `REJECTED` assessment and then
recompute `checksums.json`. This one-way finalization changes only the quality
assessment and checksum seal; it never rewrites either trace or other raw
evidence to hide gaps or jitter. Analysis rejects a trial unless its final
quality disposition is `ACCEPTED` and the primary sources satisfy the frozen
thresholds.

## Randomized matched study

Each matched block contains the same input, background trace, seed, affinity,
resource limits, and initial-state criteria:

```text
C0  background workload + sham launcher
C1  identical background trace + P1
C2  identical background trace + P2
```

Run this structure once with no co-tenant background (the isolated regime) and
again with each preregistered background context. `C0` retains the sham launcher
in both regimes.

The schedule uses reproducibly randomized, position-balanced triples. Block on
machine, day, cache regime, initial temperature, and background context. Cover
at least:

- CPU-bound batch,
- memory-bound batch,
- open-loop service, and
- mixed service/batch contexts.

Before measuring energy effects, validate that `P1` and `P2` produce equivalent
outputs. For a service workload, the offered demand is open-loop so a slower
program cannot reduce apparent energy merely by accepting less work.

Run a discarded pilot of 12 matched blocks per primary regime. Use it to choose
washout, recovery horizon, and the number of confirmatory blocks needed for 90%
power with two-sided 95% intervals. Do not reuse pilot effect estimates as
confirmatory evidence.

A positive `claims.json` publication requires an internally verified
preregistration and claim chain. `seal_study_preregistration()` writes an
exclusive, content-addressed artifact that identifies the exact confirmatory
`schedule.json` files plus one discarded-pilot, A/A, and placebo artifact per
primary estimand/context regime and the applicable meter-calibration artifact.
Free-form evidence ids are not accepted. Loading that seal recomputes the pilot
variance, A/A meaningful threshold, 90%-power paired sample size, placebo
decision, and exact disjoint confirmatory block set for every regime.

`VerifiedTrialEvidenceBundle.from_trial_directories()` reopens every trial
using the checksum/trace replay path. It requires exactly one C0, C1, and C2
trial in each block/policy cell and binds the exact arm-to-program map,
scheduled and executed command/cwd/environment, affinity and limits, complete
measurement boundary and source-topology digests, meter calibration, output,
work/SLO/quality gates, host-policy application and read-back restoration
fingerprints, and manifest-derived UTC day and machine/topology provenance.
The trial labels and acquisition result both bind the preregistration digest,
and the manifest creation timestamp must postdate its declared seal time.

Finally, `StudyClaimRecord.from_verified_results()` recomputes all paired
effects and acceptance gates from that bundle. Supplied ranking-reversal,
placebo, and independent-day results must match the recomputation,
including their exact matched block ids. Verified classes have closed
constructors, so caller-created references or all-true acceptance booleans
cannot authorize a positive publication. A report that asserts support must
bind the issued claim id and canonical claim digest. Negative and inconclusive
raw mappings remain publishable.

## Power-state mechanism experiment

Treat the proposed mechanism as a separate randomized experiment.

1. Balance target priority between nice `0` and `+10`.
2. Repeat the transition-adjacent workload under normal dynamic frequency
   management and a fixed non-turbo frequency.
3. In a separate block, restrict the relevant deep-idle states.
4. Estimate a difference in differences. The mechanism is supported only if
   preventing the proposed transition attenuates the ranking reversal.

Frequency-policy and idle-state interventions are host configuration, not
daemon interpretation. Record their exact settings in trial metadata.
Post-launch frequency, C-state residency, wakeups, utilization, temperature, and
throttling are mechanism outcomes; do not adjust the primary causal estimate
for them.

The current positive-claim publisher fails closed for mechanism assertions.
Energy-only difference-in-differences remains available for exploratory
analysis, but it cannot be described as a supported C/P-state mechanism until
the sealed preregistration binds frequency/P-state, C-state-residency, and
wakeup source UIDs and replay derives their post-launch outcomes from the raw
traces. Verified host-policy labels and attenuation of the energy contrast are
necessary but not sufficient mechanism evidence.

The Python experiment contract represents these conditions as
`MechanismPolicy.NORMAL_DYNAMIC`, `FIXED_NON_TURBO`, and
`RESTRICTED_DEEP_IDLE`. Every parent matched block contains one randomized
C0/C1/C2 triple for every requested mechanism policy. Those policy cells share
the parent `matched_block_id`, replay seed, input, background context, and one
separately randomized `target_nice`; policy order and arm order are balanced.
This produces the four same-block policy-by-arm cells required by a
difference-in-differences contrast while preserving priority as a separate
factor. `TrialRunner` contains no privileged sysfs or platform commands.
The confirmatory fixed-frequency and restricted-deep-idle interventions use
separate schedules, each paired with `NORMAL_DYNAMIC`; the scheduler does not
implicitly pool those two interventions.
For either non-normal policy it requires an injected `HostPolicyController`
whose `apply_and_verify` operation returns `HostPolicyEvidence` before capture;
otherwise the trial fails before opening a daemon session. The evidence freezes
the controller identity, exact observed settings, saved prior settings, and
both SHA-256 fingerprints in `trial.json`, daemon event labels, and
`acquisition.json`. After capture, `restore_and_verify` must return typed
read-back evidence whose controller, policy, expected-prior fingerprint, and
restored-settings fingerprint all match. A failed read-back fails the trial and
halts the remaining schedule so an unknown host policy cannot contaminate later
blocks.

Normal dynamic operation may be recorded as `DECLARED_ONLY` for ordinary
non-mechanism studies. A confirmatory mechanism study should inject the
controller for the normal cell too, so both sides of the intervention have
verified settings rather than relying on a host default.

## Offline outcome and counterfactual analysis

Raw cumulative energy is wrap-corrected and differenced only after replay.
Instant power is converted to watts and trapezoidally integrated over the
daemon-stamped window offline. Native interval-average power, such as Apple
`powermetrics`, is instead reconstructed on a contiguous native-duration
timeline, affinely synchronized to all raw daemon receipt anchors, and
multiplied by the exact mapped overlap with that window. Excessive clock drift,
receipt residuals, and missing same-frame duration evidence are rejected.
Boundary validation runs before either operation, and counterfactual feature
derivation uses the identical synchronization path.

Primary causal estimates are paired within matched blocks. Placebo estimates
must center on zero. Isolated and co-tenanted results must use the same boundary
and estimand before the ranking-reversal check is applied.

Train the counterfactual model only after randomized experiments establish
causal ground truth. The first model is a per-host dynamic linear/state-space
baseline trained only on complete `C0` background-control episodes.

The model API enforces this sequencing. Fitting requires a
`CausalGroundTruthReference` bound to a supported claim, the same measurement
boundary, and the claim artifact seal. Every `BaselineEpisode` also carries an
`AcceptedControlTrialProvenance` proving C0/no-program/background-only status,
an accepted quality disposition, and the sealed trial checksum manifest. These
identities are included in the model id and prediction provenance. Recalibration
on a target host requires a separate accepted ground-truth reference for that
target boundary.

Artifact-verifying factories reopen the published claim and sealed trial
directories before issuing provenance usable for validation. The causal
factory requires the internally issued `StudyClaimRecord`, verifies that its
canonical digest is bound by `claims.json`, and compares its serialized content
exactly.

Validated model arrays are not supplied by the caller. For each C0 training
trial, `BaselineEpisode.from_verified_trial_bundle()` derives the primary power
path and pre-launch state from replayed raw samples. For the C1/C2 target,
`PredictionRequest.from_verified_trial_bundle()` derives the same path state
and binds the accepted observed boundary energy. Pre-treatment and exogenous inputs are an
explicit external JSON artifact whose exact file SHA-256 is precommitted in the
sealed trial label `wattch.model_input_artifact_sha256`. The artifact declares
`WATTCH_MODEL_INPUT_V1`, the trial and input ids, an `OUTCOME_START` origin,
strictly increasing relative offsets, static/pre-launch declarations, and
exogenous series. It therefore does not require future absolute daemon
timestamps at preregistration time. Replay establishes the absolute origin and
Wattch aligns the declared series to the raw-derived path using deterministic
previous-step/hold-last semantics. A factory-issued `ModelInputBinding` joins
the trial checksum-manifest digest, feature-artifact digest, raw-power
derivation rule, alignment rule, and canonical aligned-input payload digest.
These verified-bundle factories also bind the preregistration, accepted meter
calibration, complete topology, and canonical host provenance carried by the
causal claim. Direct `from_trial_dir()` replay remains exploratory and cannot
authorize `VALIDATED`, even if its local quality document says `ACCEPTED`.

A `VALIDATED` status is rejected unless the causal claim, every C0 trial and
model-input binding, the C1/C2 target and target-input binding, and the
episode-level split issued by `validate_group_split()` are all factory
verified. Direct constructors and caller-provided arrays remain available for
exploration but cannot be relabeled as validated evidence. A missing or
overlapping split likewise cannot receive `VALIDATED` status. The split is
issued only from replay-verified episodes and seals their trial checksums,
model-input payloads, row counts, and derived grouping identities. Model
parameters are fit only on its TRAIN partition. The TEST controls are predicted
after fitting, and their path digests and error metrics form a held-out
validation artifact bound to the split, training-data id, and fitted-parameter
digest.

The external artifact proves that declarations were precommitted and unchanged;
it does not independently prove that a declared static or pre-launch value was
observed on the machine. Any claim requiring that correspondence must use a
replay-derived collector or a separately sealed sidecar artifact.

Allowed inputs are known without observing the treatment response:

```text
pre-launch telemetry
machine configuration
initial backlog
replay seed
exogenous offered demand
```

Do not use treatment-period frequency, C-states, wakeups, utilization, runtime,
throughput, completed work, or backlog as predictors. They are downstream
effects of `P`.

Split by complete run, seed, day, workload family, and machine; never split
telemetry rows from one run across train and test. The output contains the
predicted no-program power path, integrated counterfactual energy, uncertainty
interval, model identity and training provenance, and an explicit
out-of-distribution abstention. Every such result remains
`MODEL_DERIVED_ESTIMATE`, including an abstention record. Transfer testing on a
second machine must keep the same daemon backend and measurement-boundary
semantics, explicitly map them to that host's source uids, and report any
recalibration. Host-local source identities are never assumed equal merely
because their source paths look similar.

The prediction artifact declares trapezoidal integration explicitly. Python
publication checks and the canonical Rust validator independently reintegrate
the mean, lower, and upper power paths, then verify counterfactual energy,
reversed impact intervals, observed-minus-counterfactual arithmetic, estimand,
boundary, and target-trial identity. OOD records retain timestamps and
provenance but no numeric path or implied zero effect.

The model is evaluated against randomized ground truth; it is not a substitute
for randomization and its prediction alone does not establish causality.

## Artifact layout

An experiment stores its frozen declaration and schedule above immutable trial
evidence:

```text
<experiment-id>/
  experiment.json
  schedule.json
  trials/
    <trial-id>/
      trial.json
      manifest.json
      sources.json
      raw.trace.wch
      events.trace.wch
      quality.json
      acquisition.json
      target.stdout.log
      target.stderr.log
      target.metrics.json      # workload-written, required for confirmatory use
      background.stdout.log   # when a background command is present
      background.stderr.log   # when a background command is present
      background.metrics.json # required for confirmatory co-tenanted use
      checksums.json
  analysis/
    features.parquet
    predictions.jsonl
    claims.json
    report.md
```

`trial.json` is the canonical immutable trial declaration, including the full
measurement boundary and estimand. `manifest.json` binds the run, protocol
version, node, daemon clock instance, source descriptors, sampling interval,
and boundary id. The two trace files preserve v0.2 frames separately.
`acquisition.json` records completion status, raw/event counts, and the
daemon-stamped fixed-horizon validation without polluting the canonical trial
schema. `quality.json` begins as a sealed `REVIEW_REQUIRED` placeholder; its
offline finalization records an accepted, review-required, or rejected
disposition, measurement overhead, per-source summaries, and structured
issues, then reseals `checksums.json`. Process logs support output-equivalence
and failure checks. `checksums.json` seals every other trial file with SHA-256.
When produced, offline outputs are written under `analysis/`; they never replace
or modify acquisition evidence.

Positive ranking-reversal records are content-addressed to quality-accepted
trial seals and must include complete matched C0/C1/C2 cells for both
estimands, isolated and co-tenanted contexts, independent days, required
priority levels, and every preregistered mechanism-policy cell. A mechanism
claim additionally requires the same normal/intervention policy pair in every
matched block and verified, restored host settings for both sides. Unsupported
or incomplete results remain publishable only as negative or inconclusive
claims.

The offline artifact writer publishes files atomically and refuses to
overwrite an existing result. `features.parquet` is real Parquet and therefore
requires the optional `pyarrow` analysis dependency; Wattch fails clearly
rather than writing a disguised JSON file. Prediction records require
`MODEL_DERIVED_ESTIMATE`, explicit out-of-distribution and abstention fields,
validation status, and a boundary id. Claims and the report carry their full
boundary declarations. Positive claims also expose canonical boundary,
source-topology, trial-seal, schedule, calibration, host-policy, and claim
digests; a positive report binds the issued claim digest. Claim text that calls
a RAPL or `powermetrics` result
whole-machine or whole-system energy is rejected.

Protocol v0.2 readers can replay v0.1 raw trace frames. Legacy traces do not
contain daemon-stamped events or a clock instance id, so analyses requiring
those fields must reject or explicitly downgrade them rather than inventing
values. Live traffic keeps the 16 MiB frame cap; a bounded offline-only
migration reader accepts v0.1 raw batches up to 256 MiB per frame.

## Acceptance and falsification

A ranking reversal is supported only if all of the following hold:

- `P1` and `P2` outputs are equivalent.
- The preregistered isolated and co-tenanted `P1-P2` signs are opposite.
- Both effects exceed their A/A-derived meaningful thresholds and their
  two-sided 95% intervals exclude zero in the required direction.
- Fixed-horizon and equal-work analyses agree.
- The result is not explained by unfinished work, deferred backlog, or SLO
  degradation.
- Placebo estimates center on zero.
- The result is supported on at least two independently labeled days.
- A claimed C/P-state mechanism is attenuated by its preregistered intervention.

Failure of any condition is reported as a falsification or an inconclusive
result, not repaired through post-hoc exclusions. If daemon backends disagree,
report boundary-specific effects rather than forcing a single total-energy
conclusion.

## Layering invariant

The implementation boundary remains:

```text
wattch-powerd / wattch-testd
  source discovery, raw reads, daemon timestamps, sequence numbers, events

trial runner
  randomization, process lifecycle, event requests, immutable artifacts

offline analysis
  quality checks, integration, counter deltas, estimands, contrasts, models

reporting
  boundary-qualified claims and falsification results
```

No daemon backend may integrate power, aggregate sources, subtract a control,
train a model, assign process energy, or decide a research claim.
