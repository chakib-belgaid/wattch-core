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

## Running

```bash
# Development (any OS):
wattch-powerd --backend synthetic --socket /tmp/wattch-powerd.sock

# Linux with RAPL (typically as root, or with read access to powercap):
sudo wattch-powerd --backend linux-rapl --socket /run/wattch/wattch-powerd.sock
```

Check it from the unprivileged side:

```bash
wattch sources --socket /tmp/wattch-powerd.sock
wattch capture --socket /tmp/wattch-powerd.sock --out runs --duration-ms 2000
```

## Privilege model

Since Linux 5.10, `energy_uj` under powercap is root-readable only
(side-channel mitigation). The supported deployment is:

```text
wattch-powerd runs with the privilege required to read powercap
the socket is the boundary: make it readable by the users who may measure
clients never read RAPL files directly
```

The socket inherits default permissions from its directory. For a shared
machine, place the socket in a directory owned by a `wattch` group (e.g.
`/run/wattch`, mode 0750) instead of widening file permissions.

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
