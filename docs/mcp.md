# Wattch MCP stdio adapter

`wattch-mcp` gives local coding agents a narrow, unprivileged route to the
existing Wattch daemon and immutable run artifacts. It is a client adapter, not
a daemon backend or an energy-analysis service.

## Register with Codex

Build an absolute server command and bind its file access to the project:

```bash
cargo build --release -p wattch-mcp

codex mcp add wattch -- \
  "$PWD/target/release/wattch-mcp" \
  --workspace-root "$PWD" \
  --runs-dir "$PWD/runs"
```

If the daemon socket is under `/tmp` or another non-discoverable development
path, register it explicitly:

```bash
codex mcp add wattch -- \
  "$PWD/target/release/wattch-mcp" \
  --workspace-root "$PWD" \
  --runs-dir "$PWD/runs" \
  --socket /absolute/path/to/wattch-testd.sock
```

The server speaks newline-delimited JSON-RPC over stdin/stdout. Stdout is
reserved for MCP messages; startup failures and diagnostics go to stderr. It
supports MCP revisions `2025-11-25` and `2025-06-18`, whose structured tool
result contracts match this adapter, and advertises only the tools capability.

## Tool contract

| Tool | Effect |
| --- | --- |
| `wattch_sources` | Connect and return the daemon identity plus full validated raw source descriptors. |
| `wattch_capture_start` | Start a background capture and return an opaque `capture_id` after pre-START evidence from every selected periodic source and the `WATTCH_WINDOW_START` event are persisted. |
| `wattch_capture_stop` | Record `WATTCH_WINDOW_END`, retain a bounded fresh-sample/native-period tail, stop and drain the daemon session, sync the artifact, and return an integrity summary. |
| `wattch_inspect` | Replay one artifact and return descriptor-bound counts, event kinds, timestamps, and sequence gaps. |
| `wattch_validate` | Validate exactly one inline descriptor array, descriptor JSON file, or run artifact. |

The intended stateful agent flow is:

```text
wattch_sources
wattch_capture_start -> capture_id
run the workload with the agent's ordinary shell/tooling
wattch_capture_stop(capture_id)
wattch_inspect(run_dir)
```

The MCP server never accepts a program or shell command. `wattch_capture_start`
accepts an optional portable `run_id`, an output root confined to configured
roots, an interval, optional source ids, and bounded string labels. Useful
coordinator-supplied labels include model, skill condition, prompt digest,
implementation revision, and repetition. A server-minted `capture_id`, not the
run id, authorizes the matching stop call.

## Capture-window marker semantics

`WATTCH_WINDOW_START` and `WATTCH_WINDOW_END` are capture-only coordination
markers stamped by the daemon's monotonic clock. They are deliberately separate
from research trial markers such as `TARGET_SPAWN`, `TARGET_READY`,
`TARGET_EXIT`, and `RECOVERY_END`.

After the END acknowledgement, the capture worker retains a bounded raw tail.
Readiness is tracked independently for every selected periodic source: ordinary
sources require a fresh sample at or after END, while each interval-average
source requires either two distinct fresh generations or a sample at least its
own native period past END. A fast source cannot satisfy a slower source's
requirement. The stop result reports `tail_complete` and
`tail_incomplete_source_ids`; incomplete endpoint coverage remains a synchronized
artifact but downstream interval analysis must reject or explicitly qualify it.

MCP captures also use append-only lifecycle evidence. `mcp-capture.json` is an
immutable start record and `mcp-capture-terminal.json` is created exactly once
after either a complete stop/drain/sync or an incomplete failure. `inspect`
reports this status alongside trace integrity. `validate` rejects managed
artifacts that are still capturing, failed, identity-mismatched, or missing the
required terminal evidence; legacy non-MCP artifacts remain valid.

## Evidence boundary

MCP output contains raw descriptors and artifact integrity facts only. It does
not:

- sum CPU, GPU, ANE, package, core, DRAM, or parent/child domains;
- convert power to an energy claim;
- subtract baselines or attribute whole-system energy to a process;
- compute joules/token or compare models/implementations;
- recommend an optimization.

`wattch-testd` and deterministic backends prove the tool/protocol plumbing; their
values do not react to a model workload and cannot demonstrate its energy
footprint. Real comparisons require a suitable `wattch-powerd` source,
sequential controlled trials, an explicit offline measurement boundary, and
repeated-run analysis. Apple `INTERVAL_AVERAGE` sources additionally require the
descriptor-linked offline native-interval reconstruction.

## Bounds and shutdown

- Stdio requests and structured tool results are capped at 1 MiB.
- Descriptor files are capped at 16 MiB.
- Descriptor inputs must be regular files. They are opened nonblocking and
  read through a limit-plus-one guard, so FIFOs/devices cannot stall the MCP
  loop and files that grow during reading cannot bypass the cap.
- Capture intervals are restricted to 10–60,000 ms.
- Labels, source-id arrays, active captures, startup time, and total capture
  duration are bounded.
- Input/output paths are canonicalized beneath `--workspace-root` or the
  configured `--runs-dir`; parent traversal and symlink escapes are rejected.
- Existing run directories are never overwritten.
- Capture admission is reserved atomically. Finished/max-duration workers are
  reaped before admission so their slots are released, while bounded terminal
  results remain retrievable by capture id.
- On stdin EOF or process drop, every owned capture is asked to stop and its
  worker is joined, preventing an orphaned daemon capture.
