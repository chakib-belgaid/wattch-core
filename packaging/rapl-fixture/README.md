# RAPL filesystem regression fixture

Linux exposes package and child zones as symlinks under `/sys/class/powercap`.
A child is also reachable through its package directory. Recording both paths
produces duplicate source UIDs and prevents `wattch-powerd` from starting. If a
socket activator retains the listener, `wattch sources` connects but times out
with `Resource temporarily unavailable (os error 11)`.

This fixture creates nested device directories, flattened class symlinks, and
changing root-only counter files. It exercises the real `linux-rapl` backend
using simulated filesystem values. The supervisor retains a real Unix listener
and passes it using `LISTEN_FDS` and `LISTEN_PID`, following the systemd socket
activation contract without running systemd in the container.

## Run

Requires Linux, Docker, Python 3, and a Rust toolchain. The host-built x86-64
binaries must be compatible with Debian 13's glibc. From the repository root:

```sh
bash packaging/rapl-fixture/run.sh /tmp/wattch-rapl-results
```

The helper builds the CLI and daemon, stages the Python client and test files,
and builds a test image from pinned Debian 13. Image construction needs Debian
package repository access; the Python package installs locally without network
access. Preserve the environment's configured Docker proxy settings.

The tests run with networking disabled, without privileged mode, host mounts,
or device access. The daemon runs as container root with a grant for uid 1000;
CLI and Python clients run as the unprivileged `wattch` user. Counter files
remain unreadable to that user. The helper saves `report.json` in the selected
output directory and removes its container and staging directory.

## Checks

Four scenarios combine nested and class-symlink layouts with self-bound and
socket-activated listeners. Every scenario checks:

- unprivileged source listing and exactly four unique RAPL sources;
- denial of direct counter-file reads by the client user;
- changing raw-counter capture, offline inspection, and manifest validation;
- Python `EnergyMeter` idle/work phases with positive per-domain counter deltas.

The Rust tests also cover class aliases and preservation of distinct MSR and
MMIO directories. Discovery deduplicates canonical directory paths before
recording or descending zones.

The original daemon failed on the class-symlink scenarios with `duplicate
source uid`; retaining the activation listener reproduced the reported CLI
timeout. The corrected daemon passes all four scenarios.

These checks validate software behavior with simulated counters. They do not
establish physical RAPL availability, firmware configuration, Secure Boot
lockdown behavior, or kernel module loading on a target PC. Existing USB images
need a daemon update separately; this test does not rebuild a bootable image.
