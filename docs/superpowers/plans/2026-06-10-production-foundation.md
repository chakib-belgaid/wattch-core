# Wattch Production Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the four production subsystems from the 2026-06-10 design spec: socket-served daemons (testd + powerd), the pywattch Python client with a pyJoules-compatible API, a pyinstrument energy profiler, and the VSCode Energy Tests extension.

**Architecture:** Rust workspace gains protocol messages, a shared daemon runtime crate, and a powermeter daemon; Python client re-implements the wire protocol over Unix sockets in pure stdlib; the VSCode extension shells out to the Python runner and renders JSON. Spec: `docs/superpowers/specs/2026-06-10-production-foundation-design.md`.

**Tech Stack:** Rust (std Unix sockets, serde JSON payloads in WCH0 frames), Python 3.12 (stdlib + pyinstrument extra, pytest), TypeScript (tsc only, node --test).

**Verification commands (run after every task):**

```bash
cargo fmt --all && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace
# Python tasks:
clients/python/.venv/bin/python -m pytest clients/python/tests -q
# Extension tasks:
cd editors/vscode-energy-tests && npm run build && npm test
```

---

### Task 1: Protocol v0.1 messages + frame I/O

**Files:**
- Create: `crates/wattch-protocol/src/messages.rs` (HelloRequest, HelloResponse, StartCapture, StartCaptureAck, StopCapture, StopCaptureAck, ErrorMessage — serde JSON; `encode_message`/`decode_message` pairing FrameKind with payload type)
- Modify: `crates/wattch-protocol/src/frame.rs` (add `StartCaptureAck = 5`, `StopCaptureAck = 6`, `FLAG_PAYLOAD_PROTOBUF` const, `read_frame(r: &mut impl Read)`, `write_frame(w: &mut impl Write, …)`)
- Modify: `crates/wattch-protocol/src/lib.rs`, `crates/wattch-protocol/Cargo.toml` (serde, serde_json deps)

- [ ] Write round-trip tests: each message kind encode→decode equality; read_frame/write_frame over an in-memory cursor; unknown-kind and bad-magic errors.
- [ ] Implement; run `cargo test -p wattch-protocol`.
- [ ] Commit `feat(protocol): add v0.1 typed messages and frame I/O`.

### Task 2: wattch-daemon runtime crate

**Files:**
- Create: `crates/wattch-daemon/Cargo.toml`, `src/lib.rs`, `src/backend.rs` (`SampleBackend` trait), `src/server.rs` (listener loop, per-connection thread, session state machine, batching, sequence numbers, SIGINT/SIGTERM shutdown, stale socket unlink)
- Modify: root `Cargo.toml` workspace members.

- [ ] Integration test: spawn server on temp socket with a 2-source fixed backend; client side does hello → start → collect ≥2 batches → stop; assert descriptors, monotonic sequence numbers, ack counters.
- [ ] Error-path test: bad magic ⇒ Error frame + disconnect; StartCapture with unknown source id ⇒ Error frame.
- [ ] Implement; `cargo test -p wattch-daemon`.
- [ ] Commit `feat(daemon): shared socket serving runtime with session state machine`.

### Task 3: wattch-testd serves scenarios over the socket

**Files:**
- Modify: `crates/wattch-testd/src/main.rs` (split scenario types into `src/scenario.rs`; add `DeterministicBackend` impl of `SampleBackend`; `main` binds socket via wattch-daemon unless `--dry-run`)
- Modify: `crates/wattch-testd/Cargo.toml` (wattch-daemon dep)

- [ ] Test: backend produced from `examples/scenarios/laptop_cpu_gpu_nested.json` yields identical values to the existing `generate_batches` (golden equivalence) and serves over a socket end-to-end.
- [ ] Implement; `cargo test -p wattch-testd`; manual smoke: run daemon + connect.
- [ ] Commit `feat(testd): serve deterministic scenarios over unix socket`.

### Task 4: wattch-powerd powermeter daemon

**Files:**
- Create: `crates/wattch-powerd/Cargo.toml`, `src/main.rs` (clap: `--socket`, `--backend auto|linux-rapl|synthetic`, `--powercap-root`, `--node-id`), `src/rapl.rs` (powercap tree scan → descriptors with parent links, µJ cumulative, max_value from max_energy_range_uj; `sample()` re-reads energy_uj), `src/synthetic.rs`
- Modify: root `Cargo.toml`.
- Create: `docs/powerd.md` (install, privilege model, systemd unit), `packaging/wattch-powerd.service`.

- [ ] Tests: rapl scan against a fake powercap tree in tempdir (descriptor fields, hierarchy, values, missing/garbage file errors); synthetic backend monotonicity; end-to-end serve with synthetic backend.
- [ ] Implement; `cargo test -p wattch-powerd`.
- [ ] Commit `feat(powerd): powermeter daemon with linux-rapl and synthetic backends`.

### Task 5: wattch CLI against live daemons

**Files:**
- Modify: `crates/wattch-cli/src/main.rs` (thin arg parsing), create `src/client.rs` (blocking client: hello/capture), `src/artifact.rs` (write/read `runs/<run-id>/{manifest.json,sources.json,raw.trace.wch}`), `src/commands.rs` (sources/capture/inspect/validate)
- Modify: `crates/wattch-cli/Cargo.toml` (wattch-daemon dev-dep for tests, serde).

- [ ] Tests: capture against in-process daemon writes artifact; inspect replays it (sample counts, span, gap check); validate accepts the example scenario and rejects a corrupted one.
- [ ] Implement; `cargo test -p wattch-cli`; smoke `wattch sources` + `wattch capture` against testd.
- [ ] Commit `feat(cli): sources/capture/inspect/validate over socket with run artifacts`.

### Task 6: pywattch package + protocol + client

**Files:**
- Create: `clients/python/pyproject.toml`, `clients/python/pywattch/{__init__.py,_frames.py,_messages.py,client.py,errors.py}`
- Create: `clients/python/tests/{conftest.py (fake daemon fixture over tmp unix socket),test_frames.py,test_client.py}`

- [ ] venv setup: `python3 -m venv clients/python/.venv && .venv/bin/pip install -e 'clients/python[dev,profiler]'`.
- [ ] Tests: frame round-trip byte-compatibility with Rust constants (magic, header layout, kind values); client hello/sources/capture against fake daemon; connection-refused raises WattchConnectionError.
- [ ] Implement; pytest green.
- [ ] Commit `feat(python): pywattch protocol client`.

### Task 7: pyJoules-compatible meter API

**Files:**
- Create: `clients/python/pywattch/meter.py` (EnergyMeter start/record/stop/get_trace; cumulative delta with wrap correction via max_value; µJ→J; trapezoid integration for INSTANT watts), `handlers.py` (PrintHandler, CSVHandler), `decorator-level `measure_energy`, `EnergyContext` in `meter.py`
- Test: `clients/python/tests/test_meter.py`, `tests/test_handlers.py`

- [ ] Tests: trace math on scripted fake-daemon samples (multi-tag, wrap, unit conversion, instant integration); decorator and context manager invoke handler with correct domains.
- [ ] Implement; pytest green.
- [ ] Commit `feat(python): pyJoules-compatible energy meter API`.

### Task 8: pyinstrument profiler wrapper

**Files:**
- Create: `clients/python/pywattch/profiler.py` (EnergyProfiler context manager; attribution = self_time/total_time × measured J; render_text, to_dict)
- Test: `clients/python/tests/test_profiler.py` (attribution on synthetic frame tree; integration smoke with real pyinstrument run against fake daemon)

- [ ] Tests first, implement, pytest green.
- [ ] Commit `feat(python): pyinstrument energy profiler wrapper`.

### Task 9: energy test discovery + runner

**Files:**
- Create: `clients/python/pywattch/energytest/{__init__.py (energy_test decorator),discovery.py,runner.py,__main__.py (discover/run --json)}`
- Test: `clients/python/tests/test_energytest.py` (+ fixture tree under `tests/fixtures/energy_suite/`)

- [ ] Tests: discovery finds decorated + name-convention tests; run reports passed/failed/error/over-budget with joules from fake daemon; JSON schema stability (keys asserted).
- [ ] Implement; pytest green; manual `python -m pywattch.energytest run --root … --json`.
- [ ] Commit `feat(python): energy test discovery and runner CLI`.

### Task 10: VSCode Energy Tests extension

**Files:**
- Create: `editors/vscode-energy-tests/{package.json,tsconfig.json,src/extension.ts,src/runner.ts (spawn python runner, parse JSON),src/tree.ts (test tree provider),src/resultsPanel.ts (webview table),src/model.ts (types + JSON validation),test/model.test.ts,README.md,.vscodeignore}`

- [ ] `node --test` tests for model.ts parsing (valid runner output, malformed JSON, runner crash surface).
- [ ] Implement tree view, commands (runAll/runTest/refresh/openResults), webview results tab, settings; `npm run build` (tsc) clean.
- [ ] Commit `feat(vscode): energy tests extension with results tab`.

### Task 11: docs + final verification

**Files:**
- Modify: `README.md` (what exists now: daemons, client, profiler, energy tests, extension; quick start per subsystem), `docs/protocol-v0.md` (v0.1 message table, session flow), create `docs/energy-tests.md`.

- [ ] Full gate: cargo fmt/clippy/test, pytest, tsc + node --test, end-to-end smoke (testd → CLI capture → inspect; powerd-synthetic → energytest runner → extension-format JSON).
- [ ] Commit `docs: document production foundation subsystems`.
