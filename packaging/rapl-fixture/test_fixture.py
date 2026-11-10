#!/usr/bin/env python3
"""Exercise installed Wattch binaries against simulated RAPL files in Docker.

Run as container root, with the fixture image's wattch user (uid/gid 1000).
No host mounts, devices, networking, or privileged container are needed.
Counter values in this test are simulated, not physical energy measurements.
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time


SOCKET = Path("/run/wattch/wattch-powerd.sock")
DAEMON = "/usr/local/bin/wattch-powerd"
CLI = "/usr/local/bin/wattch"
PYTHON = "/opt/wattch/venv/bin/python"


def activated_exec():
    """Pass a real listening fd using the systemd socket activation contract."""
    fd = int(sys.argv[2])
    if fd != 3:
        os.dup2(fd, 3)
        os.close(fd)
    os.set_inheritable(3, True)
    os.environ["LISTEN_FDS"] = "1"
    os.environ["LISTEN_PID"] = str(os.getpid())
    os.execv(DAEMON, [DAEMON, *sys.argv[3:]])


def fixture(base, layout):
    class_root = base / "sys/class/powercap"
    class_root.mkdir(parents=True)
    if layout == "nested":
        controller = class_root
    else:
        controller = base / "sys/devices/virtual/powercap/intel-rapl"
        controller.mkdir(parents=True)
        (class_root / "intel-rapl").symlink_to(os.path.relpath(controller, class_root))

    package = controller / "intel-rapl:0"
    zones = [
        (package, "package-0", 1_000_000, 250_000),
        (package / "intel-rapl:0:0", "core", 400_000, 100_000),
        (package / "intel-rapl:0:1", "dram", 200_000, 50_000),
        (controller / "intel-rapl:1", "psys", 5_000_000, 400_000),
    ]
    for path, name, energy, _ in zones:
        path.mkdir(parents=True)
        (path / "name").write_text(name + "\n")
        (path / "max_energy_range_uj").write_text("262143328850\n")
        counter = path / "energy_uj"
        counter.write_text(str(energy) + "\n")
        counter.chmod(0o600)
        if layout != "nested":
            (class_root / path.name).symlink_to(os.path.relpath(path, class_root))
    return class_root, zones


def update_counters(zones, stop):
    tick = 0
    while not stop.is_set():
        for path, _, initial, increment in zones:
            pending = path / "energy_uj.next"
            pending.write_text(str(initial + increment * tick) + "\n")
            pending.chmod(0o600)
            pending.replace(path / "energy_uj")
        tick += 1
        stop.wait(0.01)


def run_client(command):
    return subprocess.run(
        command, user=1000, group=1000, extra_groups=[],
        capture_output=True, text=True, timeout=12,
        env={**os.environ, "HOME": "/home/wattch"},
    )


def checked(command):
    result = run_client(command)
    if result.returncode:
        raise AssertionError(f"{command}: {result.stdout}{result.stderr}")
    return result.stdout


def exercise_case(base, layout, activated):
    base.mkdir(mode=0o755)
    powercap, zones = fixture(base, layout)
    output = base / "results"
    output.mkdir(mode=0o755)
    os.chown(output, 1000, 1000)
    SOCKET.parent.mkdir(mode=0o750, exist_ok=True)
    SOCKET.parent.chmod(0o750)
    os.chown(SOCKET.parent, 0, 1000)
    SOCKET.unlink(missing_ok=True)
    listener = None
    if activated:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(SOCKET))
        listener.listen(16)
        SOCKET.chmod(0o660)
        os.chown(SOCKET, 0, 1000)

    args = ["--backend", "linux-rapl", "--powercap-root", str(powercap),
            "--node-id", "docker-fixture", "--allow-uid", "1000"]
    if activated:
        command = [sys.executable, __file__, "--activated", str(listener.fileno()), *args]
    else:
        command = [DAEMON, "--socket", str(SOCKET), *args]
    stop = threading.Event()
    updater = threading.Thread(target=update_counters, args=(zones, stop))
    updater.start()
    result = {"layout": layout, "socket_activation": activated, "checks": []}
    daemon = None
    log_path = base / "daemon.log"
    try:
        with log_path.open("w") as log:
            daemon = subprocess.Popen(
                command, stdout=log, stderr=log, group=1000,
                pass_fds=(listener.fileno(),) if listener else (),
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if daemon.poll() is not None or "serving on" in log_path.read_text():
                    break
                time.sleep(0.02)

            # Keep the activator's listener open even if startup fails. As with
            # systemd, a client can connect but receives no protocol response.
            if daemon.poll() is not None and not activated:
                raise AssertionError(f"daemon exited before serving: {daemon.returncode}")
            result["sources"] = checked([CLI, "sources"])
            result["checks"].append("unprivileged sources")
            if daemon.poll() is not None:
                raise AssertionError("daemon exited during sources request")

            denied = run_client(["cat", str(zones[0][0] / "energy_uj")])
            if denied.returncode == 0 or "Permission denied" not in denied.stderr:
                raise AssertionError("fixture counter must be inaccessible to the client uid")
            result["checks"].append("root-only counters")

            checked([CLI, "capture", "--out", str(output), "--run-id", "fixture",
                     "--duration-ms", "400", "--interval-ms", "10"])
            artifact = output / "fixture"
            manifest = json.loads((artifact / "manifest.json").read_text())
            sources = manifest["sources"]
            if len(sources) != 4 or len({s["source_uid"] for s in sources}) != 4:
                raise AssertionError(f"expected exactly four unique zones: {sources}")
            if any(s["backend"] != "linux-rapl" for s in sources):
                raise AssertionError("fixture must exercise the linux-rapl backend")
            result["checks"].append("four unique RAPL sources and raw capture")
            result["inspect"] = checked([CLI, "inspect", "--run", str(artifact)])
            checked([CLI, "validate", "--input", str(artifact / "manifest.json")])
            result["checks"].append("offline inspect and manifest validation")

            result["python"] = checked([PYTHON, "-c", """
import json, time
from pywattch import EnergyMeter
meter = EnergyMeter(interval_ms=10)
meter.start(tag='idle')
try:
    time.sleep(0.15)
    meter.record(tag='cpu_work')
    end = time.perf_counter() + 0.15
    while time.perf_counter() < end:
        sum(i * i for i in range(10000))
finally:
    meter.stop()
trace = meter.get_trace()
assert [s.tag for s in trace] == ['idle', 'cpu_work']
assert all(len(s.energy) == 4 and all(v > 0 for v in s.energy.values()) for s in trace)
print(json.dumps(trace.to_dict()))
"""])
            result["checks"].append("unprivileged Python idle/work experiment")
            result["passed"] = True
    except (AssertionError, subprocess.TimeoutExpired) as error:
        result.update(passed=False, error=str(error))
    finally:
        if daemon is not None:
            result["daemon_exit_before_cleanup"] = daemon.poll()
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=3)
        stop.set()
        updater.join(timeout=3)
        if listener:
            listener.close()
        SOCKET.unlink(missing_ok=True)
        result["daemon_log"] = log_path.read_text()
    return result


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this test as root inside the disposable container.")
    base = Path(tempfile.mkdtemp(prefix="wattch-rapl-"))
    base.chmod(0o755)
    cases = []
    for layout in ("nested", "sysfs-class"):
        for activated in (False, True):
            name = layout + ("-activated" if activated else "-bound")
            result = exercise_case(base / name, layout, activated)
            cases.append(result)
            print(json.dumps({"case": name, "passed": result["passed"],
                              "error": result.get("error"), "checks": result["checks"]}), flush=True)
    report = {"simulated_counters": True, "daemon_sha256": hashlib.sha256(Path(DAEMON).read_bytes()).hexdigest(),
              "case_root": str(base), "cases": cases, "passed": all(c["passed"] for c in cases)}
    Path("/tmp/rapl-fixture-report.json").write_text(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--activated"]:
        activated_exec()
    else:
        sys.exit(main())
