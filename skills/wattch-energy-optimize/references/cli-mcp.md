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
2. `wattch_capture_start` with a unique run id, frozen interval, source ids, and condition labels. For calibrated evidence, labels must include `interval_ms`, `candidate_sha256`, and `measurement_overhead_calibration_sha256`; each value must match the coordinator-owned trial record.
3. Run the workload normally with the agent's shell tool
4. `wattch_capture_stop` in cleanup, even if the workload fails
5. `wattch_inspect` and `wattch_validate` on the artifact

`capture_start` and `capture_stop` produce daemon-stamped `WATTCH_WINDOW_START` and `WATTCH_WINDOW_END` markers. The server returns bounded summaries; raw samples remain in the artifact.

## CLI

Discover and freeze the daemon identity, clock instance, protocol version, and
complete source descriptors from the single JSON object emitted by:

```bash
wattch sources --socket /absolute/wattch.sock --json
```

Select source ids from that object. Do not scrape the default human-readable
table.

Use `wattch run` when the workload is one direct program invocation:

```bash
wattch run \
  --socket /absolute/wattch.sock \
  --out runs/energy-opt \
  --run-id candidate-r01 \
  --interval-ms 100 \
  --post-exit-capture-ms 0 \
  --timeout-ms 120000 \
  --label treatment=candidate \
  --label candidate_sha256=<exact-candidate-sha256> \
  --label interval_ms=100 \
  --label measurement_overhead_calibration_sha256=<calibration-json-sha256> \
  --json \
  -- python3 benchmark.py --frozen-input input.json
```

No shell is inserted. Child stdout and stderr are stored as `target.stdout.log` and `target.stderr.log`; command provenance is stored in `command.json`. `TARGET_SPAWN`, `TARGET_EXEC`, and `TARGET_EXIT` delimit the command. `--timeout-ms` terminates the target process group but still finalizes the evidence. A nonzero child exit still leaves a `COMPLETE`, inspectable artifact and is passed through by the wrapper; finalization and correctness are separate gates.

For an interval-average source that needs later native-interval reconstruction,
you may preregister a nonzero `--post-exit-capture-ms` value, up to 60000. The
CLI continues draining raw telemetry after `TARGET_EXIT`, records the duration
in `command.json`, and leaves the measured marker window unchanged. Freeze the
same value across comparison arms; never integrate the post-exit tail as target
work or add it after observing a failed run.

Validate structure:

```bash
wattch inspect --run runs/energy-opt/candidate-r01 --json
wattch validate --input runs/energy-opt/candidate-r01/manifest.json
python3 <skill-directory>/scripts/check_evidence.py \
  runs/energy-opt/candidate-r01 \
  --source-uid <exact-source-uid> \
  --trial-record <coordinator-trial.json>
```

`inspect --json` emits one object with frames, samples, per-source gaps, event
counts, unknown-source references, and command lifecycle status. It does not
integrate energy. Reject the run unless `command_lifecycle.status` is
`COMPLETE`; a successful inspect process alone does not make an incomplete
command eligible.
The evidence helper fails closed: a readable artifact or non-synthetic source
alone is never enough for physical-claim eligibility.

The coordinator-owned trial JSON must bind the artifact `run_id` and hashes,
the live candidate path and SHA-256, exact command/cwd/stdout/stderr/exit
checks, calibration path and digest, trace-derived marker duration, and valid
before/after mutation scopes with no unexpected changes. Canonical
`quality.json` uses `sequence_gaps`; the legacy name `sequence_gap_count` is
not accepted. Schema-v1 calibration can enable only
`descriptive_local_run_eligible`; physical and confirmatory claim flags remain
false.
