# Agent energy optimization benchmark

This frozen task compares Luna medium and Terra medium with and without the
Wattch optimization skill. Each cell receives the exact bytes in `PROMPT.txt`
inside a fresh isolated checkout. Only `candidate.py` is mutable.

The prompt is 440 bytes including its final newline. Its SHA-256 is
`0babc07f5ca0a80653a508f9a04795f1d62d3b2e55ae7cda6b580ece6b35f35f`.
Do not pass it through shell command substitution, which strips that newline.

The checked-in implementation is intentionally inefficient: it repeatedly
rescans the same event stream while building deterministic per-account
aggregates. The optimization contract is exact behavior, not a preferred
implementation.

Focused correctness tests:

```bash
python3 -m unittest discover \
  -s examples/agent-energy-benchmark/tests -v
```

The trusted judge does not execute or import a candidate before timing. It
checks exact stdout/stderr and command provenance already captured by
`wattch run`:

```bash
python3 examples/agent-energy-benchmark/judge.py artifact \
  --run /absolute/runs/luna-control-workload-r001 \
  --candidate /absolute/agent-checkout/examples/agent-energy-benchmark/candidate.py \
  --cwd /absolute/agent-checkout \
  --child-exit-code 0
```

Run the public-API probe only after measured trials, in a disposable process:

```bash
python3 examples/agent-energy-benchmark/judge.py probe \
  --candidate /absolute/agent-checkout/examples/agent-energy-benchmark/candidate.py
```

## Coordinator flow

Use the same clean committed tree for all four already-isolated checkouts. The
coordinator copies the frozen skill package only to treatment checkouts under
`.agents/skills/wattch-energy-optimize`, snapshots every non-`.git` path, and
rejects any post-agent change except `candidate.py`.

Skill installation is the treatment assignment. An agent-authored declaration
of skill use is retained as an adherence observation, but is not a
post-treatment exclusion criterion. A missing declaration therefore remains a
reportable treatment outcome instead of triggering a selective rerun.

```bash
python3 examples/agent-energy-benchmark/coordinator.py prepare \
  --checkout /absolute/luna-control \
  --state /external/results/luna-control.json \
  --condition luna-control \
  --skill-package /absolute/wattch-energy-optimize \
  --trial-order 1

python3 examples/agent-energy-benchmark/coordinator.py run-agent \
  --state /external/results/luna-control.json \
  --wattch /absolute/wattch --mcp /absolute/wattch-mcp \
  --codex /absolute/codex --socket /absolute/wattch.sock \
  --out /external/results/raw --mcp-runs /external/results/mcp

python3 examples/agent-energy-benchmark/coordinator.py run-trials \
  --state /external/results/luna-control.json \
  --wattch /absolute/wattch --socket /absolute/wattch.sock \
  --out /external/results/raw --repetitions 4
```

Prepare `luna-skill`, `terra-control`, and `terra-skill` from the same commit,
then use `coordinator.py compare STATE...` to verify the common base, prompt,
candidate, and workload contract. Execute conditions and trials sequentially;
the recorded `--trial-order` supports a coordinator-owned counterbalanced
schedule. The source checkout, MCP runs, raw artifacts, and state records must
be outside every measured checkout.

Wattch may measure the local agent workflow and the generated program on the
local host. It does not observe Luna or Terra inference in the provider's
datacenter. Synthetic sources prove plumbing only and cannot support a physical
energy ranking.

For a completed `wattch run` or MCP capture artifact, compute one explicit
single-source outcome offline and bind its coordinator trial record with:

```bash
python3 examples/agent-energy-benchmark/summarize_run.py \
  --run /absolute/path/to/runs/<run-id> \
  --source-uid /exact/source/uid \
  --trial-record /external/results/trials/<run-id>.json
```

This operator-side helper loads the qualifier from either the installed
`.agents` treatment package or a Wattch source checkout containing the packaged
skill. Its marker-selection tests remain runnable in control checkouts without
importing treatment-only code.

The summary deliberately refuses to call synthetic values physical evidence
and never sums CPU, GPU, ANE, or nested RAPL domains. Physical-claim eligibility
defaults to false and additionally requires a complete bound capture, exact
correctness/useful work, accepted bound quality, calibration, and matching file
hashes. CLI outcomes use the acknowledged pre-launch `TARGET_SPAWN` through
`TARGET_EXIT` window so fast children cannot run ahead of the start marker. A
single run never enables a comparison claim, and no local run measures Luna or
Terra inference in the provider datacenter.

## Physical generated-code follow-up on macOS

The four frozen submissions can be compared on the local Apple CPU power domain.
This measures the generated programs, not remote Luna or Terra inference. Build
the current binaries first:

```bash
cargo build --release -p wattch-cli -p wattch-powerd
```

`powermetrics` requires root. In a separate terminal, start the daemon as root
while explicitly retaining the local `staff` group for the socket. Supplying
`-g staff` without `-u root` is insufficient.

```bash
sudo -u root -g staff /absolute/wattch-core/target/release/wattch-powerd \
  --socket /absolute/results/wattch-powerd.sock \
  --backend apple-powermetrics \
  --powermetrics-interval-ms 250 \
  --node-id agent-energy-mac \
  --os-telemetry off \
  --allow-uid "$(id -u)"
```

Freeze the exact four candidates, runtime, workload driver, output contract,
warm-up order, qualification pilot, and counterbalanced matrix in a create-only
plan. The runner binds macOS `sandbox-exec` and denies process creation inside
the agent-generated candidate, preventing detached descendants from escaping
the CLI process group and contaminating later trials:

```bash
python3 examples/agent-energy-benchmark/physical_study.py prepare \
  --state /results/luna-control/state.json \
          /results/luna-skill/state.json \
          /results/terra-control/state.json \
          /results/terra-skill/state.json \
  --out /results/physical/physical-plan.json \
  --plan-id agent-energy-physical-v1

python3 examples/agent-energy-benchmark/physical_study.py verify \
  --plan /results/physical/physical-plan.json

python3 examples/agent-energy-benchmark/physical_study.py preflight \
  --plan /results/physical/physical-plan.json \
  --socket /results/wattch-powerd.sock
```

Preflight requires exactly one 250 ms Apple CPU interval-average source and its
canonical native-duration companion. The plan polls the backend at 50 ms so a
native frame is not lost when the tool's actual cadence is slightly slower than
its requested 250 ms interval. The full run additionally requires a
host-specific `METER_CALIBRATION` covering IDLE, FIXED_LOAD, and A/A regimes.
Run preflight and the study from a macOS terminal context where `sandbox-exec`
is permitted; a containing application sandbox may reject the nested profile.
The lower-level schema-v1 artifact described in `clients/python/README.md` is
accepted only for an exploratory local-domain comparison; it cannot authorize a
confirmatory physical claim.

Calibration is staged so the no-collector baseline cannot be confused with a
capture taken while the daemon-wide `powermetrics` process is still running.
The instrumented collector uses the CLI's post-pre-roll delay to place workload
onset at four declared positions inside the native sampling period; raw frames
continue draining during that delay and the realized phase is verified from
the preceding source timestamp.
First collect immutable instrumented runs from the live daemon:

```bash
python3 examples/agent-energy-benchmark/physical_calibration.py instrumented \
  --plan /results/physical/physical-plan.json \
  --socket /results/wattch-powerd.sock \
  --wattch /absolute/wattch-core/target/release/wattch \
  --out /results/physical/calibration-instrumented
```

Then stop `wattch-powerd` in its root terminal. The baseline subcommand fails
closed if the socket remains or if either `wattch-powerd` or `powermetrics` is
still visible. It replays the exact trusted commands and checks exact output:

```bash
python3 examples/agent-energy-benchmark/physical_calibration.py baseline \
  --instrumented /results/physical/calibration-instrumented/instrumented.json \
  --socket /results/wattch-powerd.sock \
  --out /results/physical/calibration-baseline

python3 examples/agent-energy-benchmark/physical_calibration.py finalize \
  --instrumented /results/physical/calibration-instrumented/instrumented.json \
  --baseline /results/physical/calibration-baseline/baseline.json \
  --assessment /results/physical/meter-calibration-assessment.json \
  --out /results/physical/meter-calibration.json \
  --calibration-id agent-energy-mac-250ms-v1
```

Finalization measures overhead, missingness, and phase sensitivity offline and
publishes `meter-calibration.json` only when all three strict thresholds pass.
Raw runs and the assessment remain available when a threshold fails. This is
the lower-level exploratory workflow specified in
[`docs/energy-impact-study.md`](../../docs/energy-impact-study.md#meter-calibration);
do not create a placeholder calibration JSON.

```bash
python3 examples/agent-energy-benchmark/physical_study.py run \
  --plan /results/physical/physical-plan.json \
  --socket /results/wattch-powerd.sock \
  --calibration /results/physical/meter-calibration.json \
  --wattch /absolute/wattch-core/target/release/wattch \
  --out /results/physical/study-001
```

The runner executes balanced unmeasured warm-ups, waits for cooldown, then runs
one measured qualification pilot. The 32-trial matrix starts only if the pilot
passes exact-output, mutation-scope, lifecycle, source, native-sample, marker,
gap, quality, and calibration gates. Each repeated call uses a distinct seed so
the declared useful work cannot collapse into a cached result. `RESULTS.json`
contains paired estimates and bootstrap intervals, while
`physical_comparison_claim_eligible` and remote-model/whole-machine claim flags
remain false for this exploratory path.
