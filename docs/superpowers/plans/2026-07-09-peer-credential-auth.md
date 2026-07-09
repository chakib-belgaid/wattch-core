# Peer-Credential Auth + systemd Socket Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce kernel-verified peer authentication (`SO_PEERCRED`) at accept() time in the shared daemon server, and let `wattch-powerd` serve a systemd-activated socket, per the approved spec `docs/superpowers/specs/2026-07-09-peer-credential-auth-design.md`.

**Architecture:** A new `wattch-daemon/src/peer.rs` module holds a pure `PeerPolicy` (allow-lists) and a cfg-gated `peer_credentials()` reader (Linux: `SO_PEERCRED` via nix; non-Linux dev builds: `Ok(None)` = allow-all with a startup warning). `ServerConfig` gains `peer_policy` and replaces `socket_path: PathBuf` with a `SocketSource` enum (`Bind(PathBuf)` = today's behavior, `Activated(UnixListener)` = systemd-owned socket, no chmod/no unlink). The accept loop rejects unauthorized peers with a new protocol error code `ACCESS_DENIED = 6` before spawning the connection handler. Both daemon binaries grow repeatable `--allow-uid`/`--allow-gid` flags; `wattch-powerd` additionally picks up a `LISTEN_FDS` socket via the `listenfd` crate.

**Tech Stack:** Rust (workspace, edition 2021, rust-version 1.78), `nix` 0.29 (add `socket` feature), `listenfd` 1 (new dep, powerd only), clap 4 derive, systemd unit files.

## Global Constraints

- The workspace is **zero-`unsafe`**: never write an `unsafe` block; the raw-fd conversion must stay inside the `listenfd` crate.
- `nix` stays at workspace version `0.29` with `default-features = false`; features become `["user", "socket"]`.
- New protocol error code is exactly `ACCESS_DENIED: u32 = 6`, message string exactly `"access denied"`.
- Non-Linux builds are dev-only: peer auth compiles to allow-all with a `tracing::warn!` at startup — they must not pretend to enforce, and Linux-only tests are gated `#[cfg(target_os = "linux")]`.
- `--allow-gid` matches the peer's **effective** gid only (SO_PEERCRED reports no supplementary groups) — every place that documents the flag must say so.
- All existing suites must stay green with **no client-side changes**: `cargo fmt --all && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`; Python: `clients/python/.venv/bin/python -m pytest tests` run from `clients/python` (62 tests; integration tests need `target/debug/wattch-testd` built); extension: `cd editors/vscode-energy-tests && npm run build && npm test` (8 tests).
- Commit after every task; conventional-commit style messages (`feat:`, `docs:`, `test:` …) as used by recent history.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `crates/wattch-protocol/src/messages.rs` | modify | add `ACCESS_DENIED = 6` to `error_codes` |
| `docs/protocol-v0.md` | modify | error-code table gains code 6 |
| `Cargo.toml` (workspace) | modify | nix `socket` feature; new `listenfd` workspace dep |
| `crates/wattch-daemon/Cargo.toml` | modify | add `nix.workspace = true` |
| `crates/wattch-daemon/src/peer.rs` | **create** | `PeerPolicy`, `PeerCreds`, `peer_credentials()` + unit tests |
| `crates/wattch-daemon/src/lib.rs` | modify | export `peer` module types + `SocketSource` |
| `crates/wattch-daemon/src/server.rs` | modify | `SocketSource`, `peer_policy` field, accept-time enforcement, conditional unlink |
| `crates/wattch-daemon/tests/session.rs` | modify | update 2 helpers; add activated-socket test + deny-all rejection test |
| `crates/wattch-testd/tests/serve.rs` | modify | update 1 `ServerConfig` construction |
| `crates/wattch-cli/tests/capture.rs` | modify | update 1 `ServerConfig` construction |
| `crates/wattch-powerd/src/main.rs` | modify | `--allow-uid`/`--allow-gid`, listenfd activation, `--socket` conflict error |
| `crates/wattch-powerd/Cargo.toml` | modify | add `listenfd.workspace = true` |
| `crates/wattch-testd/src/main.rs` | modify | `--allow-uid`/`--allow-gid` (bind-only, no listenfd) |
| `packaging/wattch-powerd.socket` | **create** | declarative socket unit |
| `packaging/wattch-powerd.service` | modify | require socket unit, drop `--socket` flag, update comments |
| `docs/powerd.md` | modify | "Peer authentication" subsection, socket-activation docs |

---

### Task 1: `ACCESS_DENIED` protocol error code

**Files:**
- Modify: `crates/wattch-protocol/src/messages.rs:60-67`
- Modify: `docs/protocol-v0.md:96-103`

**Interfaces:**
- Produces: `wattch_protocol::messages::error_codes::ACCESS_DENIED: u32 = 6` — used by Task 4 (server enforcement) and Task 4's rejection test.

- [ ] **Step 1: Add the constant**

In `crates/wattch-protocol/src/messages.rs`, change the `error_codes` module:

```rust
pub mod error_codes {
    pub const PROTOCOL_VIOLATION: u32 = 1;
    pub const UNSUPPORTED_VERSION: u32 = 2;
    pub const UNKNOWN_SOURCE_ID: u32 = 3;
    pub const CAPTURE_ALREADY_ACTIVE: u32 = 4;
    pub const NO_ACTIVE_CAPTURE: u32 = 5;
    pub const ACCESS_DENIED: u32 = 6;
    pub const INTERNAL: u32 = 100;
}
```

- [ ] **Step 2: Update the protocol doc's error-code table**

In `docs/protocol-v0.md`, the code block listing error codes becomes:

```text
1   PROTOCOL_VIOLATION
2   UNSUPPORTED_VERSION
3   UNKNOWN_SOURCE_ID
4   CAPTURE_ALREADY_ACTIVE
5   NO_ACTIVE_CAPTURE
6   ACCESS_DENIED
100 INTERNAL
```

- [ ] **Step 3: Verify it builds and tests pass**

Run: `cargo test -p wattch-protocol`
Expected: PASS (existing round-trip tests unaffected).

- [ ] **Step 4: Commit**

```bash
git add crates/wattch-protocol/src/messages.rs docs/protocol-v0.md
git commit -m "feat(protocol): add ACCESS_DENIED error code (6) for peer auth rejections"
```

---

### Task 2: `peer.rs` — policy and credential reading

**Files:**
- Modify: `Cargo.toml` (workspace, line 24)
- Modify: `crates/wattch-daemon/Cargo.toml`
- Create: `crates/wattch-daemon/src/peer.rs`
- Modify: `crates/wattch-daemon/src/lib.rs`

**Interfaces:**
- Produces (used by Tasks 3–5 and tests):
  - `pub struct PeerPolicy { pub allowed_uids: Vec<u32>, pub allowed_gids: Vec<u32> }` (derives `Clone, Debug`; public fields so tests can build a deny-all literal)
  - `PeerPolicy::with_grants(extra_uids: &[u32], extra_gids: &[u32]) -> PeerPolicy`
  - `PeerPolicy::permits(&self, uid: u32, gid: u32) -> bool`
  - `pub struct PeerCreds { pub uid: u32, pub gid: u32, pub pid: i32 }`
  - `pub fn peer_credentials(stream: &std::os::unix::net::UnixStream) -> io::Result<Option<PeerCreds>>`

- [ ] **Step 1: Add the nix dependency**

In the workspace `Cargo.toml`, change line 24:

```toml
nix = { version = "0.29", default-features = false, features = ["user", "socket"] }
```

In `crates/wattch-daemon/Cargo.toml`, add to `[dependencies]`:

```toml
nix.workspace = true
```

- [ ] **Step 2: Write the failing unit tests**

Create `crates/wattch-daemon/src/peer.rs` containing ONLY the test module for now (plus the `use` lines the tests need), so the test run fails on missing items:

```rust
use std::io;
use std::os::unix::net::UnixStream;

#[cfg(test)]
mod tests {
    use super::*;

    fn stranger_uid() -> u32 {
        // Any uid that is neither 0 nor our own euid.
        let euid = nix::unistd::geteuid().as_raw();
        if euid == 42_424 {
            42_425
        } else {
            42_424
        }
    }

    #[test]
    fn root_is_always_permitted() {
        let policy = PeerPolicy::with_grants(&[], &[]);
        assert!(policy.permits(0, stranger_uid()));
    }

    #[test]
    fn own_euid_is_always_permitted() {
        let policy = PeerPolicy::with_grants(&[], &[]);
        assert!(policy.permits(nix::unistd::geteuid().as_raw(), stranger_uid()));
    }

    #[test]
    fn extra_uid_is_permitted() {
        let policy = PeerPolicy::with_grants(&[4242], &[]);
        assert!(policy.permits(4242, 999_999));
    }

    #[test]
    fn extra_gid_is_permitted() {
        let policy = PeerPolicy::with_grants(&[], &[5000]);
        assert!(policy.permits(stranger_uid(), 5000));
    }

    #[test]
    fn stranger_uid_and_gid_are_rejected() {
        let policy = PeerPolicy::with_grants(&[], &[]);
        assert!(!policy.permits(stranger_uid(), stranger_uid()));
    }

    #[test]
    fn with_grants_always_includes_root_and_own_euid() {
        let policy = PeerPolicy::with_grants(&[7], &[8]);
        assert!(policy.allowed_uids.contains(&0));
        assert!(policy
            .allowed_uids
            .contains(&nix::unistd::geteuid().as_raw()));
        assert!(policy.allowed_uids.contains(&7));
        assert_eq!(policy.allowed_gids, vec![8]);
    }

    /// `UnixStream::pair` peers are this process, so SO_PEERCRED must report
    /// our own euid and pid.
    #[cfg(target_os = "linux")]
    #[test]
    fn socketpair_reports_this_process_credentials() {
        let (a, _b) = UnixStream::pair().unwrap();
        let creds = peer_credentials(&a)
            .unwrap()
            .expect("Linux always provides SO_PEERCRED");
        assert_eq!(creds.uid, nix::unistd::geteuid().as_raw());
        assert_eq!(creds.pid, std::process::id() as i32);
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn non_linux_reports_no_credential_facility() {
        let (a, _b) = UnixStream::pair().unwrap();
        assert_eq!(peer_credentials(&a).unwrap(), None);
    }
}
```

Also register the module: in `crates/wattch-daemon/src/lib.rs`, change to:

```rust
pub mod backend;
pub mod peer;
pub mod server;

pub use backend::SampleBackend;
pub use peer::{PeerCreds, PeerPolicy};
pub use server::{spawn, DaemonIdentity, ServerConfig, ServerError, ServerHandle};
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cargo test -p wattch-daemon peer`
Expected: COMPILE ERROR — `cannot find struct PeerPolicy` / `cannot find function peer_credentials`.

- [ ] **Step 4: Write the implementation**

Add above the test module in `crates/wattch-daemon/src/peer.rs`:

```rust
//! Kernel-verified peer authentication for daemon sockets.
//!
//! RAPL-derived energy data is a documented side channel (PLATYPUS,
//! CVE-2020-8694), so file permissions on the socket are not the only
//! gate: the server also checks the connecting process's credentials as
//! reported by the kernel (`SO_PEERCRED`) at accept() time. Non-Linux
//! builds are development environments only (RAPL is Linux-only); they
//! have no credential facility wired up and allow all peers, with a
//! warning at startup.

/// Kernel-reported credentials of a connected Unix-socket peer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PeerCreds {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

/// Connection-level access policy, checked once per accepted connection.
#[derive(Clone, Debug)]
pub struct PeerPolicy {
    pub allowed_uids: Vec<u32>,
    pub allowed_gids: Vec<u32>,
}

impl PeerPolicy {
    /// The default policy plus explicit grants: uid 0 and the daemon's own
    /// effective uid are always allowed. `extra_gids` match the peer's
    /// *effective* gid only — `SO_PEERCRED` does not report supplementary
    /// groups; those are the job of the socket file's 0660 permissions.
    pub fn with_grants(extra_uids: &[u32], extra_gids: &[u32]) -> Self {
        let mut allowed_uids = vec![0, nix::unistd::geteuid().as_raw()];
        allowed_uids.extend_from_slice(extra_uids);
        Self {
            allowed_uids,
            allowed_gids: extra_gids.to_vec(),
        }
    }

    pub fn permits(&self, uid: u32, gid: u32) -> bool {
        self.allowed_uids.contains(&uid) || self.allowed_gids.contains(&gid)
    }
}

/// Read the peer's kernel-verified credentials.
///
/// `Ok(None)` means the platform has no credential facility (non-Linux
/// dev builds); the caller treats that as allow-all. An `Err` means the
/// read failed on a platform that has one — callers must treat that as a
/// denial, never as an allow.
#[cfg(target_os = "linux")]
pub fn peer_credentials(stream: &UnixStream) -> io::Result<Option<PeerCreds>> {
    let creds =
        nix::sys::socket::getsockopt(stream, nix::sys::socket::sockopt::PeerCredentials)
            .map_err(io::Error::from)?;
    Ok(Some(PeerCreds {
        uid: creds.uid(),
        gid: creds.gid(),
        pid: creds.pid(),
    }))
}

#[cfg(not(target_os = "linux"))]
pub fn peer_credentials(_stream: &UnixStream) -> io::Result<Option<PeerCreds>> {
    Ok(None)
}
```

(Keep the `use std::io;` / `use std::os::unix::net::UnixStream;` lines from Step 2 at the top. On non-Linux, if `io` triggers an unused-import warning, keep it used by the signature — the non-Linux function still returns `io::Result`, so it will not warn.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p wattch-daemon peer`
Expected: PASS — 6 tests on macOS (7 on Linux; the platform-gated ones swap).

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml crates/wattch-daemon/Cargo.toml crates/wattch-daemon/src/peer.rs crates/wattch-daemon/src/lib.rs Cargo.lock
git commit -m "feat(daemon): PeerPolicy and SO_PEERCRED credential reading in new peer module"
```

---

### Task 3: `SocketSource` + `peer_policy` plumbing (no enforcement yet)

**Files:**
- Modify: `crates/wattch-daemon/src/server.rs` (struct defs at lines 29-33, `ServerHandle` at 46-72, `spawn` at 76-143, `permission_tests` construction sites at ~581-587 and ~611-617)
- Modify: `crates/wattch-daemon/src/lib.rs`
- Modify: `crates/wattch-daemon/tests/session.rs` (helper at 69-88, direct construction at ~347-355)
- Modify: `crates/wattch-testd/tests/serve.rs` (~line 40)
- Modify: `crates/wattch-cli/tests/capture.rs` (~line 56)
- Modify: `crates/wattch-powerd/src/main.rs` (~lines 93-107)
- Modify: `crates/wattch-testd/src/main.rs` (~lines 56-67)
- Test: `crates/wattch-daemon/tests/session.rs` (new `activated_listener_serves_and_socket_survives_shutdown`)

**Interfaces:**
- Consumes: `PeerPolicy::with_grants(&[u32], &[u32])` from Task 2.
- Produces (used by Tasks 4–5):
  - `pub enum SocketSource { Bind(PathBuf), Activated(UnixListener) }` (std `UnixListener`; enum derives nothing — `UnixListener` is not `Clone`)
  - `pub struct ServerConfig { pub socket: SocketSource, pub identity: DaemonIdentity, pub peer_policy: PeerPolicy }` — **`Clone` derive removed** (`spawn` takes it by value; no caller clones it today)
  - `ServerHandle` behavior: unlinks the socket file on `shutdown()`/`wait()` only for `Bind`; `socket_path()` still returns the path in both modes (from `local_addr()` for `Activated`).

- [ ] **Step 1: Write the failing integration test**

Append to `crates/wattch-daemon/tests/session.rs`:

```rust
/// SocketSource::Activated covers the systemd-socket-activation serving
/// path on every platform: the test plays the role of systemd by binding
/// the listener itself. The server must serve sessions on it and must NOT
/// unlink the socket file on shutdown — the activator owns its lifecycle.
#[test]
fn activated_listener_serves_and_socket_survives_shutdown() {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-activated-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);
    let listener = std::os::unix::net::UnixListener::bind(&socket_path).unwrap();

    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Activated(listener),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
        },
        || Box::new(CountingBackend::new()),
    )
    .expect("server should serve a pre-bound listener");
    assert_eq!(handle.socket_path(), socket_path.as_path());

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    match hello(&mut stream) {
        Message::HelloResponse(response) => assert_eq!(response.node_id, "node-test"),
        other => panic!("expected HelloResponse, got {other:?}"),
    }
    drop(stream);

    handle.shutdown();
    assert!(
        socket_path.exists(),
        "activated sockets belong to the activator; shutdown must not unlink"
    );
    let _ = std::fs::remove_file(&socket_path);
}
```

And update the test file's import line (line 10) to:

```rust
use wattch_daemon::{
    spawn, DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, ServerHandle, SocketSource,
};
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cargo test -p wattch-daemon --test session`
Expected: COMPILE ERROR — `SocketSource` not found, `ServerConfig` has no field `socket`/`peer_policy`.

- [ ] **Step 3: Restructure `server.rs`**

In `crates/wattch-daemon/src/server.rs`:

3a. Add the import at the top (after the existing `use crate::backend::SampleBackend;`):

```rust
use crate::peer::PeerPolicy;
```

3b. Replace the `ServerConfig` definition (lines 29-33) with:

```rust
/// Where the listening socket comes from.
#[derive(Debug)]
pub enum SocketSource {
    /// Bind `path` ourselves: create the parent directory (0750), clean a
    /// stale socket, bind, chmod 0660, and unlink on shutdown.
    Bind(PathBuf),
    /// Serve on a listener bound by someone else (systemd socket
    /// activation). Mode, ownership, and lifecycle of the socket file
    /// belong to the activator: no chmod, no unlink on shutdown.
    Activated(UnixListener),
}

/// Not `Clone`: `SocketSource::Activated` holds a `UnixListener`.
#[derive(Debug)]
pub struct ServerConfig {
    pub socket: SocketSource,
    pub identity: DaemonIdentity,
    pub peer_policy: PeerPolicy,
}
```

3c. Replace `ServerHandle` (lines 46-72) with:

```rust
/// Handle to a running server. Dropping it does not stop the server;
/// call [`ServerHandle::shutdown`].
pub struct ServerHandle {
    socket_path: PathBuf,
    /// True when we bound the socket ourselves (`SocketSource::Bind`) and
    /// therefore own the socket file's lifecycle.
    owns_socket_file: bool,
    stop: Arc<AtomicBool>,
    accept_thread: thread::JoinHandle<()>,
}

impl ServerHandle {
    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    /// Stop accepting connections and join the accept loop. Connections in
    /// flight finish their current session on their own threads.
    pub fn shutdown(self) {
        self.stop.store(true, Ordering::SeqCst);
        // Wake the blocking accept() with a throwaway connection.
        let _ = UnixStream::connect(&self.socket_path);
        let _ = self.accept_thread.join();
        if self.owns_socket_file {
            let _ = std::fs::remove_file(&self.socket_path);
        }
    }

    /// Block until the server is shut down externally (for daemon mains).
    pub fn wait(self) {
        let _ = self.accept_thread.join();
        if self.owns_socket_file {
            let _ = std::fs::remove_file(&self.socket_path);
        }
    }
}
```

3d. Replace the body of `spawn` from `ensure_socket_parent_dir(&config.socket_path)?;` (line 84) through the `info!` line (line 108) with:

```rust
    let (listener, socket_path, owns_socket_file) = match config.socket {
        SocketSource::Bind(path) => {
            ensure_socket_parent_dir(&path)?;

            if path.exists() {
                // A previous daemon may have crashed without unlinking its
                // socket. If nothing accepts connections there, the socket
                // is stale.
                if UnixStream::connect(&path).is_ok() {
                    return Err(ServerError::Io(io::Error::new(
                        io::ErrorKind::AddrInUse,
                        format!("another daemon is already listening on {}", path.display()),
                    )));
                }
                warn!(path = %path.display(), "removing stale socket");
                std::fs::remove_file(&path)?;
            }

            let listener = UnixListener::bind(&path)?;
            // energy_uj-derived samples are a documented side channel
            // (PLATYPUS, CVE-2020-8694); the socket must not be group/world
            // writable, and the owned parent directory (mode 0750, set
            // above) is the TOCTOU mitigation for the stale-socket probe
            // above.
            fs::set_permissions(&path, Permissions::from_mode(0o660))?;
            (listener, path, true)
        }
        SocketSource::Activated(listener) => {
            // systemd owns the socket file: its mode/ownership come from
            // the .socket unit and it is removed by RemoveOnStop=, so no
            // chmod and no unlink here. The path is still needed for the
            // shutdown wake-up connect and for logging.
            let addr = listener.local_addr()?;
            let Some(path) = addr.as_pathname() else {
                return Err(ServerError::Io(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "activated listener has no filesystem path \
                     (unnamed/abstract sockets are unsupported)",
                )));
            };
            let path = path.to_path_buf();
            (listener, path, false)
        }
    };
    info!(path = %socket_path.display(), "listening");
```

3e. Just below, the setup lines (previously 110-114) become:

```rust
    let stop = Arc::new(AtomicBool::new(false));
    let accept_stop = Arc::clone(&stop);
    let identity = config.identity;
    let factory = Arc::new(factory);
```

(The old `let socket_path = config.socket_path.clone();` disappears — `socket_path` already exists from the match. `config.peer_policy` is intentionally unused until Task 4; add `let _peer_policy = config.peer_policy;` before the thread spawn so clippy stays clean, with no comment — Task 4 replaces it.)

3f. The `Ok(ServerHandle { ... })` at the end of `spawn` becomes:

```rust
    Ok(ServerHandle {
        socket_path,
        owns_socket_file,
        stop,
        accept_thread,
    })
```

3g. In `permission_tests` (bottom of `server.rs`), update both `spawn(ServerConfig { ... })` calls. First test:

```rust
        let handle = spawn(
            ServerConfig {
                socket: SocketSource::Bind(socket_path.clone()),
                identity: identity(),
                peer_policy: PeerPolicy::with_grants(&[], &[]),
            },
            || Box::new(NoopBackend::new()),
        )
        .expect("server should start and create its own runtime dir");
```

Second test (`bind_fails_clearly_when_grandparent_is_missing`):

```rust
        let result = spawn(
            ServerConfig {
                socket: SocketSource::Bind(socket_path),
                identity: identity(),
                peer_policy: PeerPolicy::with_grants(&[], &[]),
            },
            || Box::new(NoopBackend::new()),
        );
```

(`use super::*;` in that module already brings in `SocketSource` and `PeerPolicy` via the new top-of-file import.)

3h. Update `crates/wattch-daemon/src/lib.rs`'s server re-export line to:

```rust
pub use server::{spawn, DaemonIdentity, ServerConfig, ServerError, ServerHandle, SocketSource};
```

- [ ] **Step 4: Update the remaining construction sites**

Each site changes `socket_path: <path>` to `socket: SocketSource::Bind(<path>)` and gains `peer_policy: PeerPolicy::with_grants(&[], &[])`, plus the two imports. Exact edits:

4a. `crates/wattch-daemon/tests/session.rs` — helper `start_server` (line ~75) and the direct construction in `stop_capture_ack_reports_counts_sent_before_producer_panics` (line ~347):

```rust
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
        },
```

(Import line already updated in Step 1.)

4b. `crates/wattch-testd/tests/serve.rs` — import becomes `use wattch_daemon::{DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, SocketSource};` and the construction follows the same pattern as 4a (keep its existing `identity` value).

4c. `crates/wattch-cli/tests/capture.rs` — import becomes `use wattch_daemon::{DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, ServerHandle, SocketSource};`, same construction pattern.

4d. `crates/wattch-powerd/src/main.rs` — import becomes `use wattch_daemon::{DaemonIdentity, PeerPolicy, SampleBackend, ServerConfig, SocketSource};` and the `spawn` call becomes:

```rust
    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path),
            identity: DaemonIdentity {
                daemon_name: "wattch-powerd".to_string(),
                daemon_version: env!("CARGO_PKG_VERSION").to_string(),
                node_id: args.node_id.clone(),
            },
            peer_policy: PeerPolicy::with_grants(&[], &[]),
        },
        move || make_backend(),
    )
    .context("failed to start wattch-powerd")?;
```

4e. `crates/wattch-testd/src/main.rs` — import becomes `use wattch_daemon::{DaemonIdentity, PeerPolicy, ServerConfig, SocketSource};` and:

```rust
    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path),
            identity,
            peer_policy: PeerPolicy::with_grants(&[], &[]),
        },
        move || Box::new(DeterministicBackend::new(&scenario)),
    )
    .context("failed to start wattch-testd server")?;
```

- [ ] **Step 5: Run the workspace tests**

Run: `cargo test --workspace`
Expected: PASS, including the new `activated_listener_serves_and_socket_survives_shutdown`.

Run: `cargo clippy --workspace --all-targets -- -D warnings`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add crates/wattch-daemon crates/wattch-testd crates/wattch-powerd crates/wattch-cli
git commit -m "feat(daemon): SocketSource (Bind/Activated) and peer_policy in ServerConfig"
```

---

### Task 4: Accept-time enforcement

**Files:**
- Modify: `crates/wattch-daemon/src/server.rs` (imports, `spawn` accept loop)
- Test: `crates/wattch-daemon/tests/session.rs` (new `deny_all_policy_rejects_with_access_denied`, Linux-gated)

**Interfaces:**
- Consumes: `peer_credentials`, `PeerPolicy::permits`, `PeerCreds` (Task 2); `error_codes::ACCESS_DENIED` (Task 1).
- Produces: rejected peers receive `Error { code: 6, message: "access denied" }` and the connection closes; no API change.

- [ ] **Step 1: Write the failing test**

Append to `crates/wattch-daemon/tests/session.rs`:

```rust
/// Linux-only: on non-Linux dev builds there are no peer credentials, so
/// there is nothing to deny (the server allows all peers by design there).
#[cfg(target_os = "linux")]
#[test]
fn deny_all_policy_rejects_with_access_denied() {
    let socket_path = std::env::temp_dir().join(format!(
        "wattch-daemon-test-deny-all-{}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&socket_path);

    let handle = spawn(
        ServerConfig {
            socket: SocketSource::Bind(socket_path.clone()),
            identity: DaemonIdentity {
                daemon_name: "wattch-daemon-test".to_string(),
                daemon_version: "0.0.0".to_string(),
                node_id: "node-test".to_string(),
            },
            // Built literally (fields are public) — with_grants() would
            // always re-admit our own euid.
            peer_policy: PeerPolicy {
                allowed_uids: vec![],
                allowed_gids: vec![],
            },
        },
        || Box::new(CountingBackend::new()),
    )
    .expect("server should start");

    let mut stream = UnixStream::connect(&socket_path).unwrap();
    match read_message(&mut stream) {
        Ok(Message::Error(err)) => {
            assert_eq!(err.code, error_codes::ACCESS_DENIED);
            assert_eq!(err.message, "access denied");
        }
        other => panic!("expected ACCESS_DENIED Error frame, got {other:?}"),
    }
    // After the error frame the daemon closes the connection.
    assert!(
        read_message(&mut stream).is_err(),
        "connection must be closed after rejection"
    );

    // Shutdown still works under deny-all: the wake-up connect only needs
    // the accept() to fire; rejection happens after accept.
    handle.shutdown();
}
```

- [ ] **Step 2: Run tests to verify current state**

Run: `cargo test -p wattch-daemon --test session`
Expected on macOS: PASS but the new test is filtered out (cfg-gated) — that is fine; it runs in Linux CI. Expected on Linux: FAIL — the client receives a `HelloResponse` path (server never sends the error) or hangs until the read times out; the assertion on `Message::Error` panics.

- [ ] **Step 3: Implement enforcement in the accept loop**

In `crates/wattch-daemon/src/server.rs`:

3a. Change the peer import to:

```rust
use crate::peer::{peer_credentials, PeerPolicy};
```

3b. Add `error_codes::ACCESS_DENIED` usage — the `error_codes` import at the top of the file already imports the whole module, no change needed.

3c. In `spawn`, delete the Task 3 placeholder `let _peer_policy = config.peer_policy;` and instead, right before `let accept_thread = thread::spawn(...)`:

```rust
    let peer_policy = config.peer_policy;
    #[cfg(not(target_os = "linux"))]
    warn!(
        "peer authentication unavailable on this platform; \
         dev builds only — production target is Linux"
    );
```

3d. Replace the `Ok(stream) => { ... }` arm of the accept loop with:

```rust
                Ok(mut stream) => {
                    // Kernel-verified peer check before any protocol I/O.
                    // A credential-read failure is a deny, not an allow;
                    // Ok(None) means this platform has no credential
                    // facility (non-Linux dev builds — warned at startup).
                    match peer_credentials(&stream) {
                        Ok(None) => {}
                        Ok(Some(creds)) if peer_policy.permits(creds.uid, creds.gid) => {}
                        Ok(Some(creds)) => {
                            warn!(
                                uid = creds.uid,
                                gid = creds.gid,
                                pid = creds.pid,
                                "rejecting peer: uid/gid not in allowed lists"
                            );
                            send_error(&mut stream, error_codes::ACCESS_DENIED, "access denied");
                            continue;
                        }
                        Err(err) => {
                            warn!(error = %err, "rejecting peer: cannot read credentials");
                            send_error(&mut stream, error_codes::ACCESS_DENIED, "access denied");
                            continue;
                        }
                    }
                    let identity = identity.clone();
                    let backend = factory();
                    thread::spawn(move || {
                        if let Err(err) = handle_connection(stream, &identity, backend) {
                            debug!(error = %err, "connection ended with error");
                        }
                    });
                }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings`
Expected: PASS/clean on macOS (existing tests prove the `Ok(None)` allow path; on Linux they prove the real `SO_PEERCRED` allow path since every test client is the daemon's own uid). If a Linux box or container is available, also run `cargo test -p wattch-daemon --test session` there to see `deny_all_policy_rejects_with_access_denied` pass; otherwise CI covers it.

- [ ] **Step 5: Commit**

```bash
git add crates/wattch-daemon
git commit -m "feat(daemon): enforce peer credentials at accept time (SO_PEERCRED, deny on failure)"
```

---

### Task 5: Daemon CLI flags + powerd socket activation

**Files:**
- Modify: `Cargo.toml` (workspace `[workspace.dependencies]`)
- Modify: `crates/wattch-powerd/Cargo.toml`
- Modify: `crates/wattch-powerd/src/main.rs`
- Modify: `crates/wattch-testd/src/main.rs`

**Interfaces:**
- Consumes: `PeerPolicy::with_grants`, `SocketSource::{Bind, Activated}` (Tasks 2-3).
- Produces: `--allow-uid <UID>` / `--allow-gid <GID>` (repeatable) on both daemons; `LISTEN_FDS` support on `wattch-powerd`; startup error when `--socket` is combined with an activated fd.

- [ ] **Step 1: Add the listenfd dependency**

Workspace `Cargo.toml`, in `[workspace.dependencies]` (alphabetical, after `ctrlc`):

```toml
listenfd = "1"
```

`crates/wattch-powerd/Cargo.toml`, in `[dependencies]`:

```toml
listenfd.workspace = true
```

- [ ] **Step 2: Add the flags to `wattch-powerd`**

In `crates/wattch-powerd/src/main.rs`, add to `struct Args` (after `seed`):

```rust
    /// Additional uid allowed to connect (repeatable). uid 0 and the
    /// daemon's own effective uid are always allowed.
    #[arg(long = "allow-uid", value_name = "UID")]
    allow_uid: Vec<u32>,

    /// Additional gid allowed to connect (repeatable). Matches the peer's
    /// EFFECTIVE gid only — supplementary group membership does not count;
    /// use the socket file's group permissions (or --allow-uid) for that.
    #[arg(long = "allow-gid", value_name = "GID")]
    allow_gid: Vec<u32>,
```

- [ ] **Step 3: Wire socket activation and the conflict error**

Still in `crates/wattch-powerd/src/main.rs`, replace the socket-path resolution and `spawn` call (the `let socket_path = ...` block and the `ServerConfig` construction from Task 3 Step 4d) with:

```rust
    // systemd socket activation: if systemd handed us a pre-bound socket
    // (LISTEN_FDS), serve on it. The listenfd crate owns the only unsafe
    // raw-fd conversion; this crate stays zero-unsafe.
    let activated = listenfd::ListenFd::from_env()
        .take_unix_listener(0)
        .context("systemd-activated fd 0 is not a Unix stream listener")?;

    let socket = match activated {
        Some(listener) => {
            if let Some(socket) = &args.socket {
                anyhow::bail!(
                    "--socket {} conflicts with a systemd-activated socket \
                     (LISTEN_FDS is set); configure the path in the .socket \
                     unit or drop the flag",
                    socket.display()
                );
            }
            tracing::info!("using systemd-activated socket");
            SocketSource::Activated(listener)
        }
        None => SocketSource::Bind(
            args.socket
                .clone()
                .unwrap_or_else(|| wattch_protocol::socket::default_bind_path("wattch-powerd.sock")),
        ),
    };

    let handle = wattch_daemon::spawn(
        ServerConfig {
            socket,
            identity: DaemonIdentity {
                daemon_name: "wattch-powerd".to_string(),
                daemon_version: env!("CARGO_PKG_VERSION").to_string(),
                node_id: args.node_id.clone(),
            },
            peer_policy: PeerPolicy::with_grants(&args.allow_uid, &args.allow_gid),
        },
        move || make_backend(),
    )
    .context("failed to start wattch-powerd")?;
```

- [ ] **Step 4: Add the flags to `wattch-testd` (bind-only, no listenfd)**

In `crates/wattch-testd/src/main.rs`, add the same two fields to `struct Args` (identical doc comments as Step 2), and change the `spawn` call's policy line to:

```rust
            peer_policy: PeerPolicy::with_grants(&args.allow_uid, &args.allow_gid),
```

(`args.socket` was consumed by the `socket_path` binding earlier in that file; if the borrow checker complains after your edits, use `args.socket.clone()` there — testd keeps its existing `SocketSource::Bind(socket_path)` from Task 3.)

- [ ] **Step 5: Verify build, help text, and a live smoke test**

Run: `cargo build --workspace && cargo run -p wattch-powerd -- --help`
Expected: builds; help lists `--allow-uid <UID>` and `--allow-gid <GID>` with the egid caveat visible.

Smoke test the default policy end-to-end (same user connects and is allowed):

```bash
cargo run -p wattch-powerd -- --backend synthetic --socket /tmp/wattch-plan-smoke.sock &
sleep 1
cargo test -p wattch-cli   # cli tests spawn their own server; this re-proves clients
kill %1; rm -f /tmp/wattch-plan-smoke.sock
```

Expected: powerd prints its serving line; cli tests pass.

Run: `cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`
Expected: clean/PASS.

- [ ] **Step 6: Commit**

```bash
git add Cargo.toml Cargo.lock crates/wattch-powerd crates/wattch-testd
git commit -m "feat(daemons): --allow-uid/--allow-gid flags; systemd socket activation for powerd"
```

---

### Task 6: Packaging — socket unit + service update

**Files:**
- Create: `packaging/wattch-powerd.socket`
- Modify: `packaging/wattch-powerd.service`

**Interfaces:**
- Consumes: powerd's `LISTEN_FDS` support and the `--socket`-conflict rule (Task 5) — which is why `ExecStart` must NOT pass `--socket`.

- [ ] **Step 1: Create `packaging/wattch-powerd.socket`**

```ini
[Unit]
Description=Wattch powermeter daemon socket
Documentation=https://github.com/chakib-belgaid/wattch-core

[Socket]
ListenStream=/run/wattch/wattch-powerd.sock
# The socket file is the security boundary for RAPL side-channel data
# (PLATYPUS, CVE-2020-8694): group access only, never world-open.
SocketMode=0660
SocketUser=root
# To grant a group of trusted local users access to live measurements,
# create the group (e.g. `groupadd --system wattch`) or change the name,
# then uncomment. File permissions honor supplementary group membership,
# unlike the daemon's --allow-gid flag (effective gid only).
#SocketGroup=wattch
DirectoryMode=0750
RemoveOnStop=true

[Install]
WantedBy=sockets.target
```

- [ ] **Step 2: Update `packaging/wattch-powerd.service`**

Replace the whole file with:

```ini
[Unit]
Description=Wattch powermeter daemon (privileged RAPL acquisition)
Documentation=https://github.com/chakib-belgaid/wattch-core
After=local-fs.target wattch-powerd.socket
Requires=wattch-powerd.socket

[Service]
Type=simple
# No --socket flag: with wattch-powerd.socket active, systemd passes the
# pre-bound socket via LISTEN_FDS and the daemon serves on it (path, mode,
# and ownership live in the .socket unit). Passing --socket alongside an
# activated fd is a startup error. Without the socket unit (self-bind
# fallback), the daemon binds /run/wattch/wattch-powerd.sock itself when
# running as root — same path, imperative mode.
ExecStart=/usr/local/bin/wattch-powerd \
  --backend linux-rapl \
  --node-id %H
RuntimeDirectory=wattch
RuntimeDirectoryMode=0750
# The daemon only needs read access to /sys/class/powercap. Running as a
# dedicated user with the powercap files made group-readable is preferred;
# running as root works out of the box.
User=root

# --- Access model -----------------------------------------------------------
# Fine-grained RAPL energy counters are a documented side channel (PLATYPUS,
# CVE-2020-8694), which is why modern kernels restrict energy_uj to root.
# wattch-powerd re-exposes those counters over this socket. Two layers gate
# access:
#   1. File permissions: /run/wattch is 0750 and the socket 0660, both set
#      declaratively in wattch-powerd.socket (SocketMode=/SocketGroup=) under
#      socket activation, or imperatively by the daemon when self-binding.
#      Grant a trusted group via SocketGroup= in the .socket unit.
#   2. Kernel-verified peer credentials (SO_PEERCRED), checked at accept()
#      time: by default only uid 0 and the daemon's own uid may connect.
#      Broaden with repeatable --allow-uid/--allow-gid flags on ExecStart.
#      Note --allow-gid matches the peer's EFFECTIVE gid only; supplementary
#      group membership is only honored by layer 1's file permissions.
# -----------------------------------------------------------------------------
Restart=on-failure
RestartSec=2

# Hardening: the daemon is dumb on purpose — it reads counters and serves a
# local socket, nothing else.
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadOnlyPaths=/sys/class/powercap

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Sanity-check the units**

If on a Linux box with systemd: `systemd-analyze verify packaging/wattch-powerd.socket packaging/wattch-powerd.service` (expect only a complaint about the unit not being in a search path). On macOS, skip — review by eye that `ListenStream` matches the service's default root path (`/run/wattch/wattch-powerd.sock`) and that `ExecStart` has no `--socket`.

- [ ] **Step 4: Commit**

```bash
git add packaging/wattch-powerd.socket packaging/wattch-powerd.service
git commit -m "feat(packaging): systemd socket unit for wattch-powerd; service requires it"
```

---

### Task 7: Documentation + full verification

**Files:**
- Modify: `docs/powerd.md` (after the "Privilege model" section's existing text, and the "systemd" section)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-6; documents the egid caveat verbatim from the spec.

- [ ] **Step 1: Add the "Peer authentication" subsection**

In `docs/powerd.md`, insert after the last paragraph of `## Privilege model` (the one ending "...rather than widening the socket's own permissions."):

```markdown
### Peer authentication

File permissions are only one layer. The daemon also checks every
connecting process's kernel-verified credentials (`SO_PEERCRED`) at
accept() time, before any protocol traffic:

- **Default policy:** uid 0 and the daemon's own effective uid may
  connect; everyone else receives an `Error` frame (code 6,
  `ACCESS_DENIED`, message "access denied") and the connection is closed.
  Rejections are logged with the peer's uid, gid, and pid.
- **Broadening access:** repeatable `--allow-uid <UID>` and
  `--allow-gid <GID>` flags on `wattch-powerd` (and `wattch-testd`).
- **egid caveat:** `SO_PEERCRED` reports the peer's *effective* gid, not
  supplementary groups. `--allow-gid` therefore matches egid only: a user
  whose supplementary groups include `wattch` will NOT pass the gid
  check. Group-based human access remains the job of the 0660 socket
  file permissions (which do honor supplementary groups); `--allow-uid`
  is the explicit-grant mechanism.
- **Non-Linux builds** (development only; RAPL is Linux-only) have no
  credential facility: the daemon allows all peers and logs a warning at
  startup. Production deployments are Linux.
```

- [ ] **Step 2: Update the systemd section**

Replace the `## systemd` section body with:

```markdown
Two units are provided in `packaging/`. The recommended deployment is
socket activation: systemd owns the socket file (path, mode, group) and
starts the daemon on demand.

```bash
sudo install -m 755 target/release/wattch-powerd /usr/local/bin/
sudo install -m 644 packaging/wattch-powerd.socket /etc/systemd/system/
sudo install -m 644 packaging/wattch-powerd.service /etc/systemd/system/
sudo systemctl enable --now wattch-powerd.socket
```

Under activation the daemon detects the passed fd (`LISTEN_FDS`) and
serves on it; it neither chmods nor unlinks the socket — the `.socket`
unit's `SocketMode=`/`SocketGroup=`/`RemoveOnStop=` govern its lifecycle.
Passing `--socket` while an activated fd is present is a startup error
(conflicting intent is never silently resolved). Without the socket unit,
the daemon self-binds exactly as before; peer authentication applies
identically on both paths.

To try activation without installing units:

```bash
systemd-socket-activate -l /tmp/test.sock ./target/debug/wattch-powerd --backend synthetic
wattch sources --socket /tmp/test.sock
```
```

- [ ] **Step 3: Update the failure-behavior table**

In `docs/powerd.md`'s `## Failure behavior` block, extend the `runtime` line's block with a new line after it:

```text
access     a peer that fails the credential check gets Error code 6
           (ACCESS_DENIED) and is disconnected; the event is logged at warn
```

- [ ] **Step 4: Run every verification gate**

```bash
cargo fmt --all
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cd clients/python && .venv/bin/python -m pytest tests; cd ../..
cd editors/vscode-energy-tests && npm run build && npm test; cd ../..
```

Expected: fmt makes no diff (or commit its cleanup), clippy clean, all Rust tests pass, 62 Python tests pass (integration tests need `target/debug/wattch-testd`, built by the workspace test step), 8 extension tests pass. **No files under `clients/` or `editors/` were modified by this plan — if a client test fails, the daemon change broke compatibility; stop and investigate rather than editing the client.**

- [ ] **Step 5: Commit**

```bash
git add docs/powerd.md
git commit -m "docs(powerd): peer authentication policy, egid caveat, socket-activation deployment"
```

---

## Verification on Linux (manual, documented — not automated)

From the spec; run when a Linux host is available (CI runs the Linux-gated tests automatically):

```bash
cargo build -p wattch-powerd
systemd-socket-activate -l /tmp/test.sock ./target/debug/wattch-powerd --backend synthetic
# in another shell:
wattch sources --socket /tmp/test.sock          # same user: allowed
sudo -u nobody wattch sources --socket /tmp/test.sock   # different uid: ACCESS_DENIED
```

## Spec-coverage checklist (self-review, done)

- `ACCESS_DENIED = 6` → Task 1. Protocol doc table → Task 1.
- `peer.rs` (`PeerPolicy` public fields, `with_grants`, `permits`, `peer_credentials` cfg-gated, `PeerCreds`) → Task 2; all six spec'd unit tests → Task 2 (+ a socketpair test).
- `ServerConfig.peer_policy`, `SocketSource::{Bind,Activated}`, `Clone` dropped, no chmod/unlink for Activated, `local_addr()` path, all construction sites (spec listed 4; the codebase has 7 — powerd, testd, session.rs ×2, serve.rs, capture.rs, permission_tests ×2 — all covered) → Task 3.
- Accept-loop enforcement, deny-on-`Err`, `Ok(None)` allow + one-time startup `warn!` → Task 4.
- Integration tests: default-policy Hello (existing tests, both platforms), deny-all rejection (Linux-gated), Activated session + not-unlinked assertion → Tasks 3-4.
- `--allow-uid`/`--allow-gid` both daemons, egid caveat in help; powerd listenfd, `--socket`+fd conflict `bail!`, testd bind-only → Task 5. `nix` socket feature + `listenfd = "1"` workspace dep, zero-`unsafe` → Tasks 2/5.
- `wattch-powerd.socket` unit fields + service `Requires=`/`After=` + dual-mode comments → Task 6.
- `docs/powerd.md` "Peer authentication" subsection + egid caveat + activation docs; manual Linux verification documented → Task 7 + section above.
- Existing suites green, no client changes → Task 7 Step 4.
- Out of scope items: none implemented (no getgrouplist, no per-command auth, no SO_PASSCRED, no sd_notify).
