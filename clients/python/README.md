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
# Private runtime directory: EnergyMeter() auto-discovers this socket.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$(mktemp -d)}"
wattch-powerd --backend synthetic

# Explicit development path (including /tmp): pass it to both sides.
wattch-powerd --backend synthetic --socket /tmp/wattch-powerd.sock
# EnergyMeter(socket_path="/tmp/wattch-powerd.sock")

sudo wattch-powerd --backend linux-rapl # Linux RAPL
```

Automatic discovery never probes `/tmp`: an existence check cannot
authenticate a Unix socket in an attacker-writable directory.

## Semantics

- Tag boundaries are quantized to the sampling interval (default 10 ms).
- Cumulative counters (RAPL-style) become wrap-corrected joule deltas —
  these are the `energy` values.
- Legacy `INSTANT` power sources declared in watts or milliwatts are normalized
  to watts outside the daemon, integrated trapezoidally, and reported
  separately as `estimated_energy`. Integration is streaming: meter memory
  remains constant as capture duration grows.
- Native `INTERVAL_AVERAGE` sources such as Apple `powermetrics` retain a raw
  same-frame `elapsed_ns` duration. Offline analysis reconstructs their
  contiguous native timeline, quality-checks an affine synchronization to all
  daemon receipt anchors, clips each constant interval to daemon-stamped event
  windows, and never trapezoidally interpolates those averages as point samples.

## System-impact study artifacts

The matched-study APIs live under `pywattch.experiment` and the offline trust
boundary lives under `pywattch.analysis`. A captured trial is provisional until
separate overhead calibration is supplied:

```python
from pywattch.analysis import evaluate_trial_quality, load_trial_evidence
from pywattch.experiment import finalize_trial_quality

quality = evaluate_trial_quality(
    trial_dir,
    measurement_overhead_ratio=calibrated_overhead_ratio,
)
finalize_trial_quality(trial_dir, quality)
evidence = load_trial_evidence(trial_dir)
```

For confirmatory work, create direct same-work runtime evidence with
`write_verified_no_collector_runtime()`, then replay the instrumented raw trial
directories with `write_verified_meter_calibration()`. The latter rejects
self-authored calibration arrays and binds the host/node, UTC day and pairing
window, replay seed and command environment, actual nice policy, cache and
temperature block, complete source topology and collector identity, selected
interval, distinct idle/fixed-load/A/A contracts, overhead over every sampled
launch phase, missingness, phase sensitivity, and strict thresholds. Pass its
SHA-256 to `TrialRunner`; a supported claim must find the same
content-addressed artifact in the sealed preregistration and must match the
finalized quality scalar. The lower-level interval selector and
`write_meter_calibration_artifact()` remain useful for exploratory analysis,
but their output cannot authorize a positive claim.
The strict object uses `measurement_boundary` plus its SHA-256,
`selected_interval_ns`, `required_regimes`, `selected_metrics` (overhead,
missingness, phase sensitivity), and their `thresholds`; selected values must
be strictly below the limits.

`load_trial_evidence` verifies checksums, source identity and topology, framed
sample/event integrity, daemon-stamped lifecycle markers, fixed-horizon timing,
sealed output evidence, and accepted quality before producing an offline
`TrialObservation`. It never estimates per-process energy.

`TrialRunner` injects `WATTCH_METRICS_PATH` for the executed target and, when
present, the co-tenant background. Those processes exclusively create
`target.metrics.json` and `background.metrics.json`; the runner seals their
exact bytes and replay reparses them, including stdout, completed-work,
backlog/SLO, and arrival-trace digest checks. Callback `metrics_provider`
values are retained for exploratory runs but are never confirmatory evidence.
The runner also reads back the actual absolute child niceness before recording
`TARGET_EXEC`; an unsupported or mismatched priority fails the trial. POSIX
target and background commands run in isolated process groups, and teardown
signals the complete verified group so descendant workers cannot contaminate
recovery or later matched arms.

Fixed-horizon experiments declare an explicit `tolerance_s`; it is sealed with
the trial and checked against daemon-stamped events. It is not inferred from or
widened by the capture interval.

Publishing a positive ranking-reversal claim uses a three-step verified API:

1. `seal_study_preregistration()` content-addresses the confirmatory schedules,
   discarded pilot, A/A, placebo, and meter-calibration artifacts before the
   first trial timestamp.
   Pass the issued preregistration digest, together with the selected meter-
   calibration digest, to `TrialRunner`; both are sealed into every trial.
2. `VerifiedTrialEvidenceBundle.from_trial_directories()` reopens every sealed
   trial through `load_trial_evidence()` and binds the complete boundary/source
   topology, exact scheduled and executed commands, quality and output gates,
   calibration, host-policy application/restoration fingerprints, and
   manifest-derived UTC day and machine provenance.
3. `StudyClaimRecord.from_verified_results()` recomputes the paired effects,
   ranking reversals, independent-day results, placebo checks, and top-level
   acceptance. Supplied effect block
   ids and values must match that recomputation exactly.

The constructors of these verified types are closed; callers cannot publish a
positive claim from hand-built references or all-true booleans. A supported
report must bind the issued claim id and digest. Raw mappings may still publish
negative or inconclusive results. Host clock accuracy and the physical meaning
of `node_id` remain deployment controls; Wattch independently recomputes the
day and machine/topology digest from each sealed manifest instead of trusting
free claim labels.

`DynamicLinearBaseline.fit` additionally requires a supported
`CausalGroundTruthReference`. A control episode eligible for a `VALIDATED`
prediction must be created with `BaselineEpisode.from_verified_trial_bundle()`,
which replays its sealed,
quality-accepted background-only C0 trial, derives the power path and initial
state, and binds the accepted calibration, topology, and canonical host
provenance. `from_trial_dir()` and caller-supplied arrays remain exploratory.
Use `CausalGroundTruthReference.from_claims_artifact()` with its factory-issued
`StudyClaimRecord`. Pre-treatment and exogenous inputs live in a strict
`WATTCH_MODEL_INPUT_V1` JSON artifact; its file SHA-256 must be precommitted in
the sealed trial label `wattch.model_input_artifact_sha256`. The artifact uses
offsets relative to `OUTCOME_START`, so preregistration never needs a future
daemon timestamp. After replay, Wattch deterministically aligns each exogenous
series to the raw-derived timeline with previous-step/hold-last semantics and
seals that alignment rule in the model-input binding. Use
`PredictionRequest.from_verified_trial_bundle()` for validated targets. It
derives the initial state from the accepted C1/C2 raw trace and binds the external-input artifact,
observed path, observed joules, and checksum manifest in a factory-issued
`ModelInputBinding`. Only the complete factory-verified
causal/control-input/target-input chain may publish `VALIDATED`; manually
constructed episodes, requests, provenance, or arrays remain exploratory.
Validation also requires a `GroupSplitValidation` issued by
`validate_group_split()` from replay-issued `BaselineEpisode` objects. The
split seals each episode's trial checksum, model-input payload, row count, and
run/seed/day/workload-family/machine identity. Fitting uses only the sealed
TRAIN partition; prediction error over the sealed TEST controls is emitted as
a separate held-out validation artifact bound to the split and fitted model.

The external feature artifact proves preregistration and byte integrity. A
declared static or pre-launch value is not, by that fact alone, proof that the
value was observed on the host; claims needing that correspondence require a
replay-derived collector or separately sealed sidecar source.

Power-state mechanism schedules use explicit `MechanismPolicy` values. Each
parent matched block spans every requested policy while sharing one replay seed
and a separately balanced target nice level, so offline difference-in-
differences retains both factors. Wattch does not change privileged host
settings itself: `TrialRunner` requires a caller-supplied
`HostPolicyController` for fixed-frequency or restricted-idle trials. The
controller must return verified exact settings and restore the previous host
state. Application evidence includes the saved prior-state fingerprint, and
`restore_and_verify` must return a matching typed read-back. The applied and
restored settings are sealed with the trial; a failed read-back stops the
remaining schedule.

Mechanism difference-in-differences remains available as exploratory offline
analysis. Positive mechanism claims currently fail closed: they are rejected
until a sealed preregistration names frequency/P-state, C-state residency, and
wakeup source UIDs and Wattch derives their post-launch outcomes from the raw
trace. Host-policy labels or energy attenuation alone are not accepted as
proof of the proposed transition mechanism.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,profiler]'
.venv/bin/python -m pytest tests
```
