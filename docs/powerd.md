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

`--socket` overrides everything below and behaves exactly as before. When
omitted, `wattch-powerd`, `wattch-testd`, `wattch` (the CLI), and `pywattch`
all resolve the same default, in priority order:

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
`wattch-testd.sock` for the deterministic test daemon. A client (`wattch`,
`pywattch`) probes the same three paths in order and connects to the first
one that exists. When none exist and the default was used, both clients
report the paths they tried in the connection error.

```bash
# Development (any OS): binds under $XDG_RUNTIME_DIR or a per-uid /tmp path.
wattch-powerd --backend synthetic

# Linux with RAPL, running as root: binds /run/wattch/wattch-powerd.sock.
sudo wattch-powerd --backend linux-rapl

# Explicit path always wins, on either side:
wattch-powerd --backend synthetic --socket /tmp/wattch-powerd.sock
```

Check it from the unprivileged side:

```bash
wattch sources     # auto-detects the socket above
wattch capture --out runs --duration-ms 2000
```

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
root; grant a group of trusted local users access via `Group=` on the unit
(or `chgrp` the socket in an `ExecStartPost`) rather than widening the
socket's own permissions.

## systemd

A hardened unit is provided in `packaging/wattch-powerd.service`:

```bash
sudo install -m 755 target/release/wattch-powerd /usr/local/bin/
sudo install -m 644 packaging/wattch-powerd.service /etc/systemd/system/
sudo systemctl enable --now wattch-powerd
```

## Failure behavior

```text
startup    refuses to start if another daemon listens on the socket;
           removes stale socket files from crashed runs;
           linux-rapl backend is probed once so misconfiguration fails fast
runtime    a zone that becomes unreadable is skipped for that tick and
           logged; the capture continues and the trace shows the gap
shutdown   SIGINT/SIGTERM terminate the process; the next start cleans the
           stale socket
```
