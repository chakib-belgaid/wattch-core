"""Execute one scheduled trial while acquisition stays behind the daemon API."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence

from .._host_provenance import local_host_provenance
from .._messages import RawSampleBatch, SourceDescriptor
from .._workload_metrics import (
    BACKGROUND_WORKLOAD_METRICS_FILENAME,
    WORKLOAD_METRICS_CONTRACT,
    WORKLOAD_METRICS_FILENAME,
    WorkloadMetrics,
    WorkloadMetricsEvidence,
    WorkloadMetricsSource,
    load_workload_metrics_sidecar,
)
from .artifacts import TrialArtifactWriter, prepare_experiment_layout
from .config import (
    EqualWork,
    FixedHorizon,
    MeasurementBoundary,
    MechanismPolicy,
    WorkloadCommand,
)
from .schedule import ExperimentSchedule, TrialSpec


class MarkerKind(str, Enum):
    PRE_WINDOW_START = "PRE_WINDOW_START"
    BACKGROUND_ARRIVAL = "BACKGROUND_ARRIVAL"
    BACKGROUND_COMPLETION = "BACKGROUND_COMPLETION"
    TARGET_SPAWN = "TARGET_SPAWN"
    TARGET_EXEC = "TARGET_EXEC"
    TARGET_EXIT = "TARGET_EXIT"
    RECOVERY_END = "RECOVERY_END"


class TrialDaemonSession(Protocol):
    """Injectable high-level adapter over the v0.2 daemon client.

    Implementations own their read loop and deliver data-plane frames through
    the callbacks.  `record_event` follows the locked protocol contract:
    RecordEvent(run_id, event_id, kind, labels) -> RecordEventAck(event=Event).
    This avoids socket read races between the trial runner and a capture reader.
    """

    node_id: str
    protocol_version: str
    clock_instance_id: str
    sources: Sequence[SourceDescriptor]

    def start_capture(
        self,
        run_id: str,
        interval_ms: int,
        source_ids: list[int],
        *,
        on_raw_batch: Callable[[RawSampleBatch], None],
        on_event_batch: Callable[[object], None],
    ) -> object: ...

    def record_event(
        self,
        run_id: str,
        event_id: str,
        kind: str,
        labels: Mapping[str, str],
    ) -> object: ...

    def stop_capture(self, run_id: str) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class HostPolicyEvidence:
    """Exact settings observed after a delegated host-policy intervention."""

    policy: MechanismPolicy
    controller_id: str
    settings: Mapping[str, str]
    settings_fingerprint_sha256: str
    prior_settings: Mapping[str, str]
    prior_settings_fingerprint_sha256: str
    verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.policy, MechanismPolicy):
            raise ValueError("host policy evidence has an invalid policy")
        if not isinstance(self.controller_id, str) or not self.controller_id.strip():
            raise ValueError("host policy controller_id must not be empty")
        if not isinstance(self.verified, bool):
            raise ValueError("host policy verified must be a boolean")
        copied: dict[str, str] = {}
        for key, value in self.settings.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("host policy setting keys must not be empty")
            if not isinstance(value, str):
                raise ValueError("host policy setting values must be strings")
            copied[key] = value
        if not copied:
            raise ValueError("host policy evidence must contain exact settings")
        expected = self.fingerprint(copied)
        if self.settings_fingerprint_sha256 != expected:
            raise ValueError(
                "host policy settings_fingerprint_sha256 does not match settings"
            )
        encoded = json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(encoded.encode("utf-8")) > 1024:
            raise ValueError("host policy settings exceed the event-label limit")
        object.__setattr__(self, "settings", MappingProxyType(copied))
        prior: dict[str, str] = {}
        for key, value in self.prior_settings.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("prior host policy setting keys must not be empty")
            if not isinstance(value, str):
                raise ValueError("prior host policy setting values must be strings")
            prior[key] = value
        if not prior:
            raise ValueError("host policy evidence must contain the saved prior state")
        expected_prior = self.fingerprint(prior)
        if self.prior_settings_fingerprint_sha256 != expected_prior:
            raise ValueError(
                "prior_settings_fingerprint_sha256 does not match prior_settings"
            )
        encoded_prior = json.dumps(
            prior,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(encoded_prior.encode("utf-8")) > 1024:
            raise ValueError("prior host policy settings exceed the event-label limit")
        object.__setattr__(self, "prior_settings", MappingProxyType(prior))

    @staticmethod
    def fingerprint(settings: Mapping[str, str]) -> str:
        encoded = json.dumps(
            dict(settings),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_verified_settings(
        cls,
        policy: MechanismPolicy,
        controller_id: str,
        settings: Mapping[str, str],
        prior_settings: Mapping[str, str],
    ) -> HostPolicyEvidence:
        return cls(
            policy=policy,
            controller_id=controller_id,
            settings=settings,
            settings_fingerprint_sha256=cls.fingerprint(settings),
            prior_settings=prior_settings,
            prior_settings_fingerprint_sha256=cls.fingerprint(prior_settings),
            verified=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "controller_id": self.controller_id,
            "verified": self.verified,
            "settings": dict(sorted(self.settings.items())),
            "settings_fingerprint_sha256": self.settings_fingerprint_sha256,
            "prior_settings": dict(sorted(self.prior_settings.items())),
            "prior_settings_fingerprint_sha256": (
                self.prior_settings_fingerprint_sha256
            ),
        }

    def to_labels(self) -> dict[str, str]:
        return {
            "mechanism_policy": self.policy.value,
            "host_policy_verification": "VERIFIED" if self.verified else "FAILED",
            "host_policy_controller_id": self.controller_id,
            "host_policy_settings_fingerprint_sha256": (
                self.settings_fingerprint_sha256
            ),
            "host_policy_settings_json": json.dumps(
                dict(self.settings),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            "host_policy_prior_settings_fingerprint_sha256": (
                self.prior_settings_fingerprint_sha256
            ),
        }


@dataclass(frozen=True)
class HostPolicyRestorationEvidence:
    """Read-back proof that the controller restored the saved pre-trial state."""

    policy: MechanismPolicy
    controller_id: str
    restored_settings: Mapping[str, str]
    restored_settings_fingerprint_sha256: str
    expected_prior_settings_fingerprint_sha256: str
    verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.policy, MechanismPolicy):
            raise ValueError("host policy restoration has an invalid policy")
        if not isinstance(self.controller_id, str) or not self.controller_id.strip():
            raise ValueError("host policy restoration controller_id must not be empty")
        if not isinstance(self.verified, bool):
            raise ValueError("host policy restoration verified must be a boolean")
        restored: dict[str, str] = {}
        for key, value in self.restored_settings.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("restored host policy setting keys must not be empty")
            if not isinstance(value, str):
                raise ValueError("restored host policy setting values must be strings")
            restored[key] = value
        if not restored:
            raise ValueError("restoration evidence must contain read-back settings")
        expected = HostPolicyEvidence.fingerprint(restored)
        if self.restored_settings_fingerprint_sha256 != expected:
            raise ValueError(
                "restored_settings_fingerprint_sha256 does not match settings"
            )
        for field_name, digest in (
            (
                "expected_prior_settings_fingerprint_sha256",
                self.expected_prior_settings_fingerprint_sha256,
            ),
            (
                "restored_settings_fingerprint_sha256",
                self.restored_settings_fingerprint_sha256,
            ),
        ):
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(f"{field_name} must be 64 lowercase hex digits")
        object.__setattr__(self, "restored_settings", MappingProxyType(restored))

    @classmethod
    def from_verified_settings(
        cls,
        policy: MechanismPolicy,
        controller_id: str,
        restored_settings: Mapping[str, str],
        expected_prior_settings_fingerprint_sha256: str,
    ) -> HostPolicyRestorationEvidence:
        return cls(
            policy=policy,
            controller_id=controller_id,
            restored_settings=restored_settings,
            restored_settings_fingerprint_sha256=HostPolicyEvidence.fingerprint(
                restored_settings
            ),
            expected_prior_settings_fingerprint_sha256=(
                expected_prior_settings_fingerprint_sha256
            ),
            verified=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "controller_id": self.controller_id,
            "verified": self.verified,
            "restored_settings": dict(sorted(self.restored_settings.items())),
            "restored_settings_fingerprint_sha256": (
                self.restored_settings_fingerprint_sha256
            ),
            "expected_prior_settings_fingerprint_sha256": (
                self.expected_prior_settings_fingerprint_sha256
            ),
        }


class HostPolicyController(Protocol):
    """Privileged adapter injected by the caller; Wattch performs no mutation.

    `apply_and_verify` must either return evidence for the exact observed
    settings and saved pre-trial state or fail without leaving a partial policy
    active. `restore_and_verify` must read back that saved state after restore.
    """

    def apply_and_verify(self, trial: TrialSpec) -> HostPolicyEvidence: ...

    def restore_and_verify(
        self, trial: TrialSpec, evidence: HostPolicyEvidence
    ) -> HostPolicyRestorationEvidence: ...


class RunningProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


class ProcessLauncher(Protocol):
    def start(
        self,
        command: WorkloadCommand,
        *,
        nice: int,
        cpu_affinity: tuple[int, ...],
        resource_limits: Mapping[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> RunningProcess: ...

    def read_nice(self, process: RunningProcess) -> int: ...


class _PopenProcess:
    def __init__(self, process: subprocess.Popen[bytes], handles: tuple[Any, Any]):
        self._process = process
        self._handles = handles
        self.pid = process.pid
        self._process_group_id: int | None = None
        if os.name == "posix" and hasattr(os, "getpgid") and hasattr(os, "killpg"):
            try:
                process_group_id = os.getpgid(self.pid)
            except ProcessLookupError:
                process_group_id = None
            if (
                process_group_id == self.pid
                and process_group_id > 1
                and process_group_id != os.getpgrp()
            ):
                # SystemProcessLauncher uses start_new_session=True, so the
                # child's PID must be the id of a new, isolated process group.
                self._process_group_id = process_group_id

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        if not self._signal_group(signal.SIGTERM) and self.poll() is None:
            self._process.terminate()

    def kill(self) -> None:
        if not self._signal_group(signal.SIGKILL) and self.poll() is None:
            self._process.kill()

    def close(self) -> None:
        self.cleanup_group(timeout_s=0.1)
        for handle in self._handles:
            handle.close()

    @property
    def owns_process_group(self) -> bool:
        return self._process_group_id is not None

    def _signal_group(self, signum: int) -> bool:
        process_group_id = self._process_group_id
        if process_group_id is None:
            return False
        if (
            process_group_id != self.pid
            or process_group_id <= 1
            or process_group_id == os.getpgrp()
        ):
            # Never risk signalling Wattch's own group or an unverified group.
            self._process_group_id = None
            return False
        try:
            current_group = os.getpgid(self.pid)
        except ProcessLookupError:
            # A verified process group remains uniquely identified while any
            # descendant is alive, even after its original leader exits.
            current_group = process_group_id
        except OSError:
            return False
        if current_group != process_group_id:
            # The leader PID was reused or escaped its group. Do not signal it.
            self._process_group_id = None
            return False
        try:
            os.killpg(process_group_id, signum)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True

    def _group_alive(self) -> bool:
        process_group_id = self._process_group_id
        if process_group_id is None:
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def cleanup_group(self, *, timeout_s: float) -> None:
        """Terminate lingering descendants from the verified child group."""

        if self._process_group_id is None:
            return
        self._signal_group(signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self._group_alive() and time.monotonic() < deadline:
            time.sleep(0.005)
        if self._group_alive():
            self._signal_group(signal.SIGKILL)
            kill_deadline = time.monotonic() + 0.25
            while self._group_alive() and time.monotonic() < kill_deadline:
                time.sleep(0.005)


class SystemProcessLauncher:
    """Shell-free launcher with absolute, read-back niceness and Linux affinity."""

    def start(
        self,
        command: WorkloadCommand,
        *,
        nice: int,
        cpu_affinity: tuple[int, ...],
        resource_limits: Mapping[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> RunningProcess:
        if resource_limits:
            raise ValueError(
                "the default launcher cannot enforce resource_limits safely; "
                "inject a platform-specific ProcessLauncher"
            )
        argv = list(command.argv)
        if cpu_affinity:
            if not sys.platform.startswith("linux"):
                raise ValueError("cpu_affinity requires a platform-specific launcher")
            taskset = shutil.which("taskset")
            if taskset is None:
                raise ValueError(
                    "cpu_affinity was requested but taskset is unavailable"
                )
            argv = [taskset, "--cpu-list", ",".join(map(str, cpu_affinity)), *argv]
        if not hasattr(os, "getpriority") or not hasattr(os, "PRIO_PROCESS"):
            raise ValueError(
                "the default launcher cannot verify absolute niceness on this platform"
            )
        parent_nice = os.getpriority(os.PRIO_PROCESS, 0)
        if nice < parent_nice:
            raise PermissionError(
                f"requested absolute nice {nice} is below runner nice {parent_nice}; "
                "the default launcher cannot safely increase priority"
            )
        nice_increment = nice - parent_nice
        if nice_increment:
            nice_executable = shutil.which("nice")
            if nice_executable is None:
                raise ValueError(
                    "target nice level was requested but nice is unavailable"
                )
            argv = [nice_executable, "-n", str(nice_increment), *argv]

        env = os.environ.copy()
        env.update(command.env)
        stdout_handle = stdout_path.open("xb")
        try:
            stderr_handle = stderr_path.open("xb")
        except BaseException:
            stdout_handle.close()
            raise
        try:
            process = subprocess.Popen(
                argv,
                cwd=command.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                start_new_session=True,
            )
        except BaseException:
            stdout_handle.close()
            stderr_handle.close()
            raise
        return _PopenProcess(process, (stdout_handle, stderr_handle))

    def read_nice(self, process: RunningProcess) -> int:
        try:
            return os.getpriority(os.PRIO_PROCESS, process.pid)
        except ProcessLookupError as exc:
            raise RuntimeError(
                "process exited before its absolute niceness could be verified"
            ) from exc


WorkloadMetricsProvider = Callable[[TrialSpec, Path, float, float], WorkloadMetrics]


@dataclass(frozen=True)
class TrialRunResult:
    trial_id: str
    mechanism_policy: str
    host_provenance: Mapping[str, str]
    host_policy_evidence: HostPolicyEvidence | None
    host_policy_restored: bool | None
    host_policy_restoration_evidence: HostPolicyRestorationEvidence | None
    execution_contract: Mapping[str, Any] | None
    execution_contract_sha256: str | None
    study_preregistration_sha256: str | None
    measurement_overhead_calibration_sha256: str | None
    primary_sample_bracket: Mapping[str, Mapping[str, int]] | None
    workload_metrics_evidence: WorkloadMetricsEvidence
    background_workload_metrics_evidence: WorkloadMetricsEvidence
    status: str
    target_returncode: int | None
    background_returncode: int | None
    actual_target_nice: int | None
    actual_background_nice: int | None
    target_timed_out: bool
    horizon_terminated: bool
    target_runtime_s: float
    target_stdout_sha256: str | None
    target_stderr_sha256: str | None
    background_stdout_sha256: str | None
    background_stderr_sha256: str | None
    marker_event_ids: tuple[str, ...]
    recovery_time_s: float
    fixed_horizon_target_ns: int | None
    fixed_horizon_tolerance_ns: int | None
    fixed_horizon_observed_ns: int | None
    fixed_horizon_error_ns: int | None
    fixed_horizon_within_tolerance: bool | None
    completed_work: float | None
    work_unit: str | None
    throughput_per_s: float | None
    latency_ms: Mapping[str, float] | None
    backlog_at_end: float | None
    slo_met: bool | None
    arrival_trace_id: str | None
    arrival_trace_sha256: str | None
    arrival_trace_valid: bool | None
    claim_eligibility: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "mechanism_policy": self.mechanism_policy,
            "host_provenance": dict(self.host_provenance),
            "host_policy_evidence": (
                None
                if self.host_policy_evidence is None
                else self.host_policy_evidence.to_dict()
            ),
            "host_policy_restored": self.host_policy_restored,
            "host_policy_restoration_evidence": (
                None
                if self.host_policy_restoration_evidence is None
                else self.host_policy_restoration_evidence.to_dict()
            ),
            "execution_contract": (
                None
                if self.execution_contract is None
                else dict(self.execution_contract)
            ),
            "execution_contract_sha256": self.execution_contract_sha256,
            "study_preregistration_sha256": self.study_preregistration_sha256,
            "measurement_overhead_calibration_sha256": (
                self.measurement_overhead_calibration_sha256
            ),
            "primary_sample_bracket": (
                None
                if self.primary_sample_bracket is None
                else {
                    uid: dict(values)
                    for uid, values in sorted(self.primary_sample_bracket.items())
                }
            ),
            "workload_metrics_evidence": self.workload_metrics_evidence.to_dict(),
            "background_workload_metrics_evidence": (
                self.background_workload_metrics_evidence.to_dict()
            ),
            "status": self.status,
            "target_returncode": self.target_returncode,
            "background_returncode": self.background_returncode,
            "actual_target_nice": self.actual_target_nice,
            "actual_background_nice": self.actual_background_nice,
            "target_timed_out": self.target_timed_out,
            "horizon_terminated": self.horizon_terminated,
            "target_runtime_s": self.target_runtime_s,
            "target_stdout_sha256": self.target_stdout_sha256,
            "target_stderr_sha256": self.target_stderr_sha256,
            "background_stdout_sha256": self.background_stdout_sha256,
            "background_stderr_sha256": self.background_stderr_sha256,
            "marker_event_ids": list(self.marker_event_ids),
            "recovery_time_s": self.recovery_time_s,
            "fixed_horizon_target_ns": self.fixed_horizon_target_ns,
            "fixed_horizon_tolerance_ns": self.fixed_horizon_tolerance_ns,
            "fixed_horizon_observed_ns": self.fixed_horizon_observed_ns,
            "fixed_horizon_error_ns": self.fixed_horizon_error_ns,
            "fixed_horizon_within_tolerance": self.fixed_horizon_within_tolerance,
            "completed_work": self.completed_work,
            "work_unit": self.work_unit,
            "throughput_per_s": self.throughput_per_s,
            "latency_ms": (
                None
                if self.latency_ms is None
                else dict(sorted(self.latency_ms.items()))
            ),
            "backlog_at_end": self.backlog_at_end,
            "slo_met": self.slo_met,
            "arrival_trace_id": self.arrival_trace_id,
            "arrival_trace_sha256": self.arrival_trace_sha256,
            "arrival_trace_valid": self.arrival_trace_valid,
            "claim_eligibility": self.claim_eligibility,
            "error": self.error,
        }


def _ack_event(ack: object) -> object:
    if isinstance(ack, Mapping):
        if "event" not in ack:
            raise ValueError("RecordEventAck is missing event")
        return ack["event"]
    try:
        return getattr(ack, "event")
    except AttributeError as exc:
        raise ValueError("RecordEventAck is missing event") from exc


def _event_value(event: object, name: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _sha256_if_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _with_replay_environment(
    command: WorkloadCommand,
    trial: TrialSpec,
    *,
    workload_metrics_path: Path | None = None,
) -> WorkloadCommand:
    """Bind matched demand deterministically without arm-specific variables."""

    environment = dict(command.env)
    environment.update(
        {
            "WATTCH_MATCHED_BLOCK_ID": (
                f"{trial.experiment_id}-block-{trial.block_index:03d}"
            ),
            "WATTCH_REPLAY_SEED": str(trial.random_seed),
            "WATTCH_INPUT_ID": trial.input_id,
        }
    )
    arrival_trace_id = trial.observation.arrival_trace_id
    if arrival_trace_id is not None:
        environment["WATTCH_ARRIVAL_TRACE_ID"] = arrival_trace_id
    if workload_metrics_path is not None:
        environment.update(
            {
                "WATTCH_METRICS_PATH": str(workload_metrics_path),
                "WATTCH_METRICS_SCHEMA_VERSION": "1",
            }
        )
    return WorkloadCommand(argv=command.argv, cwd=command.cwd, env=environment)


def _execution_contract(
    trial: TrialSpec,
    launcher: ProcessLauncher,
    *,
    workload_metrics_path: Path,
    background_workload_metrics_path: Path,
) -> tuple[dict[str, Any], WorkloadCommand, WorkloadCommand | None, str]:
    """Freeze the exact command inputs passed to the process launcher.

    The digest is stored in canonical ``trial.json`` labels before capture and
    the complete document is stored in sealed ``acquisition.json``.  This
    prevents a claim from silently substituting a command, replay environment,
    priority, affinity, or resource-limit declaration after scheduling.
    """

    target = _with_replay_environment(
        trial.target,
        trial,
        workload_metrics_path=workload_metrics_path,
    )
    background = (
        None
        if trial.background is None
        else _with_replay_environment(
            trial.background,
            trial,
            workload_metrics_path=background_workload_metrics_path,
        )
    )
    launcher_type = type(launcher)
    document: dict[str, Any] = {
        "schema_version": 1,
        "launcher": f"{launcher_type.__module__}.{launcher_type.__qualname__}",
        "target": target.to_dict(),
        "background": None if background is None else background.to_dict(),
        "target_nice": trial.target_nice,
        "background_nice": trial.background_nice,
        "cpu_affinity": list(trial.cpu_affinity),
        "resource_limits": dict(sorted(trial.resource_limits.items())),
    }
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return document, target, background, hashlib.sha256(encoded).hexdigest()


def _claim_eligibility(
    trial: TrialSpec,
    metrics: WorkloadMetrics,
    *,
    process_ok: bool,
    metrics_source: WorkloadMetricsSource,
    background_metrics: WorkloadMetrics,
    background_metrics_source: WorkloadMetricsSource,
) -> str:
    if not process_ok:
        return "INELIGIBLE"
    # Python callbacks are intentionally retained for exploratory harnesses,
    # but caller-supplied observations are not confirmatory evidence.  Only a
    # sealed sidecar written at the path passed to the executed target can be
    # reopened and independently checked during replay.
    if metrics_source is not WorkloadMetricsSource.WORKLOAD_SIDECAR:
        return "NOT_EVALUATED"
    if metrics.completed_work is None or metrics.throughput_per_s is None:
        return "NOT_EVALUATED"
    if metrics.output_sha256 is None:
        return "NOT_EVALUATED"
    if metrics.arrival_trace_valid is False:
        return "INELIGIBLE"
    if any(
        value is not None
        for value in (
            metrics.arrival_trace_id,
            metrics.arrival_trace_sha256,
            metrics.arrival_trace_valid,
        )
    ) and (
        metrics.arrival_trace_id is None
        or metrics.arrival_trace_sha256 is None
        or metrics.arrival_trace_valid is not True
        or (
            trial.observation.arrival_trace_id is not None
            and metrics.arrival_trace_id != trial.observation.arrival_trace_id
        )
    ):
        return "NOT_EVALUATED"
    if trial.background is not None:
        if background_metrics_source is not WorkloadMetricsSource.WORKLOAD_SIDECAR:
            return "NOT_EVALUATED"
        if (
            background_metrics.completed_work is None
            or background_metrics.throughput_per_s is None
            or background_metrics.backlog_at_end is None
            or background_metrics.slo_met is None
            or background_metrics.output_sha256 is None
        ):
            return "NOT_EVALUATED"
        if any(
            value is not None
            for value in (
                background_metrics.arrival_trace_id,
                background_metrics.arrival_trace_sha256,
                background_metrics.arrival_trace_valid,
            )
        ) and (
            background_metrics.arrival_trace_id is None
            or background_metrics.arrival_trace_sha256 is None
            or background_metrics.arrival_trace_valid is not True
        ):
            return "NOT_EVALUATED"
        if (
            background_metrics.backlog_at_end > 0
            or not background_metrics.slo_met
            or background_metrics.arrival_trace_valid is False
        ):
            return "INELIGIBLE"
    if isinstance(trial.observation, EqualWork):
        if metrics.work_unit != trial.observation.work_unit:
            return "INELIGIBLE"
        if not math.isclose(
            metrics.completed_work,
            trial.observation.target_work,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            return "INELIGIBLE"
    if trial.observation.workload_mode.value == "SERVICE":
        if (
            metrics.latency_ms is None
            or metrics.backlog_at_end is None
            or metrics.slo_met is None
            or metrics.arrival_trace_id != trial.observation.arrival_trace_id
            or metrics.arrival_trace_sha256 is None
            or metrics.arrival_trace_valid is not True
        ):
            return "NOT_EVALUATED"
        if metrics.backlog_at_end > 0 or not metrics.slo_met:
            return "INELIGIBLE"
    if not trial.confirmatory:
        return "NOT_EVALUATED"
    return "ELIGIBLE"


def _stop_process(process: RunningProcess | None, grace_s: float = 2.0) -> int | None:
    if process is None:
        return None
    returncode = process.poll()
    if returncode is not None:
        cleanup_group = getattr(process, "cleanup_group", None)
        if callable(cleanup_group):
            cleanup_group(timeout_s=grace_s)
        return returncode
    process.terminate()
    try:
        returncode = process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait(timeout=grace_s)
    cleanup_group = getattr(process, "cleanup_group", None)
    if callable(cleanup_group):
        cleanup_group(timeout_s=grace_s)
    return returncode


def _read_verified_nice(
    launcher: ProcessLauncher,
    process: RunningProcess,
    *,
    expected: int,
    role: str,
) -> int:
    reader = getattr(launcher, "read_nice", None)
    if not callable(reader):
        raise TypeError(
            "ProcessLauncher must implement read_nice for experiment evidence"
        )
    deadline = time.monotonic() + 0.25
    while True:
        actual = reader(process)
        if isinstance(actual, bool) or not isinstance(actual, int):
            raise TypeError(f"{role} nice read-back must be an integer")
        if actual == expected:
            return actual
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError(
                f"{role} nice read-back {actual} does not match requested {expected}"
            )
        # `/usr/bin/nice` applies its increment after exec; permit that short,
        # bounded transition, then seal the observed absolute value.
        time.sleep(0.001)


def _validate_host_policy_restoration(
    application: HostPolicyEvidence,
    restoration: object,
) -> HostPolicyRestorationEvidence:
    if not isinstance(restoration, HostPolicyRestorationEvidence):
        raise TypeError(
            "HostPolicyController.restore_and_verify must return "
            "HostPolicyRestorationEvidence"
        )
    if restoration.policy is not application.policy:
        raise ValueError("host policy restoration does not match the applied policy")
    if restoration.controller_id != application.controller_id:
        raise ValueError("host policy restoration controller identity changed")
    if not restoration.verified:
        raise RuntimeError("host policy restoration was not verified")
    if (
        restoration.expected_prior_settings_fingerprint_sha256
        != application.prior_settings_fingerprint_sha256
    ):
        raise ValueError("host policy restoration expected a different prior state")
    if (
        restoration.restored_settings_fingerprint_sha256
        != application.prior_settings_fingerprint_sha256
    ):
        raise RuntimeError("host policy restoration read-back differs from prior state")
    return restoration


class TrialRunner:
    """Run scheduled commands and bind their raw daemon evidence to sidecars."""

    def __init__(
        self,
        boundary: MeasurementBoundary,
        session_factory: Callable[[TrialSpec], TrialDaemonSession],
        *,
        launcher: ProcessLauncher | None = None,
        host_policy_controller: HostPolicyController | None = None,
        metrics_provider: WorkloadMetricsProvider | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        sample_barrier_timeout_s: float | None = None,
        study_preregistration_sha256: str | None = None,
        measurement_overhead_calibration_sha256: str | None = None,
    ):
        self.boundary = boundary
        self.session_factory = session_factory
        self.launcher = launcher or SystemProcessLauncher()
        self.host_policy_controller = host_policy_controller
        self.metrics_provider = metrics_provider
        self.monotonic = monotonic
        self.sleep = sleep
        if sample_barrier_timeout_s is not None and (
            isinstance(sample_barrier_timeout_s, bool)
            or not isinstance(sample_barrier_timeout_s, (int, float))
            or not math.isfinite(sample_barrier_timeout_s)
            or sample_barrier_timeout_s <= 0
        ):
            raise ValueError("sample_barrier_timeout_s must be finite and positive")
        self.sample_barrier_timeout_s = (
            None
            if sample_barrier_timeout_s is None
            else float(sample_barrier_timeout_s)
        )
        for field_name, digest in (
            ("study_preregistration_sha256", study_preregistration_sha256),
            (
                "measurement_overhead_calibration_sha256",
                measurement_overhead_calibration_sha256,
            ),
        ):
            if digest is not None and (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{field_name} must be 64 lowercase hex digits")
        self.study_preregistration_sha256 = study_preregistration_sha256
        self.measurement_overhead_calibration_sha256 = (
            measurement_overhead_calibration_sha256
        )

    def run(self, experiment_dir: str | Path, trial: TrialSpec) -> TrialRunResult:
        host_provenance = local_host_provenance()
        session: TrialDaemonSession | None = None
        writer: TrialArtifactWriter | None = None
        target: RunningProcess | None = None
        background: RunningProcess | None = None
        marker_ids: list[str] = []
        target_returncode: int | None = None
        background_returncode: int | None = None
        actual_target_nice: int | None = None
        actual_background_nice: int | None = None
        target_timed_out = False
        horizon_terminated = False
        target_started = 0.0
        target_ended = 0.0
        recovery_time_s = 0.0
        fixed_horizon_target_ns: int | None = None
        fixed_horizon_tolerance_ns: int | None = None
        fixed_horizon_observed_ns: int | None = None
        fixed_horizon_error_ns: int | None = None
        fixed_horizon_within_tolerance: bool | None = None
        metrics = WorkloadMetrics()
        metrics_evidence = WorkloadMetricsEvidence.none()
        background_metrics = WorkloadMetrics()
        background_metrics_evidence = WorkloadMetricsEvidence.none()
        capture_started = False
        capture_stopped = False
        host_policy_evidence: HostPolicyEvidence | None = None
        host_policy_restore_attempted = False
        host_policy_restored: bool | None = None
        host_policy_restoration_evidence: HostPolicyRestorationEvidence | None = None
        execution_contract: dict[str, Any] | None = None
        execution_contract_sha256: str | None = None
        target_command: WorkloadCommand | None = None
        background_command: WorkloadCommand | None = None
        primary_sample_bracket: dict[str, dict[str, int]] | None = None
        workload_metrics_path = (
            Path(experiment_dir) / "trials" / trial.trial_id / WORKLOAD_METRICS_FILENAME
        ).absolute()
        background_workload_metrics_path = (
            Path(experiment_dir)
            / "trials"
            / trial.trial_id
            / BACKGROUND_WORKLOAD_METRICS_FILENAME
        ).absolute()

        try:
            if (
                trial.mechanism_policy is not MechanismPolicy.NORMAL_DYNAMIC
                and self.host_policy_controller is None
            ):
                raise RuntimeError(
                    f"{trial.mechanism_policy.value} requires a HostPolicyController"
                )
            (
                execution_contract,
                target_command,
                background_command,
                execution_contract_sha256,
            ) = _execution_contract(
                trial,
                self.launcher,
                workload_metrics_path=workload_metrics_path,
                background_workload_metrics_path=(background_workload_metrics_path),
            )
            runtime_labels = {
                **trial.labels,
                "mechanism_policy": trial.mechanism_policy.value,
                "host_policy_verification": "DECLARED_ONLY",
                "execution_contract_sha256": execution_contract_sha256,
                "workload_metrics_contract": WORKLOAD_METRICS_CONTRACT,
                "workload_metrics_path": WORKLOAD_METRICS_FILENAME,
                "background_workload_metrics_path": (
                    BACKGROUND_WORKLOAD_METRICS_FILENAME
                ),
            }
            if self.study_preregistration_sha256 is not None:
                runtime_labels["study_preregistration_sha256"] = (
                    self.study_preregistration_sha256
                )
            if self.measurement_overhead_calibration_sha256 is not None:
                runtime_labels["measurement_overhead_calibration_sha256"] = (
                    self.measurement_overhead_calibration_sha256
                )
            if self.host_policy_controller is not None:
                host_policy_evidence = self.host_policy_controller.apply_and_verify(
                    trial
                )
                if not isinstance(host_policy_evidence, HostPolicyEvidence):
                    raise TypeError(
                        "HostPolicyController.apply_and_verify must return "
                        "HostPolicyEvidence"
                    )
                if host_policy_evidence.policy is not trial.mechanism_policy:
                    raise ValueError(
                        "host policy evidence does not match the scheduled policy"
                    )
                if not host_policy_evidence.verified:
                    raise RuntimeError("host policy controller did not verify settings")
                runtime_labels.update(host_policy_evidence.to_labels())
            trial = replace(trial, labels=runtime_labels)
            session = self.session_factory(trial)
            writer = TrialArtifactWriter(
                experiment_dir,
                trial,
                self.boundary,
                session.sources,
                node_id=session.node_id,
                protocol_version=session.protocol_version,
                clock_instance_id=session.clock_instance_id,
            )
            if (
                workload_metrics_path
                != (writer.trial_dir / WORKLOAD_METRICS_FILENAME).absolute()
            ):  # pragma: no cover - construction guard
                raise RuntimeError("workload metrics path escaped the trial artifact")
            if (
                background_workload_metrics_path
                != (writer.trial_dir / BACKGROUND_WORKLOAD_METRICS_FILENAME).absolute()
            ):  # pragma: no cover - construction guard
                raise RuntimeError(
                    "background workload metrics path escaped the trial artifact"
                )
            source_ids = [source.local_source_id for source in session.sources]
            primary_by_id = {
                source.local_source_id: source.source_uid
                for source in writer.selected_sources
            }
            primary_sources_by_id = {
                source.local_source_id: source for source in writer.selected_sources
            }
            interval_tail_margin_by_id = {
                source_id: (
                    source.nominal_period_ns
                    if source.temporality == "INTERVAL_AVERAGE"
                    and isinstance(source.nominal_period_ns, int)
                    and not isinstance(source.nominal_period_ns, bool)
                    and source.nominal_period_ns > 0
                    else (
                        trial.interval_ms * 1_000_000
                        if source.temporality == "INTERVAL_AVERAGE"
                        else 0
                    )
                )
                for source_id, source in primary_sources_by_id.items()
            }
            latest_primary_sample_ns: dict[int, int] = {}
            recent_primary_sample_ns: dict[int, list[int]] = {}
            sample_condition = threading.Condition()

            def append_raw_batch(batch: RawSampleBatch) -> None:
                writer.append_raw_batch(batch)
                with sample_condition:
                    for sample in batch.samples:
                        if sample.source_id in primary_by_id:
                            previous = latest_primary_sample_ns.get(sample.source_id)
                            if previous is None or sample.monotonic_ns > previous:
                                latest_primary_sample_ns[sample.source_id] = (
                                    sample.monotonic_ns
                                )
                                recent = recent_primary_sample_ns.setdefault(
                                    sample.source_id, []
                                )
                                recent.append(sample.monotonic_ns)
                                del recent[:-2]
                    sample_condition.notify_all()

            barrier_timeout_s = self.sample_barrier_timeout_s
            if barrier_timeout_s is None:
                nominal_period_s = max(
                    (
                        (source.nominal_period_ns or 0) / 1_000_000_000
                        for source in writer.selected_sources
                    ),
                    default=0.0,
                )
                barrier_timeout_s = max(
                    5.0,
                    3.0 * trial.interval_ms / 1_000,
                    3.0 * nominal_period_s,
                )

            def wait_for_primary_samples(
                *,
                after_ns: int | None,
                phase: str,
                require_interval_tail: bool = False,
            ) -> tuple[dict[str, int], dict[str, int]]:
                deadline = time.monotonic() + barrier_timeout_s
                with sample_condition:
                    while True:
                        qualifying_counts: dict[int, int] = {}
                        missing: list[int] = []
                        for source_id, source in primary_sources_by_id.items():
                            qualifying = [
                                timestamp
                                for timestamp in recent_primary_sample_ns.get(
                                    source_id, ()
                                )
                                if after_ns is None or timestamp >= after_ns
                            ]
                            qualifying_counts[source_id] = len(qualifying)
                            ready = bool(qualifying)
                            if (
                                ready
                                and require_interval_tail
                                and after_ns is not None
                                and source.temporality == "INTERVAL_AVERAGE"
                            ):
                                margin_ns = interval_tail_margin_by_id[source_id]
                                ready = (
                                    len(qualifying) >= 2
                                    or qualifying[-1] >= after_ns + margin_ns
                                )
                            if not ready:
                                missing.append(source_id)
                        if not missing:
                            return (
                                {
                                    primary_by_id[source_id]: latest_primary_sample_ns[
                                        source_id
                                    ]
                                    for source_id in sorted(primary_by_id)
                                },
                                {
                                    primary_by_id[source_id]: qualifying_counts[
                                        source_id
                                    ]
                                    for source_id in sorted(primary_by_id)
                                },
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            missing_uids = ", ".join(
                                primary_by_id[source_id] for source_id in missing
                            )
                            relation = (
                                "any sample"
                                if after_ns is None
                                else (
                                    f"a sample at or after {after_ns} ns plus "
                                    "interval-tail support when required"
                                )
                            )
                            raise TimeoutError(
                                f"{phase} primary-sample barrier timed out: "
                                f"{missing_uids} did not emit {relation}"
                            )
                        sample_condition.wait(timeout=remaining)

            session.start_capture(
                trial.run_id,
                trial.interval_ms,
                source_ids,
                on_raw_batch=append_raw_batch,
                on_event_batch=writer.append_event_batch,
            )
            capture_started = True
            pre_window_samples, _ = wait_for_primary_samples(
                after_ns=None,
                phase="pre-window",
            )

            def mark(kind: MarkerKind, extra: Mapping[str, str] | None = None) -> int:
                event_id = f"{trial.run_id}:{len(marker_ids):02d}"
                labels = {
                    **trial.labels,
                    "experiment_id": trial.experiment_id,
                    "trial_id": trial.trial_id,
                    "arm": trial.arm.value,
                    "block_index": str(trial.block_index),
                    "order_position": str(trial.order_position),
                    "target_nice": str(trial.target_nice),
                    "replay_seed": str(trial.random_seed),
                }
                if extra is not None:
                    labels.update(extra)
                ack = session.record_event(
                    trial.run_id,
                    event_id,
                    kind.value,
                    labels,
                )
                event = _ack_event(ack)
                if _event_value(event, "event_id") != event_id:
                    raise ValueError(
                        "RecordEventAck event_id does not match the request"
                    )
                if _event_value(event, "run_id") != trial.run_id:
                    raise ValueError("RecordEventAck run_id does not match the trial")
                if _event_value(event, "kind") != kind.value:
                    raise ValueError("RecordEventAck kind does not match the request")
                if _event_value(event, "node_id") != session.node_id:
                    raise ValueError(
                        "RecordEventAck node_id does not match the session"
                    )
                monotonic_ns = _event_value(event, "monotonic_ns")
                if isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int):
                    raise ValueError("RecordEventAck monotonic_ns must be an integer")
                writer.append_daemon_event(event)
                marker_ids.append(event_id)
                return monotonic_ns

            mark(MarkerKind.PRE_WINDOW_START)
            mark(
                MarkerKind.BACKGROUND_ARRIVAL,
                {"background": "absent" if trial.background is None else "command"},
            )
            if trial.background is not None:
                if background_command is None:  # pragma: no cover - construction guard
                    raise RuntimeError("background execution contract is missing")
                if (
                    background_workload_metrics_path.exists()
                    or background_workload_metrics_path.is_symlink()
                ):
                    raise FileExistsError(
                        "background workload metrics path already exists before launch"
                    )
                background = self.launcher.start(
                    background_command,
                    nice=trial.background_nice,
                    cpu_affinity=trial.cpu_affinity,
                    resource_limits=trial.resource_limits,
                    stdout_path=writer.trial_dir / "background.stdout.log",
                    stderr_path=writer.trial_dir / "background.stderr.log",
                )
                actual_background_nice = _read_verified_nice(
                    self.launcher,
                    background,
                    expected=trial.background_nice,
                    role="background",
                )

            if isinstance(trial.observation, FixedHorizon):
                self.sleep(trial.observation.pre_window_s)

            spawn_mark_started = self.monotonic()
            target_spawn_ns = mark(MarkerKind.TARGET_SPAWN)
            spawn_mark_finished = self.monotonic()
            # RecordEvent is stamped between these two client-clock readings.
            # Their midpoint gives the least-biased local deadline estimate;
            # the authoritative observed horizon is checked from daemon event
            # timestamps below.
            spawn_local_reference = (spawn_mark_started + spawn_mark_finished) / 2.0
            target_started = self.monotonic()
            if target_command is None:  # pragma: no cover - construction guard
                raise RuntimeError("target execution contract is missing")
            if workload_metrics_path.exists() or workload_metrics_path.is_symlink():
                raise FileExistsError(
                    "target workload metrics path already exists before launch"
                )
            target = self.launcher.start(
                target_command,
                nice=trial.target_nice,
                cpu_affinity=trial.cpu_affinity,
                resource_limits=trial.resource_limits,
                stdout_path=writer.trial_dir / "target.stdout.log",
                stderr_path=writer.trial_dir / "target.stderr.log",
            )
            actual_target_nice = _read_verified_nice(
                self.launcher,
                target,
                expected=trial.target_nice,
                role="target",
            )
            target_exec_labels = {
                "pid": str(target.pid),
                "actual_target_nice": str(actual_target_nice),
            }
            if actual_background_nice is not None:
                target_exec_labels["actual_background_nice"] = str(
                    actual_background_nice
                )
            mark(MarkerKind.TARGET_EXEC, target_exec_labels)

            if isinstance(trial.observation, FixedHorizon):
                fixed_horizon_target_ns = round(
                    trial.observation.duration_s * 1_000_000_000
                )
                fixed_horizon_tolerance_ns = round(
                    trial.observation.tolerance_s * 1_000_000_000
                )
                deadline = spawn_local_reference + trial.observation.duration_s
                remaining = max(0.0, deadline - self.monotonic())
                try:
                    target_returncode = target.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    horizon_terminated = True
                if not horizon_terminated:
                    target_returncode = _stop_process(target)
                if horizon_terminated:
                    # Stamp the fixed observation boundary before teardown.
                    # A target still running here is explicitly ineligible;
                    # termination latency must not lengthen the energy window.
                    recovery_end_ns = mark(MarkerKind.RECOVERY_END)
                    target_returncode = _stop_process(target)
                    target_ended = self.monotonic()
                    target_exit_ns = mark(
                        MarkerKind.TARGET_EXIT,
                        {
                            "returncode": str(target_returncode),
                            "timed_out": "false",
                            "horizon_terminated": "true",
                        },
                    )
                else:
                    target_ended = self.monotonic()
                    target_exit_ns = mark(
                        MarkerKind.TARGET_EXIT,
                        {
                            "returncode": str(target_returncode),
                            "timed_out": "false",
                            "horizon_terminated": "false",
                        },
                    )
                    remaining = max(0.0, deadline - self.monotonic())
                    if remaining:
                        self.sleep(remaining)
                    recovery_end_ns = mark(MarkerKind.RECOVERY_END)

                fixed_horizon_observed_ns = recovery_end_ns - target_spawn_ns
                fixed_horizon_error_ns = (
                    fixed_horizon_observed_ns - fixed_horizon_target_ns
                )
                fixed_horizon_within_tolerance = (
                    abs(fixed_horizon_error_ns) <= fixed_horizon_tolerance_ns
                )
                recovery_time_s = max(
                    0.0, (recovery_end_ns - target_exit_ns) / 1_000_000_000
                )
            elif isinstance(trial.observation, EqualWork):
                try:
                    target_returncode = target.wait(timeout=trial.observation.timeout_s)
                except subprocess.TimeoutExpired:
                    target_timed_out = True
                    target_returncode = _stop_process(target)
                else:
                    target_returncode = _stop_process(target)
                target_ended = self.monotonic()
                target_exit_ns = mark(
                    MarkerKind.TARGET_EXIT,
                    {
                        "returncode": str(target_returncode),
                        "timed_out": str(target_timed_out).lower(),
                        "horizon_terminated": "false",
                    },
                )
            else:  # pragma: no cover - protected by the config type
                raise TypeError(
                    f"unsupported observation {type(trial.observation).__name__}"
                )

            if (
                background is not None
                and isinstance(trial.observation, EqualWork)
                and trial.observation.workload_mode.value == "SERVICE"
                and trial.observation.drain_timeout_s > 0
            ):
                try:
                    background_returncode = background.wait(
                        timeout=trial.observation.drain_timeout_s
                    )
                except subprocess.TimeoutExpired:
                    background_returncode = _stop_process(background)
                else:
                    background_returncode = _stop_process(background)
            else:
                background_returncode = _stop_process(background)
            mark(
                MarkerKind.BACKGROUND_COMPLETION,
                {
                    "background": "absent" if background is None else "command",
                    "returncode": str(background_returncode),
                    "actual_background_nice": (
                        "absent"
                        if actual_background_nice is None
                        else str(actual_background_nice)
                    ),
                },
            )
            if isinstance(trial.observation, EqualWork):
                # Drain duration is part of the declared contract.  A service
                # harness may inject a launcher whose process exits only after
                # its backlog drains; the generic runner then observes the
                # fixed recovery tail without inventing a backlog measurement.
                self.sleep(trial.observation.recovery_tail_s)
                recovery_end_ns = mark(MarkerKind.RECOVERY_END)
                recovery_time_s = max(
                    0.0, (recovery_end_ns - target_exit_ns) / 1_000_000_000
                )

            post_window_samples, post_window_counts = wait_for_primary_samples(
                after_ns=recovery_end_ns,
                phase="post-window",
                require_interval_tail=True,
            )
            primary_sample_bracket = {
                uid: {
                    "pre_window_sample_ns": pre_window_samples[uid],
                    "post_window_sample_ns": post_window_samples[uid],
                    "post_window_samples_at_or_after": post_window_counts[uid],
                    "post_window_margin_ns": interval_tail_margin_by_id[source_id],
                    "recovery_end_ns": recovery_end_ns,
                }
                for source_id, uid in sorted(primary_by_id.items())
            }

            session.stop_capture(trial.run_id)
            capture_stopped = True

            if host_policy_evidence is not None:
                if self.host_policy_controller is None:  # pragma: no cover
                    raise RuntimeError("host policy controller disappeared")
                host_policy_restore_attempted = True
                host_policy_restored = False
                host_policy_restoration_evidence = _validate_host_policy_restoration(
                    host_policy_evidence,
                    self.host_policy_controller.restore_and_verify(
                        trial, host_policy_evidence
                    ),
                )
                host_policy_restored = True

            target.close()
            target = None
            if background is not None:
                background.close()
                background = None

            stdout_path = writer.trial_dir / "target.stdout.log"
            stderr_path = writer.trial_dir / "target.stderr.log"
            stdout_sha256 = _sha256_if_file(stdout_path)
            stderr_sha256 = _sha256_if_file(stderr_path)
            background_stdout_sha256 = _sha256_if_file(
                writer.trial_dir / "background.stdout.log"
            )
            background_stderr_sha256 = _sha256_if_file(
                writer.trial_dir / "background.stderr.log"
            )
            status = "COMPLETED"
            error: str | None = None
            if target_timed_out:
                status = "FAILED"
                error = "target exceeded the declared observation timeout"
            elif target_returncode != 0 and not horizon_terminated:
                status = "FAILED"
                error = f"target exited with status {target_returncode}"
            elif (
                trial.expected_output_sha256 is not None
                and stdout_sha256 != trial.expected_output_sha256
            ):
                status = "FAILED"
                error = "target stdout did not match expected_output_sha256"

            if workload_metrics_path.exists() or workload_metrics_path.is_symlink():
                if stdout_sha256 is None:  # pragma: no cover - launcher contract guard
                    raise RuntimeError("target stdout is missing")
                metrics, metrics_sha256 = load_workload_metrics_sidecar(
                    workload_metrics_path,
                    expected_stdout_sha256=stdout_sha256,
                )
                metrics_evidence = WorkloadMetricsEvidence(
                    source=WorkloadMetricsSource.WORKLOAD_SIDECAR,
                    metrics=metrics,
                    relative_path=WORKLOAD_METRICS_FILENAME,
                    sha256=metrics_sha256,
                )
            elif self.metrics_provider is not None:
                metrics = self.metrics_provider(
                    trial,
                    writer.trial_dir,
                    max(0.0, target_ended - target_started),
                    recovery_time_s,
                )
                if not isinstance(metrics, WorkloadMetrics):
                    raise TypeError("metrics_provider must return WorkloadMetrics")
                metrics_evidence = WorkloadMetricsEvidence.exploratory(metrics)
            if (
                background_workload_metrics_path.exists()
                or background_workload_metrics_path.is_symlink()
            ):
                if background_stdout_sha256 is None:
                    raise RuntimeError("background stdout is missing")
                background_metrics, background_metrics_sha256 = (
                    load_workload_metrics_sidecar(
                        background_workload_metrics_path,
                        expected_stdout_sha256=background_stdout_sha256,
                    )
                )
                background_metrics_evidence = WorkloadMetricsEvidence(
                    source=WorkloadMetricsSource.WORKLOAD_SIDECAR,
                    metrics=background_metrics,
                    relative_path=BACKGROUND_WORKLOAD_METRICS_FILENAME,
                    sha256=background_metrics_sha256,
                )
            eligibility = _claim_eligibility(
                trial,
                metrics,
                process_ok=(
                    status == "COMPLETED"
                    and not horizon_terminated
                    and fixed_horizon_within_tolerance is not False
                ),
                metrics_source=metrics_evidence.source,
                background_metrics=background_metrics,
                background_metrics_source=background_metrics_evidence.source,
            )

            result = TrialRunResult(
                trial_id=trial.trial_id,
                mechanism_policy=trial.mechanism_policy.value,
                host_provenance=host_provenance,
                host_policy_evidence=host_policy_evidence,
                host_policy_restored=host_policy_restored,
                host_policy_restoration_evidence=(host_policy_restoration_evidence),
                execution_contract=execution_contract,
                execution_contract_sha256=execution_contract_sha256,
                study_preregistration_sha256=self.study_preregistration_sha256,
                measurement_overhead_calibration_sha256=(
                    self.measurement_overhead_calibration_sha256
                ),
                primary_sample_bracket=primary_sample_bracket,
                workload_metrics_evidence=metrics_evidence,
                background_workload_metrics_evidence=(background_metrics_evidence),
                status=status,
                target_returncode=target_returncode,
                background_returncode=background_returncode,
                actual_target_nice=actual_target_nice,
                actual_background_nice=actual_background_nice,
                target_timed_out=target_timed_out,
                horizon_terminated=horizon_terminated,
                target_runtime_s=max(0.0, target_ended - target_started),
                target_stdout_sha256=stdout_sha256,
                target_stderr_sha256=stderr_sha256,
                background_stdout_sha256=background_stdout_sha256,
                background_stderr_sha256=background_stderr_sha256,
                marker_event_ids=tuple(marker_ids),
                recovery_time_s=recovery_time_s,
                fixed_horizon_target_ns=fixed_horizon_target_ns,
                fixed_horizon_tolerance_ns=fixed_horizon_tolerance_ns,
                fixed_horizon_observed_ns=fixed_horizon_observed_ns,
                fixed_horizon_error_ns=fixed_horizon_error_ns,
                fixed_horizon_within_tolerance=fixed_horizon_within_tolerance,
                completed_work=metrics.completed_work,
                work_unit=metrics.work_unit,
                throughput_per_s=metrics.throughput_per_s,
                latency_ms=metrics.latency_ms,
                backlog_at_end=metrics.backlog_at_end,
                slo_met=metrics.slo_met,
                arrival_trace_id=metrics.arrival_trace_id,
                arrival_trace_sha256=metrics.arrival_trace_sha256,
                arrival_trace_valid=metrics.arrival_trace_valid,
                claim_eligibility=eligibility,
                error=error,
            )
            writer.finish(
                result=result.to_dict(),
                status=status,
            )
            return result
        except Exception as exc:
            failure: BaseException = exc
            _stop_process(target)
            _stop_process(background)
            if capture_started and not capture_stopped and session is not None:
                try:
                    session.stop_capture(trial.run_id)
                except BaseException:
                    pass
            if (
                host_policy_evidence is not None
                and self.host_policy_controller is not None
                and not host_policy_restore_attempted
            ):
                host_policy_restore_attempted = True
                host_policy_restored = False
                try:
                    host_policy_restoration_evidence = (
                        _validate_host_policy_restoration(
                            host_policy_evidence,
                            self.host_policy_controller.restore_and_verify(
                                trial, host_policy_evidence
                            ),
                        )
                    )
                    host_policy_restored = True
                except BaseException as restore_error:
                    host_policy_restored = False
                    failure = RuntimeError(
                        f"{type(exc).__name__}: {exc}; host policy restore failed: "
                        f"{type(restore_error).__name__}: {restore_error}"
                    )
            if writer is not None:
                try:
                    writer.abort(failure)
                except BaseException:
                    pass
            return TrialRunResult(
                trial_id=trial.trial_id,
                mechanism_policy=trial.mechanism_policy.value,
                host_provenance=host_provenance,
                host_policy_evidence=host_policy_evidence,
                host_policy_restored=host_policy_restored,
                host_policy_restoration_evidence=(host_policy_restoration_evidence),
                execution_contract=execution_contract,
                execution_contract_sha256=execution_contract_sha256,
                study_preregistration_sha256=self.study_preregistration_sha256,
                measurement_overhead_calibration_sha256=(
                    self.measurement_overhead_calibration_sha256
                ),
                primary_sample_bracket=primary_sample_bracket,
                workload_metrics_evidence=metrics_evidence,
                background_workload_metrics_evidence=(background_metrics_evidence),
                status="FAILED",
                target_returncode=target_returncode,
                background_returncode=background_returncode,
                actual_target_nice=actual_target_nice,
                actual_background_nice=actual_background_nice,
                target_timed_out=target_timed_out,
                horizon_terminated=horizon_terminated,
                target_runtime_s=max(0.0, target_ended - target_started),
                target_stdout_sha256=(
                    None
                    if writer is None
                    else _sha256_if_file(writer.trial_dir / "target.stdout.log")
                ),
                target_stderr_sha256=(
                    None
                    if writer is None
                    else _sha256_if_file(writer.trial_dir / "target.stderr.log")
                ),
                background_stdout_sha256=(
                    None
                    if writer is None
                    else _sha256_if_file(writer.trial_dir / "background.stdout.log")
                ),
                background_stderr_sha256=(
                    None
                    if writer is None
                    else _sha256_if_file(writer.trial_dir / "background.stderr.log")
                ),
                marker_event_ids=tuple(marker_ids),
                recovery_time_s=recovery_time_s,
                fixed_horizon_target_ns=fixed_horizon_target_ns,
                fixed_horizon_tolerance_ns=fixed_horizon_tolerance_ns,
                fixed_horizon_observed_ns=fixed_horizon_observed_ns,
                fixed_horizon_error_ns=fixed_horizon_error_ns,
                fixed_horizon_within_tolerance=fixed_horizon_within_tolerance,
                completed_work=metrics.completed_work,
                work_unit=metrics.work_unit,
                throughput_per_s=metrics.throughput_per_s,
                latency_ms=metrics.latency_ms,
                backlog_at_end=metrics.backlog_at_end,
                slo_met=metrics.slo_met,
                arrival_trace_id=metrics.arrival_trace_id,
                arrival_trace_sha256=metrics.arrival_trace_sha256,
                arrival_trace_valid=metrics.arrival_trace_valid,
                claim_eligibility="INELIGIBLE",
                error=f"{type(failure).__name__}: {failure}",
            )
        finally:
            if target is not None:
                target.close()
            if background is not None:
                background.close()
            if session is not None:
                session.close()


@dataclass(frozen=True)
class ExperimentRunResult:
    experiment_id: str
    experiment_dir: Path
    trials: tuple[TrialRunResult, ...]

    @property
    def completed(self) -> int:
        return sum(result.status == "COMPLETED" for result in self.trials)

    @property
    def failed(self) -> int:
        return sum(result.status == "FAILED" for result in self.trials)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "summary": {
                "trials": len(self.trials),
                "completed": self.completed,
                "failed": self.failed,
                "claim_eligible": sum(
                    result.claim_eligibility == "ELIGIBLE" for result in self.trials
                ),
                "claim_ineligible": sum(
                    result.claim_eligibility == "INELIGIBLE" for result in self.trials
                ),
                "not_evaluated": sum(
                    result.claim_eligibility == "NOT_EVALUATED"
                    for result in self.trials
                ),
            },
            "results": [result.to_dict() for result in self.trials],
        }


class ExperimentRunner:
    """Execute a complete matched schedule in its randomized order."""

    def __init__(
        self,
        trial_runner: TrialRunner,
        *,
        sleep: Callable[[float], None] = time.sleep,
        stop_on_failure: bool = False,
    ):
        self.trial_runner = trial_runner
        self.sleep = sleep
        self.stop_on_failure = stop_on_failure

    def run(
        self, output_root: str | Path, schedule: ExperimentSchedule
    ) -> ExperimentRunResult:
        if self.trial_runner.boundary != schedule.experiment.boundary:
            raise ValueError(
                "TrialRunner measurement boundary does not match the schedule"
            )
        # prepare_experiment_layout uses mkdir(exist_ok=False): rerunning the
        # same experiment id cannot overwrite or splice existing evidence.
        experiment_dir = prepare_experiment_layout(output_root, schedule)
        results: list[TrialRunResult] = []
        for index, trial in enumerate(schedule.trials):
            result = self.trial_runner.run(experiment_dir, trial)
            results.append(result)
            if result.host_policy_restored is False:
                # Continuing after a failed read-back would contaminate every
                # later block with an unknown host power-state policy.
                break
            if self.stop_on_failure and result.status == "FAILED":
                break
            if index + 1 >= len(schedule.trials):
                continue
            next_trial = schedule.trials[index + 1]
            if next_trial.block_index != trial.block_index:
                washout_s = schedule.experiment.block_washout_s
            else:
                washout_s = schedule.experiment.trial_washout_s
            if washout_s:
                self.sleep(washout_s)

        run_result = ExperimentRunResult(
            experiment_id=schedule.experiment.experiment_id,
            experiment_dir=experiment_dir,
            trials=tuple(results),
        )
        results_path = experiment_dir / "run-results.json"
        with results_path.open("x", encoding="utf-8") as handle:
            json.dump(run_result.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return run_result


def run_schedule(
    output_root: str | Path,
    schedule: ExperimentSchedule,
    trial_runner: TrialRunner,
    *,
    sleep: Callable[[float], None] = time.sleep,
    stop_on_failure: bool = False,
) -> ExperimentRunResult:
    """Convenience wrapper around :class:`ExperimentRunner`."""

    return ExperimentRunner(
        trial_runner,
        sleep=sleep,
        stop_on_failure=stop_on_failure,
    ).run(output_root, schedule)


__all__ = [
    "ExperimentRunResult",
    "ExperimentRunner",
    "HostPolicyController",
    "HostPolicyEvidence",
    "HostPolicyRestorationEvidence",
    "MarkerKind",
    "ProcessLauncher",
    "RunningProcess",
    "SystemProcessLauncher",
    "TrialDaemonSession",
    "TrialRunResult",
    "TrialRunner",
    "WorkloadMetrics",
    "WorkloadMetricsEvidence",
    "WorkloadMetricsProvider",
    "WorkloadMetricsSource",
    "run_schedule",
]
