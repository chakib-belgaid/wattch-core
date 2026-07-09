# Production hardening: peer-credential auth + systemd socket activation

Date: 2026-07-09
Status: approved

## Problem

`wattch-powerd` re-exposes root-restricted RAPL energy counters (a documented
side channel — PLATYPUS, CVE-2020-8694) over a Unix socket. Access control
today is file permissions only: a 0750 runtime directory and a 0660 socket.
That is one layer. If the socket lands somewhere with looser permissions, or
an admin broadens the directory mode, nothing else stops an arbitrary local
user from streaming energy data. The 2026-06 production-foundation review and
the 2026-07-08 hardening review both flagged kernel-verified peer
authentication as the missing second layer. Separately, the daemon binds its
own socket even under systemd, so socket ownership/mode live in imperative
daemon code instead of declarative unit configuration, and there is no
on-demand activation. Goal: by the end of this work the daemon is production
ready and safe to deploy on Linux.

**Platform stance:** the daemon's only production target is Linux (RAPL is
Linux-only; macOS has entirely different energy tooling). macOS remains a
development environment only (synthetic backend, tests); daemon security
features are Linux-enforced and macOS builds degrade explicitly (see below).

## Decision summary

- Enforce at `accept()` time in the shared `wattch-daemon` server, using
  kernel-reported peer credentials via `SO_PEERCRED` (Linux). On non-Linux
  builds the check compiles to allow-all with a `warn!` at startup — those
  builds are dev-only by definition and must not pretend to enforce.
- Default policy: allow euid 0 and the euid the daemon runs as. Everyone
  else is rejected.
- Admins broaden access with repeatable `--allow-uid <uid>` /
  `--allow-gid <gid>` flags on both daemons.
- A rejected peer receives a protocol `Error` frame (new code
  `ACCESS_DENIED = 6`, message "access denied") and the connection is
  closed; the daemon logs a `tracing::warn!` with the peer's uid, gid, pid.
- Both `wattch-powerd` and `wattch-testd` get identical enforcement — the
  policy lives in the shared server, not per-daemon hooks.
- `wattch-powerd` supports **systemd socket activation** (`LISTEN_FDS`
  protocol): when systemd hands it a pre-bound socket, it serves on that
  fd instead of binding; the socket path, mode, and ownership then live
  declaratively in a new `wattch-powerd.socket` unit. Self-bind remains
  the fallback for non-systemd use, with identical peer auth on both paths.

### Alternatives rejected

- **Powerd-only callback hook**: keeps `wattch-daemon` policy-free but adds
  API surface for a single use; testd would silently lack the check.
- **`SCM_CREDENTIALS` message-level auth**: credential passing per message
  is overkill for a connection-oriented local protocol; accept-time
  `SO_PEERCRED` is the standard pattern.
- **Log-only mode**: an audit trail without enforcement doesn't close the
  side channel.
- **macOS `LOCAL_PEERCRED` support**: the daemon never runs in production
  on macOS (no RAPL), so implementing a second credential API buys nothing;
  explicit allow-all with a startup warning is more honest than untested
  enforcement.
- **Hand-rolled `LISTEN_FDS` parsing**: turning fd 3 into a `UnixListener`
  requires `unsafe { from_raw_fd }`; the workspace is zero-`unsafe`. Use the
  `listenfd` crate (mitsuhiko; used by actix and cargo-watch) to keep the
  unsafe out of our code.

## Components

### `wattch-protocol`

- `error_codes::ACCESS_DENIED: u32 = 6` (next free code; 1–5 and 100 taken).

### `wattch-daemon` — new module `src/peer.rs`

- `pub struct PeerPolicy { pub allowed_uids: Vec<u32>, pub allowed_gids: Vec<u32> }`
  (derives `Clone`, `Debug`). Fields are public, like `ServerConfig`'s: the
  constructor below is the convenient path, but tests (and unusual callers)
  may build the struct literally — that is how the rejection test constructs
  a deny-all policy.
- Constructor `PeerPolicy::with_grants(extra_uids: &[u32], extra_gids: &[u32])`:
  `allowed_uids` always contains `0` and the current effective uid, plus
  `extra_uids`; `allowed_gids` is exactly `extra_gids`.
- `pub fn permits(&self, uid: u32, gid: u32) -> bool`: true when `uid` is in
  `allowed_uids` or `gid` is in `allowed_gids`. Pure, unit-testable.
- `pub fn peer_credentials(stream: &UnixStream) -> io::Result<Option<PeerCreds>>`
  where `PeerCreds { uid: u32, gid: u32, pid: i32 }`, cfg-gated:
  - Linux: `nix::sys::socket::getsockopt(stream, sockopt::PeerCredentials)`
    → `UnixCredentials` (uid, gid, pid), returned as `Some(creds)`. A
    getsockopt failure is an `Err` (denied by the caller).
  - Non-Linux (dev-only builds): returns `Ok(None)` — "no credential
    facility". The server treats `None` as allow-all and logs one `warn!`
    at startup ("peer authentication unavailable on this platform; dev
    builds only — production target is Linux"). This keeps the macOS dev
    loop and test suite working without pretending to enforce.

### `wattch-daemon` — `server.rs`

- `ServerConfig` gains `pub peer_policy: PeerPolicy`. All construction sites
  updated (powerd main, testd main, `tests/session.rs` helpers ×2 — tests
  use `PeerPolicy::with_grants(&[], &[])`).
- `ServerConfig.socket_path: PathBuf` is replaced by
  `pub socket: SocketSource`, where
  `pub enum SocketSource { Bind(PathBuf), Activated(UnixListener) }`.
  `UnixListener` is not `Clone`, so `ServerConfig` drops its `Clone` derive
  (`spawn` already takes the config by value; update any caller that
  relied on cloning). Details:
  - `Bind`: today's behavior — parent-dir creation (0750), stale-socket
    probe, bind, chmod 0660, unlink on shutdown.
  - `Activated`: serve on the provided listener; do NOT chmod and do NOT
    unlink on shutdown — systemd owns the socket's lifecycle, mode, and
    ownership. The path for the shutdown wake-up connect comes from
    `listener.local_addr()`.
- In the accept loop, before spawning the connection handler: read
  `peer_credentials`; on `Err` or `Some(creds)` where `permits` is false,
  `warn!` (uid/gid/pid) and reply with an `Error` frame (`ACCESS_DENIED`)
  then drop the stream. A credential-read failure is a deny, not an allow.
  `Ok(None)` (non-Linux dev build) allows, with the one-time startup
  warning.
- The shutdown wake-up connect (`ServerHandle::shutdown`) is made by the
  daemon's own uid, which the default policy always permits; even under a
  test's deny-all policy the accept still fires (rejection happens after
  accept), so shutdown wake-up is policy-independent.

### Binaries

- `wattch-powerd` and `wattch-testd` mains: repeatable clap flags
  `--allow-uid <UID>` and `--allow-gid <GID>` (both `Vec<u32>`), fed to
  `PeerPolicy::with_grants`. Help text notes the egid caveat below.
- `wattch-powerd` main additionally checks for a systemd-activated socket
  first (via `listenfd::ListenFd::from_env().take_unix_listener(0)`):
  - fd present → `SocketSource::Activated(listener)`; an explicit
    `--socket` alongside an activated fd is a startup error (conflicting
    intent must not be silently resolved).
  - no fd → today's path: `--socket` or `default_bind_path`, as
    `SocketSource::Bind`.
- `wattch-testd` stays bind-only (it is a test daemon; no systemd unit).

### Packaging (`packaging/`)

- New `wattch-powerd.socket` unit:
  `ListenStream=/run/wattch/wattch-powerd.sock`, `SocketMode=0660`,
  `SocketUser=root`, `SocketGroup=wattch` (commented, with a note to create
  the group or change it), `DirectoryMode=0750`, `RemoveOnStop=true`.
- `wattch-powerd.service` updated: `Requires=wattch-powerd.socket` +
  `After=wattch-powerd.socket`; the existing hardening block stays;
  `RuntimeDirectory=wattch` stays (under `ProtectSystem=strict` it is what
  makes `/run/wattch` writable for the self-bind fallback) but requires
  `RuntimeDirectoryPreserve=yes` under activation: without it, a service
  stop/restart unlinks `/run/wattch`, including the socket unit's
  listening socket, breaking activation until the socket unit itself is
  restarted. Comments explain both deployment modes.

### Dependencies

- Workspace `nix` gains the `socket` feature (currently `["user"]`);
  `wattch-daemon` adds `nix.workspace = true`.
- `wattch-powerd` adds `listenfd = "1"` (workspace dependency). Our code
  stays zero-`unsafe`; the raw-fd conversion lives inside `listenfd`.

## Documented caveat: egid only

`SO_PEERCRED` reports the peer's *effective* gid, not supplementary groups.
`--allow-gid` therefore matches egid only: a user whose supplementary groups
include `wattch` will NOT pass the gid check. Both layers must pass: file
permissions (`SocketGroup=`) are necessary but not sufficient on their own,
since the peer-credential policy is enforced independently. A group grant
requires `SocketGroup=` in the `.socket` unit **and** a peer-policy grant on
`ExecStart` (`--allow-uid` per user, reliably; `--allow-gid` only when the
peer's effective gid matches). `docs/powerd.md` gets a "Peer authentication"
subsection stating this and the default policy.

## Error handling

- Rejected peer: `Error` frame (`ACCESS_DENIED`, "access denied"), warn log
  with uid/gid/pid, connection closed. Clients surface this as their
  existing daemon-error types (`WattchDaemonError` in Python,
  `ClientError::Daemon` in Rust) with no client-side changes.
- Credential read failure: same as rejection (deny), logged at warn with
  the error.
- `--socket` given while a systemd-activated fd is present: startup error
  (`anyhow::bail!`) naming both, so conflicting configuration never
  silently picks one.
- Activated fd that is not a Unix stream listener: `listenfd` returns an
  error; startup fails with context.

## Testing

- Unit (`peer.rs`): root allowed; own euid allowed; extra uid allowed;
  extra gid allowed; stranger uid+gid rejected; `with_grants` always
  includes 0 and the current euid.
- Integration (`wattch-daemon/tests/session.rs`):
  - Default policy: a same-user client completes Hello — on Linux this
    exercises the real `SO_PEERCRED` read (CI runs Linux); on macOS it
    exercises the `Ok(None)` allow path.
  - Deny-all policy built literally
    (`PeerPolicy { allowed_uids: vec![], allowed_gids: vec![] }`, possible
    because the fields are public): client receives `Error` with code 6
    and the connection closes. `#[cfg(target_os = "linux")]` — on macOS
    there are no credentials to deny.
  - `SocketSource::Activated`: construct a `UnixListener` in the test,
    pass it via `Activated`, and complete a session over it — this covers
    the activation serving path on every platform without systemd; assert
    the socket file is NOT unlinked on shutdown in this mode.
- Manual/CI verification on Linux (documented in the plan, not automated):
  `systemd-socket-activate -l /tmp/test.sock ./wattch-powerd --backend synthetic`
  then `wattch sources --socket /tmp/test.sock`.
- Existing suites (Rust workspace, Python 62, extension 8) must stay green
  with no client changes; the Python integration tests connect as the
  daemon's own user and pass under the default policy.

## Out of scope

- Supplementary-group resolution (NSS `getgrouplist`) for `--allow-gid`.
- Per-source or per-command authorization; the check is connection-level.
- `SO_PASSCRED` message-level credentials.
- macOS as a production daemon target (dev/test environment only); any
  macOS-native energy backend is a separate future project.
- `sd_notify` readiness protocol (`Type=notify`) — worth considering later;
  the current `Type=simple` semantics are unchanged by socket activation.

## Amendments (2026-07-09, post-review)

Final code review of the branch surfaced two Important findings, corrected
here and in the packaging/docs files themselves:

- **Packaging / `RuntimeDirectory`:** the original text ("harmless under
  activation, useful for self-bind fallback") was wrong. Under socket
  activation, `RuntimeDirectory=wattch` with the default
  `RuntimeDirectoryPreserve=no` means a stop/restart of the *service* unit
  removes `/run/wattch`, unlinking the socket unit's listening socket and
  breaking activation until the socket unit is itself restarted. Fixed by
  adding `RuntimeDirectoryPreserve=yes` to `wattch-powerd.service`.
- **Documented caveat: egid only** — the sentence describing group-based
  access implied file permissions (SocketGroup=) alone were sufficient to
  grant a group access. The two layers AND together: a group grant needs
  `SocketGroup=` (layer 1) **and** a peer-policy grant on `ExecStart`
  (layer 2, `--allow-uid`/`--allow-gid`). Corrected in this spec and in
  `docs/powerd.md`, `packaging/wattch-powerd.socket`, and
  `packaging/wattch-powerd.service`.
