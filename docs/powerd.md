# wattch-powerd

`wattch-powerd` is the Wattch powermeter daemon. It is the privileged side of
the measurement boundary: it reads protected energy counters and exposes the
Wattch protocol v0.1 on a local Unix socket. Clients (the `wattch` CLI,
`pywattch`, the VSCode Energy Tests extension) stay unprivileged.

The daemon is intentionally dumb: it lists sources, reads raw counter values,
timestamps and batches them. All interpretation (deltas, joules, attribution,
baselines) happens in clients or offline layers.

## Backends

```text
linux-rapl   Linux powercap sysfs (/sys/class/powercap/intel-rapl*).
             Cumulative microjoule counters per RAPL zone, package/core/
             uncore/dram hierarchy preserved through parent_source_uid,
             max_energy_range_uj recorded as max_value for wrap handling.

synthetic    Deterministic laptop-like topology (package/core/dram counters
             plus a GPU instant-power source). No privilege needed; used for
             development on macOS/Windows/CI and for client testing.
```

Backend selection: `--backend auto` (default) picks `linux-rapl` when a
usable powercap tree exists, otherwise falls back to `synthetic` with a
warning. `--powercap-root` overrides the sysfs root (used by tests; also
useful in containers that bind-mount powercap somewhere else).

## Socket path

`--socket` overrides everything below. When omitted, a daemon chooses a bind
path in this order:

```text
1. /run/wattch/<name>                      when running as root (or, for
                                            clients, whenever this path
                                            exists -- e.g. under systemd's
                                            RuntimeDirectory=wattch)
2. $XDG_RUNTIME_DIR/wattch/<name>          user-private runtime directory
3. /tmp/<stem>-<uid>.sock                  per-uid fallback, e.g.
                                            /tmp/wattch-powerd-501.sock
```

`<name>` is `wattch-powerd.sock` for the powermeter daemon and
`wattch-testd.sock` for the deterministic test daemon. Automatic client
discovery (`wattch`, `pywattch`) probes only `/run/wattch` and
`$XDG_RUNTIME_DIR`: it deliberately never probes `/tmp`. An existence check
cannot authenticate a Unix socket in an attacker-writable directory. A
development socket under `/tmp` remains supported only when its path is
explicitly supplied to both daemon and client.

```bash
# Development with a private XDG runtime directory: auto-discovery works.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-$(mktemp -d)}"
wattch-powerd --backend synthetic

# Linux with RAPL, running as root: binds /run/wattch/wattch-powerd.sock.
sudo wattch-powerd --backend linux-rapl

# Explicit path always wins, on either side:
wattch-powerd --backend synthetic --socket /tmp/wattch-powerd.sock
wattch sources --socket /tmp/wattch-powerd.sock
```

Check it from the unprivileged side:

```bash
wattch sources     # auto-detects the socket above
wattch capture --out runs --duration-ms 2000
```

## Bounded sessions

The daemon treats every local client as untrusted even after peer credential
authorization. Defaults are intentionally finite:

```text
connections                  64 global / 16 per uid on Linux
active captures               8 global /  2 per uid on Linux
Hello deadline                5 seconds
idle session deadline         5 minutes
maximum capture duration     24 hours
socket write deadline         5 seconds
minimum sampling interval    10 milliseconds
```

Invalid or amplifying capture requests are rejected before backend work. Run
ids are non-empty and at most 256 UTF-8 bytes; source selections are unique;
sampling below 10 ms is rejected. The systemd unit adds independent task and
file-descriptor ceilings as defense in depth.

## Privilege model

Since Linux 5.10, `energy_uj` under powercap is root-readable only
(side-channel mitigation): fine-grained RAPL energy counters are a
documented side channel (PLATYPUS, CVE-2020-8694). The supported deployment
is:

```text
wattch-powerd runs with the privilege required to read powercap
the socket is the boundary: make it readable by the users who may measure
clients never read RAPL files directly
```

The socket is not left to inherited/default permissions:

- The daemon creates its socket's parent directory itself (if missing) with
  mode **0750** -- only the immediate directory, not an arbitrary ancestor
  chain; it errors out if the grandparent is also missing rather than
  silently creating one.
- After binding, the daemon explicitly `chmod`s the socket file to **0660**
  (root-writable, group-readable/writable, no world access).

For a shared machine under systemd, `RuntimeDirectory=wattch` (see
`packaging/wattch-powerd.service`) gives `/run/wattch` mode 0750 owned by
root; grant a group of trusted local users access via `SocketGroup=` in
`packaging/wattch-powerd.socket` plus a peer-policy grant (see the next
subsection) rather than widening the socket's own permissions.

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
  check. The 0660 socket file permissions and the peer-credential policy
  are both required — file permissions alone no longer grant access;
  they are necessary but not sufficient. Granting a group means setting
  `SocketGroup=` (layer 1, honors supplementary groups) **and** adding a
  peer-policy grant on `ExecStart` (layer 2): `--allow-uid <uid>` per
  user is the reliable mechanism, since `--allow-gid <gid>` only matches
  when the peer's effective gid is that group (e.g. it is their primary
  group, or they ran `newgrp`).
- **Non-Linux builds** (development only; RAPL is Linux-only) have no
  credential facility: the daemon allows all peers and logs a warning at
  startup. Production deployments are Linux.

## systemd

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

## Failure behavior

```text
startup    refuses to start if another daemon listens on the socket;
           removes stale socket files from crashed runs;
           linux-rapl backend is probed once so misconfiguration fails fast
runtime    a zone that becomes unreadable is skipped for that tick and
           logged; the capture continues and the trace shows the gap
access     a peer that fails the credential check gets Error code 6
           (ACCESS_DENIED) and is disconnected; the event is logged at warn
shutdown   SIGINT/SIGTERM terminate the process; the next start cleans the
           stale socket
```
