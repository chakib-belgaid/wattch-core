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

The summary deliberately refuses to call synthetic values physical evidence
and never sums CPU, GPU, ANE, or nested RAPL domains. Physical-claim eligibility
defaults to false and additionally requires a complete bound capture, exact
correctness/useful work, accepted bound quality, calibration, and matching file
hashes. A single run never enables a comparison claim, and no local run measures
Luna or Terra inference in the provider datacenter.
