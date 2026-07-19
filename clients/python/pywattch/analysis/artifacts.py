"""Durable, boundary-aware persistence for offline analysis outputs.

Acquisition artifacts are immutable evidence.  This module writes derived
outputs into a separate analysis directory and never replaces an existing
file.  Each artifact is fully staged and synced before it is atomically
published with an exclusive hard link.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .._host_provenance import local_host_provenance, validate_host_provenance
from .boundary import AnalysisError, MeasurementBoundary
from .effects import (
    MechanismAttenuationResult,
    NormalApproximationSampleSize,
    PlaceboZeroCheck,
    RankingReversalResult,
    StudyAcceptanceResult,
    assess_mechanism_attenuation,
    calibrate_aa_threshold,
    check_placebo_zero,
    conservative_required_matched_blocks,
    detect_ranking_reversal,
    evaluate_study_acceptance,
    matched_difference_in_differences,
    paired_effect,
    paired_normal_sample_size,
)
from .model import (
    MODEL_EVIDENCE_CLASS,
    CounterfactualEstimate,
    CounterfactualEstimand,
)

FEATURES_FILE = "features.parquet"
PREDICTIONS_FILE = "predictions.jsonl"
CLAIMS_FILE = "claims.json"
REPORT_FILE = "report.md"

_LOCK_FILE = ".wattch-analysis.lock"
_WHOLE_MACHINE_ENERGY = re.compile(
    r"\b(?:"
    r"whole[-_\s]+(?:machine|system)"
    r"(?:[-_\s]+electrical)?[-_\s]+energy"
    r"|energy(?:[-_\s]+consumption)?[-_\s]+(?:of|for)"
    r"[-_\s]+(?:the[-_\s]+)?whole[-_\s]+(?:machine|system)"
    r"|(?:machine|system)[-_\s]+wide[-_\s]+energy"
    r")\b",
    re.IGNORECASE,
)
_POSITIVE_RANKING_LANGUAGE = re.compile(
    r"(?:"
    r"\b(?:ranking|rank)[-_\s]+reversal\b.{0,80}"
    r"\b(?:support(?:ed|s)?|confirm(?:ed|s)?|demonstrat(?:ed|es)|"
    r"establish(?:ed|es)|observ(?:ed|es)|accept(?:ed|s)?)\b"
    r"|\b(?:support(?:ed|s)?|confirm(?:ed|s)?|demonstrat(?:ed|es)|"
    r"establish(?:ed|es)|observ(?:ed|es)|accept(?:ed|s)?)\b.{0,80}"
    r"\b(?:ranking|rank)[-_\s]+reversal\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_RANKING_REVERSAL_SUBJECT = re.compile(
    r"\b(?:"
    r"(?:ranking|rank|ordering|order|sign)[-_\s]+reversal"
    r"|(?:ranking|rank|ordering|order|sign)[-_\s]+(?:is[-_\s]+)?reversed"
    r"|opposite[-_\s]+signs?"
    r")\b",
    re.IGNORECASE,
)
_POSITIVE_CLAIM_VERDICT = re.compile(
    r"\b(?:support(?:ed|s)?|confirm(?:ed|s)?|demonstrat(?:ed|es)|"
    r"establish(?:ed|es)|observ(?:ed|es)|accept(?:ed|s)?)\b",
    re.IGNORECASE,
)
_NEGATED_CLAIM_VERDICT = re.compile(
    r"\b(?:not|never)\s+(?:(?:be|been)\s+)?(?:support(?:ed)?|confirm(?:ed)?|"
    r"demonstrat(?:ed)?|establish(?:ed)?|observ(?:ed)?|accept(?:ed)?)\b",
    re.IGNORECASE,
)
_NEGATED_REVERSAL_SUBJECT = re.compile(
    r"\bno\s+(?:(?:statistically|meaningfully|confirmed)\s+)?"
    r"(?:ranking|rank|ordering|order|sign)[-_\s]+reversal\b"
    r"|\bno\s+evidence\b.{0,80}\b(?:ranking|rank|ordering|order|sign)"
    r"[-_\s]+reversal\b",
    re.IGNORECASE | re.DOTALL,
)
_POSITIVE_VERDICT_VALUES = {
    "accepted",
    "confirmed",
    "demonstrated",
    "established",
    "observed",
    "supported",
}

_VERIFIED_PREREGISTRATION_TOKEN = object()
_VERIFIED_PRESTUDY_EVIDENCE_TOKEN = object()
_VERIFIED_NO_COLLECTOR_RUNTIME_TOKEN = object()
_VERIFIED_METER_CALIBRATION_TOKEN = object()
_VERIFIED_TRIAL_BUNDLE_TOKEN = object()
_VERIFIED_CLAIM_TOKEN = object()


class AnalysisArtifactError(AnalysisError):
    """Raised when a derived artifact is invalid or cannot be published."""


@dataclass(frozen=True)
class AnalysisArtifactPaths:
    """The four derived files produced by one analysis publication."""

    features: Path
    predictions: Path
    claims: Path
    report: Path


@dataclass(frozen=True)
class PrimaryRegimePreregistration:
    """Discarded pilot and confirmatory design for one estimand/context regime."""

    regime_id: str
    estimand: str
    context: str
    pilot_block_ids: tuple[str, ...]
    pilot_discarded: bool
    sample_size: NormalApproximationSampleSize
    confirmatory_block_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.regime_id, "regime_id")
        if self.estimand not in {"FIXED_HORIZON", "EQUAL_WORK"}:
            raise AnalysisArtifactError("primary regime estimand is invalid")
        _require_text(self.context, "primary regime context")
        pilot_ids = _validate_unique_ids(
            self.pilot_block_ids,
            f"primary regime {self.regime_id!r} pilot_block_ids",
            minimum=12,
        )
        if self.pilot_discarded is not True:
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} pilot must be discarded "
                "from confirmatory analysis"
            )
        if not isinstance(self.sample_size, NormalApproximationSampleSize):
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} sample_size must be a "
                "NormalApproximationSampleSize"
            )
        if self.sample_size.pilot_blocks != len(pilot_ids):
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} sample-size pilot_blocks "
                "must match the bound pilot block ids"
            )
        pilot_sd = _require_number(
            self.sample_size.pilot_paired_sd_joules,
            f"primary regime {self.regime_id!r} sample_size.pilot_paired_sd_joules",
        )
        meaningful_effect = _require_number(
            self.sample_size.meaningful_effect_joules,
            f"primary regime {self.regime_id!r} sample_size.meaningful_effect_joules",
        )
        power = _require_number(
            self.sample_size.power,
            f"primary regime {self.regime_id!r} sample_size.power",
        )
        alpha = _require_number(
            self.sample_size.two_sided_alpha,
            f"primary regime {self.regime_id!r} sample_size.two_sided_alpha",
        )
        if pilot_sd < 0:
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} sample-size pilot SD "
                "must be non-negative"
            )
        if meaningful_effect <= 0:
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} sample-size meaningful "
                "effect must be positive"
            )
        if not 0.90 <= power < 1:
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} confirmatory target power "
                "must be at least 0.90 and below 1"
            )
        if not math.isclose(alpha, 0.05, rel_tol=0.0, abs_tol=1e-12):
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} confirmatory analysis "
                "must use exactly two-sided 95% confidence"
            )
        expected_blocks = conservative_required_matched_blocks(
            paired_sd_joules=pilot_sd,
            meaningful_effect_joules=meaningful_effect,
            power=power,
            two_sided_alpha=alpha,
        )
        if self.sample_size.required_matched_blocks != expected_blocks:
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} sample-size "
                "required_matched_blocks does not match its inputs"
            )
        confirmatory_ids = _validate_unique_ids(
            self.confirmatory_block_ids,
            f"primary regime {self.regime_id!r} confirmatory_block_ids",
            minimum=expected_blocks,
        )
        if set(pilot_ids) & set(confirmatory_ids):
            raise AnalysisArtifactError(
                f"primary regime {self.regime_id!r} pilot and confirmatory "
                "block ids must be disjoint"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "regime_id": self.regime_id,
            "estimand": self.estimand,
            "context": self.context,
            "pilot_block_ids": list(self.pilot_block_ids),
            "pilot_disposition": "DISCARDED_FROM_CONFIRMATORY",
            "sample_size": self.sample_size.to_dict(),
            "confirmatory_block_ids": list(self.confirmatory_block_ids),
        }


@dataclass(frozen=True)
class StudyPreregistration:
    """Design document that must be sealed before confirmatory acquisition."""

    preregistration_id: str
    primary_regimes: tuple[PrimaryRegimePreregistration, ...]

    def __post_init__(self) -> None:
        _require_text(self.preregistration_id, "preregistration_id")
        if not isinstance(self.primary_regimes, tuple) or not self.primary_regimes:
            raise AnalysisArtifactError(
                "primary_regimes must be a non-empty tuple"
            )
        if any(
            not isinstance(regime, PrimaryRegimePreregistration)
            for regime in self.primary_regimes
        ):
            raise AnalysisArtifactError(
                "primary_regimes must contain PrimaryRegimePreregistration values"
            )
        regime_ids = [regime.regime_id for regime in self.primary_regimes]
        if len(regime_ids) != len(set(regime_ids)):
            raise AnalysisArtifactError("primary regime ids must be unique")
        regime_keys = [
            (regime.estimand, regime.context) for regime in self.primary_regimes
        ]
        if len(regime_keys) != len(set(regime_keys)):
            raise AnalysisArtifactError(
                "each estimand and context may identify only one primary regime"
            )
        pilot_ids = [
            block_id
            for regime in self.primary_regimes
            for block_id in regime.pilot_block_ids
        ]
        if len(pilot_ids) != len(set(pilot_ids)):
            raise AnalysisArtifactError(
                "pilot block ids must be unique across primary regimes"
            )
        confirmatory_ids = [
            block_id
            for regime in self.primary_regimes
            for block_id in regime.confirmatory_block_ids
        ]
        if len(confirmatory_ids) != len(set(confirmatory_ids)):
            raise AnalysisArtifactError(
                "confirmatory block ids must be unique across primary regimes"
            )
        if set(pilot_ids) & set(confirmatory_ids):
            raise AnalysisArtifactError(
                "pilot and confirmatory block ids must be disjoint across the study"
            )
    def to_dict(self) -> dict[str, object]:
        return {
            "preregistration_id": self.preregistration_id,
            "primary_regimes": [
                regime.to_dict() for regime in self.primary_regimes
            ],
        }


@dataclass(frozen=True)
class SealedArtifactReference:
    """Content address of one preregistered, externally stored artifact."""

    role: str
    sha256: str
    bytes: int
    regime_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.role, "artifact role")
        _require_sha256(self.sha256, "artifact sha256")
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            raise AnalysisArtifactError("artifact bytes must be a non-negative integer")
        if self.regime_id is not None:
            _require_text(self.regime_id, "artifact regime_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "regime_id": self.regime_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


@dataclass(frozen=True, init=False)
class VerifiedPrestudyEvidenceArtifact:
    """Pilot/A-A/placebo evidence recomputed from replay-verified trials."""

    path: Path
    role: str
    regime_id: str
    sha256: str
    bytes: int
    block_ids: tuple[str, ...]
    paired_differences_joules: tuple[float, ...]
    control_joules: tuple[float, ...]
    document: Mapping[str, Any]
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError(
            "use write_verified_prestudy_evidence or "
            "load_verified_prestudy_evidence"
        )

    @classmethod
    def _issue(
        cls,
        *,
        path: Path,
        document: Mapping[str, Any],
    ) -> "VerifiedPrestudyEvidenceArtifact":
        block_ids = document.get("block_ids")
        differences = document.get("paired_differences_joules")
        controls = document.get("control_joules")
        if not all(isinstance(item, list) for item in (block_ids, differences, controls)):
            raise AnalysisArtifactError("verified prestudy evidence is malformed")
        instance = object.__new__(cls)
        for name, value in (
            ("path", path),
            ("role", _require_text(document.get("kind"), "prestudy role")),
            ("regime_id", _require_text(document.get("regime_id"), "regime_id")),
            ("sha256", _file_sha256(path)),
            ("bytes", path.stat().st_size),
            ("block_ids", tuple(str(item) for item in block_ids)),
            (
                "paired_differences_joules",
                tuple(float(item) for item in differences),
            ),
            ("control_joules", tuple(float(item) for item in controls)),
            ("document", dict(document)),
            ("_verification_token", _VERIFIED_PRESTUDY_EVIDENCE_TOKEN),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def _require_verified(self) -> None:
        if self._verification_token is not _VERIFIED_PRESTUDY_EVIDENCE_TOKEN:
            raise AnalysisArtifactError("prestudy evidence was not factory-issued")
        if _file_sha256(self.path) != self.sha256:
            raise AnalysisArtifactError("prestudy evidence changed after verification")


@dataclass(frozen=True)
class NoCollectorRuntimeSpec:
    """One same-work runtime measured without a Wattch daemon collector."""

    regime: str
    pair_id: str
    interval_ns: int
    command: object
    machine_id: str
    replay_seed: int
    matched_block_id: str
    cache_regime: str
    prelaunch_temperature_source_uid: str
    prelaunch_temperature_celsius: float
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        normalized = _require_text(self.regime, "no-collector regime").upper().replace(
            "-", "_"
        )
        if normalized not in {"IDLE", "FIXED_LOAD", "AA"}:
            raise AnalysisArtifactError("no-collector regime is unsupported")
        _require_text(self.pair_id, "no-collector pair_id")
        if (
            isinstance(self.interval_ns, bool)
            or not isinstance(self.interval_ns, int)
            or self.interval_ns <= 0
        ):
            raise AnalysisArtifactError("no-collector interval_ns must be positive")
        if not callable(getattr(self.command, "to_dict", None)):
            raise AnalysisArtifactError("no-collector command must provide to_dict()")
        _require_text(self.machine_id, "no-collector machine_id")
        if isinstance(self.replay_seed, bool) or not isinstance(self.replay_seed, int):
            raise AnalysisArtifactError("no-collector replay_seed must be an integer")
        _require_text(self.matched_block_id, "no-collector matched_block_id")
        _require_text(self.cache_regime, "no-collector cache_regime")
        _require_text(
            self.prelaunch_temperature_source_uid,
            "no-collector prelaunch_temperature_source_uid",
        )
        temperature = _require_number(
            self.prelaunch_temperature_celsius,
            "no-collector prelaunch_temperature_celsius",
        )
        if not -100 <= temperature <= 250:
            raise AnalysisArtifactError(
                "no-collector prelaunch temperature is outside the supported range"
            )
        timeout = _require_number(self.timeout_s, "no-collector timeout_s")
        if timeout <= 0:
            raise AnalysisArtifactError("no-collector timeout_s must be positive")


@dataclass(frozen=True, init=False)
class VerifiedNoCollectorRuntimeArtifact:
    """Factory-measured runtimes from direct commands with no daemon session."""

    path: Path
    sha256: str
    bytes: int
    document: Mapping[str, Any]
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError("use write_verified_no_collector_runtime")

    @classmethod
    def _issue(
        cls, *, path: Path, document: Mapping[str, Any]
    ) -> "VerifiedNoCollectorRuntimeArtifact":
        instance = object.__new__(cls)
        # Detach the issued object from the builder's nested containers.  The
        # object remains intentionally inspectable, so every trust-sensitive
        # use also reopens and compares the on-disk canonical document.
        detached_document = json.loads(_json_bytes(document))
        for name, value in (
            ("path", path),
            ("sha256", _file_sha256(path)),
            ("bytes", path.stat().st_size),
            ("document", detached_document),
            ("_verification_token", _VERIFIED_NO_COLLECTOR_RUNTIME_TOKEN),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def _verified_document(self) -> Mapping[str, Any]:
        if self._verification_token is not _VERIFIED_NO_COLLECTOR_RUNTIME_TOKEN:
            raise AnalysisArtifactError("no-collector runtime was not factory-issued")
        if _file_sha256(self.path) != self.sha256:
            raise AnalysisArtifactError("no-collector runtime changed after verification")
        reopened = _read_json_artifact(self.path)
        if reopened != self.document:
            raise AnalysisArtifactError(
                "no-collector runtime object differs from its verified file"
            )
        return reopened

    def _require_verified(self) -> None:
        self._verified_document()


def write_verified_no_collector_runtime(
    path: str | Path,
    *,
    specs: Iterable[NoCollectorRuntimeSpec],
) -> VerifiedNoCollectorRuntimeArtifact:
    """Execute same-work commands directly, outside any Wattch daemon session."""

    records: list[dict[str, object]] = []
    seen: set[tuple[int, str, str]] = set()
    host_provenance = local_host_provenance()
    if not hasattr(os, "getpriority") or not hasattr(os, "PRIO_PROCESS"):
        raise AnalysisArtifactError(
            "verified no-collector calibration requires absolute nice readback"
        )
    for spec in specs:
        if not isinstance(spec, NoCollectorRuntimeSpec):
            raise AnalysisArtifactError(
                "no-collector runtime specs must be NoCollectorRuntimeSpec values"
            )
        key = (
            spec.interval_ns,
            spec.regime.upper().replace("-", "_"),
            spec.pair_id,
        )
        if key in seen:
            raise AnalysisArtifactError("no-collector runtime cells must be unique")
        seen.add(key)
        command_document = spec.command.to_dict()
        if not isinstance(command_document, Mapping):
            raise AnalysisArtifactError("no-collector command document is malformed")
        effective_command = dict(command_document)
        environment = command_document.get("env")
        if not isinstance(environment, Mapping):
            raise AnalysisArtifactError("no-collector command environment is malformed")
        effective_environment = {str(k): str(v) for k, v in environment.items()}
        effective_environment.update(
            {
                "WATTCH_MATCHED_BLOCK_ID": spec.matched_block_id,
                "WATTCH_REPLAY_SEED": str(spec.replay_seed),
            }
        )
        effective_command["env"] = dict(sorted(effective_environment.items()))
        argv = effective_command.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise AnalysisArtifactError("no-collector command is malformed")
        actual_parent_nice = os.getpriority(os.PRIO_PROCESS, 0)
        if actual_parent_nice != 0:
            raise AnalysisArtifactError(
                "verified no-collector calibration currently requires actual nice 0"
            )
        started_unix_ns = time.time_ns()
        started_ns = time.monotonic_ns()
        try:
            completed = subprocess.run(
                argv,
                cwd=effective_command.get("cwd"),
                env={**os.environ, **effective_environment},
                capture_output=True,
                check=False,
                timeout=float(spec.timeout_s),
            )
        except subprocess.TimeoutExpired as exc:
            raise AnalysisArtifactError(
                f"no-collector calibration command exceeded {spec.timeout_s:g}s"
            ) from exc
        ended_ns = time.monotonic_ns()
        ended_unix_ns = time.time_ns()
        if completed.returncode != 0 or ended_ns <= started_ns:
            raise AnalysisArtifactError(
                "no-collector calibration command failed or had invalid timing"
            )
        records.append(
            {
                "interval_ns": spec.interval_ns,
                "regime": key[1],
                "pair_id": spec.pair_id,
                "machine_id": spec.machine_id,
                "host_identity_sha256": host_provenance["identity_sha256"],
                "replay_seed": spec.replay_seed,
                "matched_block_id": spec.matched_block_id,
                "cache_regime": spec.cache_regime,
                "prelaunch_temperature_source_uid": (
                    spec.prelaunch_temperature_source_uid
                ),
                "prelaunch_temperature_celsius": float(
                    spec.prelaunch_temperature_celsius
                ),
                "prelaunch_temperature_bin_celsius": _temperature_bin_celsius(
                    float(spec.prelaunch_temperature_celsius)
                ),
                "command": effective_command,
                "command_sha256": _canonical_sha256(effective_command),
                "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
                "returncode": completed.returncode,
                "requested_target_nice": 0,
                "actual_parent_nice": actual_parent_nice,
                "actual_target_nice": actual_parent_nice,
                "nice_verification": "INHERITED_PARENT_ABSOLUTE_NICE_READBACK",
                "timeout_s": float(spec.timeout_s),
                "started_unix_ns": started_unix_ns,
                "ended_unix_ns": ended_unix_ns,
                "utc_day": datetime.fromtimestamp(
                    started_unix_ns / 1_000_000_000, tz=UTC
                ).date().isoformat(),
                "duration_ns": ended_ns - started_ns,
            }
        )
    if not records:
        raise AnalysisArtifactError("no-collector runtime artifact cannot be empty")
    document = {
        "schema_version": 2,
        "kind": "NO_COLLECTOR_RUNTIME",
        "measurement_mode": "DIRECT_PROCESS_WITHOUT_WATTCH_DAEMON_SESSION",
        "host_provenance": host_provenance,
        "launcher_policy": {
            "target_nice": 0,
            "cpu_affinity": [],
            "resource_limits": {},
            "application": "INHERIT_PARENT_AND_READ_BACK_ABSOLUTE_NICE",
        },
        "records": sorted(
            records,
            key=lambda item: (
                int(item["interval_ns"]),
                str(item["regime"]),
                str(item["pair_id"]),
            ),
        ),
    }
    target = _write_one(path, _json_bytes(document))
    return VerifiedNoCollectorRuntimeArtifact._issue(path=target, document=document)


@dataclass(frozen=True, init=False)
class VerifiedMeterCalibrationArtifact:
    """Sampling calibration recomputed from replay-verified raw trials."""

    path: Path
    sha256: str
    bytes: int
    document: Mapping[str, Any]
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError(
            "use write_verified_meter_calibration or "
            "load_verified_meter_calibration"
        )

    @classmethod
    def _issue(
        cls, *, path: Path, document: Mapping[str, Any]
    ) -> "VerifiedMeterCalibrationArtifact":
        instance = object.__new__(cls)
        for name, value in (
            ("path", path),
            ("sha256", _file_sha256(path)),
            ("bytes", path.stat().st_size),
            ("document", dict(document)),
            ("_verification_token", _VERIFIED_METER_CALIBRATION_TOKEN),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def _require_verified(self) -> None:
        if self._verification_token is not _VERIFIED_METER_CALIBRATION_TOKEN:
            raise AnalysisArtifactError("meter calibration was not factory-issued")
        if _file_sha256(self.path) != self.sha256:
            raise AnalysisArtifactError("meter calibration changed after verification")


def _target_command_identity(value: Mapping[str, Any]) -> dict[str, object]:
    """Remove only Wattch's per-trial evidence path from command identity."""

    command = dict(value)
    environment = command.get("env")
    if not isinstance(environment, Mapping):
        raise AnalysisArtifactError("target command environment must be an object")
    normalized_environment = dict(environment)
    for runtime_name in (
        "WATTCH_METRICS_PATH",
        "WATTCH_METRICS_SCHEMA_VERSION",
        "WATTCH_MATCHED_BLOCK_ID",
        "WATTCH_REPLAY_SEED",
    ):
        normalized_environment.pop(runtime_name, None)
    command["env"] = dict(sorted(normalized_environment.items()))
    return command


def _calibration_command_identity(value: Mapping[str, Any]) -> dict[str, object]:
    """Normalize storage paths while retaining scientific execution inputs."""

    command = dict(value)
    environment = command.get("env")
    if not isinstance(environment, Mapping):
        raise AnalysisArtifactError("calibration command environment must be an object")
    normalized_environment = dict(environment)
    for runtime_name in (
        "WATTCH_METRICS_PATH",
        "WATTCH_METRICS_SCHEMA_VERSION",
        "WATTCH_MATCHED_BLOCK_ID",
    ):
        normalized_environment.pop(runtime_name, None)
    command["env"] = dict(sorted(normalized_environment.items()))
    return command


def _verified_meter_calibration_document(
    *,
    calibration_id: str,
    boundary: MeasurementBoundary,
    trial_directories: Iterable[str | Path],
    no_collector_runtime: VerifiedNoCollectorRuntimeArtifact,
    max_overhead_ratio: float,
    max_missing_sample_ratio: float,
    max_phase_sensitivity_ratio: float,
    max_pairing_gap_s: float,
) -> dict[str, object]:
    from .calibration import (
        SamplingIntervalObservation,
        SamplingSelectionStatus,
        calculate_measurement_overhead,
        select_fastest_sampling_interval,
    )
    from .replay import load_trial_evidence

    _require_text(calibration_id, "calibration_id")
    if not isinstance(boundary, MeasurementBoundary):
        raise AnalysisArtifactError("verified calibration needs a MeasurementBoundary")
    if not isinstance(no_collector_runtime, VerifiedNoCollectorRuntimeArtifact):
        raise AnalysisArtifactError(
            "verified calibration needs factory-measured no-collector runtimes"
        )
    if max_pairing_gap_s <= 0 or not math.isfinite(max_pairing_gap_s):
        raise AnalysisArtifactError("max_pairing_gap_s must be finite and positive")
    baseline_document = no_collector_runtime._verified_document()
    baseline_host = baseline_document.get("host_provenance")
    if not isinstance(baseline_host, Mapping):
        raise AnalysisArtifactError("no-collector host provenance is malformed")
    current_host = local_host_provenance()
    if dict(baseline_host) != current_host:
        raise AnalysisArtifactError("no-collector runtime was captured on another host")
    baseline_records = baseline_document.get("records")
    if not isinstance(baseline_records, list):
        raise AnalysisArtifactError("no-collector runtime records are malformed")
    baselines_by_cell = {
        (
            int(item["interval_ns"]),
            str(item["regime"]),
            str(item["pair_id"]),
        ): item
        for item in baseline_records
        if isinstance(item, Mapping)
    }
    if len(baselines_by_cell) != len(baseline_records):
        raise AnalysisArtifactError("no-collector runtime cells are invalid")
    roots = tuple(Path(path) for path in trial_directories)
    if not roots:
        raise AnalysisArtifactError("verified calibration needs raw trial directories")
    evidence = tuple(load_trial_evidence(root) for root in roots)
    records: list[dict[str, object]] = []
    grouped: dict[tuple[int, str, str], dict[str, object]] = {}
    topology: tuple[Mapping[str, Any], ...] | None = None
    collector_identity: dict[str, object] | None = None
    machine_id: str | None = None
    regime_contract_digests: dict[str, set[str]] = {}
    for root, item in zip(roots, evidence):
        if any(
            gate is not True
            for gate in (
                item.observation.work_complete,
                item.observation.output_valid,
                item.observation.slo_met,
                item.observation.quality_accepted,
            )
        ):
            raise AnalysisArtifactError("calibration trial failed an evidence gate")
        boundary.assert_compatible(item.outcome.boundary)
        current_topology = tuple(
            descriptor.to_dict() for descriptor in item.descriptors
        )
        if topology is None:
            topology = current_topology
        elif topology != current_topology:
            raise AnalysisArtifactError("calibration trials changed source topology")
        trial = item.trial
        manifest = item.manifest
        labels = trial.get("labels")
        result = item.acquisition.get("result")
        if not isinstance(labels, Mapping) or not isinstance(result, Mapping):
            raise AnalysisArtifactError("calibration trial metadata is malformed")
        try:
            trial_host = validate_host_provenance(result.get("host_provenance"))
        except ValueError as exc:
            raise AnalysisArtifactError(
                "calibration trial lacks canonical host provenance"
            ) from exc
        if trial_host != baseline_host:
            raise AnalysisArtifactError(
                "instrumented and no-collector host provenance differs"
            )
        regime = _require_text(
            labels.get("meter_calibration_regime"), "meter calibration regime"
        ).upper().replace("-", "_")
        if regime not in {"IDLE", "FIXED_LOAD", "AA"}:
            raise AnalysisArtifactError("meter calibration regime is unsupported")
        mode = _require_text(
            labels.get("meter_calibration_mode"), "meter calibration mode"
        )
        if mode != "INSTRUMENTED":
            raise AnalysisArtifactError(
                "raw calibration trials must be collector-enabled; baseline "
                "runtime comes from the no-collector factory"
            )
        pair_id = _require_text(
            labels.get("meter_calibration_pair_id"), "meter calibration pair id"
        )
        try:
            interval_ns = int(_require_text(labels.get("interval_ms"), "interval_ms")) * 1_000_000
        except ValueError as exc:
            raise AnalysisArtifactError("calibration interval_ms is invalid") from exc
        execution = result.get("execution_contract")
        if not isinstance(execution, Mapping):
            raise AnalysisArtifactError("calibration execution contract is missing")
        execution_digest = _canonical_sha256(execution)
        if result.get("execution_contract_sha256") != execution_digest:
            raise AnalysisArtifactError("calibration execution digest is invalid")
        if (
            execution.get("target_nice") != 0
            or execution.get("cpu_affinity") != []
            or execution.get("resource_limits") != {}
        ):
            raise AnalysisArtifactError(
                "verified overhead calibration currently requires default nice, "
                "affinity, and resource-limit policy in both launch paths"
            )
        target = execution.get("target")
        if not isinstance(target, Mapping):
            raise AnalysisArtifactError("calibration trial must execute a target")
        target_digest = _canonical_sha256(_calibration_command_identity(target))
        regime_contract_digests.setdefault(regime, set()).add(target_digest)
        replay_seed = trial.get("replay_seed")
        if isinstance(replay_seed, bool) or not isinstance(replay_seed, int):
            raise AnalysisArtifactError("calibration replay seed is invalid")
        current_machine_id = _require_text(
            manifest.get("node_id"), "calibration manifest node_id"
        )
        if machine_id is None:
            machine_id = current_machine_id
        elif machine_id != current_machine_id:
            raise AnalysisArtifactError("calibration trials changed machine identity")
        current_collector_identity = {
            "node_id": current_machine_id,
            "protocol_version": _require_text(
                manifest.get("protocol_version"), "calibration protocol_version"
            ),
            "wattch_version": _require_text(
                manifest.get("wattch_version"), "calibration wattch_version"
            ),
            "host_identity_sha256": trial_host["identity_sha256"],
        }
        if collector_identity is None:
            collector_identity = current_collector_identity
        elif collector_identity != current_collector_identity:
            raise AnalysisArtifactError("calibration trials changed collector identity")
        cache_regime = _require_text(
            labels.get("cache_regime"), "calibration cache_regime"
        )
        temperature_source_uid = _require_text(
            labels.get("prelaunch_temperature_source_uid"),
            "calibration prelaunch temperature source",
        )
        temperature_celsius, temperature_bin = _prelaunch_temperature(
            item,
            source_uid=temperature_source_uid,
            trial_id=item.observation.trial_id,
        )
        created_unix_ns = manifest.get("created_unix_ns")
        if (
            isinstance(created_unix_ns, bool)
            or not isinstance(created_unix_ns, int)
            or created_unix_ns < 0
        ):
            raise AnalysisArtifactError("calibration creation timestamp is invalid")
        target_exec = next(
            (event for event in item.events if event.kind == "TARGET_EXEC"), None
        )
        if target_exec is None:
            raise AnalysisArtifactError("calibration trial lacks TARGET_EXEC")
        primary_phase: dict[str, dict[str, float | int]] = {}
        descriptor_by_uid = {
            descriptor.source_uid: descriptor for descriptor in item.descriptors
        }
        for source_uid in boundary.primary_source_uids:
            descriptor = descriptor_by_uid.get(source_uid)
            if descriptor is None:
                raise AnalysisArtifactError("calibration topology omits a primary source")
            timestamps = sorted(
                sample.monotonic_ns
                for sample in item.samples
                if sample.source_id == descriptor.local_source_id
            )
            preceding = [
                value for value in timestamps if value <= target_exec.monotonic_ns
            ]
            following = [
                value for value in timestamps if value > target_exec.monotonic_ns
            ]
            if not preceding or not following:
                raise AnalysisArtifactError(
                    "calibration launch is not bracketed by primary-source samples"
                )
            before_ns = preceding[-1]
            after_ns = following[0]
            cadence_ns = after_ns - before_ns
            offset_ns = target_exec.monotonic_ns - before_ns
            if cadence_ns <= 0 or not 0 <= offset_ns < cadence_ns:
                raise AnalysisArtifactError("calibration primary-source phase is invalid")
            primary_phase[source_uid] = {
                "preceding_sample_ns": before_ns,
                "following_sample_ns": after_ns,
                "observed_cadence_ns": cadence_ns,
                "launch_offset_ns": offset_ns,
                "phase_fraction": offset_ns / cadence_ns,
            }
        primary_missingness: list[float] = []
        quality_sources = item.quality.get("sources")
        if not isinstance(quality_sources, list):
            raise AnalysisArtifactError("calibration quality sources are malformed")
        by_uid = {
            source.get("source_uid"): source
            for source in quality_sources
            if isinstance(source, Mapping)
        }
        for uid in boundary.primary_source_uids:
            summary = by_uid.get(uid)
            if not isinstance(summary, Mapping):
                raise AnalysisArtifactError("calibration quality omits a primary source")
            primary_missingness.append(
                _require_number(
                    summary.get("missing_sample_ratio"),
                    "calibration source missing_sample_ratio",
                )
            )
        record = {
            "trial_id": item.observation.trial_id,
            "checksums_sha256": _file_sha256(root / "checksums.json"),
            "workload_metrics_sha256": _require_sha256(
                item.workload_metrics_sha256,
                "calibration workload metrics digest",
            ),
            "interval_ns": interval_ns,
            "regime": regime,
            "mode": mode,
            "pair_id": pair_id,
            "machine_id": current_machine_id,
            "replay_seed": replay_seed,
            "cache_regime": cache_regime,
            "prelaunch_temperature_source_uid": temperature_source_uid,
            "prelaunch_temperature_celsius": temperature_celsius,
            "prelaunch_temperature_bin_celsius": temperature_bin,
            "created_unix_ns": created_unix_ns,
            "target_command_sha256": target_digest,
            "execution_contract_sha256": execution_digest,
            "actual_target_nice": result.get("actual_target_nice"),
            "duration_s": item.outcome.duration_s,
            "target_runtime_s": _require_number(
                result.get("target_runtime_s"), "calibration target runtime"
            ),
            "target_stdout_sha256": _require_sha256(
                result.get("target_stdout_sha256"),
                "calibration target stdout digest",
            ),
            "total_joules": item.outcome.total_joules,
            "primary_sample_phase": primary_phase,
            "missing_sample_ratio": max(primary_missingness),
        }
        records.append(record)
        cell = grouped.setdefault(
            (interval_ns, regime, pair_id),
            {"instrumented": []},
        )
        values = cell["instrumented"]
        if not isinstance(values, list):  # pragma: no cover - locally constructed
            raise AnalysisArtifactError("calibration grouping failed")
        values.append(record)
    if set(grouped) != set(baselines_by_cell):
        raise AnalysisArtifactError(
            "instrumented and no-collector calibration cells differ"
        )
    if set(regime_contract_digests) != {"IDLE", "FIXED_LOAD", "AA"} or any(
        len(values) != 1 for values in regime_contract_digests.values()
    ):
        raise AnalysisArtifactError(
            "each calibration regime must have one stable executable contract"
        )
    stable_regime_contracts = {
        regime: next(iter(values))
        for regime, values in sorted(regime_contract_digests.items())
    }
    if len(set(stable_regime_contracts.values())) != len(stable_regime_contracts):
        raise AnalysisArtifactError(
            "IDLE, FIXED_LOAD, and AA need distinct executable/background contracts"
        )
    observations: list[SamplingIntervalObservation] = []
    by_interval_regime: dict[tuple[int, str], list[dict[str, object]]] = {}
    for (interval_ns, regime, _), cell in grouped.items():
        by_interval_regime.setdefault((interval_ns, regime), []).append(cell)
    for (interval_ns, regime), cells in sorted(by_interval_regime.items()):
        baseline_durations: list[float] = []
        instrumented_durations: list[float] = []
        missingness: list[float] = []
        phase_sensitivity: list[float] = []
        for cell in cells:
            instrumented = cell["instrumented"]
            if not isinstance(instrumented, list):
                raise AnalysisArtifactError("calibration grouping is malformed")
            if len(instrumented) < 2:
                raise AnalysisArtifactError(
                    "each calibration pair needs at least two instrumented phase trials"
                )
            pair_id = str(instrumented[0]["pair_id"])
            baseline = baselines_by_cell.get((interval_ns, regime, pair_id))
            if not isinstance(baseline, Mapping):
                raise AnalysisArtifactError(
                    "instrumented calibration pair lacks a no-collector runtime"
                )
            baseline_command = baseline.get("command")
            if not isinstance(baseline_command, Mapping):
                raise AnalysisArtifactError("no-collector command is malformed")
            baseline_target_digest = _canonical_sha256(
                _calibration_command_identity(baseline_command)
            )
            target_digests = {
                str(record["target_command_sha256"])
                for record in instrumented
            }
            if target_digests != {baseline_target_digest}:
                raise AnalysisArtifactError("calibration pair changed its target command")
            if {
                str(record["target_stdout_sha256"]) for record in instrumented
            } != {str(baseline.get("stdout_sha256"))}:
                raise AnalysisArtifactError(
                    "instrumented and no-collector calibration outputs differ"
                )
            if baseline.get("machine_id") != machine_id:
                raise AnalysisArtifactError(
                    "instrumented and no-collector calibration machine IDs differ"
                )
            if baseline.get("host_identity_sha256") != current_host["identity_sha256"]:
                raise AnalysisArtifactError(
                    "instrumented and no-collector host provenance differs"
                )
            if {int(record["replay_seed"]) for record in instrumented} != {
                baseline.get("replay_seed")
            }:
                raise AnalysisArtifactError(
                    "instrumented and no-collector replay seeds differ"
                )
            if {str(record["cache_regime"]) for record in instrumented} != {
                str(baseline.get("cache_regime"))
            }:
                raise AnalysisArtifactError(
                    "instrumented and no-collector cache regimes differ"
                )
            if {
                str(record["prelaunch_temperature_source_uid"])
                for record in instrumented
            } != {str(baseline.get("prelaunch_temperature_source_uid"))} or {
                str(record["prelaunch_temperature_bin_celsius"])
                for record in instrumented
            } != {str(baseline.get("prelaunch_temperature_bin_celsius"))}:
                raise AnalysisArtifactError(
                    "instrumented and no-collector temperature blocks differ"
                )
            baseline_started = baseline.get("started_unix_ns")
            baseline_ended = baseline.get("ended_unix_ns")
            if (
                isinstance(baseline_started, bool)
                or not isinstance(baseline_started, int)
                or isinstance(baseline_ended, bool)
                or not isinstance(baseline_ended, int)
                or baseline_ended < baseline_started
            ):
                raise AnalysisArtifactError("no-collector wall-clock evidence is invalid")
            baseline_midpoint = (baseline_started + baseline_ended) // 2
            paired_created = [
                int(record["created_unix_ns"]) for record in instrumented
            ]
            if any(
                abs(value - baseline_midpoint) > max_pairing_gap_s * 1_000_000_000
                for value in paired_created
            ):
                raise AnalysisArtifactError(
                    "instrumented and no-collector runs exceed the pairing window"
                )
            baseline_day = str(baseline.get("utc_day"))
            if {
                datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)
                .date()
                .isoformat()
                for value in paired_created
            } != {baseline_day}:
                raise AnalysisArtifactError(
                    "instrumented and no-collector runs cross a UTC day block"
                )
            if baseline.get("actual_target_nice") != 0 or {
                int(record.get("actual_target_nice", -1)) for record in instrumented
            } != {0}:
                raise AnalysisArtifactError(
                    "instrumented and no-collector actual nice evidence differs"
                )
            phases = {
                _canonical_sha256(record["primary_sample_phase"])
                for record in instrumented
            }
            if len(phases) < 2:
                raise AnalysisArtifactError(
                    "calibration pair did not sample distinct launch phases"
                )
            baseline_duration_ns = baseline.get("duration_ns")
            if (
                isinstance(baseline_duration_ns, bool)
                or not isinstance(baseline_duration_ns, int)
                or baseline_duration_ns <= 0
            ):
                raise AnalysisArtifactError("no-collector runtime duration is invalid")
            phase_runtimes = [
                float(record["target_runtime_s"]) for record in instrumented
            ]
            if any(value <= 0 for value in phase_runtimes):
                raise AnalysisArtifactError(
                    "instrumented target runtime must be positive"
                )
            baseline_durations.extend(
                [baseline_duration_ns / 1e9] * len(phase_runtimes)
            )
            instrumented_durations.extend(phase_runtimes)
            missingness.extend(
                float(record["missing_sample_ratio"]) for record in instrumented
            )
            energies = [float(record["total_joules"]) for record in instrumented]
            mean_energy = sum(energies) / len(energies)
            if mean_energy <= 0:
                raise AnalysisArtifactError("phase calibration energy must be positive")
            phase_sensitivity.append((max(energies) - min(energies)) / mean_energy)
        overhead = calculate_measurement_overhead(
            baseline_durations_s=baseline_durations,
            instrumented_durations_s=instrumented_durations,
        )
        observations.append(
            SamplingIntervalObservation(
                interval_ns=interval_ns,
                regime=regime,
                measurement_overhead_ratio=max(0.0, overhead.overhead_ratio),
                missing_sample_ratio=max(missingness),
                phase_sensitivity_ratio=max(phase_sensitivity),
            )
        )
    selection = select_fastest_sampling_interval(
        observations,
        max_overhead_ratio=max_overhead_ratio,
        max_missing_sample_ratio=max_missing_sample_ratio,
        max_phase_sensitivity_ratio=max_phase_sensitivity_ratio,
        required_regimes=("IDLE", "FIXED_LOAD", "AA"),
    )
    if (
        selection.status is not SamplingSelectionStatus.SELECTED
        or selection.selected_interval_ns is None
    ):
        raise AnalysisArtifactError(
            "raw calibration trials select no eligible interval: "
            + json.dumps(selection.to_dict(), sort_keys=True)
        )
    selected = next(
        item
        for item in selection.assessments
        if item.interval_ns == selection.selected_interval_ns
    )
    boundary_document = boundary.to_dict()
    if topology is None or collector_identity is None or machine_id is None:
        raise AnalysisArtifactError("calibration identity evidence is incomplete")
    topology_document = list(topology)
    return {
        "schema_version": 3,
        "kind": "METER_CALIBRATION",
        "calibration_id": calibration_id,
        "derivation": "REPLAY_VERIFIED_CALIBRATION_TRIALS",
        "no_collector_runtime_sha256": no_collector_runtime.sha256,
        "measurement_boundary": boundary_document,
        "measurement_boundary_sha256": _canonical_sha256(boundary_document),
        "source_topology": topology_document,
        "source_topology_sha256": _canonical_sha256(topology_document),
        "collector_identity": collector_identity,
        "collector_identity_sha256": _canonical_sha256(collector_identity),
        "regime_contract_sha256": stable_regime_contracts,
        "selected_interval_ns": selection.selected_interval_ns,
        "required_regimes": ["IDLE", "FIXED_LOAD", "AA"],
        "selected_metrics": {
            "measurement_overhead_ratio": selected.worst_measurement_overhead_ratio,
            "missing_sample_ratio": selected.worst_missing_sample_ratio,
            "phase_sensitivity_ratio": selected.worst_phase_sensitivity_ratio,
        },
        "thresholds": {
            "max_measurement_overhead_ratio": max_overhead_ratio,
            "max_missing_sample_ratio": max_missing_sample_ratio,
            "max_phase_sensitivity_ratio": max_phase_sensitivity_ratio,
            "max_pairing_gap_s": max_pairing_gap_s,
        },
        "sampling_observations": [
            {
                "interval_ns": item.interval_ns,
                "regime": item.regime,
                "measurement_overhead_ratio": item.measurement_overhead_ratio,
                "missing_sample_ratio": item.missing_sample_ratio,
                "phase_sensitivity_ratio": item.phase_sensitivity_ratio,
            }
            for item in observations
        ],
        "trial_evidence": sorted(records, key=lambda item: str(item["trial_id"])),
    }


def write_verified_meter_calibration(
    path: str | Path,
    *,
    calibration_id: str,
    boundary: MeasurementBoundary,
    trial_directories: Iterable[str | Path],
    no_collector_runtime: VerifiedNoCollectorRuntimeArtifact,
    max_overhead_ratio: float = 0.01,
    max_missing_sample_ratio: float = 0.001,
    max_phase_sensitivity_ratio: float = 0.01,
    max_pairing_gap_s: float = 900.0,
) -> VerifiedMeterCalibrationArtifact:
    document = _verified_meter_calibration_document(
        calibration_id=calibration_id,
        boundary=boundary,
        trial_directories=trial_directories,
        no_collector_runtime=no_collector_runtime,
        max_overhead_ratio=max_overhead_ratio,
        max_missing_sample_ratio=max_missing_sample_ratio,
        max_phase_sensitivity_ratio=max_phase_sensitivity_ratio,
        max_pairing_gap_s=max_pairing_gap_s,
    )
    target = _write_one(path, _json_bytes(document))
    return VerifiedMeterCalibrationArtifact._issue(path=target, document=document)


def load_verified_meter_calibration(
    path: str | Path,
    *,
    trial_directories: Iterable[str | Path],
    no_collector_runtime: VerifiedNoCollectorRuntimeArtifact,
) -> VerifiedMeterCalibrationArtifact:
    target = Path(path)
    document = _read_json_artifact(target)
    boundary_document = document.get("measurement_boundary")
    thresholds = document.get("thresholds")
    if not isinstance(boundary_document, Mapping) or not isinstance(
        thresholds, Mapping
    ):
        raise AnalysisArtifactError("verified meter calibration is malformed")
    recomputed = _verified_meter_calibration_document(
        calibration_id=_require_text(document.get("calibration_id"), "calibration_id"),
        boundary=MeasurementBoundary.from_dict(boundary_document),
        trial_directories=trial_directories,
        no_collector_runtime=no_collector_runtime,
        max_overhead_ratio=_require_number(
            thresholds.get("max_measurement_overhead_ratio"),
            "max_measurement_overhead_ratio",
        ),
        max_missing_sample_ratio=_require_number(
            thresholds.get("max_missing_sample_ratio"),
            "max_missing_sample_ratio",
        ),
        max_phase_sensitivity_ratio=_require_number(
            thresholds.get("max_phase_sensitivity_ratio"),
            "max_phase_sensitivity_ratio",
        ),
        max_pairing_gap_s=_require_number(
            thresholds.get("max_pairing_gap_s"),
            "max_pairing_gap_s",
        ),
    )
    if dict(document) != recomputed:
        raise AnalysisArtifactError(
            "meter calibration artifact differs from replay-derived raw trials"
        )
    return VerifiedMeterCalibrationArtifact._issue(path=target, document=document)


def _verified_prestudy_document(
    *,
    role: str,
    regime_id: str,
    trial_directories: Iterable[str | Path],
    treatment_arm: str,
    control_arm: str,
    relative_floor_fraction: float,
    uncertainty_multiplier: float,
    uncertainty_quantile: float,
    confidence: float,
) -> dict[str, object]:
    from .replay import load_trial_evidence

    if role not in {"DISCARDED_PILOT", "AA_CALIBRATION", "PLACEBO"}:
        raise AnalysisArtifactError("prestudy evidence role is unsupported")
    if not math.isclose(confidence, 0.95, rel_tol=0.0, abs_tol=1e-12):
        raise AnalysisArtifactError(
            "verified prestudy evidence requires two-sided 95% confidence"
        )
    if role == "AA_CALIBRATION" and not (
        math.isclose(relative_floor_fraction, 0.05, rel_tol=0.0, abs_tol=1e-12)
        and math.isclose(
            uncertainty_multiplier, 2.0, rel_tol=0.0, abs_tol=1e-12
        )
        and math.isclose(
            uncertainty_quantile, 0.95, rel_tol=0.0, abs_tol=1e-12
        )
    ):
        raise AnalysisArtifactError(
            "verified A/A evidence requires exactly max(5% control energy, "
            "2x the 95% absolute-difference uncertainty)"
        )
    _require_text(regime_id, "regime_id")
    roots = tuple(Path(path) for path in trial_directories)
    if not roots:
        raise AnalysisArtifactError("prestudy evidence needs trial directories")
    evidence = tuple(load_trial_evidence(root) for root in roots)
    if any(
        gate is not True
        for item in evidence
        for gate in (
            item.observation.work_complete,
            item.observation.output_valid,
            item.observation.slo_met,
            item.observation.quality_accepted,
        )
    ):
        raise AnalysisArtifactError(
            "prestudy evidence contains an ineligible work, output, SLO, or quality gate"
        )
    observations = tuple(item.observation for item in evidence)
    estimate = paired_effect(
        observations,
        treatment_arm=treatment_arm,
        control_arm=control_arm,
        confidence=confidence,
    )
    if not estimate.claim_eligible:
        raise AnalysisArtifactError(
            "prestudy paired evidence is not eligible: "
            + "; ".join(estimate.ineligibility_reasons)
        )
    by_block: dict[str, list[tuple[object, Path]]] = {}
    trial_refs: list[dict[str, object]] = []
    boundary_document = estimate.boundary.to_dict()
    topology: tuple[Mapping[str, Any], ...] | None = None
    for root, item in zip(roots, evidence):
        observation = item.observation
        result = item.acquisition.get("result")
        if not isinstance(result, Mapping):
            raise AnalysisArtifactError("prestudy trial has no acquisition result")
        execution = result.get("execution_contract")
        if not isinstance(execution, Mapping):
            raise AnalysisArtifactError("prestudy trial has no execution contract")
        execution_digest = _canonical_sha256(execution)
        if result.get("execution_contract_sha256") != execution_digest:
            raise AnalysisArtifactError("prestudy execution-contract digest is invalid")
        target = execution.get("target")
        if not isinstance(target, Mapping):
            raise AnalysisArtifactError("prestudy trial must execute a target command")
        current_topology = tuple(
            descriptor.to_dict() for descriptor in item.descriptors
        )
        if topology is None:
            topology = current_topology
        elif current_topology != topology:
            raise AnalysisArtifactError("prestudy trials changed source topology")
        estimate.boundary.assert_compatible(item.outcome.boundary)
        trial = item.trial
        arm_document = trial.get("arm")
        if not isinstance(arm_document, Mapping):
            raise AnalysisArtifactError("prestudy trial arm is malformed")
        reference = {
            "trial_id": observation.trial_id,
            "matched_block_id": observation.block_id,
            "arm": observation.arm,
            "program_id": arm_document.get("program_id"),
            "checksums_sha256": _file_sha256(root / "checksums.json"),
            "execution_contract_sha256": execution_digest,
            "workload_metrics_sha256": _require_sha256(
                item.workload_metrics_sha256,
                "prestudy workload metrics digest",
            ),
            "target_command_sha256": _canonical_sha256(
                _target_command_identity(target)
            ),
            "total_joules": item.outcome.total_joules,
        }
        trial_refs.append(reference)
        by_block.setdefault(observation.block_id, []).append((reference, root))
    if topology is None:  # pragma: no cover - roots are non-empty
        raise AnalysisArtifactError("prestudy source topology is missing")
    if len({str(item["trial_id"]) for item in trial_refs}) != len(trial_refs):
        raise AnalysisArtifactError("prestudy trial ids must be unique")
    for block_id, cell in by_block.items():
        refs = [item for item, _ in cell]
        arms = [str(item["arm"]) for item in refs]
        if len(refs) != 3 or sorted(arms) != ["C0", "C1", "C2"]:
            raise AnalysisArtifactError(
                f"prestudy block {block_id!r} must contain exactly one C0/C1/C2 triple"
            )
        if role in {"AA_CALIBRATION", "PLACEBO"}:
            contrasted = [
                item for item in refs if item["arm"] in {treatment_arm, control_arm}
            ]
            if len({item["program_id"] for item in contrasted}) != 1 or len(
                {item["target_command_sha256"] for item in contrasted}
            ) != 1:
                raise AnalysisArtifactError(
                    f"{role} block {block_id!r} must compare identical sham targets"
                )
    if role == "AA_CALIBRATION" and len(by_block) < 12:
        raise AnalysisArtifactError(
            "verified A/A evidence requires at least 12 matched sham blocks"
        )
    controls_by_block = {
        item.observation.block_id: item.outcome.total_joules
        for item in evidence
        if item.observation.arm == control_arm
    }
    parameters: dict[str, object]
    if role == "AA_CALIBRATION":
        parameters = {
            "relative_floor_fraction": relative_floor_fraction,
            "uncertainty_multiplier": uncertainty_multiplier,
            "uncertainty_quantile": uncertainty_quantile,
        }
    elif role == "PLACEBO":
        parameters = {"confidence": confidence}
    else:
        parameters = {}
    return {
        "schema_version": 1,
        "kind": role,
        "regime_id": regime_id,
        "derivation": "REPLAY_VERIFIED_TRIALS",
        "contrast": {"treatment_arm": treatment_arm, "control_arm": control_arm},
        "estimand": estimate.estimand.value,
        "context": estimate.context,
        "policy": estimate.policy,
        "measurement_boundary": boundary_document,
        "measurement_boundary_sha256": _canonical_sha256(boundary_document),
        "source_topology_sha256": _canonical_sha256(topology),
        "block_ids": list(estimate.block_ids),
        "paired_differences_joules": list(estimate.paired_differences_joules),
        "control_joules": [controls_by_block[item] for item in estimate.block_ids],
        "parameters": parameters,
        "trial_evidence": sorted(trial_refs, key=lambda item: str(item["trial_id"])),
    }


def write_verified_prestudy_evidence(
    path: str | Path,
    *,
    role: str,
    regime_id: str,
    trial_directories: Iterable[str | Path],
    treatment_arm: str = "C1",
    control_arm: str = "C2",
    relative_floor_fraction: float = 0.05,
    uncertainty_multiplier: float = 2.0,
    uncertainty_quantile: float = 0.95,
    confidence: float = 0.95,
) -> VerifiedPrestudyEvidenceArtifact:
    """Replay trials, derive paired values, and exclusively publish evidence."""

    document = _verified_prestudy_document(
        role=role,
        regime_id=regime_id,
        trial_directories=trial_directories,
        treatment_arm=treatment_arm,
        control_arm=control_arm,
        relative_floor_fraction=relative_floor_fraction,
        uncertainty_multiplier=uncertainty_multiplier,
        uncertainty_quantile=uncertainty_quantile,
        confidence=confidence,
    )
    target = _write_one(path, _json_bytes(document))
    return VerifiedPrestudyEvidenceArtifact._issue(path=target, document=document)


def load_verified_prestudy_evidence(
    path: str | Path,
    *,
    trial_directories: Iterable[str | Path],
) -> VerifiedPrestudyEvidenceArtifact:
    """Reopen raw trials and require exact equality with a derived artifact."""

    target = Path(path)
    document = _read_json_artifact(target)
    parameters = document.get("parameters")
    contrast = document.get("contrast")
    if not isinstance(parameters, Mapping) or not isinstance(contrast, Mapping):
        raise AnalysisArtifactError("prestudy evidence parameters are malformed")
    recomputed = _verified_prestudy_document(
        role=_require_text(document.get("kind"), "prestudy role"),
        regime_id=_require_text(document.get("regime_id"), "regime_id"),
        trial_directories=trial_directories,
        treatment_arm=_require_text(contrast.get("treatment_arm"), "treatment arm"),
        control_arm=_require_text(contrast.get("control_arm"), "control arm"),
        relative_floor_fraction=float(parameters.get("relative_floor_fraction", 0.05)),
        uncertainty_multiplier=float(parameters.get("uncertainty_multiplier", 2.0)),
        uncertainty_quantile=float(parameters.get("uncertainty_quantile", 0.95)),
        confidence=float(parameters.get("confidence", 0.95)),
    )
    if dict(document) != recomputed:
        raise AnalysisArtifactError(
            "prestudy artifact differs from replay-derived trial evidence"
        )
    return VerifiedPrestudyEvidenceArtifact._issue(path=target, document=document)


@dataclass(frozen=True, init=False)
class VerifiedStudyPreregistration:
    """Internally issued verification of a sealed preregistration bundle."""

    plan: StudyPreregistration
    sealed_at_unix_ns: int
    artifact_sha256: str
    referenced_artifacts: tuple[SealedArtifactReference, ...]
    placebo_checks: tuple[PlaceboZeroCheck, ...]
    _meaningful_thresholds: tuple[tuple[str, str, float], ...]
    _schedule_documents: tuple[tuple[str, Mapping[str, Any]], ...]
    _calibration_documents: tuple[tuple[str, Mapping[str, Any]], ...]
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError(
            "use load_study_preregistration or seal_study_preregistration"
        )

    @classmethod
    def _issue(
        cls,
        *,
        plan: StudyPreregistration,
        sealed_at_unix_ns: int,
        artifact_sha256: str,
        referenced_artifacts: tuple[SealedArtifactReference, ...],
        placebo_checks: tuple[PlaceboZeroCheck, ...],
        meaningful_thresholds: tuple[tuple[str, str, float], ...],
        schedule_documents: tuple[tuple[str, Mapping[str, Any]], ...],
        calibration_documents: tuple[tuple[str, Mapping[str, Any]], ...],
    ) -> "VerifiedStudyPreregistration":
        instance = object.__new__(cls)
        for name, value in (
            ("plan", plan),
            ("sealed_at_unix_ns", sealed_at_unix_ns),
            ("artifact_sha256", artifact_sha256),
            ("referenced_artifacts", referenced_artifacts),
            ("placebo_checks", placebo_checks),
            ("_meaningful_thresholds", meaningful_thresholds),
            ("_schedule_documents", schedule_documents),
            ("_calibration_documents", calibration_documents),
            ("_verification_token", _VERIFIED_PREREGISTRATION_TOKEN),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def _require_verified(self) -> None:
        if self._verification_token is not _VERIFIED_PREREGISTRATION_TOKEN:
            raise AnalysisArtifactError("preregistration was not internally verified")

    @property
    def calibration_digests(self) -> frozenset[str]:
        self._require_verified()
        return frozenset(
            item.sha256
            for item in self.referenced_artifacts
            if item.role == "METER_CALIBRATION"
        )

    def _calibration(self, digest: str) -> Mapping[str, Any]:
        self._require_verified()
        for candidate, document in self._calibration_documents:
            if candidate == digest:
                return document
        raise AnalysisArtifactError(
            "trial refers to a meter calibration outside the sealed preregistration"
        )

    def _meaningful_threshold(self, estimand: str, context: str) -> float:
        self._require_verified()
        for candidate_estimand, candidate_context, threshold in self._meaningful_thresholds:
            if (candidate_estimand, candidate_context) == (estimand, context):
                return threshold
        raise AnalysisArtifactError(
            f"no sealed A/A threshold for {(estimand, context)!r}"
        )

    def to_dict(self) -> dict[str, object]:
        self._require_verified()
        return {
            "artifact_sha256": self.artifact_sha256,
            "sealed_at_unix_ns": self.sealed_at_unix_ns,
            "plan": self.plan.to_dict(),
            "referenced_artifacts": [
                item.to_dict() for item in self.referenced_artifacts
            ],
        }


@dataclass(frozen=True, init=False)
class TrialEvidenceReference:
    """Reference issued only after a sealed trial is replayed and cross-checked."""

    trial_id: str
    experiment_id: str
    matched_block_id: str
    arm: str
    program_id: str | None
    context: str
    day_utc: str
    machine_id: str
    machine_provenance_sha256: str
    estimand: str
    boundary_id: str
    boundary_sha256: str
    source_topology_sha256: str
    cache_regime: str
    prelaunch_temperature_source_uid: str
    prelaunch_temperature_celsius: float
    prelaunch_temperature_bin_celsius: str
    mechanism_policy: str
    mechanism_position: int
    order_position: int
    target_nice: int
    replay_seed: int
    match_fingerprint: str
    checksums_sha256: str
    schedule_sha256: str
    execution_contract_sha256: str
    target_command_sha256: str
    input_id: str
    outcome_duration_s: float
    fixed_horizon_tolerance_ns: int | None
    workload_metrics_sha256: str
    arrival_trace_sha256: str | None
    background_workload_metrics_sha256: str | None
    background_completed_work: float | None
    background_work_unit: str | None
    background_output_sha256: str | None
    background_arrival_trace_sha256: str | None
    background_present: bool
    background_command_sha256: str | None
    host_policy_settings_fingerprint_sha256: str
    host_policy_prior_settings_fingerprint_sha256: str
    host_policy_restoration_fingerprint_sha256: str
    measurement_overhead_calibration_sha256: str
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError(
            "trial evidence references are issued by VerifiedTrialEvidenceBundle"
        )

    @classmethod
    def _issue(cls, **values: object) -> "TrialEvidenceReference":
        instance = object.__new__(cls)
        for name in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            value = (
                _VERIFIED_TRIAL_BUNDLE_TOKEN
                if name == "_verification_token"
                else values[name]
            )
            object.__setattr__(instance, name, value)
        return instance

    def _require_verified(self) -> None:
        if self._verification_token is not _VERIFIED_TRIAL_BUNDLE_TOKEN:
            raise AnalysisArtifactError("trial reference was not internally verified")

    def to_dict(self) -> dict[str, object]:
        self._require_verified()
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__  # type: ignore[attr-defined]
            if not name.startswith("_")
        }


@dataclass(frozen=True, init=False)
class VerifiedTrialEvidenceBundle:
    """Replay-verified confirmatory trials tied to one sealed design."""

    references: tuple[TrialEvidenceReference, ...]
    boundary: MeasurementBoundary
    boundary_sha256: str
    source_topology: tuple[Mapping[str, Any], ...]
    source_topology_sha256: str
    preregistration_sha256: str
    _observations_by_trial: tuple[tuple[str, object], ...]
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError(
            "use VerifiedTrialEvidenceBundle.from_trial_directories"
        )

    @classmethod
    def from_trial_directories(
        cls,
        trial_directories: Iterable[str | Path],
        *,
        preregistration: VerifiedStudyPreregistration,
    ) -> "VerifiedTrialEvidenceBundle":
        """Reopen every trial and bind schedules, provenance, and raw gates."""

        from .replay import load_trial_evidence, trial_match_fingerprint

        preregistration._require_verified()
        roots = tuple(Path(path) for path in trial_directories)
        if not roots:
            raise AnalysisArtifactError("verified trial bundle cannot be empty")
        schedule_index = _schedule_trial_index(preregistration._schedule_documents)
        calibration_digests = preregistration.calibration_digests
        references: list[TrialEvidenceReference] = []
        observations: list[tuple[str, object]] = []
        boundary: MeasurementBoundary | None = None
        boundary_document: Mapping[str, Any] | None = None
        topology: tuple[Mapping[str, Any], ...] | None = None
        for root in roots:
            evidence = load_trial_evidence(root)
            trial = evidence.trial
            manifest = evidence.manifest
            result = evidence.acquisition.get("result")
            if not isinstance(result, Mapping):
                raise AnalysisArtifactError("verified trial has no acquisition result")
            try:
                trial_host = validate_host_provenance(result.get("host_provenance"))
            except ValueError as exc:
                raise AnalysisArtifactError(
                    "verified trial lacks canonical host provenance"
                ) from exc
            if any(
                gate is not True
                for gate in (
                    evidence.observation.work_complete,
                    evidence.observation.output_valid,
                    evidence.observation.slo_met,
                    evidence.observation.quality_accepted,
                )
            ):
                raise AnalysisArtifactError(
                    f"trial {evidence.observation.trial_id!r} failed a work, output, "
                    "SLO, or quality gate"
                )
            trial_id = _require_text(trial.get("trial_id"), "trial_id")
            schedule_match = schedule_index.get(trial_id)
            if schedule_match is None:
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} is absent from the sealed schedules"
                )
            schedule_sha256, scheduled = schedule_match
            _validate_scheduled_trial_metadata(
                trial,
                scheduled.get("trial_metadata"),
                trial_id=trial_id,
            )
            execution = result.get("execution_contract")
            if not isinstance(execution, Mapping):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} did not seal its execution contract"
                )
            _validate_execution_contract(
                execution,
                expected=_execution_contract_from_schedule(scheduled),
                trial_id=trial_id,
            )
            execution_digest = _canonical_sha256(execution)
            if result.get("execution_contract_sha256") != execution_digest:
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} execution-contract digest is invalid"
                )
            executed_target = execution.get("target")
            if not isinstance(executed_target, Mapping):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} target command is malformed"
                )
            target_command_digest = _canonical_sha256(
                _target_command_identity(executed_target)
            )
            executed_background = execution.get("background")
            if executed_background is not None and not isinstance(
                executed_background, Mapping
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} background command is malformed"
                )
            background_digest = (
                None
                if executed_background is None
                else _canonical_sha256(
                    _target_command_identity(executed_background)
                )
            )
            workload_metrics_digest = _require_sha256(
                evidence.workload_metrics_sha256,
                f"trial {trial_id!r} workload metrics digest",
            )
            workload_labels = trial.get("labels")
            if not isinstance(workload_labels, Mapping):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} labels are malformed"
                )
            workload_mode = _require_text(
                workload_labels.get("workload_mode"), "workload_mode"
            )
            arrival_trace_digest: str | None = None
            metrics = evidence.workload_metrics
            if metrics is None:
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} lacks workload metrics"
                )
            if metrics.arrival_trace_id is not None:
                if workload_labels.get("arrival_trace_id") != metrics.arrival_trace_id:
                    raise AnalysisArtifactError(
                        f"trial {trial_id!r} executed a different offered-demand trace id"
                    )
                arrival_trace_digest = _require_sha256(
                    metrics.arrival_trace_sha256,
                    f"trial {trial_id!r} arrival trace digest",
                )
            if workload_mode == "SERVICE" and arrival_trace_digest is None:
                raise AnalysisArtifactError(
                    f"service trial {trial_id!r} lacks an offered-demand trace"
                )
            background_metrics_digest: str | None = None
            background_completed_work: float | None = None
            background_work_unit: str | None = None
            background_output_digest: str | None = None
            background_arrival_digest: str | None = None
            if executed_background is not None:
                background_metrics_digest = _require_sha256(
                    evidence.background_workload_metrics_sha256,
                    f"trial {trial_id!r} background workload metrics digest",
                )
                background_metrics = evidence.background_workload_metrics
                if (
                    background_metrics is None
                    or background_metrics.completed_work is None
                    or background_metrics.work_unit is None
                    or background_metrics.backlog_at_end != 0
                    or background_metrics.slo_met is not True
                ):
                    raise AnalysisArtifactError(
                        f"trial {trial_id!r} lacks completed, drained background evidence"
                    )
                background_completed_work = float(
                    background_metrics.completed_work
                )
                background_work_unit = _require_text(
                    background_metrics.work_unit,
                    f"trial {trial_id!r} background work unit",
                )
                background_output_digest = _require_sha256(
                    background_metrics.output_sha256,
                    f"trial {trial_id!r} background output digest",
                )
                if background_metrics.arrival_trace_id is not None:
                    background_arrival_digest = _require_sha256(
                        background_metrics.arrival_trace_sha256,
                        f"trial {trial_id!r} background arrival trace digest",
                    )
            calibration_digest = result.get(
                "measurement_overhead_calibration_sha256"
            )
            _require_sha256(
                calibration_digest,
                f"trial {trial_id!r} measurement overhead calibration digest",
            )
            if calibration_digest not in calibration_digests:
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} overhead scalar is not tied to a "
                    "preregistered calibration artifact"
                )
            policy_evidence = result.get("host_policy_evidence")
            if (
                not isinstance(policy_evidence, Mapping)
                or policy_evidence.get("verified") is not True
                or result.get("host_policy_restored") is not True
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} lacks verified and restored host-policy settings"
                )
            host_policy_digest = policy_evidence.get("settings_fingerprint_sha256")
            _require_sha256(
                host_policy_digest,
                f"trial {trial_id!r} host-policy settings fingerprint",
            )
            policy_settings = policy_evidence.get("settings")
            if (
                not isinstance(policy_settings, Mapping)
                or _canonical_sha256(dict(policy_settings)) != host_policy_digest
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} host-policy settings digest is invalid"
                )
            prior_policy_digest = _require_sha256(
                policy_evidence.get("prior_settings_fingerprint_sha256"),
                f"trial {trial_id!r} prior host-policy fingerprint",
            )
            prior_settings = policy_evidence.get("prior_settings")
            if (
                not isinstance(prior_settings, Mapping)
                or _canonical_sha256(dict(prior_settings)) != prior_policy_digest
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} prior host-policy digest is invalid"
                )
            restoration = result.get("host_policy_restoration_evidence")
            if (
                not isinstance(restoration, Mapping)
                or restoration.get("verified") is not True
                or restoration.get("policy") != policy_evidence.get("policy")
                or restoration.get("controller_id")
                != policy_evidence.get("controller_id")
                or restoration.get("expected_prior_settings_fingerprint_sha256")
                != prior_policy_digest
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} lacks read-back host-policy restoration proof"
                )
            restoration_digest = _require_sha256(
                restoration.get("restored_settings_fingerprint_sha256"),
                f"trial {trial_id!r} restored host-policy fingerprint",
            )
            restored_settings = restoration.get("restored_settings")
            if (
                restoration_digest != prior_policy_digest
                or not isinstance(restored_settings, Mapping)
                or _canonical_sha256(dict(restored_settings)) != restoration_digest
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} did not restore the sealed prior host policy"
                )
            labels = trial.get("labels")
            arm_document = trial.get("arm")
            estimand_document = trial.get("estimand")
            trial_boundary_document = trial.get("measurement_boundary")
            if not all(
                isinstance(item, Mapping)
                for item in (
                    labels,
                    arm_document,
                    estimand_document,
                    trial_boundary_document,
                )
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} has malformed claim metadata"
                )
            if (
                labels.get("study_preregistration_sha256")
                != preregistration.artifact_sha256
                or result.get("study_preregistration_sha256")
                != preregistration.artifact_sha256
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} was not launched from the verified "
                    "preregistration artifact"
                )
            current_boundary = MeasurementBoundary.from_dict(trial_boundary_document)
            current_topology = tuple(
                descriptor.to_dict() for descriptor in evidence.descriptors
            )
            cache_regime = _require_text(
                labels.get("cache_regime"), "cache_regime"
            )
            temperature_source_uid = _require_text(
                labels.get("prelaunch_temperature_source_uid"),
                "prelaunch_temperature_source_uid",
            )
            temperature_celsius, temperature_bin = _prelaunch_temperature(
                evidence,
                source_uid=temperature_source_uid,
                trial_id=trial_id,
            )
            if boundary is None:
                boundary = current_boundary
                boundary_document = dict(trial_boundary_document)
                topology = current_topology
            else:
                boundary.assert_compatible(current_boundary)
                if dict(trial_boundary_document) != dict(boundary_document or {}):
                    raise AnalysisArtifactError(
                        "verified trials changed the canonical boundary declaration"
                    )
                if current_topology != topology:
                    raise AnalysisArtifactError(
                        "verified trials changed the complete source topology"
                    )
            _validate_trial_calibration(
                preregistration._calibration(calibration_digest),
                calibration_digest=calibration_digest,
                trial_id=trial_id,
                trial_labels=labels,
                boundary_document=trial_boundary_document,
                quality=evidence.quality,
                source_topology=current_topology,
                manifest=manifest,
                result=result,
            )
            created_unix_ns = manifest.get("created_unix_ns")
            if (
                isinstance(created_unix_ns, bool)
                or not isinstance(created_unix_ns, int)
                or created_unix_ns < preregistration.sealed_at_unix_ns
            ):
                raise AnalysisArtifactError(
                    f"trial {trial_id!r} does not postdate the sealed preregistration"
                )
            day_utc = datetime.fromtimestamp(
                created_unix_ns / 1_000_000_000, tz=UTC
            ).date().isoformat()
            machine_id = _require_text(manifest.get("node_id"), "manifest node_id")
            machine_digest = _canonical_sha256(
                {
                    "node_id": machine_id,
                    "host_provenance": trial_host,
                    "source_topology": current_topology,
                }
            )
            arm = _require_text(labels.get("arm_code"), "trial arm_code")
            if arm not in {"C0", "C1", "C2"}:
                raise AnalysisArtifactError("trial arm must be C0, C1, or C2")
            program_id = arm_document.get("program_id")
            if arm == "C0":
                if program_id is not None:
                    raise AnalysisArtifactError("C0 cannot identify a program")
                program_id = None
            else:
                program_id = _require_text(program_id, "trial program_id")
            checksums_sha256 = _file_sha256(root / "checksums.json")
            reference = TrialEvidenceReference._issue(
                trial_id=trial_id,
                experiment_id=_require_text(trial.get("experiment_id"), "experiment_id"),
                matched_block_id=_require_text(
                    trial.get("matched_block_id"), "matched_block_id"
                ),
                arm=arm,
                program_id=program_id,
                context=_require_text(
                    trial.get("background_workload_id"), "background context"
                ),
                day_utc=day_utc,
                machine_id=machine_id,
                machine_provenance_sha256=machine_digest,
                estimand=_require_text(estimand_document.get("kind"), "estimand"),
                boundary_id=current_boundary.boundary_id,
                boundary_sha256=_canonical_sha256(trial_boundary_document),
                source_topology_sha256=_canonical_sha256(current_topology),
                cache_regime=cache_regime,
                prelaunch_temperature_source_uid=temperature_source_uid,
                prelaunch_temperature_celsius=temperature_celsius,
                prelaunch_temperature_bin_celsius=temperature_bin,
                mechanism_policy=_require_text(
                    labels.get("mechanism_policy"), "mechanism_policy"
                ),
                mechanism_position=int(
                    _require_text(labels.get("mechanism_position"), "mechanism_position")
                ),
                order_position=int(
                    _require_text(labels.get("order_position"), "order_position")
                ),
                target_nice=int(_require_text(labels.get("target_nice"), "target_nice")),
                replay_seed=_strict_schedule_int(
                    trial.get("replay_seed"), "trial replay_seed"
                ),
                match_fingerprint=trial_match_fingerprint(trial),
                checksums_sha256=checksums_sha256,
                schedule_sha256=schedule_sha256,
                execution_contract_sha256=execution_digest,
                target_command_sha256=target_command_digest,
                input_id=_require_text(trial.get("input_id"), "trial input_id"),
                outcome_duration_s=evidence.outcome.duration_s,
                fixed_horizon_tolerance_ns=(
                    int(
                        _require_text(
                            labels.get("fixed_horizon_tolerance_ns"),
                            "fixed_horizon_tolerance_ns",
                        )
                    )
                    if estimand_document.get("kind") == "FIXED_HORIZON"
                    else None
                ),
                workload_metrics_sha256=workload_metrics_digest,
                arrival_trace_sha256=arrival_trace_digest,
                background_workload_metrics_sha256=background_metrics_digest,
                background_completed_work=background_completed_work,
                background_work_unit=background_work_unit,
                background_output_sha256=background_output_digest,
                background_arrival_trace_sha256=background_arrival_digest,
                background_present=executed_background is not None,
                background_command_sha256=background_digest,
                host_policy_settings_fingerprint_sha256=host_policy_digest,
                host_policy_prior_settings_fingerprint_sha256=prior_policy_digest,
                host_policy_restoration_fingerprint_sha256=restoration_digest,
                measurement_overhead_calibration_sha256=calibration_digest,
            )
            references.append(reference)
            observations.append((trial_id, evidence.observation))
        if boundary is None or boundary_document is None or topology is None:
            raise AnalysisArtifactError("verified trial bundle contains no trial evidence")
        captured_trial_ids = [item.trial_id for item in references]
        if (
            len(captured_trial_ids) != len(set(captured_trial_ids))
            or set(captured_trial_ids) != set(schedule_index)
        ):
            raise AnalysisArtifactError(
                "verified trials must exactly cover every content-addressed "
                "confirmatory schedule trial"
            )
        _validate_verified_trial_cells(references, preregistration.plan)
        instance = object.__new__(cls)
        for name, value in (
            ("references", tuple(sorted(references, key=lambda item: item.trial_id))),
            ("boundary", boundary),
            ("boundary_sha256", _canonical_sha256(boundary_document)),
            ("source_topology", topology),
            ("source_topology_sha256", _canonical_sha256(topology)),
            ("preregistration_sha256", preregistration.artifact_sha256),
            ("_observations_by_trial", tuple(observations)),
            ("_verification_token", _VERIFIED_TRIAL_BUNDLE_TOKEN),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def _require_verified(self) -> None:
        if self._verification_token is not _VERIFIED_TRIAL_BUNDLE_TOKEN:
            raise AnalysisArtifactError("trial bundle was not internally verified")
        for reference in self.references:
            reference._require_verified()

    def _observations(self, *, day: str | None = None) -> tuple[object, ...]:
        self._require_verified()
        references = {item.trial_id: item for item in self.references}
        return tuple(
            observation
            for trial_id, observation in self._observations_by_trial
            if day is None or references[trial_id].day_utc == day
        )

    def observations(self, *, day_utc: str | None = None) -> tuple[object, ...]:
        """Return replay-issued observations for effect construction."""

        return self._observations(day=day_utc)


@dataclass(frozen=True, init=False)
class StudyClaimRecord:
    """Ranking conclusion issued from replayed evidence and recomputed gates."""

    claim_id: str
    study_id: str
    treatment_program_id: str
    control_program_id: str
    acceptance: StudyAcceptanceResult
    preregistration: VerifiedStudyPreregistration
    trial_bundle: VerifiedTrialEvidenceBundle
    fixed_horizon_reversal: RankingReversalResult
    equal_work_reversal: RankingReversalResult
    independent_day_reversals: tuple[tuple[str, tuple[RankingReversalResult, ...]], ...]
    placebo_checks: tuple[PlaceboZeroCheck, ...]
    mechanism_attenuation: MechanismAttenuationResult | None
    _verification_token: object

    def __init__(self) -> None:
        raise AnalysisArtifactError("use StudyClaimRecord.from_verified_results")

    @classmethod
    def from_verified_results(
        cls,
        *,
        claim_id: str,
        study_id: str,
        treatment_program_id: str,
        control_program_id: str,
        preregistration: VerifiedStudyPreregistration,
        trial_bundle: VerifiedTrialEvidenceBundle,
        fixed_horizon_reversal: RankingReversalResult,
        equal_work_reversal: RankingReversalResult,
        placebo_checks: Iterable[PlaceboZeroCheck],
        independent_day_reversals: Mapping[str, Sequence[RankingReversalResult]],
        mechanism_attenuation: MechanismAttenuationResult | None = None,
        require_mechanism: bool = False,
    ) -> "StudyClaimRecord":
        """Recompute every supplied result from sealed trials before issuing."""

        if require_mechanism or mechanism_attenuation is not None:
            raise AnalysisArtifactError(
                "mechanism claims are disabled until the preregistration seals "
                "frequency/P-state, C-state residency, and wakeup source UIDs and "
                "their replay-derived post-launch outcomes"
            )

        for name, value in (
            ("claim_id", claim_id),
            ("study_id", study_id),
            ("treatment_program_id", treatment_program_id),
            ("control_program_id", control_program_id),
        ):
            _require_text(value, name)
        if treatment_program_id == control_program_id:
            raise AnalysisArtifactError("study claim programs must differ")
        preregistration._require_verified()
        trial_bundle._require_verified()
        if trial_bundle.preregistration_sha256 != preregistration.artifact_sha256:
            raise AnalysisArtifactError("trial bundle belongs to another preregistration")
        _validate_arm_program_mapping(
            trial_bundle.references,
            treatment_program_id=treatment_program_id,
            control_program_id=control_program_id,
        )
        _validate_reversal_context_roles(
            trial_bundle.references,
            fixed_horizon_reversal=fixed_horizon_reversal,
            equal_work_reversal=equal_work_reversal,
        )
        observations = trial_bundle._observations()
        fixed = _recompute_reversal(
            fixed_horizon_reversal, observations, preregistration=preregistration
        )
        equal = _recompute_reversal(
            equal_work_reversal, observations, preregistration=preregistration
        )
        supplied_days = {
            day: tuple(results) for day, results in independent_day_reversals.items()
        }
        evidence_days = {item.day_utc for item in trial_bundle.references}
        if set(supplied_days) != evidence_days:
            raise AnalysisArtifactError(
                "independent-day results must exactly match manifest-derived UTC days"
            )
        verified_days: dict[str, tuple[RankingReversalResult, ...]] = {}
        for day, supplied_results in supplied_days.items():
            day_observations = trial_bundle._observations(day=day)
            verified_days[day] = tuple(
                _recompute_reversal(
                    result,
                    day_observations,
                    preregistration=preregistration,
                )
                for result in supplied_results
            )
        supplied_placebos = tuple(placebo_checks)
        if supplied_placebos != preregistration.placebo_checks:
            raise AnalysisArtifactError(
                "supplied placebo results do not match content-addressed evidence"
            )
        verified_mechanism: MechanismAttenuationResult | None = None
        if mechanism_attenuation is not None:
            supplied = mechanism_attenuation
            estimate = supplied.estimate
            relevant = tuple(
                item
                for item in observations
                if item.outcome.estimand == estimate.estimand
                and item.context == estimate.context
            )
            recomputed_estimate = matched_difference_in_differences(
                relevant,
                treatment_arm=estimate.treatment_arm,
                control_arm=estimate.control_arm,
                baseline_policy=estimate.baseline_policy,
                intervention_policy=estimate.intervention_policy,
                confidence=estimate.confidence,
            )
            verified_mechanism = assess_mechanism_attenuation(
                recomputed_estimate,
                minimum_attenuation_joules=supplied.minimum_attenuation_joules,
                require_confidence_interval=True,
            )
            _require_same_result(
                supplied.to_dict(),
                verified_mechanism.to_dict(),
                "mechanism attenuation",
            )
        acceptance = evaluate_study_acceptance(
            fixed_horizon_reversal=fixed,
            equal_work_reversal=equal,
            placebo_checks=supplied_placebos,
            independent_day_reversals=verified_days,
            mechanism_attenuation=verified_mechanism,
            require_mechanism=require_mechanism,
        )
        if acceptance.supported:
            references = trial_bundle.references
            if len({item.day_utc for item in references}) < 2:
                raise AnalysisArtifactError(
                    "supported evidence needs two manifest-derived UTC days"
                )
            if len({item.machine_provenance_sha256 for item in references}) != 1:
                raise AnalysisArtifactError(
                    "an independent-day claim must hold machine provenance fixed"
                )
            if {item.estimand for item in references} != {
                "FIXED_HORIZON",
                "EQUAL_WORK",
            }:
                raise AnalysisArtifactError(
                    "supported evidence must cover fixed-horizon and equal-work trials"
                )
            if len({item.context for item in references}) < 2:
                raise AnalysisArtifactError(
                    "supported evidence needs isolated and co-tenanted contexts"
                )
            if require_mechanism:
                policies = {item.mechanism_policy for item in references}
                interventions = policies - {"NORMAL_DYNAMIC"}
                if "NORMAL_DYNAMIC" not in policies or len(interventions) != 1:
                    raise AnalysisArtifactError(
                        "mechanism evidence needs normal dynamic policy and exactly "
                        "one intervention"
                    )
                priorities_by_regime: dict[tuple[str, str], set[int]] = {}
                for item in references:
                    priorities_by_regime.setdefault(
                        (item.estimand, item.context), set()
                    ).add(item.target_nice)
                if any(
                    not {0, 10}.issubset(priorities)
                    for priorities in priorities_by_regime.values()
                ):
                    raise AnalysisArtifactError(
                        "mechanism evidence must cover target nice 0 and +10 in "
                        "every primary regime"
                    )
        instance = object.__new__(cls)
        for name, value in (
            ("claim_id", claim_id),
            ("study_id", study_id),
            ("treatment_program_id", treatment_program_id),
            ("control_program_id", control_program_id),
            ("acceptance", acceptance),
            ("preregistration", preregistration),
            ("trial_bundle", trial_bundle),
            ("fixed_horizon_reversal", fixed),
            ("equal_work_reversal", equal),
            ("independent_day_reversals", tuple(sorted(verified_days.items()))),
            ("placebo_checks", supplied_placebos),
            ("mechanism_attenuation", verified_mechanism),
            ("_verification_token", _VERIFIED_CLAIM_TOKEN),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def _require_verified(self) -> None:
        if self._verification_token is not _VERIFIED_CLAIM_TOKEN:
            raise AnalysisArtifactError("study claim was not internally issued")
        self.preregistration._require_verified()
        self.trial_bundle._require_verified()

    def require_verified(self) -> None:
        """Revalidate the private issuance chain for downstream consumers."""

        self._require_verified()

    @property
    def digest_sha256(self) -> str:
        self._require_verified()
        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        self._require_verified()
        return {
            "claim_id": self.claim_id,
            "study_id": self.study_id,
            "claim_kind": "CONTEXT_DEPENDENT_RANKING_REVERSAL",
            "treatment_program_id": self.treatment_program_id,
            "control_program_id": self.control_program_id,
            "supported": self.acceptance.supported,
            "acceptance": self.acceptance.to_dict(),
            "preregistration": self.preregistration.to_dict(),
            "measurement_boundary": self.trial_bundle.boundary.to_dict(),
            "measurement_boundary_sha256": self.trial_bundle.boundary_sha256,
            "source_topology": list(self.trial_bundle.source_topology),
            "source_topology_sha256": self.trial_bundle.source_topology_sha256,
            "trial_evidence": [
                item.to_dict() for item in self.trial_bundle.references
            ],
            "effects": {
                "fixed_horizon": self.fixed_horizon_reversal.to_dict(),
                "equal_work": self.equal_work_reversal.to_dict(),
                "independent_days": {
                    day: [result.to_dict() for result in results]
                    for day, results in self.independent_day_reversals
                },
                "placebos": [item.to_dict() for item in self.placebo_checks],
                "mechanism": (
                    None
                    if self.mechanism_attenuation is None
                    else self.mechanism_attenuation.to_dict()
                ),
            },
        }


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AnalysisArtifactError(
            f"content-addressed artifact is not strict JSON: {exc}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AnalysisArtifactError(f"cannot read evidence artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AnalysisArtifactError(
            f"{field_name} must be 64 lowercase hexadecimal characters"
        )
    return value


def _artifact_reference(
    path: Path,
    *,
    role: str,
    regime_id: str | None = None,
) -> SealedArtifactReference:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AnalysisArtifactError(f"cannot stat evidence artifact {path}: {exc}") from exc
    return SealedArtifactReference(
        role=role,
        regime_id=regime_id,
        sha256=_file_sha256(path),
        bytes=size,
    )


def _read_json_artifact(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisArtifactError(
            f"cannot read strict JSON evidence artifact {path}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise AnalysisArtifactError(f"evidence artifact {path} must contain an object")
    return value


def _sample_size_from_dict(value: object) -> NormalApproximationSampleSize:
    if not isinstance(value, Mapping):
        raise AnalysisArtifactError("sample_size must be an object")
    try:
        return NormalApproximationSampleSize(
            pilot_blocks=int(value["pilot_blocks"]),
            pilot_paired_sd_joules=float(value["pilot_paired_sd_joules"]),
            meaningful_effect_joules=float(value["meaningful_effect_joules"]),
            power=float(value["power"]),
            two_sided_alpha=float(value["two_sided_alpha"]),
            required_matched_blocks=int(value["required_matched_blocks"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisArtifactError("sample_size fields are invalid") from exc


def _plan_from_dict(value: object) -> StudyPreregistration:
    if not isinstance(value, Mapping):
        raise AnalysisArtifactError("sealed preregistration plan must be an object")
    regimes = value.get("primary_regimes")
    if not isinstance(regimes, list):
        raise AnalysisArtifactError("sealed preregistration primary_regimes must be a list")
    parsed: list[PrimaryRegimePreregistration] = []
    for item in regimes:
        if not isinstance(item, Mapping):
            raise AnalysisArtifactError("sealed primary regime must be an object")
        pilot_ids = item.get("pilot_block_ids")
        confirmatory_ids = item.get("confirmatory_block_ids")
        if not isinstance(pilot_ids, list) or not isinstance(confirmatory_ids, list):
            raise AnalysisArtifactError("sealed regime block ids must be lists")
        parsed.append(
            PrimaryRegimePreregistration(
                regime_id=_require_text(item.get("regime_id"), "regime_id"),
                estimand=_require_text(item.get("estimand"), "regime estimand"),
                context=_require_text(item.get("context"), "regime context"),
                pilot_block_ids=tuple(
                    _require_text(block_id, "pilot block id") for block_id in pilot_ids
                ),
                pilot_discarded=item.get("pilot_disposition")
                == "DISCARDED_FROM_CONFIRMATORY",
                sample_size=_sample_size_from_dict(item.get("sample_size")),
                confirmatory_block_ids=tuple(
                    _require_text(block_id, "confirmatory block id")
                    for block_id in confirmatory_ids
                ),
            )
        )
    return StudyPreregistration(
        preregistration_id=_require_text(
            value.get("preregistration_id"), "preregistration_id"
        ),
        primary_regimes=tuple(parsed),
    )


def seal_study_preregistration(
    path: str | Path,
    plan: StudyPreregistration,
    *,
    schedule_paths: Iterable[str | Path],
    pilot_evidence_paths: Mapping[str, VerifiedPrestudyEvidenceArtifact],
    aa_evidence_paths: Mapping[str, VerifiedPrestudyEvidenceArtifact],
    placebo_evidence_paths: Mapping[str, VerifiedPrestudyEvidenceArtifact],
    calibration_evidence_paths: Iterable[object],
    sealed_at_unix_ns: int,
) -> VerifiedStudyPreregistration:
    """Exclusively seal a design and content addresses before any trial starts."""

    if not isinstance(plan, StudyPreregistration):
        raise AnalysisArtifactError("plan must be a StudyPreregistration")
    if (
        isinstance(sealed_at_unix_ns, bool)
        or not isinstance(sealed_at_unix_ns, int)
        or sealed_at_unix_ns < 0
    ):
        raise AnalysisArtifactError("sealed_at_unix_ns must be non-negative")
    regime_ids = {regime.regime_id for regime in plan.primary_regimes}
    for label, supplied in (
        ("pilot", pilot_evidence_paths),
        ("A/A", aa_evidence_paths),
        ("placebo", placebo_evidence_paths),
    ):
        if set(supplied) != regime_ids:
            raise AnalysisArtifactError(
                f"{label} evidence must contain exactly every primary regime"
            )
    references: list[SealedArtifactReference] = []
    referenced_paths: list[Path] = []
    for schedule_path in schedule_paths:
        current = Path(schedule_path)
        references.append(_artifact_reference(current, role="CONFIRMATORY_SCHEDULE"))
        referenced_paths.append(current)
    if not referenced_paths:
        raise AnalysisArtifactError("at least one confirmatory schedule is required")
    for regime_id in sorted(regime_ids):
        for role, supplied in (
            ("DISCARDED_PILOT", pilot_evidence_paths),
            ("AA_CALIBRATION", aa_evidence_paths),
            ("PLACEBO", placebo_evidence_paths),
        ):
            verified = supplied[regime_id]
            if not isinstance(verified, VerifiedPrestudyEvidenceArtifact):
                raise AnalysisArtifactError(
                    f"{role} evidence must be factory-issued from replayed trials"
                )
            verified._require_verified()
            if verified.role != role or verified.regime_id != regime_id:
                raise AnalysisArtifactError(
                    f"{role} evidence identifies the wrong primary regime"
                )
            current = verified.path
            references.append(
                _artifact_reference(current, role=role, regime_id=regime_id)
            )
            referenced_paths.append(current)
    calibration_artifacts = tuple(calibration_evidence_paths)
    if not calibration_artifacts:
        raise AnalysisArtifactError("at least one meter calibration artifact is required")
    calibration_paths: list[Path] = []
    for artifact in calibration_artifacts:
        require_verified = getattr(artifact, "_require_verified", None)
        current = getattr(artifact, "path", None)
        if not callable(require_verified) or not isinstance(current, Path):
            raise AnalysisArtifactError(
                "meter calibration evidence must be factory-issued from raw trials"
            )
        require_verified()
        calibration_paths.append(current)
        references.append(_artifact_reference(current, role="METER_CALIBRATION"))
        referenced_paths.append(current)
    digests = [item.sha256 for item in references]
    if len(digests) != len(set(digests)):
        raise AnalysisArtifactError(
            "preregistration evidence artifacts must have distinct content digests"
        )
    document = {
        "schema_version": 1,
        "kind": "WATTCH_STUDY_PREREGISTRATION",
        "sealed_at_unix_ns": sealed_at_unix_ns,
        "plan": plan.to_dict(),
        "referenced_artifacts": [item.to_dict() for item in references],
    }
    target = _write_one(path, _json_bytes(document))
    return load_study_preregistration(
        target,
        referenced_paths=referenced_paths,
        verified_prestudy_evidence=(
            *pilot_evidence_paths.values(),
            *aa_evidence_paths.values(),
            *placebo_evidence_paths.values(),
        ),
        verified_meter_calibrations=calibration_artifacts,
    )


def load_study_preregistration(
    path: str | Path,
    *,
    referenced_paths: Iterable[str | Path],
    verified_prestudy_evidence: Iterable[VerifiedPrestudyEvidenceArtifact] = (),
    verified_meter_calibrations: Iterable[object] = (),
) -> VerifiedStudyPreregistration:
    """Verify a sealed preregistration and all content-addressed inputs."""

    root = Path(path)
    document = _read_json_artifact(root)
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "WATTCH_STUDY_PREREGISTRATION"
    ):
        raise AnalysisArtifactError("unsupported preregistration artifact schema")
    sealed_at = document.get("sealed_at_unix_ns")
    if isinstance(sealed_at, bool) or not isinstance(sealed_at, int) or sealed_at < 0:
        raise AnalysisArtifactError("sealed preregistration timestamp is invalid")
    plan = _plan_from_dict(document.get("plan"))
    raw_references = document.get("referenced_artifacts")
    if not isinstance(raw_references, list) or not raw_references:
        raise AnalysisArtifactError("sealed preregistration has no artifact references")
    references: list[SealedArtifactReference] = []
    for item in raw_references:
        if not isinstance(item, Mapping):
            raise AnalysisArtifactError("preregistration artifact reference is malformed")
        references.append(
            SealedArtifactReference(
                role=_require_text(item.get("role"), "artifact role"),
                regime_id=(
                    None
                    if item.get("regime_id") is None
                    else _require_text(item.get("regime_id"), "artifact regime_id")
                ),
                sha256=_require_sha256(item.get("sha256"), "artifact sha256"),
                bytes=int(item.get("bytes", -1)),
            )
        )
    if len({item.sha256 for item in references}) != len(references):
        raise AnalysisArtifactError("sealed artifact digests must be unique")
    actual_paths = tuple(Path(item) for item in referenced_paths)
    actual_by_digest: dict[str, Path] = {}
    for actual_path in actual_paths:
        digest = _file_sha256(actual_path)
        if digest in actual_by_digest:
            raise AnalysisArtifactError("referenced evidence paths duplicate content")
        actual_by_digest[digest] = actual_path
    if set(actual_by_digest) != {item.sha256 for item in references}:
        raise AnalysisArtifactError(
            "supplied evidence paths do not exactly match sealed content addresses"
        )
    documents: dict[str, Mapping[str, Any]] = {}
    for reference in references:
        actual_path = actual_by_digest[reference.sha256]
        if actual_path.stat().st_size != reference.bytes:
            raise AnalysisArtifactError(
                f"evidence size disagrees for digest {reference.sha256}"
            )
        documents[reference.sha256] = _read_json_artifact(actual_path)
    verified_prestudy_by_digest: dict[str, VerifiedPrestudyEvidenceArtifact] = {}
    for artifact in verified_prestudy_evidence:
        if not isinstance(artifact, VerifiedPrestudyEvidenceArtifact):
            raise AnalysisArtifactError(
                "verified_prestudy_evidence contains a non-factory artifact"
            )
        artifact._require_verified()
        verified_prestudy_by_digest[artifact.sha256] = artifact
    verified_calibration_by_digest: dict[str, object] = {}
    for artifact in verified_meter_calibrations:
        require_verified = getattr(artifact, "_require_verified", None)
        digest = getattr(artifact, "sha256", None)
        if not callable(require_verified) or not isinstance(digest, str):
            raise AnalysisArtifactError(
                "verified_meter_calibrations contains a non-factory artifact"
            )
        require_verified()
        verified_calibration_by_digest[digest] = artifact
    schedule_documents = tuple(
        (item.sha256, documents[item.sha256])
        for item in references
        if item.role == "CONFIRMATORY_SCHEDULE"
    )
    if not schedule_documents:
        raise AnalysisArtifactError("sealed preregistration has no confirmatory schedule")
    _validate_schedule_plan(schedule_documents, plan)
    placebo_checks: list[PlaceboZeroCheck] = []
    meaningful_thresholds: list[tuple[str, str, float]] = []
    for regime in plan.primary_regimes:
        role_documents: dict[str, Mapping[str, Any]] = {}
        for role in ("DISCARDED_PILOT", "AA_CALIBRATION", "PLACEBO"):
            matched = [
                item
                for item in references
                if item.role == role and item.regime_id == regime.regime_id
            ]
            if len(matched) != 1:
                raise AnalysisArtifactError(
                    f"regime {regime.regime_id!r} needs exactly one {role} artifact"
                )
            role_documents[role] = documents[matched[0].sha256]
            verified = verified_prestudy_by_digest.get(matched[0].sha256)
            if (
                verified is None
                or verified.role != role
                or verified.regime_id != regime.regime_id
            ):
                raise AnalysisArtifactError(
                    f"{role} evidence must be replay-verified from its raw trials"
                )
        pilot = role_documents["DISCARDED_PILOT"]
        aa = role_documents["AA_CALIBRATION"]
        placebo = role_documents["PLACEBO"]
        _require_evidence_identity(pilot, "DISCARDED_PILOT", regime.regime_id)
        _require_evidence_identity(aa, "AA_CALIBRATION", regime.regime_id)
        _require_evidence_identity(placebo, "PLACEBO", regime.regime_id)
        for role, prestudy in role_documents.items():
            if (
                prestudy.get("derivation") != "REPLAY_VERIFIED_TRIALS"
                or prestudy.get("estimand") != regime.estimand
                or prestudy.get("context") != regime.context
            ):
                raise AnalysisArtifactError(
                    f"{role} evidence does not match the preregistered regime"
                )
        pilot_ids = pilot.get("block_ids")
        pilot_differences = pilot.get("paired_differences_joules")
        if not isinstance(pilot_ids, list) or tuple(pilot_ids) != regime.pilot_block_ids:
            raise AnalysisArtifactError(
                f"regime {regime.regime_id!r} pilot artifact changed its block ids"
            )
        if not isinstance(pilot_differences, list):
            raise AnalysisArtifactError("pilot paired differences must be a list")
        aa_differences = aa.get("paired_differences_joules")
        aa_controls = aa.get("control_joules")
        if not isinstance(aa_differences, list) or not isinstance(aa_controls, list):
            raise AnalysisArtifactError("A/A artifact must contain paired and control data")
        aa_parameters = aa.get("parameters")
        if not isinstance(aa_parameters, Mapping):
            raise AnalysisArtifactError("A/A artifact parameters are malformed")
        threshold = calibrate_aa_threshold(
            aa_paired_differences_joules=aa_differences,
            control_joules=aa_controls,
            relative_floor_fraction=float(
                aa_parameters.get("relative_floor_fraction", 0.05)
            ),
            uncertainty_multiplier=float(
                aa_parameters.get("uncertainty_multiplier", 2.0)
            ),
            uncertainty_quantile=float(
                aa_parameters.get("uncertainty_quantile", 0.95)
            ),
        )
        meaningful_thresholds.append(
            (regime.estimand, regime.context, threshold.meaningful_effect_joules)
        )
        recomputed_size = paired_normal_sample_size(
            pilot_differences,
            aa_threshold=threshold,
            power=regime.sample_size.power,
            two_sided_alpha=regime.sample_size.two_sided_alpha,
        )
        if recomputed_size != regime.sample_size:
            raise AnalysisArtifactError(
                f"regime {regime.regime_id!r} sample size does not match sealed "
                "pilot and A/A evidence"
            )
        placebo_differences = placebo.get("paired_differences_joules")
        if not isinstance(placebo_differences, list):
            raise AnalysisArtifactError("placebo paired differences must be a list")
        placebo_parameters = placebo.get("parameters")
        if not isinstance(placebo_parameters, Mapping):
            raise AnalysisArtifactError("placebo artifact parameters are malformed")
        placebo_checks.append(
            check_placebo_zero(
                placebo_differences,
                meaningful_threshold_joules=threshold.meaningful_effect_joules,
                confidence=float(placebo_parameters.get("confidence", 0.95)),
            )
        )
    calibrations = [
        (item.sha256, documents[item.sha256])
        for item in references
        if item.role == "METER_CALIBRATION"
    ]
    if not calibrations or any(
        item.get("kind") != "METER_CALIBRATION" for _, item in calibrations
    ):
        raise AnalysisArtifactError(
            "every meter calibration reference must be a METER_CALIBRATION artifact"
        )
    for digest, calibration in calibrations:
        if digest not in verified_calibration_by_digest:
            raise AnalysisArtifactError(
                "meter calibration must be factory-verified from raw trials"
            )
        _validate_calibration_document(calibration, digest=digest)
    return VerifiedStudyPreregistration._issue(
        plan=plan,
        sealed_at_unix_ns=sealed_at,
        artifact_sha256=_file_sha256(root),
        referenced_artifacts=tuple(references),
        placebo_checks=tuple(placebo_checks),
        meaningful_thresholds=tuple(meaningful_thresholds),
        schedule_documents=schedule_documents,
        calibration_documents=tuple(calibrations),
    )


def _require_evidence_identity(
    document: Mapping[str, Any], role: str, regime_id: str
) -> None:
    if document.get("kind") != role or document.get("regime_id") != regime_id:
        raise AnalysisArtifactError(
            f"{role} artifact does not identify regime {regime_id!r}"
        )


def _validate_calibration_document(
    document: Mapping[str, Any], *, digest: str
) -> None:
    required = {
        "schema_version",
        "kind",
        "calibration_id",
        "derivation",
        "no_collector_runtime_sha256",
        "measurement_boundary",
        "measurement_boundary_sha256",
        "source_topology",
        "source_topology_sha256",
        "collector_identity",
        "collector_identity_sha256",
        "regime_contract_sha256",
        "selected_interval_ns",
        "required_regimes",
        "selected_metrics",
        "thresholds",
        "sampling_observations",
        "trial_evidence",
    }
    if set(document) != required:
        raise AnalysisArtifactError(
            f"meter calibration {digest!r} must contain exactly {sorted(required)}"
        )
    if (
        document.get("schema_version") != 3
        or document.get("kind") != "METER_CALIBRATION"
        or document.get("derivation") != "REPLAY_VERIFIED_CALIBRATION_TRIALS"
    ):
        raise AnalysisArtifactError("unsupported meter calibration schema")
    _require_text(document.get("calibration_id"), "meter calibration_id")
    _require_sha256(
        document.get("no_collector_runtime_sha256"),
        "meter no-collector runtime digest",
    )
    boundary = document.get("measurement_boundary")
    if not isinstance(boundary, Mapping):
        raise AnalysisArtifactError("meter calibration boundary must be an object")
    MeasurementBoundary.from_dict(boundary)
    if document.get("measurement_boundary_sha256") != _canonical_sha256(boundary):
        raise AnalysisArtifactError("meter calibration boundary digest is invalid")
    topology = document.get("source_topology")
    if not isinstance(topology, list) or not topology or not all(
        isinstance(item, Mapping) for item in topology
    ):
        raise AnalysisArtifactError("meter calibration source topology is invalid")
    if document.get("source_topology_sha256") != _canonical_sha256(topology):
        raise AnalysisArtifactError("meter calibration source topology digest is invalid")
    collector = document.get("collector_identity")
    if not isinstance(collector, Mapping) or document.get(
        "collector_identity_sha256"
    ) != _canonical_sha256(collector):
        raise AnalysisArtifactError("meter calibration collector identity is invalid")
    regime_contracts = document.get("regime_contract_sha256")
    if not isinstance(regime_contracts, Mapping) or set(regime_contracts) != {
        "IDLE",
        "FIXED_LOAD",
        "AA",
    }:
        raise AnalysisArtifactError("meter calibration regime contracts are invalid")
    regime_digests = [
        _require_sha256(value, f"meter {regime} regime contract")
        for regime, value in regime_contracts.items()
    ]
    if len(regime_digests) != len(set(regime_digests)):
        raise AnalysisArtifactError("meter calibration regime contracts must be distinct")
    interval = document.get("selected_interval_ns")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise AnalysisArtifactError("meter calibration selected_interval_ns must be positive")
    regimes = document.get("required_regimes")
    if not isinstance(regimes, list) or not {
        "IDLE",
        "FIXED_LOAD",
        "AA",
    }.issubset(regimes):
        raise AnalysisArtifactError(
            "meter calibration must bind IDLE, FIXED_LOAD, and AA regimes"
        )
    if len(regimes) != len(set(regimes)):
        raise AnalysisArtifactError("meter calibration required_regimes must be unique")
    observations = document.get("sampling_observations")
    trial_evidence = document.get("trial_evidence")
    if not isinstance(observations, list) or not observations:
        raise AnalysisArtifactError("meter calibration observations must be non-empty")
    if not isinstance(trial_evidence, list) or not trial_evidence:
        raise AnalysisArtifactError("meter calibration trial evidence must be non-empty")
    selected = document.get("selected_metrics")
    thresholds = document.get("thresholds")
    if not isinstance(selected, Mapping) or not isinstance(thresholds, Mapping):
        raise AnalysisArtifactError("meter calibration metrics and thresholds must be objects")
    pairs = (
        ("measurement_overhead_ratio", "max_measurement_overhead_ratio", 0.01),
        ("missing_sample_ratio", "max_missing_sample_ratio", 0.001),
        ("phase_sensitivity_ratio", "max_phase_sensitivity_ratio", None),
    )
    for selected_name, threshold_name, hard_maximum in pairs:
        selected_value = _require_number(
            selected.get(selected_name), f"meter calibration {selected_name}"
        )
        threshold_value = _require_number(
            thresholds.get(threshold_name), f"meter calibration {threshold_name}"
        )
        if selected_value < 0 or threshold_value < 0:
            raise AnalysisArtifactError("meter calibration ratios must be non-negative")
        if selected_value >= threshold_value:
            raise AnalysisArtifactError(
                f"meter calibration {selected_name} must be strictly below its threshold"
            )
        if hard_maximum is not None and threshold_value > hard_maximum:
            raise AnalysisArtifactError(
                f"meter calibration {threshold_name} exceeds the study maximum"
            )
    pairing_gap = _require_number(
        thresholds.get("max_pairing_gap_s"), "meter calibration max_pairing_gap_s"
    )
    if pairing_gap <= 0:
        raise AnalysisArtifactError("meter calibration pairing gap must be positive")


def _validate_trial_calibration(
    document: Mapping[str, Any],
    *,
    calibration_digest: str,
    trial_id: str,
    trial_labels: Mapping[str, Any],
    boundary_document: Mapping[str, Any],
    quality: Mapping[str, Any],
    source_topology: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    _validate_calibration_document(document, digest=calibration_digest)
    if document.get("measurement_boundary") != dict(boundary_document):
        raise AnalysisArtifactError(
            f"trial {trial_id!r} uses a calibration for another boundary"
        )
    topology_document = list(source_topology)
    if (
        document.get("source_topology") != topology_document
        or document.get("source_topology_sha256")
        != _canonical_sha256(topology_document)
    ):
        raise AnalysisArtifactError(
            f"trial {trial_id!r} changed the calibrated source topology"
        )
    collector = document.get("collector_identity")
    if not isinstance(collector, Mapping):  # pragma: no cover - validated above
        raise AnalysisArtifactError("meter calibration collector identity is invalid")
    try:
        trial_host = validate_host_provenance(result.get("host_provenance"))
    except ValueError as exc:  # pragma: no cover - checked by bundle construction
        raise AnalysisArtifactError("trial host provenance is invalid") from exc
    trial_collector = {
        "node_id": _require_text(manifest.get("node_id"), "trial node_id"),
        "protocol_version": _require_text(
            manifest.get("protocol_version"), "trial protocol_version"
        ),
        "wattch_version": _require_text(
            manifest.get("wattch_version"), "trial wattch_version"
        ),
        "host_identity_sha256": trial_host["identity_sha256"],
    }
    if dict(collector) != trial_collector:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} changed the calibrated collector identity"
        )
    if trial_labels.get("measurement_overhead_calibration_sha256") != calibration_digest:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} labels do not seal the calibration digest"
        )
    try:
        interval_ns = int(trial_labels["interval_ms"]) * 1_000_000
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} has an invalid sampling interval label"
        ) from exc
    if interval_ns != document.get("selected_interval_ns"):
        raise AnalysisArtifactError(
            f"trial {trial_id!r} did not use the calibrated sampling interval"
        )
    selected = document.get("selected_metrics")
    if not isinstance(selected, Mapping):  # pragma: no cover - validated above
        raise AnalysisArtifactError("meter calibration selected_metrics is invalid")
    overhead = _require_number(
        quality.get("measurement_overhead_ratio"),
        f"trial {trial_id!r} quality measurement_overhead_ratio",
    )
    calibrated_overhead = _require_number(
        selected.get("measurement_overhead_ratio"),
        "meter calibration measurement_overhead_ratio",
    )
    if not math.isclose(
        overhead, calibrated_overhead, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise AnalysisArtifactError(
            f"trial {trial_id!r} overhead scalar differs from its calibration artifact"
        )


def _prelaunch_temperature(
    evidence: object,
    *,
    source_uid: str,
    trial_id: str,
) -> tuple[float, str]:
    descriptors = tuple(getattr(evidence, "descriptors", ()))
    descriptor = next(
        (item for item in descriptors if item.source_uid == source_uid), None
    )
    if descriptor is None:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} temperature source is absent from its topology"
        )
    metric_label = descriptor.labels.get("wattch.metric")
    if descriptor.unit != "CELSIUS" or metric_label != "temperature":
        raise AnalysisArtifactError(
            f"trial {trial_id!r} prelaunch source is not declared Celsius temperature telemetry"
        )
    spawn = next(
        (
            event
            for event in getattr(evidence, "events", ())
            if event.kind == "TARGET_SPAWN"
        ),
        None,
    )
    if spawn is None:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} has no daemon-stamped target spawn"
        )
    candidates = [
        sample
        for sample in getattr(evidence, "samples", ())
        if sample.source_id == descriptor.local_source_id
        and sample.monotonic_ns <= spawn.monotonic_ns
        and not isinstance(sample.value, (bool, bytes))
    ]
    if not candidates:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} has no prelaunch temperature sample"
        )
    value = float(max(candidates, key=lambda item: item.monotonic_ns).value)
    if not math.isfinite(value) or not -100 <= value <= 250:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} prelaunch temperature is invalid"
        )
    return value, _temperature_bin_celsius(value)


def _temperature_bin_celsius(value: float) -> str:
    lower = math.floor(value / 5.0) * 5
    return f"[{lower},{lower + 5})"


def _schedule_trial_index(
    schedule_documents: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for schedule_sha256, schedule in schedule_documents:
        trials = schedule.get("trials")
        if not isinstance(trials, list):
            raise AnalysisArtifactError("sealed schedule trials must be a list")
        for trial in trials:
            if not isinstance(trial, Mapping):
                raise AnalysisArtifactError("sealed schedule trial must be an object")
            metadata = trial.get("trial_metadata")
            if not isinstance(metadata, Mapping):
                raise AnalysisArtifactError("sealed schedule trial metadata is missing")
            trial_id = _require_text(metadata.get("trial_id"), "scheduled trial_id")
            if trial_id in index:
                raise AnalysisArtifactError(
                    f"trial id {trial_id!r} occurs in more than one sealed schedule"
                )
            index[trial_id] = (schedule_sha256, trial)
    return index


def _strict_schedule_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisArtifactError(f"{field_name} must be an integer")
    return value


def _assert_floor_ceil_balance(
    counts: Sequence[int], *, total: int, label: str
) -> None:
    if not counts:
        raise AnalysisArtifactError(f"{label} has an empty balance domain")
    floor, remainder = divmod(total, len(counts))
    expected = sorted(
        [floor] * (len(counts) - remainder) + [floor + 1] * remainder
    )
    if sorted(counts) != expected:
        raise AnalysisArtifactError(f"{label} is not position-balanced")


def _audit_schedule_document(
    digest: str, schedule: Mapping[str, Any]
) -> dict[str, object]:
    """Reconstruct and validate a schedule's randomized block contract."""

    experiment = schedule.get("experiment")
    trials = schedule.get("trials")
    block_summaries = schedule.get("blocks")
    if (
        not isinstance(experiment, Mapping)
        or not isinstance(trials, list)
        or not isinstance(block_summaries, list)
    ):
        raise AnalysisArtifactError(f"sealed schedule {digest!r} is malformed")
    if experiment.get("schema_version") != 1 or experiment.get("confirmatory") is not True:
        raise AnalysisArtifactError("sealed schedules must be confirmatory schema v1")
    experiment_id = _require_text(
        experiment.get("experiment_id"), "scheduled experiment_id"
    )
    block_count = _strict_schedule_int(
        experiment.get("blocks"), "scheduled block count"
    )
    if block_count <= 0:
        raise AnalysisArtifactError("scheduled block count must be positive")
    policies_value = experiment.get("mechanism_policies")
    nice_value = experiment.get("target_nice_levels")
    if (
        not isinstance(policies_value, list)
        or not policies_value
        or not all(isinstance(value, str) and value for value in policies_value)
        or len(policies_value) != len(set(policies_value))
        or not isinstance(nice_value, list)
        or not nice_value
    ):
        raise AnalysisArtifactError("scheduled policy or nice-level domain is invalid")
    policies = tuple(policies_value)
    nice_levels = tuple(
        _strict_schedule_int(value, "scheduled target nice level")
        for value in nice_value
    )
    if len(nice_levels) != len(set(nice_levels)):
        raise AnalysisArtifactError("scheduled target nice levels must be unique")
    if len(trials) != block_count * len(policies) * 3:
        raise AnalysisArtifactError("scheduled trial count disagrees with its factors")

    parsed: list[dict[str, object]] = []
    regime_key: tuple[str, str] | None = None
    for scheduled in trials:
        if not isinstance(scheduled, Mapping):
            raise AnalysisArtifactError("sealed schedule trial is malformed")
        metadata = scheduled.get("trial_metadata")
        if not isinstance(metadata, Mapping):
            raise AnalysisArtifactError("scheduled trial metadata is missing")
        labels = metadata.get("labels")
        estimand = metadata.get("estimand")
        if not isinstance(labels, Mapping) or not isinstance(estimand, Mapping):
            raise AnalysisArtifactError("scheduled trial labels or estimand are missing")
        current_regime = (
            _require_text(estimand.get("kind"), "scheduled estimand"),
            _require_text(
                metadata.get("background_workload_id"), "scheduled context"
            ),
        )
        if regime_key is None:
            regime_key = current_regime
        elif regime_key != current_regime:
            raise AnalysisArtifactError("one schedule cannot mix primary regimes")
        if metadata.get("experiment_id") != experiment_id:
            raise AnalysisArtifactError("scheduled trial changed its experiment id")
        block_index = _strict_schedule_int(
            scheduled.get("block_index"), "scheduled block_index"
        )
        order_position = _strict_schedule_int(
            scheduled.get("order_position"), "scheduled order_position"
        )
        mechanism_position = _strict_schedule_int(
            scheduled.get("mechanism_position"), "scheduled mechanism_position"
        )
        target_nice = _strict_schedule_int(
            scheduled.get("target_nice"), "scheduled target_nice"
        )
        replay_seed = _strict_schedule_int(
            metadata.get("replay_seed"), "scheduled replay_seed"
        )
        policy = _require_text(
            scheduled.get("mechanism_policy"), "scheduled mechanism policy"
        )
        arm = _require_text(labels.get("arm_code"), "scheduled arm")
        block_id = _require_text(
            metadata.get("matched_block_id"), "scheduled matched_block_id"
        )
        if (
            not 1 <= block_index <= block_count
            or order_position not in {1, 2, 3}
            or not 1 <= mechanism_position <= len(policies)
            or policy not in policies
            or target_nice not in nice_levels
            or arm not in {"C0", "C1", "C2"}
        ):
            raise AnalysisArtifactError("scheduled factor assignment is out of domain")
        expected_labels = {
            "order_position": str(order_position),
            "mechanism_position": str(mechanism_position),
            "mechanism_policy": policy,
            "target_nice": str(target_nice),
        }
        if any(labels.get(name) != value for name, value in expected_labels.items()):
            raise AnalysisArtifactError(
                "scheduled operational factors disagree with trial metadata labels"
            )
        if estimand.get("kind") == "FIXED_HORIZON":
            horizon_ns = _strict_schedule_int(
                estimand.get("horizon_ns"), "scheduled fixed horizon"
            )
            try:
                tolerance_ns = int(
                    _require_text(
                        labels.get("fixed_horizon_tolerance_ns"),
                        "scheduled fixed-horizon tolerance",
                    )
                )
            except ValueError as exc:
                raise AnalysisArtifactError(
                    "scheduled fixed-horizon tolerance is invalid"
                ) from exc
            if tolerance_ns <= 0 or tolerance_ns > max(
                1, math.floor(horizon_ns * 0.01)
            ):
                raise AnalysisArtifactError(
                    "scheduled fixed-horizon tolerance exceeds 1% of its horizon"
                )
        parsed.append(
            {
                "scheduled": scheduled,
                "block_index": block_index,
                "block_id": block_id,
                "order_position": order_position,
                "mechanism_position": mechanism_position,
                "policy": policy,
                "arm": arm,
                "target_nice": target_nice,
                "replay_seed": replay_seed,
            }
        )

    ordered = sorted(
        parsed,
        key=lambda item: (
            int(item["block_index"]),
            int(item["mechanism_position"]),
            int(item["order_position"]),
        ),
    )
    if [item["scheduled"] for item in ordered] != trials:
        raise AnalysisArtifactError("sealed schedule trial execution order is inconsistent")

    reconstructed: list[dict[str, object]] = []
    arm_position_counts = {
        (policy, arm, position): 0
        for policy in policies
        for arm in ("C0", "C1", "C2")
        for position in (1, 2, 3)
    }
    policy_position_counts = {
        (policy, position): 0
        for policy in policies
        for position in range(1, len(policies) + 1)
    }
    nice_counts = {level: 0 for level in nice_levels}
    for block_index in range(1, block_count + 1):
        block = [item for item in parsed if item["block_index"] == block_index]
        if len(block) != len(policies) * 3:
            raise AnalysisArtifactError("scheduled parent block is incomplete")
        block_ids = {str(item["block_id"]) for item in block}
        seeds = {int(item["replay_seed"]) for item in block}
        block_nice = {int(item["target_nice"]) for item in block}
        if len(block_ids) != 1 or len(seeds) != 1 or len(block_nice) != 1:
            raise AnalysisArtifactError(
                "scheduled parent block changed id, replay seed, or nice level"
            )
        policy_cells: list[dict[str, object]] = []
        seen_mechanism_positions: set[int] = set()
        for policy in policies:
            cell = [item for item in block if item["policy"] == policy]
            if (
                len(cell) != 3
                or {str(item["arm"]) for item in cell} != {"C0", "C1", "C2"}
                or {int(item["order_position"]) for item in cell} != {1, 2, 3}
                or len({int(item["mechanism_position"]) for item in cell}) != 1
            ):
                raise AnalysisArtifactError(
                    f"schedule block {next(iter(block_ids))!r}/{policy!r} must "
                    "contain one arm at every order position"
                )
            mechanism_position = int(cell[0]["mechanism_position"])
            seen_mechanism_positions.add(mechanism_position)
            policy_position_counts[(policy, mechanism_position)] += 1
            for item in cell:
                arm_position_counts[
                    (policy, str(item["arm"]), int(item["order_position"]))
                ] += 1
            policy_cells.append(
                {
                    "mechanism_policy": policy,
                    "mechanism_position": mechanism_position,
                    "order": [
                        str(item["arm"])
                        for item in sorted(
                            cell, key=lambda value: int(value["order_position"])
                        )
                    ],
                }
            )
        if seen_mechanism_positions != set(range(1, len(policies) + 1)):
            raise AnalysisArtifactError(
                "scheduled policies do not occupy unique mechanism positions"
            )
        nice = next(iter(block_nice))
        nice_counts[nice] += 1
        reconstructed.append(
            {
                "block_index": block_index,
                "matched_block_id": next(iter(block_ids)),
                "replay_seed": next(iter(seeds)),
                "target_nice": nice,
                "policy_cells": sorted(
                    policy_cells, key=lambda item: int(item["mechanism_position"])
                ),
            }
        )
    if block_summaries != reconstructed:
        raise AnalysisArtifactError("sealed schedule block summaries are inconsistent")
    for policy in policies:
        for arm in ("C0", "C1", "C2"):
            _assert_floor_ceil_balance(
                [arm_position_counts[(policy, arm, position)] for position in (1, 2, 3)],
                total=block_count,
                label=f"{policy}/{arm} schedule order",
            )
        _assert_floor_ceil_balance(
            [
                policy_position_counts[(policy, position)]
                for position in range(1, len(policies) + 1)
            ],
            total=block_count,
            label=f"{policy} mechanism position",
        )
    _assert_floor_ceil_balance(
        [nice_counts[level] for level in nice_levels],
        total=block_count,
        label="target nice schedule",
    )
    if regime_key is None:  # pragma: no cover - positive count above
        raise AnalysisArtifactError("sealed schedule is empty")
    return {
        "regime_key": regime_key,
        "policies": policies,
        "nice_levels": nice_levels,
        "block_count": block_count,
        "arm_position_counts": arm_position_counts,
        "policy_position_counts": policy_position_counts,
        "nice_counts": nice_counts,
    }


def _validate_schedule_plan(
    schedule_documents: Sequence[tuple[str, Mapping[str, Any]]],
    plan: StudyPreregistration,
) -> None:
    scheduled_by_regime: dict[tuple[str, str], dict[str, list[Mapping[str, Any]]]] = {}
    audits: list[dict[str, object]] = []
    for digest, schedule in schedule_documents:
        audits.append(_audit_schedule_document(digest, schedule))
        trials = schedule.get("trials")
        if not isinstance(trials, list):
            raise AnalysisArtifactError("sealed schedule trials must be a list")
        for scheduled in trials:
            if not isinstance(scheduled, Mapping):
                raise AnalysisArtifactError("sealed schedule trial is malformed")
            metadata = scheduled.get("trial_metadata")
            if not isinstance(metadata, Mapping):
                raise AnalysisArtifactError("scheduled trial metadata is missing")
            estimand = metadata.get("estimand")
            if not isinstance(estimand, Mapping):
                raise AnalysisArtifactError("scheduled estimand is missing")
            key = (
                _require_text(estimand.get("kind"), "scheduled estimand"),
                _require_text(
                    metadata.get("background_workload_id"), "scheduled context"
                ),
            )
            block_id = _require_text(
                metadata.get("matched_block_id"), "scheduled matched_block_id"
            )
            scheduled_by_regime.setdefault(key, {}).setdefault(block_id, []).append(
                scheduled
            )
    expected = {
        (regime.estimand, regime.context): set(regime.confirmatory_block_ids)
        for regime in plan.primary_regimes
    }
    if set(scheduled_by_regime) != set(expected):
        raise AnalysisArtifactError(
            "sealed schedules do not exactly cover preregistered primary regimes"
        )
    for regime_key, blocks in scheduled_by_regime.items():
        if set(blocks) != expected[regime_key]:
            raise AnalysisArtifactError(
                f"sealed schedule blocks differ for primary regime {regime_key!r}"
            )
        for block_id, trials in blocks.items():
            by_policy: dict[str, list[Mapping[str, Any]]] = {}
            for scheduled in trials:
                policy = _require_text(
                    scheduled.get("mechanism_policy"), "scheduled mechanism policy"
                )
                by_policy.setdefault(policy, []).append(scheduled)
            for policy, cell in by_policy.items():
                arms = [
                    _require_text(item.get("trial_metadata", {}).get("labels", {}).get("arm_code"), "scheduled arm")
                    for item in cell
                ]
                if len(cell) != 3 or sorted(arms) != ["C0", "C1", "C2"]:
                    raise AnalysisArtifactError(
                        f"schedule block {block_id!r}/{policy!r} must contain exactly "
                        "one C0, C1, and C2 trial"
                    )
    audits_by_regime: dict[tuple[str, str], list[dict[str, object]]] = {}
    for audit in audits:
        audit_key = audit["regime_key"]
        if not isinstance(audit_key, tuple):  # pragma: no cover - locally issued
            raise AnalysisArtifactError("schedule audit lost its regime identity")
        audits_by_regime.setdefault(audit_key, []).append(audit)
    for regime_key, regime_audits in audits_by_regime.items():
        policies = tuple(regime_audits[0]["policies"])
        nice_levels = tuple(regime_audits[0]["nice_levels"])
        if any(
            tuple(item["policies"]) != policies
            or tuple(item["nice_levels"]) != nice_levels
            for item in regime_audits
        ):
            raise AnalysisArtifactError(
                f"split schedules changed factor domains for regime {regime_key!r}"
            )
        total_blocks = sum(int(item["block_count"]) for item in regime_audits)
        for policy in policies:
            for arm in ("C0", "C1", "C2"):
                counts = []
                for position in (1, 2, 3):
                    counts.append(
                        sum(
                            int(item["arm_position_counts"][(policy, arm, position)])
                            for item in regime_audits
                        )
                    )
                _assert_floor_ceil_balance(
                    counts,
                    total=total_blocks,
                    label=f"{regime_key}/{policy}/{arm} aggregate order",
                )
            _assert_floor_ceil_balance(
                [
                    sum(
                        int(item["policy_position_counts"][(policy, position)])
                        for item in regime_audits
                    )
                    for position in range(1, len(policies) + 1)
                ],
                total=total_blocks,
                label=f"{regime_key}/{policy} aggregate mechanism position",
            )
        _assert_floor_ceil_balance(
            [
                sum(int(item["nice_counts"][level]) for item in regime_audits)
                for level in nice_levels
            ],
            total=total_blocks,
            label=f"{regime_key} aggregate nice schedule",
        )


def _execution_contract_from_schedule(scheduled: Mapping[str, Any]) -> dict[str, object]:
    metadata = scheduled.get("trial_metadata")
    if not isinstance(metadata, Mapping):
        raise AnalysisArtifactError("scheduled execution contract has no trial metadata")
    labels = metadata.get("labels")
    if not isinstance(labels, Mapping):
        raise AnalysisArtifactError("scheduled execution contract has no labels")

    def expanded_command(value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise AnalysisArtifactError("scheduled command must be an object or null")
        document = dict(value)
        environment = document.get("env")
        if not isinstance(environment, Mapping):
            raise AnalysisArtifactError("scheduled command environment must be an object")
        replay_environment = dict(environment)
        replay_environment.update(
            {
                "WATTCH_MATCHED_BLOCK_ID": _require_text(
                    metadata.get("matched_block_id"), "scheduled matched_block_id"
                ),
                "WATTCH_REPLAY_SEED": str(metadata.get("replay_seed")),
                "WATTCH_INPUT_ID": _require_text(
                    metadata.get("input_id"), "scheduled input_id"
                ),
            }
        )
        arrival_trace_id = labels.get("arrival_trace_id")
        if isinstance(arrival_trace_id, str):
            replay_environment["WATTCH_ARRIVAL_TRACE_ID"] = arrival_trace_id
        document["env"] = dict(sorted(replay_environment.items()))
        return document

    return {
        "schema_version": 1,
        "target": expanded_command(scheduled.get("target")),
        "background": expanded_command(scheduled.get("background")),
        "target_nice": scheduled.get("target_nice"),
        "background_nice": scheduled.get("background_nice"),
        "cpu_affinity": scheduled.get("cpu_affinity"),
        "resource_limits": scheduled.get("resource_limits"),
    }


def _validate_scheduled_trial_metadata(
    trial: Mapping[str, Any],
    scheduled_value: object,
    *,
    trial_id: str,
) -> None:
    if not isinstance(scheduled_value, Mapping):
        raise AnalysisArtifactError("sealed schedule trial metadata is missing")
    actual = dict(trial)
    scheduled = dict(scheduled_value)
    actual_labels = actual.get("labels")
    scheduled_labels = scheduled.get("labels")
    if not isinstance(actual_labels, Mapping) or not isinstance(
        scheduled_labels, Mapping
    ):
        raise AnalysisArtifactError("scheduled and captured labels must be objects")
    extra_labels = set(actual_labels) - set(scheduled_labels)
    allowed_exact = {
        "execution_contract_sha256",
        "measurement_overhead_calibration_sha256",
        "study_preregistration_sha256",
        "workload_metrics_contract",
        "workload_metrics_path",
        "background_workload_metrics_path",
    }
    disallowed = {
        label
        for label in extra_labels
        if label not in allowed_exact and not str(label).startswith("host_policy_")
    }
    if disallowed:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} introduced unscheduled labels: "
            + ", ".join(sorted(disallowed))
        )
    if any(actual_labels.get(key) != value for key, value in scheduled_labels.items()):
        raise AnalysisArtifactError(
            f"trial {trial_id!r} changed a content-addressed schedule label"
        )
    actual["labels"] = {
        key: value for key, value in actual_labels.items() if key in scheduled_labels
    }
    if actual != scheduled:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} metadata differs from its sealed schedule"
        )


def _validate_execution_contract(
    execution: Mapping[str, Any],
    *,
    expected: Mapping[str, object],
    trial_id: str,
) -> None:
    allowed = {*expected, "launcher"}
    if set(execution) != allowed:
        raise AnalysisArtifactError(
            f"trial {trial_id!r} execution contract has unexpected fields"
        )
    launcher = execution.get("launcher")
    _require_text(launcher, f"trial {trial_id!r} launcher identity")
    for field_name, expected_value in expected.items():
        actual_value = execution.get(field_name)
        if field_name in {"target", "background"} and isinstance(
            actual_value, Mapping
        ) and isinstance(expected_value, Mapping):
            matches = _target_command_identity(actual_value) == _target_command_identity(
                expected_value
            )
        else:
            matches = actual_value == expected_value
        if not matches:
            raise AnalysisArtifactError(
                f"trial {trial_id!r} execution field {field_name!r} differs "
                "from its content-addressed schedule"
            )


def _validate_verified_trial_cells(
    references: Sequence[TrialEvidenceReference],
    plan: StudyPreregistration,
) -> None:
    cells: dict[tuple[str, str, str, str, str], list[TrialEvidenceReference]] = {}
    for item in references:
        key = (
            item.day_utc,
            item.estimand,
            item.context,
            item.matched_block_id,
            item.mechanism_policy,
        )
        cells.setdefault(key, []).append(item)
    for key, cell in cells.items():
        if len(cell) != 3 or {item.arm for item in cell} != {"C0", "C1", "C2"}:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} must contain exactly one C0/C1/C2 triple"
            )
        if len({item.match_fingerprint for item in cell}) != 1:
            raise AnalysisArtifactError(f"verified evidence cell {key!r} is not matched")
        if key[1] == "FIXED_HORIZON":
            tolerances = {item.fixed_horizon_tolerance_ns for item in cell}
            if len(tolerances) != 1 or None in tolerances:
                raise AnalysisArtifactError(
                    f"verified fixed-horizon cell {key!r} changed its tolerance"
                )
            tolerance_s = next(iter(tolerances)) / 1_000_000_000
            durations = [item.outcome_duration_s for item in cell]
            if max(durations) - min(durations) > tolerance_s + 1e-12:
                raise AnalysisArtifactError(
                    f"verified fixed-horizon cell {key!r} has unmatched observed windows"
                )
        if {item.order_position for item in cell} != {1, 2, 3}:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} lost its balanced arm positions"
            )
        if len({item.mechanism_position for item in cell}) != 1:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} mixes mechanism positions"
            )
        if len({item.target_nice for item in cell}) != 1:
            raise AnalysisArtifactError(f"verified evidence cell {key!r} mixes priorities")
        if len({item.cache_regime for item in cell}) != 1:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} mixes cache regimes"
            )
        if len({item.prelaunch_temperature_source_uid for item in cell}) != 1 or len(
            {item.prelaunch_temperature_bin_celsius for item in cell}
        ) != 1:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} is not blocked by initial temperature"
            )
        if len({item.host_policy_settings_fingerprint_sha256 for item in cell}) != 1:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} mixes host-policy settings"
            )
        if len({item.background_present for item in cell}) != 1 or len(
            {item.background_command_sha256 for item in cell}
        ) != 1:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} changes the executed background"
            )
        arrival_digests = {item.arrival_trace_sha256 for item in cell}
        if arrival_digests != {None} and (
            None in arrival_digests or len(arrival_digests) != 1
        ):
            raise AnalysisArtifactError(
                f"verified service cell {key!r} changed its offered-demand trace"
            )
        if len(
            {
                (
                    item.background_completed_work,
                    item.background_work_unit,
                    item.background_output_sha256,
                    item.background_arrival_trace_sha256,
                )
                for item in cell
            }
        ) != 1:
            raise AnalysisArtifactError(
                f"verified evidence cell {key!r} changed background completed work"
            )
    parent_blocks: dict[
        tuple[str, str, str, str], list[TrialEvidenceReference]
    ] = {}
    for item in references:
        parent_blocks.setdefault(
            (item.day_utc, item.estimand, item.context, item.matched_block_id), []
        ).append(item)
    for key, block in parent_blocks.items():
        if len({item.replay_seed for item in block}) != 1:
            raise AnalysisArtifactError(
                f"verified parent block {key!r} changed replay seed"
            )
        if len({item.target_nice for item in block}) != 1:
            raise AnalysisArtifactError(
                f"verified parent block {key!r} changed target priority"
            )
        policies = {item.mechanism_policy for item in block}
        positions = {
            next(
                item.mechanism_position
                for item in block
                if item.mechanism_policy == policy
            )
            for policy in policies
        }
        if positions != set(range(1, len(policies) + 1)):
            raise AnalysisArtifactError(
                f"verified parent block {key!r} lost mechanism-position balance"
            )
    evidence_by_regime: dict[tuple[str, str], set[str]] = {}
    for item in references:
        evidence_by_regime.setdefault((item.estimand, item.context), set()).add(
            item.matched_block_id
        )
    planned = {
        (regime.estimand, regime.context): set(regime.confirmatory_block_ids)
        for regime in plan.primary_regimes
    }
    if evidence_by_regime != planned:
        raise AnalysisArtifactError(
            "verified trials must exactly cover every preregistered confirmatory block"
        )


def _validate_arm_program_mapping(
    references: Sequence[TrialEvidenceReference],
    *,
    treatment_program_id: str,
    control_program_id: str,
) -> None:
    expected = {"C0": None, "C1": treatment_program_id, "C2": control_program_id}
    for item in references:
        if item.program_id != expected[item.arm]:
            raise AnalysisArtifactError(
                f"trial {item.trial_id!r} does not match the exact C0/C1/C2 "
                "program mapping"
            )
    for arm in ("C1", "C2"):
        identities = {
            (item.program_id, item.target_command_sha256, item.input_id)
            for item in references
            if item.arm == arm
        }
        if len(identities) != 1:
            raise AnalysisArtifactError(
                f"{arm}/{expected[arm]} changed target command or input identity "
                "across primary regimes"
            )


def _validate_reversal_context_roles(
    references: Sequence[TrialEvidenceReference],
    *,
    fixed_horizon_reversal: RankingReversalResult,
    equal_work_reversal: RankingReversalResult,
) -> None:
    pairs = {
        (
            reversal.isolated_effect.context,
            reversal.cotenanted_effect.context,
        )
        for reversal in (fixed_horizon_reversal, equal_work_reversal)
    }
    if len(pairs) != 1:
        raise AnalysisArtifactError(
            "fixed-horizon and equal-work reversals changed their context roles"
        )
    isolated_context, cotenanted_context = next(iter(pairs))
    if isolated_context == cotenanted_context:
        raise AnalysisArtifactError(
            "isolated and co-tenanted contexts must be distinct"
        )
    if {item.context for item in references} != {
        isolated_context,
        cotenanted_context,
    }:
        raise AnalysisArtifactError(
            "verified evidence contexts must exactly match the reversal contexts"
        )
    isolated = [item for item in references if item.context == isolated_context]
    cotenanted = [item for item in references if item.context == cotenanted_context]
    if not isolated or any(
        item.background_present or item.background_command_sha256 is not None
        for item in isolated
    ):
        raise AnalysisArtifactError(
            "the isolated reversal context must execute without a background command"
        )
    if not cotenanted or any(
        not item.background_present or item.background_command_sha256 is None
        for item in cotenanted
    ):
        raise AnalysisArtifactError(
            "the co-tenanted reversal context must execute a sealed background command"
        )


def _require_same_result(
    supplied: Mapping[str, object],
    recomputed: Mapping[str, object],
    field_name: str,
) -> None:
    if _canonical_sha256(supplied) != _canonical_sha256(recomputed):
        raise AnalysisArtifactError(
            f"supplied {field_name} does not match recomputation from sealed trials"
        )


def _recompute_reversal(
    supplied: RankingReversalResult,
    observations: Sequence[object],
    *,
    preregistration: VerifiedStudyPreregistration,
) -> RankingReversalResult:
    isolated = supplied.isolated_effect
    cotenanted = supplied.cotenanted_effect
    if not math.isclose(
        isolated.confidence, 0.95, rel_tol=0.0, abs_tol=1e-12
    ) or not math.isclose(
        cotenanted.confidence, 0.95, rel_tol=0.0, abs_tol=1e-12
    ):
        raise AnalysisArtifactError(
            "positive ranking-reversal claims require two-sided 95% confidence"
        )
    expected_isolated_threshold = preregistration._meaningful_threshold(
        isolated.estimand.value, isolated.context
    )
    expected_cotenanted_threshold = preregistration._meaningful_threshold(
        cotenanted.estimand.value, cotenanted.context
    )
    if not math.isclose(
        supplied.isolated_threshold_joules,
        expected_isolated_threshold,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ) or not math.isclose(
        supplied.cotenanted_threshold_joules,
        expected_cotenanted_threshold,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise AnalysisArtifactError(
            "ranking-reversal thresholds do not match sealed A/A evidence"
        )
    isolated_observations = tuple(
        item
        for item in observations
        if item.outcome.estimand == isolated.estimand
        and item.context == isolated.context
        and item.policy == isolated.policy
    )
    cotenanted_observations = tuple(
        item
        for item in observations
        if item.outcome.estimand == cotenanted.estimand
        and item.context == cotenanted.context
        and item.policy == cotenanted.policy
    )
    recomputed_isolated = paired_effect(
        isolated_observations,
        treatment_arm=isolated.treatment_arm,
        control_arm=isolated.control_arm,
        confidence=0.95,
    )
    recomputed_cotenanted = paired_effect(
        cotenanted_observations,
        treatment_arm=cotenanted.treatment_arm,
        control_arm=cotenanted.control_arm,
        confidence=0.95,
    )
    recomputed = detect_ranking_reversal(
        recomputed_isolated,
        recomputed_cotenanted,
        isolated_threshold_joules=supplied.isolated_threshold_joules,
        cotenanted_threshold_joules=supplied.cotenanted_threshold_joules,
        require_confidence_interval=True,
    )
    _require_same_result(supplied.to_dict(), recomputed.to_dict(), "ranking reversal")
    return recomputed


def _as_document(value: object, field_name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        document = to_dict()
        if isinstance(document, Mapping):
            return dict(document)
    raise AnalysisArtifactError(
        f"{field_name} must be a mapping or provide a mapping-valued to_dict()"
    )


def _json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise AnalysisArtifactError(f"artifact is not strict JSON: {exc}") from exc
    return text.encode("utf-8")


def _json_line(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AnalysisArtifactError(f"prediction is not strict JSON: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisArtifactError(f"{field_name} must be a non-empty string")
    return value


def _validate_unique_ids(
    values: object,
    field_name: str,
    *,
    minimum: int,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise AnalysisArtifactError(f"{field_name} must be a tuple of ids")
    identifiers = tuple(
        _require_text(value, f"{field_name}[{index}]")
        for index, value in enumerate(values)
    )
    if len(identifiers) < minimum:
        raise AnalysisArtifactError(
            f"{field_name} needs at least {minimum} unique ids"
        )
    if len(identifiers) != len(set(identifiers)):
        raise AnalysisArtifactError(f"{field_name} must contain unique ids")
    return identifiers


def _require_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisArtifactError(f"{field_name} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise AnalysisArtifactError(f"{field_name} must be finite")
    return converted


def _validate_estimated_result(result: Mapping[str, object], index: int) -> None:
    observed = _require_number(
        result.get("observed_joules"), f"prediction {index} observed_joules"
    )
    counterfactual = _require_number(
        result.get("counterfactual_joules"),
        f"prediction {index} counterfactual_joules",
    )
    impact = _require_number(
        result.get("impact_joules"), f"prediction {index} impact_joules"
    )
    if observed < 0 or counterfactual < 0:
        raise AnalysisArtifactError(
            f"prediction {index} observed and counterfactual energy must be non-negative"
        )
    if not math.isclose(
        impact,
        observed - counterfactual,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise AnalysisArtifactError(
            f"prediction {index} impact_joules must equal observed minus counterfactual"
        )
    interval = result.get("interval")
    if not isinstance(interval, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} ESTIMATED result needs an interval"
        )
    confidence = _require_number(
        interval.get("confidence_level"),
        f"prediction {index} interval confidence_level",
    )
    lower = _require_number(
        interval.get("lower_joules"),
        f"prediction {index} interval lower_joules",
    )
    upper = _require_number(
        interval.get("upper_joules"),
        f"prediction {index} interval upper_joules",
    )
    if not 0 < confidence < 1:
        raise AnalysisArtifactError(
            f"prediction {index} interval confidence_level must be between zero and one"
        )
    if lower > upper or not lower <= impact <= upper:
        raise AnalysisArtifactError(
            f"prediction {index} impact must lie inside its ordered interval"
        )
    validation_status = _require_text(
        result.get("validation_status"),
        f"prediction {index} validation_status",
    )
    if validation_status not in {"EXPLORATORY", "VALIDATED"}:
        raise AnalysisArtifactError(
            f"prediction {index} validation_status must be EXPLORATORY or VALIDATED"
        )


def _prediction_path(
    document: Mapping[str, object],
    result: Mapping[str, object],
    estimand: CounterfactualEstimand,
    index: int,
) -> None:
    path = document.get("prediction_path")
    if not isinstance(path, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} must preserve its no-program prediction_path"
        )
    if path.get("integration_method") != "TRAPEZOIDAL":
        raise AnalysisArtifactError(
            f"prediction {index} must declare TRAPEZOIDAL path integration"
        )
    timestamps = path.get("timestamps_ns")
    if not isinstance(timestamps, list) or len(timestamps) < 2:
        raise AnalysisArtifactError(
            f"prediction {index} prediction_path needs at least two timestamps"
        )
    if any(
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        for timestamp in timestamps
    ) or any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise AnalysisArtifactError(
            f"prediction {index} prediction_path timestamps must strictly increase"
        )
    if (
        estimand.kind == "FIXED_HORIZON"
        and timestamps[-1] - timestamps[0] != estimand.horizon_ns
    ):
        raise AnalysisArtifactError(
            f"prediction {index} path duration does not match its fixed horizon"
        )

    series: dict[str, list[object]] = {}
    for field_name in (
        "predicted_power_watts",
        "lower_power_watts",
        "upper_power_watts",
    ):
        values = path.get(field_name)
        if not isinstance(values, list):
            raise AnalysisArtifactError(
                f"prediction {index} prediction_path.{field_name} must be a list"
            )
        series[field_name] = values

    provenance = path.get("training_provenance")
    if not isinstance(provenance, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} must preserve training provenance"
        )
    episode_ids = provenance.get("control_episode_ids")
    if (
        not isinstance(episode_ids, list)
        or not episode_ids
        or any(not isinstance(item, str) or not item.strip() for item in episode_ids)
        or len(set(episode_ids)) != len(episode_ids)
    ):
        raise AnalysisArtifactError(
            f"prediction {index} needs unique non-empty control episode ids"
        )
    semantic_rebind = provenance.get("semantic_rebind")
    if semantic_rebind is not None and not isinstance(semantic_rebind, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} semantic_rebind must be an object or null"
        )
    split_validation = provenance.get("group_split_validation")
    if split_validation is not None:
        if not isinstance(split_validation, Mapping):
            raise AnalysisArtifactError(
                f"prediction {index} group_split_validation must be an object or null"
            )
        for count_name in (
            "train_rows",
            "test_rows",
            "train_groups",
            "test_groups",
        ):
            count = split_validation.get(count_name)
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise AnalysisArtifactError(
                    f"prediction {index} group split {count_name} must be positive"
                )
        if split_validation.get("strategy") not in {
            "per_host",
            "cross_machine_transfer",
        }:
            raise AnalysisArtifactError(
                f"prediction {index} group split strategy is invalid"
            )
        if split_validation.get("artifact_verified") is not True:
            raise AnalysisArtifactError(
                f"prediction {index} serialized group split must be validator-issued"
            )
    causal_ground_truth = provenance.get("causal_ground_truth")
    controls = provenance.get("accepted_control_trials")
    target = path.get("accepted_target_trial")
    if not isinstance(causal_ground_truth, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} needs causal ground-truth provenance"
        )
    if not isinstance(controls, list) or not controls or any(
        not isinstance(control, Mapping) for control in controls
    ):
        raise AnalysisArtifactError(
            f"prediction {index} needs accepted control-trial provenance"
        )
    if not isinstance(target, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} needs accepted target-trial provenance"
        )
    target_trial_id = _require_text(
        target.get("trial_id"),
        f"prediction {index} target trial_id",
    )
    if target_trial_id != document.get("trial_id"):
        raise AnalysisArtifactError(
            f"prediction {index} target trial_id does not match the estimate"
        )
    target_boundary_id = _require_text(
        target.get("measurement_boundary_id"),
        f"prediction {index} target measurement_boundary_id",
    )
    boundary = document.get("measurement_boundary")
    if not isinstance(boundary, Mapping) or target_boundary_id != boundary.get(
        "boundary_id"
    ):
        raise AnalysisArtifactError(
            f"prediction {index} target trial uses a different boundary"
        )
    target_estimand = target.get("estimand")
    if not isinstance(target_estimand, Mapping) or dict(target_estimand) != estimand.to_dict():
        raise AnalysisArtifactError(
            f"prediction {index} target trial uses a different estimand"
        )
    for label, item in (
        ("causal ground truth", causal_ground_truth),
        *(('control trial', control) for control in controls),
        ("target trial", target),
    ):
        if not isinstance(item.get("artifact_verified"), bool):
            raise AnalysisArtifactError(
                f"prediction {index} {label} artifact_verified must be boolean"
            )

    if result.get("kind") == "ABSTAINED_OUT_OF_DISTRIBUTION":
        if any(series.values()) or path.get("integrated_counterfactual") is not None:
            raise AnalysisArtifactError(
                f"prediction {index} OOD path cannot contain a numeric prediction"
            )
        return

    if any(len(values) != len(timestamps) for values in series.values()):
        raise AnalysisArtifactError(
            f"prediction {index} power paths must align with timestamps"
        )
    predicted = series["predicted_power_watts"]
    lower = series["lower_power_watts"]
    upper = series["upper_power_watts"]
    for position, (mean, low, high) in enumerate(zip(predicted, lower, upper)):
        mean_value = _require_number(
            mean, f"prediction {index} path mean at {position}"
        )
        low_value = _require_number(
            low, f"prediction {index} path lower at {position}"
        )
        high_value = _require_number(
            high, f"prediction {index} path upper at {position}"
        )
        if low_value < 0 or not low_value <= mean_value <= high_value:
            raise AnalysisArtifactError(
                f"prediction {index} power path has an invalid interval at {position}"
            )

    integrated = path.get("integrated_counterfactual")
    if not isinstance(integrated, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} needs its integrated counterfactual interval"
        )
    integrated_joules = _require_number(
        integrated.get("joules"),
        f"prediction {index} integrated counterfactual joules",
    )
    result_joules = _require_number(
        result.get("counterfactual_joules"),
        f"prediction {index} result counterfactual_joules",
    )
    numeric_timestamps = [int(timestamp) for timestamp in timestamps]
    numeric_predicted = [float(value) for value in predicted]
    numeric_lower = [float(value) for value in lower]
    numeric_upper = [float(value) for value in upper]

    def trapezoid(values: Sequence[float]) -> float:
        return sum(
            (left_value + right_value)
            / 2.0
            * (right_ns - left_ns)
            / 1e9
            for left_ns, right_ns, left_value, right_value in zip(
                numeric_timestamps,
                numeric_timestamps[1:],
                values,
                values[1:],
            )
        )

    recomputed_joules = trapezoid(numeric_predicted)
    recomputed_lower = trapezoid(numeric_lower)
    recomputed_upper = trapezoid(numeric_upper)
    if not math.isclose(
        integrated_joules,
        recomputed_joules,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise AnalysisArtifactError(
            f"prediction {index} integrated energy does not match its power path"
        )
    if not math.isclose(integrated_joules, result_joules, rel_tol=1e-12, abs_tol=1e-12):
        raise AnalysisArtifactError(
            f"prediction {index} path and result counterfactual energy disagree"
        )
    interval = integrated.get("interval")
    if not isinstance(interval, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} integrated counterfactual needs an interval"
        )
    confidence = _require_number(
        interval.get("confidence_level"),
        f"prediction {index} counterfactual confidence_level",
    )
    low = _require_number(
        interval.get("lower_joules"),
        f"prediction {index} counterfactual lower_joules",
    )
    high = _require_number(
        interval.get("upper_joules"),
        f"prediction {index} counterfactual upper_joules",
    )
    result_interval = result["interval"]
    result_confidence = _require_number(
        result_interval["confidence_level"],
        f"prediction {index} result confidence_level",
    )
    if (
        not 0 < confidence < 1
        or not math.isclose(confidence, result_confidence)
        or low < 0
        or not low <= integrated_joules <= high
    ):
        raise AnalysisArtifactError(
            f"prediction {index} integrated counterfactual interval is invalid"
        )
    if not math.isclose(low, recomputed_lower, rel_tol=1e-12, abs_tol=1e-12) or not math.isclose(
        high,
        recomputed_upper,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise AnalysisArtifactError(
            f"prediction {index} counterfactual interval does not match its power paths"
        )
    target_observed = _require_number(
        target.get("observed_joules"),
        f"prediction {index} target observed_joules",
    )
    result_observed = _require_number(
        result.get("observed_joules"),
        f"prediction {index} result observed_joules",
    )
    if not math.isclose(
        target_observed,
        result_observed,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise AnalysisArtifactError(
            f"prediction {index} observed energy does not match its target trial"
        )


def _prediction_document(value: object, index: int) -> dict[str, Any]:
    document = _as_document(value, f"prediction {index}")
    _require_text(document.get("estimate_id"), f"prediction {index} estimate_id")
    _require_text(document.get("trial_id"), f"prediction {index} trial_id")
    if document.get("evidence_class") != MODEL_EVIDENCE_CLASS:
        raise AnalysisArtifactError(
            f"prediction {index} must have evidence_class {MODEL_EVIDENCE_CLASS}"
        )

    model = document.get("model")
    if not isinstance(model, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} must preserve canonical model identity"
        )
    for field_name in ("model_id", "model_version", "training_data_id"):
        _require_text(
            model.get(field_name), f"prediction {index} model.{field_name}"
        )

    estimand = document.get("estimand")
    if not isinstance(estimand, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} must preserve its canonical estimand"
        )
    try:
        parsed_estimand = CounterfactualEstimand.from_dict(estimand)
    except AnalysisError as exc:
        raise AnalysisArtifactError(
            f"prediction {index} has an invalid estimand: {exc}"
        ) from exc
    document["estimand"] = parsed_estimand.to_dict()

    boundary = document.get("measurement_boundary")
    if not isinstance(boundary, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} must preserve its measurement_boundary"
        )
    try:
        parsed_boundary = MeasurementBoundary.from_dict(boundary)
    except AnalysisError as exc:
        raise AnalysisArtifactError(
            f"prediction {index} has an invalid measurement_boundary: {exc}"
        ) from exc
    document["measurement_boundary"] = parsed_boundary.to_dict()

    result = document.get("result")
    if not isinstance(result, Mapping):
        raise AnalysisArtifactError(
            f"prediction {index} must contain a canonical tagged result"
        )
    result_kind = result.get("kind")
    if result_kind == "ESTIMATED":
        _validate_estimated_result(result, index)
        if result.get("validation_status") == "VALIDATED":
            if not isinstance(value, CounterfactualEstimate):
                raise AnalysisArtifactError(
                    f"prediction {index} raw mappings cannot publish VALIDATED estimates"
                )
            if not value.artifact_chain_verified:
                raise AnalysisArtifactError(
                    f"prediction {index} VALIDATED estimate lacks a verified artifact chain"
                )
    elif result_kind == "ABSTAINED_OUT_OF_DISTRIBUTION":
        _require_text(result.get("reason"), f"prediction {index} abstention reason")
        forbidden = {
            "observed_joules",
            "counterfactual_joules",
            "impact_joules",
            "interval",
        }
        leaked = forbidden & set(result)
        if leaked:
            raise AnalysisArtifactError(
                f"prediction {index} OOD abstention cannot contain numeric estimate fields"
            )
    else:
        raise AnalysisArtifactError(
            f"prediction {index} has unsupported result kind {result_kind!r}"
        )

    _prediction_path(document, result, parsed_estimand, index)

    labels = document.get("labels", {})
    if not isinstance(labels, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in labels.items()
    ):
        raise AnalysisArtifactError(
            f"prediction {index} labels must map strings to strings"
        )
    document["labels"] = dict(labels)
    return document


def _prediction_documents(predictions: Iterable[object]) -> tuple[dict[str, Any], ...]:
    documents = tuple(
        _prediction_document(prediction, index)
        for index, prediction in enumerate(predictions)
    )
    if not documents:
        raise AnalysisArtifactError("predictions.jsonl needs at least one record")
    return documents


def _boundary_documents(
    boundaries: Iterable[MeasurementBoundary | Mapping[str, object]],
) -> tuple[dict[str, Any], ...]:
    documents: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, boundary in enumerate(boundaries):
        document = _as_document(boundary, f"measurement boundary {index}")
        try:
            parsed_boundary = MeasurementBoundary.from_dict(document)
        except AnalysisError as exc:
            raise AnalysisArtifactError(
                f"measurement boundary {index} is invalid: {exc}"
            ) from exc
        document = parsed_boundary.to_dict()
        identifier = parsed_boundary.boundary_id
        if identifier in identifiers:
            raise AnalysisArtifactError(
                f"duplicate measurement boundary id {identifier!r}"
            )
        identifiers.add(identifier)
        documents.append(document)
    if not documents:
        raise AnalysisArtifactError("at least one measurement boundary is required")
    return tuple(documents)


def _uses_domain_meter(boundaries: Sequence[Mapping[str, object]]) -> bool:
    for boundary in boundaries:
        backend = str(boundary["backend"]).lower()
        if "rapl" in backend or "powermetrics" in backend:
            return True
    return False


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _reject_whole_machine_language(
    value: object,
    boundaries: Sequence[Mapping[str, object]],
    artifact_name: str,
) -> None:
    if not _uses_domain_meter(boundaries):
        return
    if any(_WHOLE_MACHINE_ENERGY.search(text) for text in _strings(value)):
        raise AnalysisArtifactError(
            f"{artifact_name} cannot label a RAPL or powermetrics boundary as "
            "whole-machine energy"
        )


def _validate_prediction_boundaries(
    predictions: Sequence[Mapping[str, object]],
    boundaries: Sequence[Mapping[str, object]],
) -> None:
    declared = {
        str(boundary["boundary_id"]): boundary for boundary in boundaries
    }
    for index, prediction in enumerate(predictions):
        measurement_boundary = prediction["measurement_boundary"]
        if not isinstance(measurement_boundary, Mapping):
            raise AnalysisArtifactError(
                f"prediction {index} must preserve its measurement_boundary"
            )
        identifier = str(measurement_boundary["boundary_id"])
        if identifier not in declared:
            raise AnalysisArtifactError(
                f"prediction {index} uses undeclared measurement boundary "
                f"{identifier!r}"
            )
        if dict(measurement_boundary) != dict(declared[identifier]):
            raise AnalysisArtifactError(
                f"prediction {index} changed the frozen measurement boundary "
                f"{identifier!r}"
            )


def _claims_document(
    claims: object,
    boundaries: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    typed_claims: tuple[StudyClaimRecord, ...] = ()
    if isinstance(claims, StudyClaimRecord):
        typed_claims = (claims,)
        claims._require_verified()
        claim_payload: object = claims.to_dict()
    elif isinstance(claims, (list, tuple)) and all(
        isinstance(claim, StudyClaimRecord) for claim in claims
    ):
        typed_claims = tuple(claims)
        for claim in typed_claims:
            claim._require_verified()
        claim_payload = [claim.to_dict() for claim in typed_claims]
    elif isinstance(claims, Mapping):
        claim_payload: object = dict(claims)
    elif isinstance(claims, (list, tuple)):
        claim_payload = [
            _as_document(claim, f"claim {index}") for index, claim in enumerate(claims)
        ]
    else:
        claim_payload = _as_document(claims, "claims")
    if not typed_claims and _contains_positive_ranking_claim(claim_payload):
        raise AnalysisArtifactError(
            "a positive ranking-reversal claim requires a typed StudyClaimRecord "
            "bound to accepted trial checksums"
        )
    boundary_ids = {str(boundary["boundary_id"]) for boundary in boundaries}
    for claim in typed_claims:
        evidence_boundary_ids = {
            reference.boundary_id for reference in claim.trial_bundle.references
        }
        if not evidence_boundary_ids.issubset(boundary_ids):
            raise AnalysisArtifactError(
                "study claim evidence refers to an undeclared measurement boundary"
            )
        declared = next(
            boundary
            for boundary in boundaries
            if boundary["boundary_id"] == claim.trial_bundle.boundary.boundary_id
        )
        if declared != claim.trial_bundle.boundary.to_dict():
            raise AnalysisArtifactError(
                "study claim changed its replay-verified measurement boundary"
            )
    _reject_whole_machine_language(claim_payload, boundaries, CLAIMS_FILE)
    return {
        "schema_version": 1,
        "measurement_boundary_ids": [
            boundary["boundary_id"] for boundary in boundaries
        ],
        "measurement_boundaries": list(boundaries),
        "verified_claim_digests": [claim.digest_sha256 for claim in typed_claims],
        "claims": claim_payload,
    }


def _contains_positive_ranking_claim(value: object) -> bool:
    if isinstance(value, Mapping):
        strings = tuple(_strings(value))
        has_reversal_subject = any(
            _RANKING_REVERSAL_SUBJECT.search(
                re.sub(r"[^a-z]+", " ", item.lower())
            )
            is not None
            for item in strings
        ) or any(
            "reversal" in re.sub(r"[^a-z]", "", str(key).lower())
            for key in value
        )
        has_positive_verdict = False
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if item is True and (
                normalized in {"supported", "accepted", "confirmed", "demonstrated"}
                or "reversal" in normalized
            ):
                has_positive_verdict = True
            if isinstance(item, str) and re.sub(
                r"[^a-z]", "", item.lower()
            ) in _POSITIVE_VERDICT_VALUES:
                has_positive_verdict = True
        if has_reversal_subject and has_positive_verdict:
            return True
        for item in value.values():
            if _contains_positive_ranking_claim(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_positive_ranking_claim(item) for item in value)
    if isinstance(value, str):
        return _contains_positive_report_language(value)
    return False


def _contains_positive_report_language(markdown: str) -> bool:
    normalized = _NEGATED_CLAIM_VERDICT.sub(" rejected ", markdown)
    normalized = _NEGATED_REVERSAL_SUBJECT.sub(" no supported claim ", normalized)
    return _POSITIVE_RANKING_LANGUAGE.search(normalized) is not None or (
        _RANKING_REVERSAL_SUBJECT.search(normalized) is not None
        and _POSITIVE_CLAIM_VERDICT.search(normalized) is not None
    )


def _report_bytes(
    markdown: str,
    boundaries: Sequence[Mapping[str, object]],
    *,
    bound_claim: StudyClaimRecord | None = None,
) -> bytes:
    if not isinstance(markdown, str) or not markdown.strip():
        raise AnalysisArtifactError("report markdown must be non-empty")
    _reject_whole_machine_language(markdown, boundaries, REPORT_FILE)
    positive_language = _contains_positive_report_language(markdown)
    if positive_language:
        if bound_claim is None:
            raise AnalysisArtifactError(
                "a report asserting ranking-reversal support must bind an "
                "internally verified StudyClaimRecord"
            )
        bound_claim._require_verified()
        if not bound_claim.acceptance.supported:
            raise AnalysisArtifactError(
                "an unsupported claim cannot authorize positive report wording"
            )
    identifiers = [str(boundary["boundary_id"]) for boundary in boundaries]
    metadata = json.dumps(identifiers, separators=(",", ":"), ensure_ascii=False)
    claim_metadata = ""
    if bound_claim is not None:
        bound_claim._require_verified()
        claim_metadata = (
            f"\n<!-- wattch-verified-claim: {bound_claim.claim_id}:"
            f"{bound_claim.digest_sha256} -->"
        )
    report = (
        f"<!-- wattch-measurement-boundary-ids: {metadata} -->"
        f"{claim_metadata}\n\n{markdown}"
    )
    if not report.endswith("\n"):
        report += "\n"
    return report.encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _directory_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / _LOCK_FILE
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise AnalysisArtifactError(
            f"analysis publication is already active in {directory}"
        ) from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _stage_bytes(directory: Path, filename: str, contents: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _stage_parquet(directory: Path, filename: str, features: object) -> Path:
    try:
        import pyarrow as pa
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise AnalysisArtifactError(
            "features.parquet requires pyarrow; install pywattch[analysis]"
        ) from exc

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if isinstance(features, pa.Table):
            table = features
        else:
            try:
                rows = list(features)  # type: ignore[arg-type]
            except TypeError as exc:
                raise AnalysisArtifactError(
                    "features must be a pyarrow.Table or iterable of mappings"
                ) from exc
            if not all(isinstance(row, Mapping) for row in rows):
                raise AnalysisArtifactError("feature rows must all be mappings")
            table = pa.Table.from_pylist([dict(row) for row in rows])
        parquet.write_table(table, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_staged(staged: Sequence[tuple[Path, Path]]) -> None:
    published: list[Path] = []
    try:
        for temporary, target in staged:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise AnalysisArtifactError(
                    f"refusing to overwrite existing analysis artifact {target}"
                ) from exc
            except OSError as exc:
                raise AnalysisArtifactError(
                    f"cannot atomically publish analysis artifact {target}: {exc}"
                ) from exc
            published.append(target)
        if staged:
            _fsync_directory(staged[0][1].parent)
    except BaseException:
        for target in reversed(published):
            target.unlink(missing_ok=True)
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def _write_one(path: str | Path, contents: bytes) -> Path:
    target = Path(path)
    with _directory_lock(target.parent):
        temporary = _stage_bytes(target.parent, target.name, contents)
        _publish_staged(((temporary, target),))
    return target


def write_predictions_jsonl(path: str | Path, predictions: Iterable[object]) -> Path:
    """Validate and exclusively publish model-derived prediction records."""

    documents = _prediction_documents(predictions)
    return _write_one(path, b"".join(_json_line(item) for item in documents))


def write_claims_json(
    path: str | Path,
    claims: object,
    *,
    boundaries: Iterable[MeasurementBoundary | Mapping[str, object]],
) -> Path:
    """Publish claims with their complete boundary declarations and ids."""

    boundary_documents = _boundary_documents(boundaries)
    return _write_one(path, _json_bytes(_claims_document(claims, boundary_documents)))


def write_report_markdown(
    path: str | Path,
    markdown: str,
    *,
    boundaries: Iterable[MeasurementBoundary | Mapping[str, object]],
    claim: StudyClaimRecord | None = None,
) -> Path:
    """Publish a report carrying machine-readable measurement-boundary ids."""

    boundary_documents = _boundary_documents(boundaries)
    return _write_one(
        path,
        _report_bytes(markdown, boundary_documents, bound_claim=claim),
    )


def write_features_parquet(path: str | Path, features: object) -> Path:
    """Write real Parquet through optional pyarrow and never replace a file."""

    target = Path(path)
    with _directory_lock(target.parent):
        temporary = _stage_parquet(target.parent, target.name, features)
        _publish_staged(((temporary, target),))
    return target


def write_analysis_artifacts(
    analysis_dir: str | Path,
    *,
    features: object,
    predictions: Iterable[object],
    claims: object,
    report_markdown: str,
    boundaries: Iterable[MeasurementBoundary | Mapping[str, object]],
) -> AnalysisArtifactPaths:
    """Validate, stage, and exclusively publish all four derived artifacts.

    The directory may already exist (the experiment runner creates it), but
    none of the four destination files may exist.  All validation and staging
    completes before the first destination becomes visible.  A publication
    failure removes files created by this call and leaves pre-existing files
    untouched.
    """

    directory = Path(analysis_dir)
    boundary_documents = _boundary_documents(boundaries)
    prediction_documents = _prediction_documents(predictions)
    _validate_prediction_boundaries(prediction_documents, boundary_documents)
    claims_document = _claims_document(claims, boundary_documents)
    supported_claims: tuple[StudyClaimRecord, ...] = ()
    if isinstance(claims, StudyClaimRecord) and claims.acceptance.supported:
        supported_claims = (claims,)
    elif isinstance(claims, (list, tuple)):
        supported_claims = tuple(
            claim
            for claim in claims
            if isinstance(claim, StudyClaimRecord) and claim.acceptance.supported
        )
    if _contains_positive_report_language(report_markdown):
        if len(supported_claims) != 1:
            raise AnalysisArtifactError(
                "positive report wording must bind exactly one supported typed claim"
            )
        bound_claim: StudyClaimRecord | None = supported_claims[0]
    else:
        bound_claim = supported_claims[0] if len(supported_claims) == 1 else None
    report_contents = _report_bytes(
        report_markdown,
        boundary_documents,
        bound_claim=bound_claim,
    )

    paths = AnalysisArtifactPaths(
        features=directory / FEATURES_FILE,
        predictions=directory / PREDICTIONS_FILE,
        claims=directory / CLAIMS_FILE,
        report=directory / REPORT_FILE,
    )
    with _directory_lock(directory):
        targets = (paths.features, paths.predictions, paths.claims, paths.report)
        existing = [target for target in targets if target.exists()]
        if existing:
            raise AnalysisArtifactError(
                "refusing to overwrite existing analysis artifact(s): "
                + ", ".join(str(path) for path in existing)
            )

        staged: list[tuple[Path, Path]] = []
        try:
            staged.append(
                (_stage_parquet(directory, FEATURES_FILE, features), paths.features)
            )
            prediction_bytes = b"".join(
                _json_line(item) for item in prediction_documents
            )
            staged.extend(
                [
                    (
                        _stage_bytes(directory, PREDICTIONS_FILE, prediction_bytes),
                        paths.predictions,
                    ),
                    (
                        _stage_bytes(
                            directory, CLAIMS_FILE, _json_bytes(claims_document)
                        ),
                        paths.claims,
                    ),
                    (
                        _stage_bytes(directory, REPORT_FILE, report_contents),
                        paths.report,
                    ),
                ]
            )
            _publish_staged(staged)
        except BaseException:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)
            raise
    return paths


__all__ = [
    "AnalysisArtifactError",
    "AnalysisArtifactPaths",
    "CLAIMS_FILE",
    "FEATURES_FILE",
    "PREDICTIONS_FILE",
    "PrimaryRegimePreregistration",
    "REPORT_FILE",
    "SealedArtifactReference",
    "StudyClaimRecord",
    "StudyPreregistration",
    "TrialEvidenceReference",
    "VerifiedStudyPreregistration",
    "VerifiedTrialEvidenceBundle",
    "load_study_preregistration",
    "seal_study_preregistration",
    "write_analysis_artifacts",
    "write_claims_json",
    "write_features_parquet",
    "write_predictions_jsonl",
    "write_report_markdown",
]
