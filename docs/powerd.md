# wattch-powerd

`wattch-powerd` is the Wattch powermeter daemon. It is the privileged side of
the acquisition boundary: it reads protected energy and power sources and
exposes Wattch protocol v0.2 on a local Unix socket. Clients (the `wattch` CLI,
`pywattch`, the VSCode Energy Tests extension) stay unprivileged.

The daemon is intentionally dumb: it lists sources, reads raw counter or tool
values, timestamps and batches them, and stamps requested benchmark events.
All interpretation (deltas, power integration, aggregation, attribution,
baselines, models, and claims) happens in offline layers.

## Backends

```text
linux-rapl   Linux powercap sysfs (/sys/class/powercap/intel-rapl*).
             Cumulative microjoule counters per RAPL zone, package/core/
             uncore/dram hierarchy preserved through parent_source_uid,
             max_energy_range_uj recorded as max_value for wrap handling.
             Coexisting intel-rapl (MSR) and intel-rapl-mmio trees remain
             separate namespaces in source uids, source paths, labels, and
             parent lookup even when their numeric zone ids overlap.

apple-powermetrics
             One daemon-wide /usr/bin/powermetrics process on macOS. Exposes
             the available CPU, GPU, and ANE native interval-average power
             fields separately in milliwatts, plus every plist frame's raw
             elapsed_ns. Values retain TOOL_DERIVED_ESTIMATE; the backend
             intentionally ignores combined_power and never integrates or
             aggregates the domains. Running powermetrics requires root.

synthetic    Deterministic laptop-like topology (package/core/dram counters
             plus a GPU instant-power source). No privilege needed; used for
             local development, CI, and client testing.
```

Backend selection is `--backend auto|linux-rapl|apple-powermetrics|synthetic`.
`auto` chooses `apple-powermetrics` on macOS, `linux-rapl` when a usable
powercap tree exists on Linux, and otherwise `synthetic`. An explicitly or
automatically selected Apple backend fails clearly if the daemon lacks the
required privilege; it does not silently relabel synthetic data as Apple
evidence.

`--powercap-root` overrides the Linux sysfs root. `--powermetrics-interval-ms`
sets the native Apple collection interval from 100 through 10,000 ms (default
1,000 ms). Client capture intervals may be faster, but only a fresh Apple
sample generation is emitted. Calibrate against native update cadence rather
than treating repeated polls as independent measurements.

Each accepted capture snapshots the collector generation and waits for a
newer one, so it cannot begin with a value cached before `StartCapture`.
The reader records a process-monotonic receipt instant as soon as each complete
plist frame arrives. The daemon translates that instant into its shared
`clock_instance_id` epoch, so a delayed client poll does not make an older
generation look newly acquired. This remains a daemon receipt timestamp, not a
native Apple hardware-acquisition timestamp. Each power descriptor is
`INTERVAL_AVERAGE` and links through
`wattch.interval_duration_source_uid` to the same-frame raw
`SAMPLE_INTERVAL_DURATION` source. Its raw sample timestamp is the receipt
anchor used as synchronization evidence, and its descriptor declares
`wattch.interval_timeline_reconstruction=AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1`.
Offline analysis reconstructs a contiguous timeline from cumulative native
`elapsed_ns` values, then fits one affine synchronization against all daemon
receipt anchors. It rejects excessive clock-scale drift or receipt residuals
before multiplying each average by only the exact mapped overlap with
daemon-stamped event windows. It never treats the values as instantaneous
points, applies trapezoidal interpolation, silently fills a missing native
frame, or double counts receipt jitter. Interval calibration must still account
for pipe delivery and tool scheduling delay and jitter.

The plist parser and collector lifecycle are covered by deterministic tests.
Availability and semantics of live Apple domains still depend on the host and
must pass the study's per-meter calibration before empirical use. These tests
are not evidence that a privileged live `powermetrics` capture has been
validated on the reader's machine.

## Composed OS telemetry

`--os-telemetry auto|on|off` controls composition of Linux OS telemetry with
the selected meter backend. `auto` includes available sources, `on` requires
at least one supported source, and `off` exposes only the meter. Composition
preserves each child's nearest acquisition or receipt instant. Sequential
collectors therefore do not all inherit a timestamp taken before the first
child was read. Composition also assigns a collision-free set of local numeric
ids while preserving every source uid, descriptor, evidence class, and parent
relationship.

The Linux collector currently exposes:

```text
/proc/stat aggregate CPU-time fields       cumulative USER_HZ counters
/proc/stat context switches, interrupts    cumulative counters
/proc/stat runnable/blocked tasks           gauges
cpufreq scaling_cur_freq                    hertz (native kHz x 1000)
cpuidle state time                          nanoseconds (native us x 1000)
sysfs class/wakeup event_count              cumulative signaled-wakeup counters
thermal-zone temp                           Celsius (native millidegrees / 1000)
CPU thermal throttle counts                 cumulative counters
```

Every such source remains `OS_TELEMETRY`. Conversion labels preserve the
native unit and operation. The daemon does not calculate utilization, rates,
deltas, idle-state percentages, or correlations with energy. Unreadable
optional files are absent from discovery; a file that disappears later creates
a visible sample gap.

Test/container paths can be overridden with `--proc-stat`,
`--cpu-sysfs-root`, `--thermal-sysfs-root`, and `--wakeup-sources-root`.

## Research measurement boundaries

The daemon can expose many sources in one capture, but a study still declares
one primary source or an explicitly disjoint source set. Capturing OS telemetry
beside RAPL does not make it part of the energy sum.

- RAPL package supports a CPU-package energy claim.
- RAPL PSys is described as `PLATFORM_ENERGY`; when the platform exposes it,
  that source supports a platform-domain claim.
- Apple CPU/GPU/ANE sources support measured compute-domain claims and remain
  tool-derived estimates.
- Package/core/DRAM parent and child counters are never summed.
- `intel-rapl` and `intel-rapl-mmio` may expose overlapping views of the same
  physical domain. They are separate measurement boundaries and are never
  summed or substituted for one another. Linux RAPL paths have the stable form
  `powercap/<driver-namespace>/<zone-id-segments>/energy`.
- CPU/GPU/ANE values are summed offline only when descriptor evidence and an
  explicit preregistered declaration establish disjoint domains. Top-level
  placement alone is insufficient. Specifically, all selected descriptors
  must share `wattch.disjoint_group` and carry unique
  `wattch.disjoint_member` values. Current `powermetrics` descriptors do not
  assert those labels, so the default is separate outcomes.
- No current backend supports the phrase "whole-machine electrical energy."

See [Daemon-Based System Energy Impact Study](energy-impact-study.md) for the
boundary contract, calibration protocol, and permitted claim language.

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

Apple `powermetrics` also requires root. On macOS, where this implementation
does not have Linux `SO_PEERCRED`, run replication on a controlled host and
restrict the socket through its directory and file permissions; the daemon
logs that kernel peer credentials are unavailable.

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
           the selected meter and composed descriptors are probed once so
           privilege, configuration, and identity failures surface early
runtime    a zone that becomes unreadable is skipped for that tick and
           logged; an OS source that disappears is likewise omitted; the
           capture continues and the trace shows the gap; Apple samples are
           emitted only when powermetrics publishes a fresh generation
access     a peer that fails the credential check gets Error code 6
           (ACCESS_DENIED) and is disconnected; the event is logged at warn
shutdown   SIGINT/SIGTERM terminate the process; the next start cleans the
           stale socket
```
