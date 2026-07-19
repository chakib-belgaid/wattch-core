# CLI and MCP workflows

## MCP

Configure the server once:

```bash
cargo build --release -p wattch-mcp
codex mcp add wattch -- \
  /absolute/wattch-core/target/release/wattch-mcp \
  --workspace-root /absolute/project \
  --runs-dir /absolute/project/runs \
  --socket /absolute/wattch.sock
```

For each workload:

1. `wattch_sources`
2. `wattch_capture_start` with a unique run id, frozen interval, source ids, and condition labels
3. Run the workload normally with the agent's shell tool
4. `wattch_capture_stop` in cleanup, even if the workload fails
5. `wattch_inspect` and `wattch_validate` on the artifact

`capture_start` and `capture_stop` produce daemon-stamped `WATTCH_WINDOW_START` and `WATTCH_WINDOW_END` markers. The server returns bounded summaries; raw samples remain in the artifact.

## CLI

Use `wattch run` when the workload is one direct program invocation:

```bash
wattch run \
  --socket /absolute/wattch.sock \
  --out runs/energy-opt \
  --run-id candidate-r01 \
  --interval-ms 100 \
  --label treatment=candidate \
  --json \
  -- python3 benchmark.py --frozen-input input.json
```

No shell is inserted. Child stdout and stderr are stored as `target.stdout.log` and `target.stderr.log`; command provenance is stored in `command.json`. `TARGET_SPAWN`, `TARGET_EXEC`, and `TARGET_EXIT` delimit the command. A nonzero child exit still leaves an inspectable artifact and is passed through by the wrapper.

Validate structure:

```bash
wattch inspect --run runs/energy-opt/candidate-r01
wattch validate --input runs/energy-opt/candidate-r01/manifest.json
python3 <skill-directory>/scripts/check_evidence.py \
  runs/energy-opt/candidate-r01 \
  --source-uid <exact-source-uid> \
  --trial-record <coordinator-trial.json>
```

`inspect` reports frames, samples, gaps, and events. It does not integrate energy.
The evidence helper fails closed: a readable artifact or non-synthetic source
alone is never enough for physical-claim eligibility.
