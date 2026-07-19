"""Control-only dynamic linear counterfactual baseline.

The model predicts the no-program power path for one frozen measurement
boundary.  Its API separates pre-treatment features from exogenous time-series
inputs and rejects known treatment-period mediators.  This is intentional:
frequency, C-states, wakeups, utilization, runtime, throughput, and backlog are
effects to explain, not inputs to the no-program counterfactual.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from ..meter import cumulative_delta
from ._intervals import synchronize_interval_averages
from .boundary import AnalysisError, MeasurementBoundary, SemanticBoundaryRebind
from .effects import StudyAcceptanceResult


MODEL_EVIDENCE_CLASS = "MODEL_DERIVED_ESTIMATE"
MODEL_INPUT_ARTIFACT_LABEL = "wattch.model_input_artifact_sha256"
MODEL_INPUT_ARTIFACT_KIND = "WATTCH_MODEL_INPUT_V1"
MODEL_INPUT_ALIGNMENT = "OUTCOME_START_STEP_PREVIOUS_V1"

_MEDIATOR_TERMS = (
    "frequency",
    "p_state",
    "pstate",
    "c_state",
    "cstate",
    "wakeup",
    "context_switch",
    "interrupt",
    "run_queue",
    "utilization",
    "temperature",
    "throttling",
    "runtime",
    "throughput",
    "backlog",
    "completed_work",
    "power",
    "energy",
)


def _canonical_feature_name(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _validate_feature_names(
    pre_treatment_names: Sequence[str], exogenous_names: Sequence[str]
) -> None:
    all_names = tuple(pre_treatment_names) + tuple(exogenous_names)
    if any(not name or not name.strip() for name in all_names):
        raise AnalysisError("model feature names must not be empty")
    canonical = [_canonical_feature_name(name) for name in all_names]
    if len(canonical) != len(set(canonical)):
        raise AnalysisError("model feature names must be unique")
    for name in exogenous_names:
        normalized = _canonical_feature_name(name)
        if any(term in normalized for term in _MEDIATOR_TERMS):
            raise AnalysisError(
                f"exogenous feature {name!r} looks like a treatment-period "
                "mediator; keep it as an outcome instead"
            )


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisError(f"{label} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise AnalysisError(f"{label} must be finite")
    return converted


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisError(f"{label} must not be empty")
    return value


def _sha256_digest(value: object, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise AnalysisError(
            f"{label} must be 64 lowercase hexadecimal characters"
        )
    return text


def _read_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        contents = path.read_bytes()
        value = json.loads(contents)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot verify {label}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise AnalysisError(f"{label} must contain a JSON object")
    return dict(value), contents


def _canonical_sha256(value: object, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AnalysisError(f"{label} is not canonical strict JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _acceptance_from_dict(value: object) -> StudyAcceptanceResult:
    if not isinstance(value, Mapping):
        raise AnalysisError("claim acceptance must be an object")
    required = {
        "supported",
        "fixed_horizon_supported",
        "equal_work_supported",
        "placebo_supported",
        "independent_day_replication_supported",
        "mechanism_required",
        "mechanism_supported",
        "reasons",
    }
    if set(value) != required:
        raise AnalysisError("claim acceptance has unexpected or missing fields")
    boolean_fields = required - {"mechanism_supported", "reasons"}
    if any(not isinstance(value[name], bool) for name in boolean_fields):
        raise AnalysisError("claim acceptance gates must be booleans")
    mechanism_supported = value["mechanism_supported"]
    if mechanism_supported is not None and not isinstance(mechanism_supported, bool):
        raise AnalysisError("claim mechanism_supported must be boolean or null")
    reasons = value["reasons"]
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in reasons
    ):
        raise AnalysisError("claim acceptance reasons must be non-empty strings")
    return StudyAcceptanceResult(
        supported=value["supported"],
        fixed_horizon_supported=value["fixed_horizon_supported"],
        equal_work_supported=value["equal_work_supported"],
        placebo_supported=value["placebo_supported"],
        independent_day_replication_supported=value[
            "independent_day_replication_supported"
        ],
        mechanism_required=value["mechanism_required"],
        mechanism_supported=mechanism_supported,
        reasons=tuple(reasons),
    )


def _verified_trial(path: str | Path) -> tuple[object, Path, str]:
    """Replay a sealed trial and return its immutable checksum-manifest seal."""

    from .replay import load_trial_evidence

    root = Path(path)
    evidence = load_trial_evidence(root)
    checksum_path = root / "checksums.json"
    try:
        checksum_digest = hashlib.sha256(checksum_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise AnalysisError(f"cannot seal verified trial checksums: {exc}") from exc
    return evidence, root, checksum_digest


def _numeric_sample_points(samples: Iterable[object]) -> list[tuple[int, float]]:
    points: list[tuple[int, float]] = []
    for sample in samples:
        timestamp = getattr(sample, "monotonic_ns", None)
        raw_value = getattr(sample, "value", None)
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or isinstance(raw_value, (bool, bytes))
            or not isinstance(raw_value, (int, float))
        ):
            raise AnalysisError("model power derivation needs numeric raw samples")
        value = float(raw_value)
        if not math.isfinite(value):
            raise AnalysisError("model power derivation encountered a non-finite sample")
        points.append((timestamp, value))
    points.sort(key=lambda item: item[0])
    if len(points) < 2 or any(
        right[0] <= left[0] for left, right in zip(points, points[1:])
    ):
        raise AnalysisError(
            "model power derivation needs two or more strictly ordered samples"
        )
    return points


def _interpolate_point(points: Sequence[tuple[int, float]], timestamp_ns: int) -> float:
    if timestamp_ns < points[0][0] or timestamp_ns > points[-1][0]:
        raise AnalysisError("raw samples do not bracket the model outcome window")
    for index, (timestamp, value) in enumerate(points):
        if timestamp == timestamp_ns:
            return value
        if timestamp > timestamp_ns:
            left_ns, left_value = points[index - 1]
            ratio = (timestamp_ns - left_ns) / (timestamp - left_ns)
            return left_value + ratio * (value - left_value)
    return points[-1][1]


def _clipped_points(
    points: Sequence[tuple[int, float]],
    start_ns: int,
    end_ns: int,
) -> list[tuple[int, float]]:
    return [
        (start_ns, _interpolate_point(points, start_ns)),
        *((timestamp, value) for timestamp, value in points if start_ns < timestamp < end_ns),
        (end_ns, _interpolate_point(points, end_ns)),
    ]


def _power_scale(unit: str) -> float:
    scales = {"WATT": 1.0, "MILLIWATT": 1e-3}
    try:
        return scales[unit]
    except KeyError as exc:
        raise AnalysisError(f"unsupported model power unit {unit!r}") from exc


def _energy_scale(unit: str) -> float:
    scales = {"JOULE": 1.0, "MICROJOULE": 1e-6}
    try:
        return scales[unit]
    except KeyError as exc:
        raise AnalysisError(f"unsupported model energy unit {unit!r}") from exc


def _derive_trial_model_power(
    evidence: object,
) -> tuple[tuple[int, ...], tuple[float, ...], float, str]:
    """Derive one model power path solely from replay-verified raw evidence.

    Validated dynamic modeling currently requires one primary meter source.
    Cumulative energy is converted to interval rates; point power is clipped to
    the daemon-stamped outcome window; native interval averages use the same
    native-timeline-to-receipt synchronization as causal energy outcomes. No
    caller-supplied power values participate.
    """

    outcome = getattr(evidence, "outcome", None)
    boundary = getattr(outcome, "boundary", None)
    descriptors = tuple(getattr(evidence, "descriptors", ()))
    samples = tuple(getattr(evidence, "samples", ()))
    if not isinstance(boundary, MeasurementBoundary):
        raise AnalysisError("verified trial has no canonical measurement boundary")
    selected = boundary.validate_descriptors(descriptors)
    if len(selected) != 1:
        raise AnalysisError(
            "validated dynamic model inputs currently require one primary source"
        )
    descriptor = selected[0]
    start_ns = getattr(outcome, "start_monotonic_ns", None)
    end_ns = getattr(outcome, "end_monotonic_ns", None)
    if (
        isinstance(start_ns, bool)
        or not isinstance(start_ns, int)
        or isinstance(end_ns, bool)
        or not isinstance(end_ns, int)
        or end_ns <= start_ns
    ):
        raise AnalysisError("verified trial has an invalid model outcome window")
    primary_samples = [
        sample
        for sample in samples
        if getattr(sample, "source_id", None) == descriptor.local_source_id
    ]
    points = _numeric_sample_points(primary_samples)

    if descriptor.temporality == "INSTANT":
        scale = _power_scale(descriptor.unit)
        prior = [value for timestamp, value in points if timestamp <= start_ns]
        if not prior:
            raise AnalysisError("instant power lacks a pre-launch model state")
        clipped = _clipped_points(points, start_ns, end_ns)
        values = tuple(value * scale for _, value in clipped)
        initial = prior[-1] * scale
        method = "RAW_INSTANT_CLIPPED"
        timestamps = tuple(timestamp for timestamp, _ in clipped)
    elif descriptor.temporality == "CUMULATIVE":
        scale = _energy_scale(descriptor.unit)
        unwrapped: list[tuple[int, float]] = [(points[0][0], 0.0)]
        total = 0.0
        for (_, previous), (timestamp, value) in zip(points, points[1:]):
            try:
                total += cumulative_delta(previous, value, descriptor.max_value)
            except Exception as exc:
                raise AnalysisError(f"cannot derive cumulative model power: {exc}") from exc
            unwrapped.append((timestamp, total * scale))
        clipped_energy = _clipped_points(unwrapped, start_ns, end_ns)
        interval_power = tuple(
            (right_value - left_value) / ((right_ns - left_ns) / 1e9)
            for (left_ns, left_value), (right_ns, right_value) in zip(
                clipped_energy,
                clipped_energy[1:],
            )
        )
        if not interval_power or any(value < 0 for value in interval_power):
            raise AnalysisError("cumulative source produced invalid model power")
        values = (
            interval_power[0],
            *(
                (left + right) / 2.0
                for left, right in zip(interval_power, interval_power[1:])
            ),
            interval_power[-1],
        )
        timestamps = tuple(timestamp for timestamp, _ in clipped_energy)
        pre_intervals = [
            (right_value - left_value) / ((right_ns - left_ns) / 1e9)
            for (left_ns, left_value), (right_ns, right_value) in zip(
                unwrapped,
                unwrapped[1:],
            )
            if right_ns <= start_ns
        ]
        if not pre_intervals:
            raise AnalysisError("cumulative energy lacks a pre-launch model state")
        initial = pre_intervals[-1]
        method = "RAW_COUNTER_INTERVAL_RATE"
    elif descriptor.temporality == "INTERVAL_AVERAGE":
        scale = _power_scale(descriptor.unit)
        duration_uid = descriptor.labels.get(
            "wattch.interval_duration_source_uid"
        )
        duration_descriptor = next(
            (
                item
                for item in descriptors
                if item.source_uid == duration_uid
            ),
            None,
        )
        if duration_descriptor is None:
            raise AnalysisError("interval-average power lacks duration evidence")
        duration_points = _numeric_sample_points(
            sample
            for sample in samples
            if getattr(sample, "source_id", None)
            == duration_descriptor.local_source_id
        )
        synchronized = synchronize_interval_averages(points, duration_points)
        segments: list[tuple[int, int, float]] = []
        pre_values: list[float] = []
        for interval in synchronized.intervals:
            power_watts = interval.value * scale
            if interval.end_ns <= start_ns:
                pre_values.append(power_watts)
            overlap_start = max(start_ns, interval.start_ns)
            overlap_end = min(end_ns, interval.end_ns)
            if overlap_end > overlap_start:
                segments.append((overlap_start, overlap_end, power_watts))
        if not pre_values:
            raise AnalysisError("interval-average power lacks a pre-launch model state")
        if not segments or segments[0][0] != start_ns or segments[-1][1] != end_ns:
            raise AnalysisError("interval-average power does not cover the model window")
        if any(right[0] != left[1] for left, right in zip(segments, segments[1:])):
            raise AnalysisError("interval-average model power contains gaps or overlaps")
        timestamps = (start_ns, *(segment[1] for segment in segments))
        values = (segments[0][2], *(segment[2] for segment in segments))
        initial = pre_values[-1]
        method = "RAW_INTERVAL_AVERAGE_AFFINE_SYNC_V1"
    else:
        raise AnalysisError(
            f"unsupported model source temporality {descriptor.temporality!r}"
        )

    if (
        len(timestamps) < 2
        or len(timestamps) != len(values)
        or any(value < 0 or not math.isfinite(value) for value in values)
        or initial < 0
        or not math.isfinite(initial)
    ):
        raise AnalysisError("replayed trial produced an invalid model power path")
    return tuple(timestamps), tuple(values), float(initial), method


def _load_bound_model_features(
    evidence: object,
    artifact_path: str | Path,
    timestamps_ns: tuple[int, ...],
) -> tuple[
    Mapping[str, float],
    Mapping[str, tuple[float, ...]],
    str,
    str,
    str,
]:
    path = Path(artifact_path)
    document, contents = _read_json_object(path, "model input artifact")
    expected_keys = {
        "schema_version",
        "artifact_kind",
        "trial_id",
        "input_id",
        "time_origin",
        "alignment_method",
        "relative_offsets_ns",
        "static_features",
        "prelaunch_features",
        "exogenous_features",
    }
    if set(document) != expected_keys:
        raise AnalysisError("model input artifact has unexpected or missing fields")
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("artifact_kind") != MODEL_INPUT_ARTIFACT_KIND
    ):
        raise AnalysisError("model input artifact uses an unsupported schema")
    trial = getattr(evidence, "trial", None)
    if not isinstance(trial, Mapping):
        raise AnalysisError("verified trial metadata is malformed")
    trial_id = _required_text(trial.get("trial_id"), "model input trial_id")
    input_id = _required_text(trial.get("input_id"), "model input input_id")
    if document.get("trial_id") != trial_id or document.get("input_id") != input_id:
        raise AnalysisError("model input artifact identifies another trial or input")
    labels = trial.get("labels")
    digest = hashlib.sha256(contents).hexdigest()
    if not isinstance(labels, Mapping) or labels.get(MODEL_INPUT_ARTIFACT_LABEL) != digest:
        raise AnalysisError(
            "model input artifact digest was not precommitted in sealed trial metadata"
        )
    if (
        document.get("time_origin") != "OUTCOME_START"
        or document.get("alignment_method") != "STEP_PREVIOUS_HOLD_LAST"
    ):
        raise AnalysisError("model input artifact uses an unsupported time alignment")
    artifact_offsets = document.get("relative_offsets_ns")
    if (
        not isinstance(artifact_offsets, list)
        or not artifact_offsets
        or any(
            isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            for offset in artifact_offsets
        )
        or artifact_offsets[0] != 0
        or any(
            right <= left for left, right in zip(artifact_offsets, artifact_offsets[1:])
        )
    ):
        raise AnalysisError(
            "model input artifact relative offsets must start at zero and "
            "strictly increase"
        )
    outcome = getattr(evidence, "outcome", None)
    outcome_start_ns = getattr(outcome, "start_monotonic_ns", None)
    if (
        isinstance(outcome_start_ns, bool)
        or not isinstance(outcome_start_ns, int)
        or not timestamps_ns
        or timestamps_ns[0] != outcome_start_ns
    ):
        raise AnalysisError(
            "replayed model timestamps do not start at the outcome origin"
        )
    replay_offsets = tuple(timestamp - outcome_start_ns for timestamp in timestamps_ns)
    if any(offset < 0 for offset in replay_offsets):
        raise AnalysisError("replayed model timestamps precede the outcome origin")
    static_document = document.get("static_features")
    prelaunch_document = document.get("prelaunch_features")
    exogenous_document = document.get("exogenous_features")
    if not all(
        isinstance(item, Mapping)
        for item in (static_document, prelaunch_document, exogenous_document)
    ):
        raise AnalysisError("model input artifact features must be objects")
    static = {
        _required_text(name, "static feature name"): _finite_number(
            value,
            f"static feature {name!r}",
        )
        for name, value in static_document.items()
    }
    prelaunch = {
        _required_text(name, "prelaunch feature name"): _finite_number(
            value,
            f"prelaunch feature {name!r}",
        )
        for name, value in prelaunch_document.items()
    }
    if set(static) & set(prelaunch):
        raise AnalysisError("static and prelaunch feature names must be disjoint")
    pre = {**static, **prelaunch}
    exogenous: dict[str, tuple[float, ...]] = {}
    for name, values in exogenous_document.items():
        feature_name = _required_text(name, "exogenous feature name")
        if not isinstance(values, list) or len(values) != len(artifact_offsets):
            raise AnalysisError(
                f"exogenous feature {feature_name!r} must align with relative offsets"
            )
        declared = tuple(
            _finite_number(value, f"exogenous feature {feature_name!r}")
            for value in values
        )
        aligned: list[float] = []
        declared_index = 0
        for replay_offset in replay_offsets:
            while (
                declared_index + 1 < len(artifact_offsets)
                and artifact_offsets[declared_index + 1] <= replay_offset
            ):
                declared_index += 1
            aligned.append(declared[declared_index])
        exogenous[feature_name] = tuple(aligned)
    return (
        MappingProxyType(pre),
        MappingProxyType(exogenous),
        digest,
        input_id,
        MODEL_INPUT_ALIGNMENT,
    )


@dataclass(frozen=True)
class CausalGroundTruthReference:
    """Content-addressed proof that confirmatory causal ground truth passed.

    The reference deliberately binds the acceptance decision to one measurement
    boundary.  Constructing a model is therefore impossible before the caller
    has a supported fixed-horizon/equal-work study and a sealed claim artifact.
    """

    study_id: str
    claim_id: str
    measurement_boundary_id: str
    claims_artifact_sha256: str
    acceptance: StudyAcceptanceResult
    preregistration_sha256: str | None = None
    measurement_boundary_sha256: str | None = None
    source_topology_sha256: str | None = None
    accepted_calibration_sha256: tuple[str, ...] = ()
    accepted_machine_provenance_sha256: tuple[str, ...] = ()
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _required_text(self.study_id, "causal ground-truth study_id")
        _required_text(self.claim_id, "causal ground-truth claim_id")
        _required_text(
            self.measurement_boundary_id,
            "causal ground-truth measurement_boundary_id",
        )
        _sha256_digest(
            self.claims_artifact_sha256,
            "causal ground-truth claims_artifact_sha256",
        )
        if not isinstance(self.acceptance, StudyAcceptanceResult):
            raise AnalysisError(
                "causal ground truth needs a StudyAcceptanceResult"
            )
        required_gates = (
            self.acceptance.supported,
            self.acceptance.fixed_horizon_supported,
            self.acceptance.equal_work_supported,
            self.acceptance.placebo_supported,
            self.acceptance.independent_day_replication_supported,
        )
        if not all(required_gates):
            raise AnalysisError(
                "causal ground truth must pass every confirmatory acceptance gate"
            )
        if (
            self.acceptance.mechanism_required
            and self.acceptance.mechanism_supported is not True
        ):
            raise AnalysisError(
                "causal ground truth requires a supported mechanism result"
            )
        if self.acceptance.reasons:
            raise AnalysisError(
                "accepted causal ground truth cannot retain falsification reasons"
            )
        for name in (
            "preregistration_sha256",
            "measurement_boundary_sha256",
            "source_topology_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _sha256_digest(value, f"causal ground-truth {name}")
        for name in (
            "accepted_calibration_sha256",
            "accepted_machine_provenance_sha256",
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise AnalysisError(f"causal ground-truth {name} must be unique")
            for value in values:
                _sha256_digest(value, f"causal ground-truth {name}")

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    def assert_accepts_trial_bundle_seal(self, seal: "TrialBundleSeal") -> None:
        if (
            not self.artifact_verified
            or not isinstance(seal, TrialBundleSeal)
            or not seal.artifact_verified
            or self.preregistration_sha256 is None
            or self.measurement_boundary_sha256 is None
            or self.source_topology_sha256 is None
            or not self.accepted_calibration_sha256
            or not self.accepted_machine_provenance_sha256
        ):
            raise AnalysisError(
                "validated trial needs a verified calibration/topology/host seal "
                "accepted by causal ground truth"
            )
        if (
            seal.preregistration_sha256 != self.preregistration_sha256
            or seal.measurement_boundary_sha256
            != self.measurement_boundary_sha256
            or seal.source_topology_sha256 != self.source_topology_sha256
            or seal.measurement_overhead_calibration_sha256
            not in self.accepted_calibration_sha256
            or seal.machine_provenance_sha256
            not in self.accepted_machine_provenance_sha256
        ):
            raise AnalysisError(
                "trial calibration, topology, host, or preregistration seal is "
                "not accepted by causal ground truth"
            )

    @classmethod
    def from_claims_artifact(
        cls,
        path: str | Path,
        *,
        verified_claim: object,
    ) -> "CausalGroundTruthReference":
        """Verify and content-address one published supported claim.

        A plain mapping cannot acquire this factory-only verification bit.  The
        claims artifact must retain the typed positive-claim schema, its frozen
        measurement boundary, accepted trial seals, and all acceptance gates.
        """

        from .artifacts import StudyClaimRecord

        if not isinstance(verified_claim, StudyClaimRecord):
            raise AnalysisError(
                "causal ground truth requires a factory-issued StudyClaimRecord"
            )
        try:
            verified_claim.require_verified()
            verified_claim_document = verified_claim.to_dict()
        except Exception as exc:
            raise AnalysisError(
                f"causal ground-truth claim was not internally verified: {exc}"
            ) from exc
        claim_id = _required_text(
            verified_claim_document.get("claim_id"),
            "verified claim_id",
        )
        claims_path = Path(path)
        document, contents = _read_json_object(claims_path, "claims artifact")
        if document.get("schema_version") != 1:
            raise AnalysisError("claims artifact uses an unsupported schema")
        boundaries = document.get("measurement_boundary_ids")
        if (
            not isinstance(boundaries, list)
            or not boundaries
            or any(not isinstance(item, str) or not item.strip() for item in boundaries)
            or len(boundaries) != len(set(boundaries))
        ):
            raise AnalysisError("claims artifact has invalid measurement boundaries")
        payload = document.get("claims")
        candidates = payload if isinstance(payload, list) else [payload]
        matches = [
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and candidate.get("claim_id") == claim_id
        ]
        if len(matches) != 1:
            raise AnalysisError(
                f"claims artifact must contain exactly one claim {claim_id!r}"
            )
        claim = matches[0]
        if dict(claim) != dict(verified_claim_document):
            raise AnalysisError(
                "claims artifact does not match the factory-issued claim record"
            )
        verified_digests = document.get("verified_claim_digests")
        expected_claim_digest = _canonical_sha256(
            verified_claim_document,
            "verified claim record",
        )
        if (
            not isinstance(verified_digests, list)
            or expected_claim_digest not in verified_digests
        ):
            raise AnalysisError(
                "claims artifact does not bind the verified claim digest"
            )
        if (
            claim.get("claim_kind") != "CONTEXT_DEPENDENT_RANKING_REVERSAL"
            or claim.get("supported") is not True
            or not isinstance(claim.get("preregistration"), Mapping)
        ):
            raise AnalysisError(
                "causal ground truth must be a preregistered supported ranking claim"
            )
        study_id = _required_text(claim.get("study_id"), "claim study_id")
        acceptance = _acceptance_from_dict(claim.get("acceptance"))
        evidence = claim.get("trial_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise AnalysisError("supported claim must bind accepted trial evidence")
        boundary_ids: set[str] = set()
        calibration_digests: set[str] = set()
        machine_digests: set[str] = set()
        evidence_boundary_digests: set[str] = set()
        evidence_topology_digests: set[str] = set()
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                raise AnalysisError(f"claim trial evidence {index} must be an object")
            boundary_ids.add(
                _required_text(
                    item.get("boundary_id"),
                    f"claim trial evidence {index} boundary_id",
                )
            )
            _sha256_digest(
                item.get("checksums_sha256"),
                f"claim trial evidence {index} checksums_sha256",
            )
            if item.get("arm") not in {"C0", "C1", "C2"}:
                raise AnalysisError(f"claim trial evidence {index} has invalid arm")
            calibration_digests.add(
                _sha256_digest(
                    item.get("measurement_overhead_calibration_sha256"),
                    f"claim trial evidence {index} calibration sha256",
                )
            )
            machine_digests.add(
                _sha256_digest(
                    item.get("machine_provenance_sha256"),
                    f"claim trial evidence {index} machine provenance sha256",
                )
            )
            evidence_boundary_digests.add(
                _sha256_digest(
                    item.get("boundary_sha256"),
                    f"claim trial evidence {index} boundary sha256",
                )
            )
            evidence_topology_digests.add(
                _sha256_digest(
                    item.get("source_topology_sha256"),
                    f"claim trial evidence {index} source topology sha256",
                )
            )
        if len(boundary_ids) != 1:
            raise AnalysisError("one causal ground-truth claim cannot pool boundaries")
        measurement_boundary_id = next(iter(boundary_ids))
        if measurement_boundary_id not in boundaries:
            raise AnalysisError(
                "claim evidence boundary is not declared by the claims artifact"
            )
        boundary_document = claim.get("measurement_boundary")
        if not isinstance(boundary_document, Mapping):
            raise AnalysisError("supported claim must preserve its measurement boundary")
        if boundary_document.get("boundary_id") != measurement_boundary_id:
            raise AnalysisError("claim measurement boundary and trial evidence disagree")
        declared_boundaries = document.get("measurement_boundaries")
        if not isinstance(declared_boundaries, list):
            raise AnalysisError("claims artifact must preserve boundary declarations")
        declared_matches = [
            item
            for item in declared_boundaries
            if isinstance(item, Mapping)
            and item.get("boundary_id") == measurement_boundary_id
        ]
        if len(declared_matches) != 1 or dict(declared_matches[0]) != dict(
            boundary_document
        ):
            raise AnalysisError(
                "claim measurement boundary differs from its wrapper declaration"
            )
        boundary_digest = _sha256_digest(
            claim.get("measurement_boundary_sha256"),
            "claim measurement_boundary_sha256",
        )
        if boundary_digest != _canonical_sha256(
            boundary_document,
            "claim measurement boundary",
        ):
            raise AnalysisError("claim measurement-boundary digest does not match")
        topology = claim.get("source_topology")
        if not isinstance(topology, list) or not topology:
            raise AnalysisError("supported claim must preserve source topology")
        topology_digest = _sha256_digest(
            claim.get("source_topology_sha256"),
            "claim source_topology_sha256",
        )
        if topology_digest != _canonical_sha256(topology, "claim source topology"):
            raise AnalysisError("claim source-topology digest does not match")
        if evidence_boundary_digests != {boundary_digest}:
            raise AnalysisError("claim trial evidence changed the measurement boundary")
        if evidence_topology_digests != {topology_digest}:
            raise AnalysisError("claim trial evidence changed the source topology")
        preregistration = claim.get("preregistration")
        if not isinstance(preregistration, Mapping):  # pragma: no cover - gated above
            raise AnalysisError("supported claim has no preregistration seal")
        preregistration_digest = _sha256_digest(
            preregistration.get("artifact_sha256"),
            "claim preregistration artifact_sha256",
        )
        reference = cls(
            study_id=study_id,
            claim_id=claim_id,
            measurement_boundary_id=measurement_boundary_id,
            claims_artifact_sha256=hashlib.sha256(contents).hexdigest(),
            acceptance=acceptance,
            preregistration_sha256=preregistration_digest,
            measurement_boundary_sha256=boundary_digest,
            source_topology_sha256=topology_digest,
            accepted_calibration_sha256=tuple(sorted(calibration_digests)),
            accepted_machine_provenance_sha256=tuple(sorted(machine_digests)),
        )
        object.__setattr__(reference, "_artifact_verified", True)
        return reference

    def to_dict(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "claim_id": self.claim_id,
            "measurement_boundary_id": self.measurement_boundary_id,
            "claims_artifact_sha256": self.claims_artifact_sha256,
            "acceptance": self.acceptance.to_dict(),
            "preregistration_sha256": self.preregistration_sha256,
            "measurement_boundary_sha256": self.measurement_boundary_sha256,
            "source_topology_sha256": self.source_topology_sha256,
            "accepted_calibration_sha256": list(
                self.accepted_calibration_sha256
            ),
            "accepted_machine_provenance_sha256": list(
                self.accepted_machine_provenance_sha256
            ),
            "artifact_verified": self.artifact_verified,
        }


@dataclass(frozen=True)
class TrialBundleSeal:
    """Factory-only calibration, topology, host, and preregistration seal."""

    preregistration_sha256: str
    measurement_boundary_sha256: str
    source_topology_sha256: str
    machine_provenance_sha256: str
    measurement_overhead_calibration_sha256: str
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "preregistration_sha256",
            "measurement_boundary_sha256",
            "source_topology_sha256",
            "machine_provenance_sha256",
            "measurement_overhead_calibration_sha256",
        ):
            _sha256_digest(getattr(self, name), f"trial bundle {name}")

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    @classmethod
    def _issue(cls, **values: str) -> "TrialBundleSeal":
        seal = cls(**values)
        object.__setattr__(seal, "_artifact_verified", True)
        return seal

    def to_dict(self) -> dict[str, object]:
        return {
            "preregistration_sha256": self.preregistration_sha256,
            "measurement_boundary_sha256": self.measurement_boundary_sha256,
            "source_topology_sha256": self.source_topology_sha256,
            "machine_provenance_sha256": self.machine_provenance_sha256,
            "measurement_overhead_calibration_sha256": (
                self.measurement_overhead_calibration_sha256
            ),
            "artifact_verified": self.artifact_verified,
        }


@dataclass(frozen=True)
class AcceptedControlTrialProvenance:
    """Seal for one quality-accepted, background-only C0 trial artifact.

    ``arm``, ``program_id``, ``quality_disposition``, and ``background_only``
    are type-owned constants rather than caller-selectable flags.  The digest
    addresses the trial's checksum manifest, which in turn seals its raw trace,
    event trace, descriptors, and accepted quality document.
    """

    trial_id: str
    experiment_id: str
    matched_block_id: str
    measurement_boundary_id: str
    checksums_sha256: str
    trial_bundle_seal: TrialBundleSeal | None = None
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "trial_id",
            "experiment_id",
            "matched_block_id",
            "measurement_boundary_id",
        ):
            _required_text(getattr(self, name), f"control provenance {name}")
        _sha256_digest(
            self.checksums_sha256,
            "control provenance checksums_sha256",
        )
        if self.trial_bundle_seal is not None and not isinstance(
            self.trial_bundle_seal,
            TrialBundleSeal,
        ):
            raise AnalysisError("control provenance trial bundle seal is invalid")

    @property
    def arm(self) -> str:
        return "C0"

    @property
    def program_id(self) -> None:
        return None

    @property
    def quality_disposition(self) -> str:
        return "ACCEPTED"

    @property
    def background_only(self) -> bool:
        return True

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    @classmethod
    def from_trial_dir(
        cls,
        trial_dir: str | Path,
        *,
        expected_boundary: MeasurementBoundary | None = None,
    ) -> "AcceptedControlTrialProvenance":
        """Replay and seal one quality-accepted background-only C0 trial."""

        evidence, _, checksum_digest = _verified_trial(trial_dir)
        trial = evidence.trial
        labels = trial.get("labels")
        arm = trial.get("arm")
        boundary_document = trial.get("measurement_boundary")
        if not all(
            isinstance(item, Mapping) for item in (labels, arm, boundary_document)
        ):
            raise AnalysisError("verified control trial has malformed metadata")
        boundary = MeasurementBoundary.from_dict(boundary_document)
        if expected_boundary is not None:
            expected_boundary.assert_compatible(boundary)
        if labels.get("arm_code") != "C0" or arm.get("program_id") is not None:
            raise AnalysisError(
                "accepted control provenance requires a background-only C0 trial"
            )
        if evidence.quality.get("disposition") != "ACCEPTED":
            raise AnalysisError("control trial quality is not ACCEPTED")
        reference = cls(
            trial_id=_required_text(trial.get("trial_id"), "control trial_id"),
            experiment_id=_required_text(
                trial.get("experiment_id"), "control experiment_id"
            ),
            matched_block_id=_required_text(
                trial.get("matched_block_id"), "control matched_block_id"
            ),
            measurement_boundary_id=boundary.boundary_id,
            checksums_sha256=checksum_digest,
        )
        object.__setattr__(reference, "_artifact_verified", True)
        return reference

    def to_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "matched_block_id": self.matched_block_id,
            "measurement_boundary_id": self.measurement_boundary_id,
            "checksums_sha256": self.checksums_sha256,
            "arm": self.arm,
            "program_id": self.program_id,
            "quality_disposition": self.quality_disposition,
            "background_only": self.background_only,
            "trial_bundle_seal": (
                None
                if self.trial_bundle_seal is None
                else self.trial_bundle_seal.to_dict()
            ),
            "artifact_verified": self.artifact_verified,
        }


@dataclass(frozen=True)
class AcceptedTargetTrialProvenance:
    """Seal and observed outcome for one quality-accepted C1/C2 target trial."""

    trial_id: str
    experiment_id: str
    matched_block_id: str
    measurement_boundary_id: str
    checksums_sha256: str
    arm: str
    program_id: str
    estimand: "CounterfactualEstimand"
    observed_joules: float
    trial_bundle_seal: TrialBundleSeal | None = None
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        for name in (
            "trial_id",
            "experiment_id",
            "matched_block_id",
            "measurement_boundary_id",
            "program_id",
        ):
            _required_text(getattr(self, name), f"target provenance {name}")
        _sha256_digest(
            self.checksums_sha256,
            "target provenance checksums_sha256",
        )
        if self.arm not in {"C1", "C2"}:
            raise AnalysisError("target provenance arm must be C1 or C2")
        if isinstance(self.estimand, Mapping):
            object.__setattr__(
                self,
                "estimand",
                CounterfactualEstimand.from_dict(self.estimand),
            )
        elif not isinstance(self.estimand, CounterfactualEstimand):
            raise AnalysisError("target provenance needs a canonical estimand")
        observed = _finite_number(
            self.observed_joules,
            "target provenance observed_joules",
        )
        if observed < 0:
            raise AnalysisError(
                "target provenance observed_joules must be non-negative"
            )
        object.__setattr__(self, "observed_joules", observed)
        if self.trial_bundle_seal is not None and not isinstance(
            self.trial_bundle_seal,
            TrialBundleSeal,
        ):
            raise AnalysisError("target provenance trial bundle seal is invalid")

    @property
    def quality_disposition(self) -> str:
        return "ACCEPTED"

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    @classmethod
    def from_trial_dir(
        cls,
        trial_dir: str | Path,
        *,
        expected_boundary: MeasurementBoundary | None = None,
    ) -> "AcceptedTargetTrialProvenance":
        """Replay a target trial and bind its outcome to its checksum seal."""

        evidence, _, checksum_digest = _verified_trial(trial_dir)
        trial = evidence.trial
        labels = trial.get("labels")
        arm = trial.get("arm")
        boundary_document = trial.get("measurement_boundary")
        estimand_document = trial.get("estimand")
        if not all(
            isinstance(item, Mapping)
            for item in (labels, arm, boundary_document, estimand_document)
        ):
            raise AnalysisError("verified target trial has malformed metadata")
        arm_code = labels.get("arm_code")
        program_id = arm.get("program_id")
        if arm_code not in {"C1", "C2"} or not isinstance(program_id, str):
            raise AnalysisError("accepted target provenance requires a C1/C2 program")
        boundary = MeasurementBoundary.from_dict(boundary_document)
        if expected_boundary is not None:
            expected_boundary.assert_compatible(boundary)
        if evidence.quality.get("disposition") != "ACCEPTED":
            raise AnalysisError("target trial quality is not ACCEPTED")
        observation = evidence.observation
        if (
            observation.work_complete is not True
            or observation.output_valid is not True
            or observation.quality_accepted is not True
            or observation.slo_met is False
        ):
            raise AnalysisError(
                "accepted target provenance requires complete work, equivalent "
                "output, accepted quality, and no SLO failure"
            )
        reference = cls(
            trial_id=_required_text(trial.get("trial_id"), "target trial_id"),
            experiment_id=_required_text(
                trial.get("experiment_id"), "target experiment_id"
            ),
            matched_block_id=_required_text(
                trial.get("matched_block_id"), "target matched_block_id"
            ),
            measurement_boundary_id=boundary.boundary_id,
            checksums_sha256=checksum_digest,
            arm=arm_code,
            program_id=program_id,
            estimand=CounterfactualEstimand.from_dict(estimand_document),
            observed_joules=evidence.outcome.total_joules,
        )
        object.__setattr__(reference, "_artifact_verified", True)
        return reference

    def to_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "matched_block_id": self.matched_block_id,
            "measurement_boundary_id": self.measurement_boundary_id,
            "checksums_sha256": self.checksums_sha256,
            "arm": self.arm,
            "program_id": self.program_id,
            "quality_disposition": self.quality_disposition,
            "observed_joules": self.observed_joules,
            "estimand": self.estimand.to_dict(),
            "trial_bundle_seal": (
                None
                if self.trial_bundle_seal is None
                else self.trial_bundle_seal.to_dict()
            ),
            "artifact_verified": self.artifact_verified,
        }


@dataclass(frozen=True, init=False)
class ModelInputBinding:
    """Factory-issued digest binding model inputs to one replayed trial."""

    role: str
    trial_id: str
    trial_checksums_sha256: str
    feature_artifact_sha256: str
    payload_sha256: str
    power_derivation: str
    feature_alignment: str
    artifact_verified: bool

    @classmethod
    def _issue(
        cls,
        *,
        role: str,
        trial_id: str,
        trial_checksums_sha256: str,
        feature_artifact_sha256: str,
        payload: Mapping[str, object],
        power_derivation: str,
        feature_alignment: str,
    ) -> "ModelInputBinding":
        if role not in {"CONTROL_EPISODE", "TARGET_REQUEST"}:
            raise AnalysisError("model input binding role is invalid")
        instance = object.__new__(cls)
        for name, value in (
            ("role", role),
            ("trial_id", _required_text(trial_id, "model input trial_id")),
            (
                "trial_checksums_sha256",
                _sha256_digest(
                    trial_checksums_sha256,
                    "model input trial_checksums_sha256",
                ),
            ),
            (
                "feature_artifact_sha256",
                _sha256_digest(
                    feature_artifact_sha256,
                    "model input feature_artifact_sha256",
                ),
            ),
            (
                "payload_sha256",
                _canonical_sha256(payload, "model input payload"),
            ),
            (
                "power_derivation",
                _required_text(power_derivation, "model power_derivation"),
            ),
            (
                "feature_alignment",
                _required_text(feature_alignment, "model feature_alignment"),
            ),
            ("artifact_verified", True),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def assert_payload(self, payload: Mapping[str, object], *, role: str) -> None:
        if (
            self.role != role
            or not self.artifact_verified
            or self.payload_sha256
            != _canonical_sha256(payload, "model input payload")
        ):
            raise AnalysisError(
                "model inputs differ from their replay-issued cryptographic binding"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "trial_id": self.trial_id,
            "trial_checksums_sha256": self.trial_checksums_sha256,
            "feature_artifact_sha256": self.feature_artifact_sha256,
            "payload_sha256": self.payload_sha256,
            "power_derivation": self.power_derivation,
            "feature_alignment": self.feature_alignment,
            "artifact_verified": self.artifact_verified,
        }


def _baseline_input_payload(
    *,
    episode_id: str,
    boundary: MeasurementBoundary,
    timestamps_ns: Sequence[int],
    power_watts: Sequence[float],
    initial_power_watts: float,
    pre_treatment_features: Mapping[str, float],
    exogenous_features: Mapping[str, Sequence[float]],
) -> dict[str, object]:
    return {
        "role": "CONTROL_EPISODE",
        "trial_id": episode_id,
        "measurement_boundary": boundary.to_dict(),
        "timestamps_ns": list(timestamps_ns),
        "power_watts": list(power_watts),
        "initial_power_watts": initial_power_watts,
        "pre_treatment_features": dict(pre_treatment_features),
        "exogenous_features": {
            name: list(values) for name, values in exogenous_features.items()
        },
    }


def _request_input_payload(
    *,
    run_id: str,
    boundary: MeasurementBoundary,
    estimand: "CounterfactualEstimand",
    timestamps_ns: Sequence[int],
    observed_power_watts: Sequence[float],
    initial_power_watts: float,
    pre_treatment_features: Mapping[str, float],
    exogenous_features: Mapping[str, Sequence[float]],
) -> dict[str, object]:
    return {
        "role": "TARGET_REQUEST",
        "trial_id": run_id,
        "measurement_boundary": boundary.to_dict(),
        "estimand": estimand.to_dict(),
        "timestamps_ns": list(timestamps_ns),
        "observed_power_watts_sha256": _canonical_sha256(
            list(observed_power_watts),
            "target observed power path",
        ),
        "initial_power_watts": initial_power_watts,
        "pre_treatment_features": dict(pre_treatment_features),
        "exogenous_features": {
            name: list(values) for name, values in exogenous_features.items()
        },
    }


def _verified_trial_bundle_seal(
    trial_dir: str | Path,
    trial_bundle: object,
    *,
    expected_arm: str,
) -> TrialBundleSeal:
    """Recheck one trial against an internally verified confirmatory bundle."""

    from .artifacts import VerifiedTrialEvidenceBundle

    if not isinstance(trial_bundle, VerifiedTrialEvidenceBundle):
        raise AnalysisError(
            "validated model input requires a VerifiedTrialEvidenceBundle"
        )
    try:
        trial_bundle._require_verified()
    except Exception as exc:
        raise AnalysisError(f"trial bundle is not internally verified: {exc}") from exc
    evidence, _, checksum_digest = _verified_trial(trial_dir)
    trial = getattr(evidence, "trial", None)
    manifest = getattr(evidence, "manifest", None)
    acquisition = getattr(evidence, "acquisition", None)
    if not all(isinstance(item, Mapping) for item in (trial, manifest, acquisition)):
        raise AnalysisError("replayed trial lacks bundle-verification metadata")
    trial_id = _required_text(trial.get("trial_id"), "bundle trial_id")
    matches = [
        reference
        for reference in trial_bundle.references
        if reference.trial_id == trial_id
    ]
    if len(matches) != 1:
        raise AnalysisError(
            "trial is missing from, or duplicated in, the verified trial bundle"
        )
    reference = matches[0]
    try:
        reference._require_verified()
    except Exception as exc:
        raise AnalysisError(f"trial reference is not internally verified: {exc}") from exc
    labels = trial.get("labels")
    boundary_document = trial.get("measurement_boundary")
    result = acquisition.get("result")
    if not all(isinstance(item, Mapping) for item in (labels, boundary_document, result)):
        raise AnalysisError("replayed trial has malformed bundle-verification metadata")
    topology = [descriptor.to_dict() for descriptor in evidence.descriptors]
    topology_digest = _canonical_sha256(topology, "trial source topology")
    boundary_digest = _canonical_sha256(
        boundary_document,
        "trial measurement boundary",
    )
    try:
        from .._host_provenance import validate_host_provenance

        host_provenance = validate_host_provenance(result.get("host_provenance"))
    except ValueError as exc:
        raise AnalysisError("replayed trial lacks canonical host provenance") from exc
    machine_digest = _canonical_sha256(
        {
            "node_id": _required_text(manifest.get("node_id"), "trial node_id"),
            "host_provenance": host_provenance,
            "source_topology": topology,
        },
        "trial machine provenance",
    )
    calibration_digest = _sha256_digest(
        result.get("measurement_overhead_calibration_sha256"),
        "trial measurement overhead calibration sha256",
    )
    if (
        reference.arm != expected_arm
        or labels.get("arm_code") != expected_arm
        or reference.checksums_sha256 != checksum_digest
        or reference.boundary_sha256 != boundary_digest
        or reference.source_topology_sha256 != topology_digest
        or reference.machine_provenance_sha256 != machine_digest
        or reference.measurement_overhead_calibration_sha256
        != calibration_digest
        or trial_bundle.boundary_sha256 != boundary_digest
        or trial_bundle.source_topology_sha256 != topology_digest
    ):
        raise AnalysisError(
            "replayed trial differs from its verified calibration/topology/host bundle"
        )
    return TrialBundleSeal._issue(
        preregistration_sha256=_sha256_digest(
            trial_bundle.preregistration_sha256,
            "trial bundle preregistration sha256",
        ),
        measurement_boundary_sha256=boundary_digest,
        source_topology_sha256=topology_digest,
        machine_provenance_sha256=machine_digest,
        measurement_overhead_calibration_sha256=calibration_digest,
    )


@dataclass(frozen=True)
class BaselineEpisode:
    """One complete training episode; only the configured control arm is valid."""

    episode_id: str
    arm: str
    provenance: AcceptedControlTrialProvenance
    boundary: MeasurementBoundary
    timestamps_ns: tuple[int, ...]
    power_watts: tuple[float, ...]
    initial_power_watts: float
    pre_treatment_features: Mapping[str, float]
    exogenous_features: Mapping[str, tuple[float, ...]]
    input_binding: ModelInputBinding | None = None
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )
    _group_key: "EpisodeGroupKey | None" = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    @property
    def group_key(self) -> "EpisodeGroupKey | None":
        return self._group_key

    def _binding_payload(self) -> dict[str, object]:
        return _baseline_input_payload(
            episode_id=self.episode_id,
            boundary=self.boundary,
            timestamps_ns=self.timestamps_ns,
            power_watts=self.power_watts,
            initial_power_watts=self.initial_power_watts,
            pre_treatment_features=self.pre_treatment_features,
            exogenous_features=self.exogenous_features,
        )

    @classmethod
    def from_trial_dir(
        cls,
        trial_dir: str | Path,
        *,
        model_input_artifact: str | Path,
    ) -> "BaselineEpisode":
        """Build an exploratory C0 episode from replayed raw evidence."""

        evidence, _, checksum_digest = _verified_trial(trial_dir)
        provenance = AcceptedControlTrialProvenance.from_trial_dir(
            trial_dir,
            expected_boundary=evidence.outcome.boundary,
        )
        if provenance.checksums_sha256 != checksum_digest:
            raise AnalysisError(
                "control trial changed while its model inputs were being replayed"
            )
        timestamps, power, initial, derivation = _derive_trial_model_power(evidence)
        pre, exogenous, feature_digest, _, feature_alignment = (
            _load_bound_model_features(
                evidence,
                model_input_artifact,
                timestamps,
            )
        )
        payload = _baseline_input_payload(
            episode_id=provenance.trial_id,
            boundary=evidence.outcome.boundary,
            timestamps_ns=timestamps,
            power_watts=power,
            initial_power_watts=initial,
            pre_treatment_features=pre,
            exogenous_features=exogenous,
        )
        binding = ModelInputBinding._issue(
            role="CONTROL_EPISODE",
            trial_id=provenance.trial_id,
            trial_checksums_sha256=checksum_digest,
            feature_artifact_sha256=feature_digest,
            payload=payload,
            power_derivation=derivation,
            feature_alignment=feature_alignment,
        )
        episode = cls(
            episode_id=provenance.trial_id,
            arm="C0",
            provenance=provenance,
            boundary=evidence.outcome.boundary,
            timestamps_ns=timestamps,
            power_watts=power,
            initial_power_watts=initial,
            pre_treatment_features=pre,
            exogenous_features=exogenous,
            input_binding=binding,
        )
        object.__setattr__(episode, "_group_key", _episode_group_key(evidence))
        return episode

    @classmethod
    def from_verified_trial_bundle(
        cls,
        trial_dir: str | Path,
        *,
        model_input_artifact: str | Path,
        trial_bundle: object,
    ) -> "BaselineEpisode":
        """Build a C0 episode tied to verified calibration and study seals."""

        episode = cls.from_trial_dir(
            trial_dir,
            model_input_artifact=model_input_artifact,
        )
        seal = _verified_trial_bundle_seal(
            trial_dir,
            trial_bundle,
            expected_arm="C0",
        )
        provenance = AcceptedControlTrialProvenance(
            trial_id=episode.provenance.trial_id,
            experiment_id=episode.provenance.experiment_id,
            matched_block_id=episode.provenance.matched_block_id,
            measurement_boundary_id=episode.provenance.measurement_boundary_id,
            checksums_sha256=episode.provenance.checksums_sha256,
            trial_bundle_seal=seal,
        )
        object.__setattr__(provenance, "_artifact_verified", True)
        object.__setattr__(episode, "provenance", provenance)
        object.__setattr__(episode, "_artifact_verified", True)
        return episode


@dataclass(frozen=True)
class PredictionRequest:
    """Features available for a no-program prediction of one complete run."""

    run_id: str
    boundary: MeasurementBoundary
    estimand: "CounterfactualEstimand"
    target_trial_provenance: AcceptedTargetTrialProvenance
    timestamps_ns: tuple[int, ...]
    initial_power_watts: float
    pre_treatment_features: Mapping[str, float]
    exogenous_features: Mapping[str, tuple[float, ...]]
    input_binding: ModelInputBinding | None = None
    _observed_power_watts: tuple[float, ...] = field(
        default=(),
        init=False,
        repr=False,
        compare=False,
    )
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if isinstance(self.estimand, Mapping):
            object.__setattr__(
                self,
                "estimand",
                CounterfactualEstimand.from_dict(self.estimand),
            )
        elif not isinstance(self.estimand, CounterfactualEstimand):
            raise AnalysisError(
                "prediction estimand must be a CounterfactualEstimand or mapping"
            )
        if not isinstance(
            self.target_trial_provenance,
            AcceptedTargetTrialProvenance,
        ):
            raise AnalysisError(
                "prediction request needs accepted target trial provenance"
            )
        if self.target_trial_provenance.trial_id != self.run_id:
            raise AnalysisError(
                "prediction run_id must match its target trial provenance"
            )
        if (
            self.target_trial_provenance.measurement_boundary_id
            != self.boundary.boundary_id
        ):
            raise AnalysisError(
                "prediction target provenance uses a different measurement boundary"
            )
        if self.target_trial_provenance.estimand != self.estimand:
            raise AnalysisError(
                "prediction target provenance uses a different estimand"
            )

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    def _binding_payload(self) -> dict[str, object]:
        return _request_input_payload(
            run_id=self.run_id,
            boundary=self.boundary,
            estimand=self.estimand,
            timestamps_ns=self.timestamps_ns,
            observed_power_watts=self._observed_power_watts,
            initial_power_watts=self.initial_power_watts,
            pre_treatment_features=self.pre_treatment_features,
            exogenous_features=self.exogenous_features,
        )

    @classmethod
    def from_trial_dir(
        cls,
        trial_dir: str | Path,
        *,
        model_input_artifact: str | Path,
    ) -> "PredictionRequest":
        """Build an exploratory target request from replayed raw evidence."""

        evidence, _, checksum_digest = _verified_trial(trial_dir)
        provenance = AcceptedTargetTrialProvenance.from_trial_dir(
            trial_dir,
            expected_boundary=evidence.outcome.boundary,
        )
        if provenance.checksums_sha256 != checksum_digest:
            raise AnalysisError(
                "target trial changed while its model inputs were being replayed"
            )
        timestamps, observed_power, initial, derivation = (
            _derive_trial_model_power(evidence)
        )
        pre, exogenous, feature_digest, _, feature_alignment = (
            _load_bound_model_features(
                evidence,
                model_input_artifact,
                timestamps,
            )
        )
        payload = _request_input_payload(
            run_id=provenance.trial_id,
            boundary=evidence.outcome.boundary,
            estimand=provenance.estimand,
            timestamps_ns=timestamps,
            observed_power_watts=observed_power,
            initial_power_watts=initial,
            pre_treatment_features=pre,
            exogenous_features=exogenous,
        )
        binding = ModelInputBinding._issue(
            role="TARGET_REQUEST",
            trial_id=provenance.trial_id,
            trial_checksums_sha256=checksum_digest,
            feature_artifact_sha256=feature_digest,
            payload=payload,
            power_derivation=derivation,
            feature_alignment=feature_alignment,
        )
        request = cls(
            run_id=provenance.trial_id,
            boundary=evidence.outcome.boundary,
            estimand=provenance.estimand,
            target_trial_provenance=provenance,
            timestamps_ns=timestamps,
            initial_power_watts=initial,
            pre_treatment_features=pre,
            exogenous_features=exogenous,
            input_binding=binding,
        )
        object.__setattr__(request, "_observed_power_watts", observed_power)
        return request

    @classmethod
    def from_verified_trial_bundle(
        cls,
        trial_dir: str | Path,
        *,
        model_input_artifact: str | Path,
        trial_bundle: object,
    ) -> "PredictionRequest":
        """Build a target tied to verified calibration and study seals."""

        request = cls.from_trial_dir(
            trial_dir,
            model_input_artifact=model_input_artifact,
        )
        seal = _verified_trial_bundle_seal(
            trial_dir,
            trial_bundle,
            expected_arm=request.target_trial_provenance.arm,
        )
        prior = request.target_trial_provenance
        provenance = AcceptedTargetTrialProvenance(
            trial_id=prior.trial_id,
            experiment_id=prior.experiment_id,
            matched_block_id=prior.matched_block_id,
            measurement_boundary_id=prior.measurement_boundary_id,
            checksums_sha256=prior.checksums_sha256,
            arm=prior.arm,
            program_id=prior.program_id,
            estimand=prior.estimand,
            observed_joules=prior.observed_joules,
            trial_bundle_seal=seal,
        )
        object.__setattr__(provenance, "_artifact_verified", True)
        object.__setattr__(request, "target_trial_provenance", provenance)
        object.__setattr__(request, "_artifact_verified", True)
        return request


@dataclass(frozen=True)
class CounterfactualEstimand:
    """Canonical Rust-compatible ``EnergyEstimand`` declaration.

    A model request carries the estimand explicitly so its serialized output
    cannot silently lose whether the trial used a fixed horizon or equal work.
    """

    kind: str
    horizon_ns: int | None = None
    work_amount: float | None = None
    work_unit: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "FIXED_HORIZON":
            if (
                isinstance(self.horizon_ns, bool)
                or not isinstance(self.horizon_ns, int)
                or self.horizon_ns <= 0
            ):
                raise AnalysisError(
                    "fixed-horizon counterfactual estimand needs positive horizon_ns"
                )
            if self.work_amount is not None or self.work_unit is not None:
                raise AnalysisError(
                    "fixed-horizon counterfactual estimand cannot declare equal work"
                )
            return
        if self.kind == "EQUAL_WORK":
            amount = _finite_number(self.work_amount, "work_amount")
            if amount <= 0:
                raise AnalysisError("equal-work amount must be greater than zero")
            if not isinstance(self.work_unit, str) or not self.work_unit.strip():
                raise AnalysisError("equal-work unit must not be empty")
            if self.horizon_ns is not None:
                raise AnalysisError(
                    "equal-work counterfactual estimand cannot declare horizon_ns"
                )
            object.__setattr__(self, "work_amount", amount)
            return
        raise AnalysisError(f"unsupported counterfactual estimand {self.kind!r}")

    @classmethod
    def fixed_horizon(cls, horizon_ns: int) -> "CounterfactualEstimand":
        return cls(kind="FIXED_HORIZON", horizon_ns=horizon_ns)

    @classmethod
    def equal_work(
        cls, work_amount: float, work_unit: str
    ) -> "CounterfactualEstimand":
        return cls(
            kind="EQUAL_WORK",
            work_amount=work_amount,
            work_unit=work_unit,
        )

    @classmethod
    def from_dict(
        cls, document: Mapping[str, object]
    ) -> "CounterfactualEstimand":
        kind = document.get("kind")
        if not isinstance(kind, str):
            raise AnalysisError("counterfactual estimand kind must be a string")
        if kind == "FIXED_HORIZON":
            expected = {"kind", "horizon_ns"}
            if set(document) != expected:
                raise AnalysisError(
                    "FIXED_HORIZON estimand needs exactly kind and horizon_ns"
                )
            return cls.fixed_horizon(document["horizon_ns"])  # type: ignore[arg-type]
        if kind == "EQUAL_WORK":
            expected = {"kind", "work_amount", "work_unit"}
            if set(document) != expected:
                raise AnalysisError(
                    "EQUAL_WORK estimand needs exactly kind, work_amount, and work_unit"
                )
            return cls.equal_work(
                document["work_amount"],  # type: ignore[arg-type]
                document["work_unit"],  # type: ignore[arg-type]
            )
        raise AnalysisError(f"unsupported counterfactual estimand {kind!r}")

    def to_dict(self) -> dict[str, object]:
        if self.kind == "FIXED_HORIZON":
            return {"kind": self.kind, "horizon_ns": self.horizon_ns}
        return {
            "kind": self.kind,
            "work_amount": self.work_amount,
            "work_unit": self.work_unit,
        }


@dataclass(frozen=True)
class EpisodeGroupKey:
    """Indivisible train/test group for all telemetry rows of one run."""

    run_id: str
    replay_seed: int
    day: str
    workload_family: str
    machine: str

    def __post_init__(self) -> None:
        for field_name in ("run_id", "day", "workload_family", "machine"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AnalysisError(f"group key {field_name} must not be empty")
        if isinstance(self.replay_seed, bool) or not isinstance(self.replay_seed, int):
            raise AnalysisError("group key replay_seed must be an integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "replay_seed": self.replay_seed,
            "day": self.day,
            "workload_family": self.workload_family,
            "machine": self.machine,
        }


def _episode_group_key(evidence: object) -> EpisodeGroupKey:
    """Derive split metadata only from replay-verified sealed artifacts."""

    trial = getattr(evidence, "trial", None)
    manifest = getattr(evidence, "manifest", None)
    outcome = getattr(evidence, "outcome", None)
    boundary = getattr(outcome, "boundary", None)
    if not isinstance(trial, Mapping) or not isinstance(manifest, Mapping):
        raise AnalysisError("verified control trial lacks split metadata")
    if not isinstance(boundary, MeasurementBoundary):
        raise AnalysisError("verified control trial lacks a split boundary")
    replay_seed = trial.get("replay_seed")
    created_unix_ns = manifest.get("created_unix_ns")
    if (
        isinstance(replay_seed, bool)
        or not isinstance(replay_seed, int)
        or isinstance(created_unix_ns, bool)
        or not isinstance(created_unix_ns, int)
        or created_unix_ns < 0
    ):
        raise AnalysisError("verified control trial has invalid split seed or day")
    labels = trial.get("labels")
    if not isinstance(labels, Mapping):
        raise AnalysisError("verified control trial split labels are malformed")
    workload_mode = _required_text(
        labels.get("workload_mode"),
        "control split workload_mode",
    )
    background_workload_id = _required_text(
        trial.get("background_workload_id"),
        "control split background_workload_id",
    )
    declared_family = labels.get("workload_family")
    workload_family = (
        _required_text(declared_family, "control split workload_family")
        if declared_family is not None
        else f"{workload_mode}:{background_workload_id}"
    )
    node_id = _required_text(manifest.get("node_id"), "control split node_id")
    machine = _canonical_sha256(
        {
            "node_id": node_id,
            "source_topology": boundary.to_dict()["required_sources"],
        },
        "control split machine provenance",
    )
    day = datetime.fromtimestamp(created_unix_ns / 1e9, tz=UTC).date().isoformat()
    return EpisodeGroupKey(
        run_id=_required_text(trial.get("run_id"), "control split run_id"),
        replay_seed=replay_seed,
        day=day,
        workload_family=workload_family,
        machine=machine,
    )


@dataclass(frozen=True)
class SplitEpisodeBinding:
    episode_id: str
    trial_checksums_sha256: str
    model_input_payload_sha256: str
    row_count: int
    group_key: EpisodeGroupKey

    def to_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "trial_checksums_sha256": self.trial_checksums_sha256,
            "model_input_payload_sha256": self.model_input_payload_sha256,
            "row_count": self.row_count,
            "group_key": self.group_key.to_dict(),
        }


@dataclass(frozen=True)
class GroupSplitValidation:
    train_rows: int
    test_rows: int
    train_groups: int
    test_groups: int
    strategy: str
    disjoint_axes: tuple[str, ...]
    overlapping_groups: tuple[EpisodeGroupKey, ...] = ()
    train_episodes: tuple[SplitEpisodeBinding, ...] = ()
    test_episodes: tuple[SplitEpisodeBinding, ...] = ()
    split_artifact_sha256: str = ""
    _artifact_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def artifact_verified(self) -> bool:
        return self._artifact_verified

    def to_dict(self) -> dict[str, object]:
        return {
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "train_groups": self.train_groups,
            "test_groups": self.test_groups,
            "strategy": self.strategy,
            "disjoint_axes": list(self.disjoint_axes),
            "train_episodes": [item.to_dict() for item in self.train_episodes],
            "test_episodes": [item.to_dict() for item in self.test_episodes],
            "split_artifact_sha256": self.split_artifact_sha256,
            "artifact_verified": self.artifact_verified,
        }

    @property
    def train_episode_ids(self) -> tuple[str, ...]:
        return tuple(item.episode_id for item in self.train_episodes)

    @property
    def test_episode_ids(self) -> tuple[str, ...]:
        return tuple(item.episode_id for item in self.test_episodes)

    def assert_episodes(self, episodes: Sequence[BaselineEpisode]) -> None:
        if not self.artifact_verified:
            raise AnalysisError("group split is not replay-verified")
        expected = {
            item.episode_id: item
            for item in self.train_episodes + self.test_episodes
        }
        actual = {episode.episode_id: episode for episode in episodes}
        if len(actual) != len(episodes) or set(actual) != set(expected):
            raise AnalysisError(
                "fit episodes must exactly match the replay-bound train/test split"
            )
        split_payload = {
            "strategy": self.strategy,
            "disjoint_axes": list(self.disjoint_axes),
            "train_episodes": [item.to_dict() for item in self.train_episodes],
            "test_episodes": [item.to_dict() for item in self.test_episodes],
        }
        if self.split_artifact_sha256 != _canonical_sha256(
            split_payload,
            "group split artifact",
        ):
            raise AnalysisError("group split artifact digest is inconsistent")
        for episode_id, binding in expected.items():
            episode = actual[episode_id]
            model_binding = episode.input_binding
            if (
                not episode.artifact_verified
                or not isinstance(model_binding, ModelInputBinding)
                or not model_binding.artifact_verified
                or episode.provenance.checksums_sha256
                != binding.trial_checksums_sha256
                or model_binding.payload_sha256
                != binding.model_input_payload_sha256
                or len(episode.timestamps_ns) != binding.row_count
                or episode.group_key != binding.group_key
            ):
                raise AnalysisError(
                    f"fit episode {episode_id!r} differs from its replay-bound split entry"
                )


_GROUP_AXES = ("run_id", "replay_seed", "day", "workload_family", "machine")
_PER_HOST_DISJOINT_AXES = ("run_id", "replay_seed", "day", "workload_family")
_TRANSFER_DISJOINT_AXES = _GROUP_AXES
_GROUP_SPLIT_STRATEGIES = {
    "per_host": _PER_HOST_DISJOINT_AXES,
    "cross_machine_transfer": _TRANSFER_DISJOINT_AXES,
}


def _validate_run_metadata(keys: Sequence[EpisodeGroupKey], *, side: str) -> None:
    """Reject rows that assign conflicting experiment metadata to one run."""

    by_run_id: dict[str, EpisodeGroupKey] = {}
    for key in keys:
        previous = by_run_id.setdefault(key.run_id, key)
        if previous != key:
            raise AnalysisError(
                f"{side} run_id {key.run_id!r} maps to multiple seed/day/"
                "workload-family/machine groups"
            )


def _axis_overlap(
    train_groups: set[EpisodeGroupKey],
    test_groups: set[EpisodeGroupKey],
    axis: str,
) -> tuple[object, ...]:
    train_values = {getattr(key, axis) for key in train_groups}
    test_values = {getattr(key, axis) for key in test_groups}
    return tuple(sorted(train_values & test_values, key=str))


def _split_episode_binding(episode: BaselineEpisode) -> SplitEpisodeBinding:
    model_binding = episode.input_binding
    if (
        not isinstance(episode, BaselineEpisode)
        or not episode.artifact_verified
        or not episode.provenance.artifact_verified
        or not isinstance(model_binding, ModelInputBinding)
        or not model_binding.artifact_verified
        or episode.group_key is None
    ):
        raise AnalysisError(
            "group split requires replay-issued BaselineEpisode objects"
        )
    model_binding.assert_payload(
        episode._binding_payload(),
        role="CONTROL_EPISODE",
    )
    if (
        model_binding.trial_id != episode.episode_id
        or model_binding.trial_checksums_sha256
        != episode.provenance.checksums_sha256
    ):
        raise AnalysisError("split episode identity differs from its sealed trial")
    return SplitEpisodeBinding(
        episode_id=episode.episode_id,
        trial_checksums_sha256=episode.provenance.checksums_sha256,
        model_input_payload_sha256=model_binding.payload_sha256,
        row_count=len(episode.timestamps_ns),
        group_key=episode.group_key,
    )


def validate_group_split(
    *,
    train_episodes: Iterable[BaselineEpisode],
    test_episodes: Iterable[BaselineEpisode],
    strategy: str = "per_host",
) -> GroupSplitValidation:
    """Issue a whole-episode split bound to replayed C0 trial artifacts."""

    train_episode_list = tuple(train_episodes)
    test_episode_list = tuple(test_episodes)
    if not train_episode_list or not test_episode_list:
        raise AnalysisError("group split needs non-empty train and test episodes")
    if any(
        not isinstance(episode, BaselineEpisode)
        for episode in train_episode_list + test_episode_list
    ):
        raise AnalysisError("group split accepts only BaselineEpisode objects")
    all_ids = [
        episode.episode_id
        for episode in train_episode_list + test_episode_list
    ]
    if len(all_ids) != len(set(all_ids)):
        raise AnalysisError(
            "an episode id cannot be duplicated or appear in both split partitions"
        )
    if strategy not in _GROUP_SPLIT_STRATEGIES:
        supported = ", ".join(sorted(_GROUP_SPLIT_STRATEGIES))
        raise AnalysisError(
            f"unsupported group split strategy {strategy!r}; expected {supported}"
        )

    train_bindings = tuple(
        sorted(
            (_split_episode_binding(episode) for episode in train_episode_list),
            key=lambda item: item.episode_id,
        )
    )
    test_bindings = tuple(
        sorted(
            (_split_episode_binding(episode) for episode in test_episode_list),
            key=lambda item: item.episode_id,
        )
    )
    train_keys = tuple(item.group_key for item in train_bindings)
    test_keys = tuple(item.group_key for item in test_bindings)
    _validate_run_metadata(train_keys, side="train")
    _validate_run_metadata(test_keys, side="test")
    train_groups = set(train_keys)
    test_groups = set(test_keys)

    if strategy == "per_host":
        train_machines = {key.machine for key in train_groups}
        test_machines = {key.machine for key in test_groups}
        if len(train_machines) != 1 or len(test_machines) != 1:
            raise AnalysisError(
                "per_host split requires exactly one machine in each split"
            )
        if train_machines != test_machines:
            raise AnalysisError(
                "per_host split requires the same machine in train and test; "
                "use cross_machine_transfer for a held-out host"
            )

    disjoint_axes = _GROUP_SPLIT_STRATEGIES[strategy]
    overlaps = {
        axis: values
        for axis in disjoint_axes
        if (values := _axis_overlap(train_groups, test_groups, axis))
    }
    if overlaps:
        details = "; ".join(
            f"{axis}={list(values)!r}" for axis, values in overlaps.items()
        )
        raise AnalysisError(
            "train/test overlap on required grouping axes would leak complete "
            f"episodes: {details}"
        )
    split_payload = {
        "strategy": strategy,
        "disjoint_axes": list(disjoint_axes),
        "train_episodes": [item.to_dict() for item in train_bindings],
        "test_episodes": [item.to_dict() for item in test_bindings],
    }
    validation = GroupSplitValidation(
        train_rows=sum(item.row_count for item in train_bindings),
        test_rows=sum(item.row_count for item in test_bindings),
        train_groups=len(train_groups),
        test_groups=len(test_groups),
        strategy=strategy,
        disjoint_axes=disjoint_axes,
        train_episodes=train_bindings,
        test_episodes=test_bindings,
        split_artifact_sha256=_canonical_sha256(
            split_payload,
            "group split artifact",
        ),
    )
    object.__setattr__(validation, "_artifact_verified", True)
    return validation


@dataclass(frozen=True, init=False)
class HeldOutModelValidation:
    """Fit-issued metrics over replay-bound C0 episodes excluded from training."""

    split_artifact_sha256: str
    training_data_id: str
    fitted_parameters_sha256: str
    observed_paths_sha256: str
    predicted_paths_sha256: str
    test_episode_ids: tuple[str, ...]
    test_rows: int
    root_mean_squared_error_watts: float
    mean_absolute_error_watts: float
    mean_error_watts: float
    out_of_distribution_rows: int
    artifact_sha256: str
    artifact_verified: bool

    @classmethod
    def _issue(
        cls,
        *,
        split_artifact_sha256: str,
        training_data_id: str,
        fitted_parameters_sha256: str,
        observed_paths: Mapping[str, Sequence[float]],
        predicted_paths: Mapping[str, Sequence[float]],
        errors: Sequence[float],
        out_of_distribution_rows: int,
    ) -> "HeldOutModelValidation":
        if not errors or set(observed_paths) != set(predicted_paths):
            raise AnalysisError("held-out validation needs matching non-empty paths")
        test_episode_ids = tuple(sorted(observed_paths))
        test_rows = len(errors)
        if out_of_distribution_rows < 0 or out_of_distribution_rows > test_rows:
            raise AnalysisError("held-out OOD row count is invalid")
        observed_digest = _canonical_sha256(
            {name: list(observed_paths[name]) for name in test_episode_ids},
            "held-out observed paths",
        )
        predicted_digest = _canonical_sha256(
            {name: list(predicted_paths[name]) for name in test_episode_ids},
            "held-out predicted paths",
        )
        rmse = math.sqrt(statistics.fmean(error * error for error in errors))
        mae = statistics.fmean(abs(error) for error in errors)
        mean_error = statistics.fmean(errors)
        document: dict[str, object] = {
            "split_artifact_sha256": _sha256_digest(
                split_artifact_sha256,
                "held-out split_artifact_sha256",
            ),
            "training_data_id": _required_text(
                training_data_id,
                "held-out training_data_id",
            ),
            "fitted_parameters_sha256": _sha256_digest(
                fitted_parameters_sha256,
                "held-out fitted_parameters_sha256",
            ),
            "observed_paths_sha256": observed_digest,
            "predicted_paths_sha256": predicted_digest,
            "test_episode_ids": list(test_episode_ids),
            "test_rows": test_rows,
            "root_mean_squared_error_watts": rmse,
            "mean_absolute_error_watts": mae,
            "mean_error_watts": mean_error,
            "out_of_distribution_rows": out_of_distribution_rows,
        }
        instance = object.__new__(cls)
        for name, value in document.items():
            object.__setattr__(
                instance,
                name,
                tuple(value) if name == "test_episode_ids" else value,
            )
        object.__setattr__(
            instance,
            "artifact_sha256",
            _canonical_sha256(document, "held-out validation artifact"),
        )
        object.__setattr__(instance, "artifact_verified", True)
        return instance

    def to_dict(self) -> dict[str, object]:
        return {
            "split_artifact_sha256": self.split_artifact_sha256,
            "training_data_id": self.training_data_id,
            "fitted_parameters_sha256": self.fitted_parameters_sha256,
            "observed_paths_sha256": self.observed_paths_sha256,
            "predicted_paths_sha256": self.predicted_paths_sha256,
            "test_episode_ids": list(self.test_episode_ids),
            "test_rows": self.test_rows,
            "root_mean_squared_error_watts": self.root_mean_squared_error_watts,
            "mean_absolute_error_watts": self.mean_absolute_error_watts,
            "mean_error_watts": self.mean_error_watts,
            "out_of_distribution_rows": self.out_of_distribution_rows,
            "artifact_sha256": self.artifact_sha256,
            "artifact_verified": self.artifact_verified,
        }


@dataclass(frozen=True)
class CounterfactualEstimate:
    """Serializable model-derived estimate or explicit OOD abstention."""

    run_id: str
    evidence_class: str
    model_id: str
    model_version: str
    training_data_id: str
    training_episode_ids: tuple[str, ...]
    causal_ground_truth: CausalGroundTruthReference
    control_episode_provenance: tuple[AcceptedControlTrialProvenance, ...]
    control_model_inputs: tuple[ModelInputBinding, ...]
    group_split_validation: GroupSplitValidation | None
    held_out_model_validation: HeldOutModelValidation | None
    target_trial_provenance: AcceptedTargetTrialProvenance
    target_model_input: ModelInputBinding | None
    semantic_rebind: SemanticBoundaryRebind | None
    outcome_boundary: MeasurementBoundary
    estimand: CounterfactualEstimand
    interval_confidence: float
    validation_status: str
    out_of_distribution: bool
    abstained: bool
    abstention_reason: str | None
    timestamps_ns: tuple[int, ...]
    predicted_power_watts: tuple[float, ...]
    lower_power_watts: tuple[float, ...]
    upper_power_watts: tuple[float, ...]
    observed_joules: float | None
    counterfactual_joules: float | None
    counterfactual_lower_joules: float | None
    counterfactual_upper_joules: float | None
    effect_joules: float | None
    effect_lower_joules: float | None
    effect_upper_joules: float | None
    _artifact_chain_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def artifact_chain_verified(self) -> bool:
        return self._artifact_chain_verified

    def to_dict(self) -> dict[str, object]:
        if self.evidence_class != MODEL_EVIDENCE_CLASS:
            raise AnalysisError(
                f"counterfactual evidence_class must remain {MODEL_EVIDENCE_CLASS}"
            )
        if self.validation_status == "VALIDATED" and not self._artifact_chain_verified:
            raise AnalysisError(
                "VALIDATED counterfactual estimates require a factory-verified "
                "causal, training-control, model-input, replay-bound split, "
                "held-out-validation, and target-trial chain"
            )
        model = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "training_data_id": self.training_data_id,
        }
        prediction_path = {
            "timestamps_ns": list(self.timestamps_ns),
            "integration_method": "TRAPEZOIDAL",
            "predicted_power_watts": list(self.predicted_power_watts),
            "lower_power_watts": list(self.lower_power_watts),
            "upper_power_watts": list(self.upper_power_watts),
            "integrated_counterfactual": (
                None
                if self.counterfactual_joules is None
                else {
                    "joules": self.counterfactual_joules,
                    "interval": {
                        "confidence_level": self.interval_confidence,
                        "lower_joules": self.counterfactual_lower_joules,
                        "upper_joules": self.counterfactual_upper_joules,
                    },
                }
            ),
            "training_provenance": {
                "control_episode_ids": list(self.training_episode_ids),
                "causal_ground_truth": self.causal_ground_truth.to_dict(),
                "accepted_control_trials": [
                    provenance.to_dict()
                    for provenance in self.control_episode_provenance
                ],
                "control_model_inputs": [
                    binding.to_dict() for binding in self.control_model_inputs
                ],
                "group_split_validation": (
                    None
                    if self.group_split_validation is None
                    else self.group_split_validation.to_dict()
                ),
                "held_out_model_validation": (
                    None
                    if self.held_out_model_validation is None
                    else self.held_out_model_validation.to_dict()
                ),
                "semantic_rebind": (
                    None
                    if self.semantic_rebind is None
                    else self.semantic_rebind.to_dict()
                ),
            },
            "accepted_target_trial": self.target_trial_provenance.to_dict(),
            "target_model_input": (
                None
                if self.target_model_input is None
                else self.target_model_input.to_dict()
            ),
            "artifact_chain_verified": self._artifact_chain_verified,
        }
        if self.abstained:
            if not self.out_of_distribution:
                raise AnalysisError(
                    "counterfactual abstention must be explicitly out of distribution"
                )
            if not isinstance(self.abstention_reason, str) or not self.abstention_reason.strip():
                raise AnalysisError("OOD abstention needs a non-empty reason")
            result: dict[str, object] = {
                "kind": "ABSTAINED_OUT_OF_DISTRIBUTION",
                "reason": self.abstention_reason,
            }
        else:
            if self.out_of_distribution or self.abstention_reason is not None:
                raise AnalysisError(
                    "numeric counterfactual estimate cannot carry OOD abstention state"
                )
            required_numeric = {
                "observed_joules": self.observed_joules,
                "counterfactual_joules": self.counterfactual_joules,
                "impact_joules": self.effect_joules,
                "lower_joules": self.effect_lower_joules,
                "upper_joules": self.effect_upper_joules,
            }
            if any(value is None for value in required_numeric.values()):
                raise AnalysisError(
                    "serializing an ESTIMATED impact requires observed energy and "
                    "a complete impact interval"
                )
            if not math.isclose(
                float(required_numeric["observed_joules"]),
                self.target_trial_provenance.observed_joules,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise AnalysisError(
                    "observed energy must equal the sealed target trial outcome"
                )
            result = {
                "kind": "ESTIMATED",
                "observed_joules": required_numeric["observed_joules"],
                "counterfactual_joules": required_numeric[
                    "counterfactual_joules"
                ],
                "impact_joules": required_numeric["impact_joules"],
                "interval": {
                    "confidence_level": self.interval_confidence,
                    "lower_joules": required_numeric["lower_joules"],
                    "upper_joules": required_numeric["upper_joules"],
                },
                "validation_status": self.validation_status,
            }
        return {
            "estimate_id": f"{self.run_id}:{self.model_id}",
            "trial_id": self.run_id,
            "measurement_boundary": self.outcome_boundary.to_dict(),
            "estimand": self.estimand.to_dict(),
            "evidence_class": self.evidence_class,
            "model": model,
            "result": result,
            "labels": {"prediction_path": "included"},
            "prediction_path": prediction_path,
        }


@dataclass(frozen=True)
class _FeatureScale:
    mean: float
    deviation: float
    minimum: float
    maximum: float

    @classmethod
    def from_values(cls, values: Sequence[float]) -> "_FeatureScale":
        return cls(
            mean=statistics.fmean(values),
            deviation=statistics.pstdev(values),
            minimum=min(values),
            maximum=max(values),
        )

    @property
    def divisor(self) -> float:
        return self.deviation if self.deviation > 0 else 1.0

    def normalize(self, value: float) -> float:
        return (value - self.mean) / self.divisor

    def ood_reason(
        self, value: float, *, name: str, z_limit: float, range_margin: float
    ) -> str | None:
        span = self.maximum - self.minimum
        if span == 0:
            tolerance = max(abs(self.mean) * 1e-9, 1e-9)
            if abs(value - self.mean) > tolerance:
                return f"{name} differs from its constant training value"
            return None
        padding = span * range_margin
        if value < self.minimum - padding or value > self.maximum + padding:
            return f"{name} is outside the training range"
        if self.deviation > 0 and abs(value - self.mean) / self.deviation > z_limit:
            return f"{name} exceeds the configured OOD z-distance"
        return None


def _solve_linear_system(
    matrix: Sequence[Sequence[float]], vector: Sequence[float]
) -> list[float]:
    """Solve a small dense system with partial-pivot Gaussian elimination."""

    size = len(vector)
    augmented = [list(row) + [float(value)] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            raise AnalysisError("dynamic linear model design matrix is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= pivot_value
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][-1] for row in range(size)]


def _ridge_fit(
    rows: Sequence[Sequence[float]], targets: Sequence[float], ridge: float
) -> list[float]:
    width = len(rows[0])
    gram = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for row, target in zip(rows, targets):
        for left in range(width):
            rhs[left] += row[left] * target
            for right in range(width):
                gram[left][right] += row[left] * row[right]
    for index in range(1, width):  # do not penalize the intercept
        gram[index][index] += ridge
    return _solve_linear_system(gram, rhs)


def _integration_weights(timestamps_ns: Sequence[int]) -> list[float]:
    intervals = [
        (right - left) / 1e9 for left, right in zip(timestamps_ns, timestamps_ns[1:])
    ]
    weights = [intervals[0] / 2]
    weights.extend(
        (intervals[index - 1] + intervals[index]) / 2
        for index in range(1, len(timestamps_ns) - 1)
    )
    weights.append(intervals[-1] / 2)
    return weights


class DynamicLinearBaseline:
    """Per-boundary autoregressive linear baseline trained on C0 episodes."""

    MODEL_VERSION = "dynamic-linear-v1"

    def __init__(
        self,
        *,
        pre_treatment_feature_names: Sequence[str],
        exogenous_feature_names: Sequence[str],
        control_arm: str = "C0",
        ridge: float = 1e-8,
        confidence: float = 0.95,
        ood_z_limit: float = 4.0,
        ood_range_margin: float = 0.1,
    ):
        _validate_feature_names(pre_treatment_feature_names, exogenous_feature_names)
        if control_arm != "C0":
            raise AnalysisError(
                "dynamic counterfactual training is restricted to the C0 control arm"
            )
        if ridge <= 0:
            raise AnalysisError("ridge must be positive")
        if not 0 < confidence < 1:
            raise AnalysisError("confidence must be between zero and one")
        if ood_z_limit <= 0 or ood_range_margin < 0:
            raise AnalysisError("OOD limits must be positive/non-negative")
        self.pre_treatment_feature_names = tuple(pre_treatment_feature_names)
        self.exogenous_feature_names = tuple(exogenous_feature_names)
        self.control_arm = control_arm
        self.ridge = ridge
        self.confidence = confidence
        self.ood_z_limit = ood_z_limit
        self.ood_range_margin = ood_range_margin

        self._feature_names = tuple(
            [f"pre:{name}" for name in self.pre_treatment_feature_names]
            + [f"exo:{name}" for name in self.exogenous_feature_names]
            + ["state:previous_power_watts"]
        )
        self._boundary: MeasurementBoundary | None = None
        self._scales: dict[str, _FeatureScale] = {}
        self._coefficients: tuple[float, ...] = ()
        self._residual_variance = 0.0
        self._training_episode_ids: tuple[str, ...] = ()
        self._control_episode_provenance: tuple[
            AcceptedControlTrialProvenance, ...
        ] = ()
        self._control_model_inputs: tuple[ModelInputBinding, ...] = ()
        self._causal_ground_truth: CausalGroundTruthReference | None = None
        self._group_split_validation: GroupSplitValidation | None = None
        self._held_out_model_validation: HeldOutModelValidation | None = None
        self._training_data_id = ""
        self._model_id = ""
        self._semantic_rebind: SemanticBoundaryRebind | None = None

    @property
    def fitted(self) -> bool:
        return self._boundary is not None

    @property
    def model_id(self) -> str:
        if not self.fitted:
            raise AnalysisError("fit the dynamic linear baseline first")
        return self._model_id

    @property
    def boundary(self) -> MeasurementBoundary:
        if self._boundary is None:
            raise AnalysisError("fit the dynamic linear baseline first")
        return self._boundary

    @property
    def causal_ground_truth(self) -> CausalGroundTruthReference:
        if self._causal_ground_truth is None:
            raise AnalysisError("fit the dynamic linear baseline first")
        return self._causal_ground_truth

    def _validate_series(
        self,
        *,
        identifier: str,
        timestamps_ns: Sequence[int],
        initial_power_watts: float,
        pre_treatment_features: Mapping[str, float],
        exogenous_features: Mapping[str, Sequence[float]],
        power_watts: Sequence[float] | None = None,
    ) -> tuple[
        dict[str, float], dict[str, tuple[float, ...]], tuple[float, ...] | None
    ]:
        if not identifier:
            raise AnalysisError("episode/run id must not be empty")
        if len(timestamps_ns) < 2:
            raise AnalysisError("a dynamic baseline path needs at least two timestamps")
        if any(timestamp < 0 for timestamp in timestamps_ns):
            raise AnalysisError("timestamps must be non-negative")
        if any(right <= left for left, right in zip(timestamps_ns, timestamps_ns[1:])):
            raise AnalysisError("timestamps must strictly increase")
        initial = _finite_number(initial_power_watts, "initial_power_watts")
        if initial < 0:
            raise AnalysisError("initial_power_watts must be non-negative")

        if set(pre_treatment_features) != set(self.pre_treatment_feature_names):
            raise AnalysisError(
                "pre-treatment feature keys do not match the model contract"
            )
        if set(exogenous_features) != set(self.exogenous_feature_names):
            raise AnalysisError(
                "exogenous feature keys do not match the model contract"
            )
        pre = {
            name: _finite_number(pre_treatment_features[name], f"pre:{name}")
            for name in self.pre_treatment_feature_names
        }
        exogenous: dict[str, tuple[float, ...]] = {}
        for name in self.exogenous_feature_names:
            values = tuple(
                _finite_number(value, f"exo:{name}")
                for value in exogenous_features[name]
            )
            if len(values) != len(timestamps_ns):
                raise AnalysisError(
                    f"exogenous feature {name!r} length does not match timestamps"
                )
            exogenous[name] = values

        targets: tuple[float, ...] | None = None
        if power_watts is not None:
            targets = tuple(
                _finite_number(value, "power_watts") for value in power_watts
            )
            if len(targets) != len(timestamps_ns):
                raise AnalysisError("power_watts length does not match timestamps")
            if any(value < 0 for value in targets):
                raise AnalysisError("power_watts must be non-negative")
        return pre, exogenous, targets

    def fit(
        self,
        episodes: Iterable[BaselineEpisode],
        *,
        causal_ground_truth: CausalGroundTruthReference,
        group_split_validation: GroupSplitValidation | None = None,
    ) -> "DynamicLinearBaseline":
        if not isinstance(causal_ground_truth, CausalGroundTruthReference):
            raise AnalysisError(
                "fit requires an accepted CausalGroundTruthReference"
            )
        if group_split_validation is not None and (
            not isinstance(group_split_validation, GroupSplitValidation)
            or not group_split_validation.artifact_verified
            or group_split_validation.overlapping_groups
        ):
            raise AnalysisError(
                "group_split_validation must come from validate_group_split"
            )
        episode_list = tuple(episodes)
        if not episode_list:
            raise AnalysisError("dynamic linear baseline needs control episodes")
        if len({episode.episode_id for episode in episode_list}) != len(episode_list):
            raise AnalysisError("training episode ids must be unique")
        if group_split_validation is None:
            training_episodes = episode_list
            held_out_episodes: tuple[BaselineEpisode, ...] = ()
        else:
            group_split_validation.assert_episodes(episode_list)
            by_id = {episode.episode_id: episode for episode in episode_list}
            training_episodes = tuple(
                by_id[episode_id]
                for episode_id in group_split_validation.train_episode_ids
            )
            held_out_episodes = tuple(
                by_id[episode_id]
                for episode_id in group_split_validation.test_episode_ids
            )
        verified_bindings: dict[str, ModelInputBinding] = {}
        for episode in episode_list:
            if episode.arm != self.control_arm:
                raise AnalysisError(
                    f"training episode {episode.episode_id!r} uses arm {episode.arm!r}; "
                    f"only control arm {self.control_arm!r} is permitted"
                )
            if not isinstance(
                episode.provenance, AcceptedControlTrialProvenance
            ):
                raise AnalysisError(
                    f"training episode {episode.episode_id!r} lacks accepted "
                    "sealed C0 provenance"
                )
            if episode.provenance.trial_id != episode.episode_id:
                raise AnalysisError(
                    f"training episode {episode.episode_id!r} does not match its "
                    "sealed control trial id"
                )
            if episode.artifact_verified:
                binding = episode.input_binding
                if not isinstance(binding, ModelInputBinding):
                    raise AnalysisError(
                        f"training episode {episode.episode_id!r} lacks its "
                        "factory-issued model input binding"
                    )
                if (
                    not episode.provenance.artifact_verified
                    or binding.role != "CONTROL_EPISODE"
                    or binding.trial_id != episode.episode_id
                    or binding.trial_checksums_sha256
                    != episode.provenance.checksums_sha256
                ):
                    raise AnalysisError(
                        f"training episode {episode.episode_id!r} model inputs "
                        "do not match its replayed control trial"
                    )
                binding.assert_payload(
                    episode._binding_payload(),
                    role="CONTROL_EPISODE",
                )
                seal = episode.provenance.trial_bundle_seal
                if seal is None:
                    raise AnalysisError(
                        f"training episode {episode.episode_id!r} lacks its "
                        "verified calibration/topology/host seal"
                    )
                causal_ground_truth.assert_accepts_trial_bundle_seal(seal)
                verified_bindings[episode.episode_id] = binding

        boundary = episode_list[0].boundary
        if causal_ground_truth.measurement_boundary_id != boundary.boundary_id:
            raise AnalysisError(
                "causal ground-truth measurement boundary does not match "
                "the training boundary"
            )
        if self._semantic_rebind is not None:
            self._semantic_rebind.target_boundary.assert_compatible(boundary)
        validated_series: dict[
            str,
            tuple[
                dict[str, float],
                dict[str, tuple[float, ...]],
                tuple[float, ...],
            ],
        ] = {}
        for episode in episode_list:
            boundary.assert_compatible(episode.boundary)
            if episode.provenance.measurement_boundary_id != boundary.boundary_id:
                raise AnalysisError(
                    f"training episode {episode.episode_id!r} provenance uses a "
                    "different measurement boundary"
                )
            pre, exogenous, episode_targets = self._validate_series(
                identifier=episode.episode_id,
                timestamps_ns=episode.timestamps_ns,
                initial_power_watts=episode.initial_power_watts,
                pre_treatment_features=episode.pre_treatment_features,
                exogenous_features=episode.exogenous_features,
                power_watts=episode.power_watts,
            )
            if episode_targets is None:
                raise AnalysisError(
                    f"training episode {episode.episode_id!r} has no power path"
                )
            validated_series[episode.episode_id] = (
                pre,
                exogenous,
                episode_targets,
            )

        raw_rows: list[list[float]] = []
        targets: list[float] = []
        for episode in training_episodes:
            pre, exogenous, episode_targets = validated_series[episode.episode_id]
            for index, target in enumerate(episode_targets):
                previous_power = (
                    episode.initial_power_watts
                    if index == 0
                    else episode_targets[index - 1]
                )
                raw_rows.append(
                    [pre[name] for name in self.pre_treatment_feature_names]
                    + [exogenous[name][index] for name in self.exogenous_feature_names]
                    + [float(previous_power)]
                )
                targets.append(target)

        parameter_count = len(self._feature_names) + 1
        if len(raw_rows) <= parameter_count:
            raise AnalysisError(
                "dynamic linear baseline needs more training rows than parameters"
            )
        scales = {
            name: _FeatureScale.from_values([row[index] for row in raw_rows])
            for index, name in enumerate(self._feature_names)
        }
        design = [
            [1.0]
            + [
                scales[name].normalize(row[index])
                for index, name in enumerate(self._feature_names)
            ]
            for row in raw_rows
        ]
        coefficients = _ridge_fit(design, targets, self.ridge)
        residuals = [
            target
            - sum(coefficient * value for coefficient, value in zip(coefficients, row))
            for row, target in zip(design, targets)
        ]
        degrees_of_freedom = len(design) - len(coefficients)
        residual_variance = (
            sum(value * value for value in residuals) / degrees_of_freedom
        )
        training_payload = [
            {
                "episode_id": episode.episode_id,
                "arm": episode.arm,
                "provenance": episode.provenance.to_dict(),
                "model_input_binding": (
                    None
                    if episode.input_binding is None
                    else episode.input_binding.to_dict()
                ),
                "boundary": episode.boundary.to_dict(),
                "timestamps_ns": list(episode.timestamps_ns),
                "power_watts": list(episode.power_watts),
                "initial_power_watts": episode.initial_power_watts,
                "pre_treatment_features": dict(episode.pre_treatment_features),
                "exogenous_features": {
                    name: list(values)
                    for name, values in episode.exogenous_features.items()
                },
            }
            for episode in sorted(training_episodes, key=lambda item: item.episode_id)
        ]
        training_digest = hashlib.sha256(
            json.dumps(
                training_payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        training_data_id = f"sha256:{training_digest}"
        scales_document = {
            name: {
                "mean": scale.mean,
                "deviation": scale.deviation,
                "minimum": scale.minimum,
                "maximum": scale.maximum,
            }
            for name, scale in scales.items()
        }
        fitted_parameters_sha256 = _canonical_sha256(
            {
                "version": self.MODEL_VERSION,
                "boundary": boundary.to_dict(),
                "feature_names": list(self._feature_names),
                "coefficients": coefficients,
                "residual_variance": residual_variance,
                "feature_scales": scales_document,
                "ridge": self.ridge,
            },
            "fitted model parameters",
        )
        held_out_validation: HeldOutModelValidation | None = None
        if held_out_episodes:
            if group_split_validation is None:  # pragma: no cover - invariant
                raise AnalysisError("held-out episodes require a split artifact")
            observed_paths: dict[str, tuple[float, ...]] = {}
            predicted_paths: dict[str, tuple[float, ...]] = {}
            held_out_errors: list[float] = []
            held_out_ood_rows = 0
            for episode in sorted(
                held_out_episodes,
                key=lambda item: item.episode_id,
            ):
                pre, exogenous, observed_path = validated_series[
                    episode.episode_id
                ]
                predicted_path: list[float] = []
                previous_power = float(episode.initial_power_watts)
                for index, observed_power in enumerate(observed_path):
                    raw = (
                        [pre[name] for name in self.pre_treatment_feature_names]
                        + [
                            exogenous[name][index]
                            for name in self.exogenous_feature_names
                        ]
                        + [previous_power]
                    )
                    if any(
                        scales[name].ood_reason(
                            value,
                            name=name,
                            z_limit=self.ood_z_limit,
                            range_margin=self.ood_range_margin,
                        )
                        is not None
                        for name, value in zip(self._feature_names, raw)
                    ):
                        held_out_ood_rows += 1
                    row = [1.0] + [
                        scales[name].normalize(value)
                        for name, value in zip(self._feature_names, raw)
                    ]
                    predicted_power = sum(
                        coefficient * value
                        for coefficient, value in zip(coefficients, row)
                    )
                    if not math.isfinite(predicted_power) or predicted_power < 0:
                        raise AnalysisError(
                            "fitted model produced non-physical held-out power"
                        )
                    predicted_path.append(predicted_power)
                    held_out_errors.append(predicted_power - observed_power)
                    previous_power = predicted_power
                observed_paths[episode.episode_id] = observed_path
                predicted_paths[episode.episode_id] = tuple(predicted_path)
            held_out_validation = HeldOutModelValidation._issue(
                split_artifact_sha256=(
                    group_split_validation.split_artifact_sha256
                ),
                training_data_id=training_data_id,
                fitted_parameters_sha256=fitted_parameters_sha256,
                observed_paths=observed_paths,
                predicted_paths=predicted_paths,
                errors=held_out_errors,
                out_of_distribution_rows=held_out_ood_rows,
            )

        sorted_training = tuple(
            sorted(training_episodes, key=lambda item: item.episode_id)
        )
        identity_payload = {
            "version": self.MODEL_VERSION,
            "boundary": boundary.to_dict(),
            "feature_names": self._feature_names,
            "fitted_parameters_sha256": fitted_parameters_sha256,
            "ridge": self.ridge,
            "confidence": self.confidence,
            "ood_z_limit": self.ood_z_limit,
            "ood_range_margin": self.ood_range_margin,
            "semantic_rebind": (
                None
                if self._semantic_rebind is None
                else self._semantic_rebind.to_dict()
            ),
            "training_episode_ids": [
                episode.episode_id for episode in sorted_training
            ],
            "causal_ground_truth": causal_ground_truth.to_dict(),
            "accepted_control_trials": [
                episode.provenance.to_dict() for episode in sorted_training
            ],
            "control_model_inputs": [
                episode.input_binding.to_dict()
                if episode.input_binding is not None
                else None
                for episode in sorted_training
            ],
            "group_split_validation": (
                None
                if group_split_validation is None
                else group_split_validation.to_dict()
            ),
            "held_out_model_validation": (
                None
                if held_out_validation is None
                else held_out_validation.to_dict()
            ),
        }
        digest = _canonical_sha256(identity_payload, "model identity")[:16]

        self._boundary = boundary
        self._scales = scales
        self._coefficients = tuple(coefficients)
        self._residual_variance = residual_variance
        self._training_episode_ids = tuple(
            episode.episode_id for episode in sorted_training
        )
        self._control_episode_provenance = tuple(
            episode.provenance for episode in sorted_training
        )
        self._control_model_inputs = tuple(
            verified_bindings[episode.episode_id]
            for episode in sorted_training
            if episode.episode_id in verified_bindings
        )
        self._causal_ground_truth = causal_ground_truth
        self._group_split_validation = group_split_validation
        self._held_out_model_validation = held_out_validation
        self._training_data_id = training_data_id
        self._model_id = f"{self.MODEL_VERSION}:{digest}"
        return self

    def recalibrate_for_target(
        self,
        rebind: SemanticBoundaryRebind,
        target_control_episodes: Iterable[BaselineEpisode],
        *,
        causal_ground_truth: CausalGroundTruthReference,
        group_split_validation: GroupSplitValidation | None = None,
    ) -> "DynamicLinearBaseline":
        """Fit a fresh target-host model after an explicit semantic rebind.

        Source-host coefficients and raw observations are never copied or
        pooled.  The returned model is trained exclusively on the supplied
        target ``C0`` episodes and records both boundary ids in provenance.
        """

        if not self.fitted:
            raise AnalysisError("fit the source-host baseline before recalibration")
        self.boundary.assert_compatible(rebind.source_boundary)
        episodes = tuple(target_control_episodes)
        if not episodes:
            raise AnalysisError("target recalibration needs control episodes")
        for episode in episodes:
            rebind.target_boundary.assert_compatible(episode.boundary)
        target_model = DynamicLinearBaseline(
            pre_treatment_feature_names=self.pre_treatment_feature_names,
            exogenous_feature_names=self.exogenous_feature_names,
            control_arm=self.control_arm,
            ridge=self.ridge,
            confidence=self.confidence,
            ood_z_limit=self.ood_z_limit,
            ood_range_margin=self.ood_range_margin,
        )
        target_model._semantic_rebind = rebind
        return target_model.fit(
            episodes,
            causal_ground_truth=causal_ground_truth,
            group_split_validation=group_split_validation,
        )

    def _abstention(
        self,
        request: PredictionRequest,
        reason: str,
        observed_joules: float | None,
        artifact_chain_verified: bool,
    ) -> CounterfactualEstimate:
        estimate = CounterfactualEstimate(
            run_id=request.run_id,
            evidence_class=MODEL_EVIDENCE_CLASS,
            model_id=self.model_id,
            model_version=self.MODEL_VERSION,
            training_data_id=self._training_data_id,
            training_episode_ids=self._training_episode_ids,
            causal_ground_truth=self.causal_ground_truth,
            control_episode_provenance=self._control_episode_provenance,
            control_model_inputs=self._control_model_inputs,
            group_split_validation=self._group_split_validation,
            held_out_model_validation=self._held_out_model_validation,
            target_trial_provenance=request.target_trial_provenance,
            target_model_input=request.input_binding,
            semantic_rebind=self._semantic_rebind,
            outcome_boundary=self.boundary,
            estimand=request.estimand,
            interval_confidence=self.confidence,
            validation_status="ABSTAINED_OUT_OF_DISTRIBUTION",
            out_of_distribution=True,
            abstained=True,
            abstention_reason=reason,
            timestamps_ns=tuple(request.timestamps_ns),
            predicted_power_watts=(),
            lower_power_watts=(),
            upper_power_watts=(),
            observed_joules=observed_joules,
            counterfactual_joules=None,
            counterfactual_lower_joules=None,
            counterfactual_upper_joules=None,
            effect_joules=None,
            effect_lower_joules=None,
            effect_upper_joules=None,
        )
        object.__setattr__(
            estimate,
            "_artifact_chain_verified",
            artifact_chain_verified,
        )
        return estimate

    def predict(
        self,
        request: PredictionRequest,
        *,
        observed_joules: float | None = None,
        validation_status: str = "EXPLORATORY",
    ) -> CounterfactualEstimate:
        """Predict the no-program path or return an OOD abstention artifact."""

        if not self.fitted:
            raise AnalysisError("fit the dynamic linear baseline first")
        self.boundary.assert_compatible(request.boundary)
        if validation_status not in {"EXPLORATORY", "VALIDATED"}:
            raise AnalysisError(
                "validation_status must be EXPLORATORY or VALIDATED"
            )
        target_input_verified = False
        if request.artifact_verified:
            binding = request.input_binding
            if not isinstance(binding, ModelInputBinding):
                raise AnalysisError(
                    "factory-issued prediction request lacks its model input binding"
                )
            if (
                not request.target_trial_provenance.artifact_verified
                or binding.role != "TARGET_REQUEST"
                or binding.trial_id != request.run_id
                or binding.trial_checksums_sha256
                != request.target_trial_provenance.checksums_sha256
            ):
                raise AnalysisError(
                    "prediction model inputs do not match the replayed target trial"
                )
            seal = request.target_trial_provenance.trial_bundle_seal
            if seal is None:
                raise AnalysisError(
                    "prediction target lacks its verified calibration/topology/host seal"
                )
            self.causal_ground_truth.assert_accepts_trial_bundle_seal(seal)
            binding.assert_payload(
                request._binding_payload(),
                role="TARGET_REQUEST",
            )
            target_input_verified = True
        control_input_pairs = {
            binding.trial_id: binding.trial_checksums_sha256
            for binding in self._control_model_inputs
            if binding.artifact_verified and binding.role == "CONTROL_EPISODE"
        }
        control_trial_pairs = {
            provenance.trial_id: provenance.checksums_sha256
            for provenance in self._control_episode_provenance
        }
        artifact_chain_verified = (
            self.causal_ground_truth.artifact_verified
            and all(
                provenance.artifact_verified
                for provenance in self._control_episode_provenance
            )
            and len(self._control_model_inputs)
            == len(self._control_episode_provenance)
            and control_input_pairs == control_trial_pairs
            and self._group_split_validation is not None
            and self._group_split_validation.artifact_verified
            and self._held_out_model_validation is not None
            and self._held_out_model_validation.artifact_verified
            and self._held_out_model_validation.split_artifact_sha256
            == self._group_split_validation.split_artifact_sha256
            and self._held_out_model_validation.training_data_id
            == self._training_data_id
            and request.target_trial_provenance.artifact_verified
            and target_input_verified
        )
        if validation_status == "VALIDATED" and not artifact_chain_verified:
            raise AnalysisError(
                "VALIDATED prediction requires factory-verified causal ground "
                "truth, replay-bound train/test controls, held-out metrics, "
                "target model inputs, group split, and target trial"
            )
        observed = request.target_trial_provenance.observed_joules
        if observed_joules is not None:
            supplied_observed = _finite_number(observed_joules, "observed_joules")
            if supplied_observed < 0:
                raise AnalysisError("observed_joules must be non-negative")
            if not math.isclose(
                supplied_observed,
                observed,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise AnalysisError(
                    "observed_joules does not match accepted target trial provenance"
                )
        pre, exogenous, _ = self._validate_series(
            identifier=request.run_id,
            timestamps_ns=request.timestamps_ns,
            initial_power_watts=request.initial_power_watts,
            pre_treatment_features=request.pre_treatment_features,
            exogenous_features=request.exogenous_features,
        )

        predicted: list[float] = []
        variances: list[float] = []
        previous_power = float(request.initial_power_watts)
        previous_variance = 0.0
        state_name = "state:previous_power_watts"
        state_scale = self._scales[state_name]
        state_coefficient = self._coefficients[-1]
        phi = state_coefficient / state_scale.divisor

        for index in range(len(request.timestamps_ns)):
            raw = (
                [pre[name] for name in self.pre_treatment_feature_names]
                + [exogenous[name][index] for name in self.exogenous_feature_names]
                + [previous_power]
            )
            for name, value in zip(self._feature_names, raw):
                reason = self._scales[name].ood_reason(
                    value,
                    name=name,
                    z_limit=self.ood_z_limit,
                    range_margin=self.ood_range_margin,
                )
                if reason is not None:
                    return self._abstention(
                        request,
                        reason,
                        observed,
                        artifact_chain_verified,
                    )
            row = [1.0] + [
                self._scales[name].normalize(value)
                for name, value in zip(self._feature_names, raw)
            ]
            mean = sum(
                coefficient * value
                for coefficient, value in zip(self._coefficients, row)
            )
            if not math.isfinite(mean) or mean < 0:
                return self._abstention(
                    request,
                    "model produced non-physical predicted power",
                    observed,
                    artifact_chain_verified,
                )
            variance = self._residual_variance + phi * phi * previous_variance
            predicted.append(mean)
            variances.append(variance)
            previous_power = mean
            previous_variance = variance

        critical = NormalDist().inv_cdf((1 + self.confidence) / 2)
        lower_power = tuple(
            max(0.0, mean - critical * math.sqrt(max(variance, 0.0)))
            for mean, variance in zip(predicted, variances)
        )
        upper_power = tuple(
            mean + critical * math.sqrt(max(variance, 0.0))
            for mean, variance in zip(predicted, variances)
        )
        weights = _integration_weights(request.timestamps_ns)
        counterfactual = sum(weight * mean for weight, mean in zip(weights, predicted))
        # Preserve an independently replayable interval: the serialized lower
        # and upper power paths are integrated with the same trapezoidal rule.
        # Artifact validators can therefore recompute all three energies
        # without access to hidden model covariance state.
        counterfactual_lower = sum(
            weight * value for weight, value in zip(weights, lower_power)
        )
        counterfactual_upper = sum(
            weight * value for weight, value in zip(weights, upper_power)
        )
        effect = None if observed is None else observed - counterfactual
        effect_lower = None if observed is None else observed - counterfactual_upper
        effect_upper = None if observed is None else observed - counterfactual_lower

        estimate = CounterfactualEstimate(
            run_id=request.run_id,
            evidence_class=MODEL_EVIDENCE_CLASS,
            model_id=self.model_id,
            model_version=self.MODEL_VERSION,
            training_data_id=self._training_data_id,
            training_episode_ids=self._training_episode_ids,
            causal_ground_truth=self.causal_ground_truth,
            control_episode_provenance=self._control_episode_provenance,
            control_model_inputs=self._control_model_inputs,
            group_split_validation=self._group_split_validation,
            held_out_model_validation=self._held_out_model_validation,
            target_trial_provenance=request.target_trial_provenance,
            target_model_input=request.input_binding,
            semantic_rebind=self._semantic_rebind,
            outcome_boundary=self.boundary,
            estimand=request.estimand,
            interval_confidence=self.confidence,
            validation_status=validation_status,
            out_of_distribution=False,
            abstained=False,
            abstention_reason=None,
            timestamps_ns=tuple(request.timestamps_ns),
            predicted_power_watts=tuple(predicted),
            lower_power_watts=lower_power,
            upper_power_watts=upper_power,
            observed_joules=observed,
            counterfactual_joules=counterfactual,
            counterfactual_lower_joules=counterfactual_lower,
            counterfactual_upper_joules=counterfactual_upper,
            effect_joules=effect,
            effect_lower_joules=effect_lower,
            effect_upper_joules=effect_upper,
        )
        object.__setattr__(
            estimate,
            "_artifact_chain_verified",
            artifact_chain_verified,
        )
        return estimate
