# Peer-credential authentication on the daemon socket

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
authentication as the missing second layer.

## Decision summary

- Enforce at `accept()` time in the shared `wattch-daemon` server, using
  kernel-reported peer credentials: `SO_PEERCRED` on Linux,
  `LOCAL_PEERCRED` on macOS (so the dev loop exercises the same code path).
- Default policy: allow euid 0 and the euid the daemon runs as. Everyone
  else is rejected.
- Admins broaden access with repeatable `--allow-uid <uid>` /
  `--allow-gid <gid>` flags on both daemons.
- A rejected peer receives a protocol `Error` frame (new code
  `ACCESS_DENIED = 6`, message "access denied") and the connection is
  closed; the daemon logs a `tracing::warn!` with the peer's uid (and pid on
  Linux).
- Both `wattch-powerd` and `wattch-testd` get identical enforcement — the
  policy lives in the shared server, not per-daemon hooks.

### Alternatives rejected

- **Powerd-only callback hook**: keeps `wattch-daemon` policy-free but adds
  API surface for a single use; testd would silently lack the check.
- **`SCM_CREDENTIALS` message-level auth**: credential passing per message
  is overkill for a connection-oriented local protocol; accept-time
  `SO_PEERCRED` is the standard pattern.
- **Log-only mode**: an audit trail without enforcement doesn't close the
  side channel.

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
- `pub fn peer_credentials(stream: &UnixStream) -> io::Result<(u32, u32)>`
  returning `(euid, egid)`, cfg-gated:
  - Linux: `nix::sys::socket::getsockopt(stream, sockopt::PeerCredentials)`
    → `UnixCredentials` (uid, gid, pid — pid included in the warn log).
  - macOS: `getsockopt(stream, sockopt::LocalPeerCred)` → `xucred`
    (`cr_uid`, effective gid = `cr_groups[0]`).
  - Other unix: compile error is acceptable (the workspace only targets
    Linux + macOS today); use `#[cfg(any(target_os = "linux", target_os = "macos"))]`
    with a fallback `cfg` arm returning an `Unsupported` io error so the
    crate still builds elsewhere.

### `wattch-daemon` — `server.rs`

- `ServerConfig` gains `pub peer_policy: PeerPolicy`. All construction sites
  updated (powerd main, testd main, `tests/session.rs` helpers ×2 — tests
  use `PeerPolicy::with_grants(&[], &[])`).
- In the accept loop, before spawning the connection handler: read
  `peer_credentials`; if the read fails or `permits` is false, warn and
  reply with an `Error` frame (`ACCESS_DENIED`) then drop the stream. A
  credential-read failure is a deny, not an allow.
- The shutdown wake-up connect (`ServerHandle::shutdown`) is made by the
  daemon's own uid, which the default policy always permits — no special
  case needed.

### Binaries

- `wattch-powerd` and `wattch-testd` mains: repeatable clap flags
  `--allow-uid <UID>` and `--allow-gid <GID>` (both `Vec<u32>`), fed to
  `PeerPolicy::with_grants`. Help text notes the egid caveat below.

### Dependencies

- Workspace `nix` gains the `socket` feature (currently `["user"]`);
  `wattch-daemon` adds `nix.workspace = true`. Zero `unsafe` — nix wraps
  both getsockopt variants.

## Documented caveat: egid only

`SO_PEERCRED` reports the peer's *effective* gid, not supplementary groups.
`--allow-gid` therefore matches egid only: a user whose supplementary groups
include `wattch` will NOT pass the gid check. Group-based human access
remains the job of the 0660 socket file permissions (which do honor
supplementary groups); `--allow-uid` is the explicit-grant mechanism.
`docs/powerd.md` gets a "Peer authentication" subsection stating this and
the default policy.

## Error handling

- Rejected peer: `Error` frame (`ACCESS_DENIED`, "access denied"), warn log
  with uid/gid (and pid on Linux), connection closed. Clients surface this
  as their existing daemon-error types (`WattchDaemonError` in Python,
  `ClientError::Daemon` in Rust) with no client-side changes.
- Credential read failure: same as rejection (deny), logged at warn with
  the error.

## Testing

- Unit (`peer.rs`): root allowed; own euid allowed; extra uid allowed;
  extra gid allowed; stranger uid+gid rejected; `with_grants` always
  includes 0 and the current euid.
- Integration (`wattch-daemon/tests/session.rs`): default policy — a
  same-user client completes Hello (exercises the real credential read on
  both Linux and macOS); a deny-all policy built literally
  (`PeerPolicy { allowed_uids: vec![], allowed_gids: vec![] }`, possible
  because the fields are public) — client receives `Error` with code 6 and
  the connection closes.
- Existing suites (Rust workspace, Python 62, extension 8) must stay green
  with no client changes; the Python integration tests connect as the
  daemon's own user and pass under the default policy.

## Out of scope

- Supplementary-group resolution (NSS `getgrouplist`) for `--allow-gid`.
- Per-source or per-command authorization; the check is connection-level.
- systemd socket activation / `SO_PASSCRED` message credentials.
