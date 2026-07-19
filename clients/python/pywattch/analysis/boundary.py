"""Canonical measurement-boundary declarations for offline Wattch analysis.

The JSON shape in this module mirrors
``wattch_core::research::MeasurementBoundary``.  Local source ids are
capture-local and deliberately absent; compatibility instead freezes stable
source semantics, evidence class, clock domain, and complete ancestry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from .._messages import SourceDescriptor
from ..errors import WattchError

DISJOINT_GROUP_LABEL = "wattch.disjoint_group"
DISJOINT_MEMBER_LABEL = "wattch.disjoint_member"
INTERVAL_DURATION_SOURCE_UID_LABEL = "wattch.interval_duration_source_uid"
INTERVAL_ANCHOR_LABEL = "wattch.interval_anchor"
INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP = (
    "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP"
)
INTERVAL_TIMELINE_RECONSTRUCTION_LABEL = (
    "wattch.interval_timeline_reconstruction"
)
INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1 = (
    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"
)


class AnalysisError(WattchError):
    """Base error for invalid or unsupported offline analysis."""


class BoundaryMismatchError(AnalysisError):
    """Raised when outcomes do not describe the same measurement boundary."""


class AggregationRule(str, Enum):
    """Canonical permitted aggregation for primary energy sources."""

    SINGLE_SOURCE = "SINGLE_SOURCE"
    SUM_DECLARED_DISJOINT = "SUM_DECLARED_DISJOINT"


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisError(f"{field_name} must be a non-empty string")
    return value


def _validate_interval_average_descriptor(
    source: SourceDescriptor,
    by_uid: Mapping[str, SourceDescriptor],
) -> SourceDescriptor:
    """Resolve and validate the raw duration evidence for an interval average."""

    duration_uid = source.labels.get(INTERVAL_DURATION_SOURCE_UID_LABEL)
    if not duration_uid:
        raise BoundaryMismatchError(
            f"INTERVAL_AVERAGE source {source.source_uid!r} lacks descriptor "
            f"label {INTERVAL_DURATION_SOURCE_UID_LABEL!r}"
        )
    if (
        source.labels.get(INTERVAL_ANCHOR_LABEL)
        != INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP
    ):
        raise BoundaryMismatchError(
            f"INTERVAL_AVERAGE source {source.source_uid!r} must declare "
            f"{INTERVAL_ANCHOR_LABEL!r}="
            f"{INTERVAL_ENDS_AT_DAEMON_RECEIPT_TIMESTAMP!r}"
        )
    reconstruction = source.labels.get(INTERVAL_TIMELINE_RECONSTRUCTION_LABEL)
    if (
        reconstruction is not None
        and reconstruction != INTERVAL_AFFINE_NATIVE_TO_DAEMON_RECEIPTS_V1
    ):
        raise BoundaryMismatchError(
            f"INTERVAL_AVERAGE source {source.source_uid!r} declares unsupported "
            f"{INTERVAL_TIMELINE_RECONSTRUCTION_LABEL!r}={reconstruction!r}"
        )
    duration = by_uid.get(duration_uid)
    if duration is None:
        raise BoundaryMismatchError(
            f"trial is missing linked interval duration source {duration_uid!r}"
        )
    if (
        duration.kind,
        duration.unit,
        duration.temporality,
    ) != ("SAMPLE_INTERVAL_DURATION", "NANOSECOND", "DELTA"):
        raise BoundaryMismatchError(
            f"linked source {duration_uid!r} must be "
            "SAMPLE_INTERVAL_DURATION/NANOSECOND/DELTA"
        )
    if (
        duration.node_id,
        duration.backend,
        duration.clock_domain,
        duration.evidence_class,
    ) != (
        source.node_id,
        source.backend,
        source.clock_domain,
        source.evidence_class,
    ):
        raise BoundaryMismatchError(
            f"linked source {duration_uid!r} must share node, backend, clock, "
            "and evidence class with its interval average"
        )
    return duration


@dataclass(frozen=True, order=True)
class BoundarySourceSignature:
    """Stable source semantics required by a canonical boundary."""

    source_uid: str
    kind: str
    unit: str
    temporality: str
    evidence_class: str
    parent_source_uid: str | None
    clock_domain: str
    disjoint_group: str | None = None
    disjoint_member: str | None = None
    interval_duration_source_uid: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "source_uid",
            "kind",
            "unit",
            "temporality",
            "evidence_class",
            "clock_domain",
        ):
            _text(getattr(self, field_name), field_name)
        if self.parent_source_uid is not None:
            _text(self.parent_source_uid, "parent_source_uid")
            if self.parent_source_uid == self.source_uid:
                raise AnalysisError("a source cannot be its own parent")
        if (self.disjoint_group is None) != (self.disjoint_member is None):
            raise AnalysisError(
                "a source signature must provide both disjoint_group and "
                "disjoint_member or neither"
            )
        if self.disjoint_group is not None:
            _text(self.disjoint_group, "disjoint_group")
            _text(self.disjoint_member, "disjoint_member")
        if self.temporality == "INTERVAL_AVERAGE":
            if self.interval_duration_source_uid is None:
                raise AnalysisError(
                    "an INTERVAL_AVERAGE source signature must freeze its "
                    "interval_duration_source_uid"
                )
            _text(
                self.interval_duration_source_uid,
                "interval_duration_source_uid",
            )
        elif self.interval_duration_source_uid is not None:
            raise AnalysisError(
                "only INTERVAL_AVERAGE sources may declare an "
                "interval_duration_source_uid"
            )

    @classmethod
    def from_descriptor(cls, source: SourceDescriptor) -> "BoundarySourceSignature":
        return cls(
            source_uid=source.source_uid,
            kind=source.kind,
            unit=source.unit,
            temporality=source.temporality,
            evidence_class=source.evidence_class,
            parent_source_uid=source.parent_source_uid,
            clock_domain=source.clock_domain,
            disjoint_group=source.labels.get(DISJOINT_GROUP_LABEL),
            disjoint_member=source.labels.get(DISJOINT_MEMBER_LABEL),
            interval_duration_source_uid=source.labels.get(
                INTERVAL_DURATION_SOURCE_UID_LABEL
            ),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "BoundarySourceSignature":
        try:
            parent = data["parent_source_uid"]
            if parent is not None and not isinstance(parent, str):
                raise AnalysisError("parent_source_uid must be a string or null")
            disjoint_group = data.get("disjoint_group")
            if disjoint_group is not None and not isinstance(disjoint_group, str):
                raise AnalysisError("disjoint_group must be a string or null")
            disjoint_member = data.get("disjoint_member")
            if disjoint_member is not None and not isinstance(disjoint_member, str):
                raise AnalysisError("disjoint_member must be a string or null")
            interval_duration_source_uid = data.get(
                "interval_duration_source_uid"
            )
            if interval_duration_source_uid is not None and not isinstance(
                interval_duration_source_uid, str
            ):
                raise AnalysisError(
                    "interval_duration_source_uid must be a string or null"
                )
            return cls(
                source_uid=_text(data["source_uid"], "source_uid"),
                kind=_text(data["kind"], "kind"),
                unit=_text(data["unit"], "unit"),
                temporality=_text(data["temporality"], "temporality"),
                evidence_class=_text(data["evidence_class"], "evidence_class"),
                parent_source_uid=parent,
                clock_domain=_text(data["clock_domain"], "clock_domain"),
                disjoint_group=disjoint_group,
                disjoint_member=disjoint_member,
                interval_duration_source_uid=interval_duration_source_uid,
            )
        except KeyError as exc:
            raise AnalysisError(
                f"required source signature is missing {exc.args[0]!r}"
            ) from exc

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "source_uid": self.source_uid,
            "kind": self.kind,
            "unit": self.unit,
            "temporality": self.temporality,
            "evidence_class": self.evidence_class,
            "parent_source_uid": self.parent_source_uid,
            "clock_domain": self.clock_domain,
            "disjoint_group": self.disjoint_group,
            "disjoint_member": self.disjoint_member,
        }
        if self.interval_duration_source_uid is not None:
            document["interval_duration_source_uid"] = (
                self.interval_duration_source_uid
            )
        return document


@dataclass(frozen=True)
class MeasurementBoundary:
    """Canonical identity and aggregation contract for one energy outcome."""

    boundary_id: str
    backend: str
    primary_source_uids: tuple[str, ...]
    aggregation_rule: AggregationRule
    required_sources: tuple[BoundarySourceSignature, ...]

    def __post_init__(self) -> None:
        _text(self.boundary_id, "boundary_id")
        _text(self.backend, "backend")
        if not self.primary_source_uids:
            raise AnalysisError("measurement boundary needs a primary source")
        for source_uid in self.primary_source_uids:
            _text(source_uid, "primary_source_uid")
        if len(self.primary_source_uids) != len(set(self.primary_source_uids)):
            raise AnalysisError("primary_source_uids must be unique")
        if not isinstance(self.aggregation_rule, AggregationRule):
            try:
                object.__setattr__(
                    self, "aggregation_rule", AggregationRule(self.aggregation_rule)
                )
            except ValueError as exc:
                raise AnalysisError(
                    f"unsupported aggregation rule {self.aggregation_rule!r}"
                ) from exc
        if not self.required_sources:
            raise AnalysisError("measurement boundary needs required_sources")
        required_by_uid: dict[str, BoundarySourceSignature] = {}
        for signature in self.required_sources:
            if not isinstance(signature, BoundarySourceSignature):
                raise AnalysisError(
                    "required_sources must contain BoundarySourceSignature values"
                )
            if signature.source_uid in required_by_uid:
                raise AnalysisError(
                    f"duplicate required source {signature.source_uid!r}"
                )
            required_by_uid[signature.source_uid] = signature

        missing = set(self.primary_source_uids) - set(required_by_uid)
        if missing:
            raise AnalysisError(
                "required_sources omit primary sources: " + ", ".join(sorted(missing))
            )

        used_topology: set[str] = set()
        for primary_uid in self.primary_source_uids:
            current_uid: str | None = primary_uid
            chain: set[str] = set()
            while current_uid is not None:
                if current_uid in chain:
                    raise AnalysisError(
                        f"source topology contains a cycle at {current_uid!r}"
                    )
                chain.add(current_uid)
                used_topology.add(current_uid)
                signature = required_by_uid.get(current_uid)
                if signature is None:
                    raise AnalysisError(
                        f"required_sources omit ancestor {current_uid!r}"
                    )
                current_uid = signature.parent_source_uid
        unused = set(required_by_uid) - used_topology
        if unused:
            raise AnalysisError(
                "required_sources contain unrelated sources: "
                + ", ".join(sorted(unused))
            )

        if self.aggregation_rule == AggregationRule.SINGLE_SOURCE:
            if len(self.primary_source_uids) != 1:
                raise AnalysisError(
                    "SINGLE_SOURCE boundaries require exactly one primary source"
                )
        else:
            if len(self.primary_source_uids) < 2:
                raise AnalysisError(
                    "SUM_DECLARED_DISJOINT boundaries require at least two "
                    "primary sources"
                )
            primary_set = set(self.primary_source_uids)
            for uid in self.primary_source_uids:
                parent = required_by_uid[uid].parent_source_uid
                while parent is not None:
                    if parent in primary_set:
                        raise AnalysisError(
                            "SUM_DECLARED_DISJOINT cannot combine nested source "
                            f"{uid!r} with ancestor {parent!r}"
                        )
                    parent = required_by_uid[parent].parent_source_uid
            first = required_by_uid[self.primary_source_uids[0]]
            if first.disjoint_group is None:
                raise AnalysisError(
                    "SUM_DECLARED_DISJOINT primary source "
                    f"{first.source_uid!r} lacks descriptor label "
                    f"{DISJOINT_GROUP_LABEL!r}"
                )
            if first.disjoint_member is None:
                raise AnalysisError(
                    "SUM_DECLARED_DISJOINT primary source "
                    f"{first.source_uid!r} lacks descriptor label "
                    f"{DISJOINT_MEMBER_LABEL!r}"
                )
            disjoint_members = {first.disjoint_member}
            for uid in self.primary_source_uids[1:]:
                current = required_by_uid[uid]
                if (
                    current.unit,
                    current.temporality,
                    current.clock_domain,
                ) != (first.unit, first.temporality, first.clock_domain):
                    raise AnalysisError(
                        "SUM_DECLARED_DISJOINT sources must share unit, "
                        "temporality, and clock_domain"
                    )
                if current.disjoint_group != first.disjoint_group:
                    raise AnalysisError(
                        "SUM_DECLARED_DISJOINT sources must share descriptor "
                        f"label {DISJOINT_GROUP_LABEL!r}="
                        f"{first.disjoint_group!r}"
                    )
                if current.disjoint_member is None:
                    raise AnalysisError(
                        "SUM_DECLARED_DISJOINT primary source "
                        f"{current.source_uid!r} lacks descriptor label "
                        f"{DISJOINT_MEMBER_LABEL!r}"
                    )
                if current.disjoint_member in disjoint_members:
                    raise AnalysisError(
                        "SUM_DECLARED_DISJOINT sources must have unique "
                        f"{DISJOINT_MEMBER_LABEL!r} values within group "
                        f"{first.disjoint_group!r}"
                    )
                disjoint_members.add(current.disjoint_member)

    @property
    def primary_sources(self) -> tuple[BoundarySourceSignature, ...]:
        """Signatures of only the declared primary sources."""

        by_uid = {source.source_uid: source for source in self.required_sources}
        return tuple(by_uid[uid] for uid in self.primary_source_uids)

    @property
    def evidence_classes(self) -> tuple[str, ...]:
        return tuple(sorted({source.evidence_class for source in self.primary_sources}))

    @classmethod
    def from_descriptors(
        cls,
        descriptors: Iterable[SourceDescriptor],
        primary_source_uids: Iterable[str],
        aggregation_rule: AggregationRule | str = AggregationRule.SINGLE_SOURCE,
        *,
        boundary_id: str,
    ) -> "MeasurementBoundary":
        """Freeze the canonical boundary and complete ancestry from a run."""

        by_uid: dict[str, SourceDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.source_uid in by_uid:
                raise AnalysisError(
                    f"duplicate source uid {descriptor.source_uid!r} in descriptors"
                )
            by_uid[descriptor.source_uid] = descriptor
        requested = tuple(primary_source_uids)
        if len(requested) != len(set(requested)):
            raise AnalysisError("primary_source_uids must be unique")
        if not requested:
            raise AnalysisError("primary_source_uids must not be empty")
        missing = set(requested) - set(by_uid)
        if missing:
            raise AnalysisError(
                "unknown primary source uids: " + ", ".join(sorted(missing))
            )
        selected = tuple(by_uid[uid] for uid in requested)
        for source in selected:
            if source.temporality == "INTERVAL_AVERAGE":
                _validate_interval_average_descriptor(source, by_uid)
        backends = {source.backend for source in selected}
        if len(backends) != 1:
            raise AnalysisError(
                "one measurement boundary cannot mix daemon backends: "
                + ", ".join(sorted(backends))
            )

        required: dict[str, BoundarySourceSignature] = {}
        for source in selected:
            current = source
            seen: set[str] = set()
            while True:
                if current.source_uid in seen:
                    raise AnalysisError(
                        f"source topology contains a cycle at {current.source_uid!r}"
                    )
                seen.add(current.source_uid)
                if current.backend != selected[0].backend:
                    raise AnalysisError(
                        f"required source {current.source_uid!r} uses backend "
                        f"{current.backend!r}, expected {selected[0].backend!r}"
                    )
                required[current.source_uid] = BoundarySourceSignature.from_descriptor(
                    current
                )
                if current.parent_source_uid is None:
                    break
                try:
                    current = by_uid[current.parent_source_uid]
                except KeyError as exc:
                    raise AnalysisError(
                        f"source {current.source_uid!r} references missing parent "
                        f"{current.parent_source_uid!r}"
                    ) from exc
        try:
            rule = AggregationRule(aggregation_rule)
        except ValueError as exc:
            raise AnalysisError(
                f"unsupported aggregation rule {aggregation_rule!r}"
            ) from exc
        return cls(
            boundary_id=boundary_id,
            backend=selected[0].backend,
            primary_source_uids=requested,
            aggregation_rule=rule,
            required_sources=tuple(sorted(required.values())),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MeasurementBoundary":
        """Parse the exact Rust-compatible JSON object."""

        try:
            primary = data["primary_source_uids"]
            required = data["required_sources"]
            if not isinstance(primary, (list, tuple)) or not all(
                isinstance(item, str) for item in primary
            ):
                raise AnalysisError("primary_source_uids must be an array of strings")
            if not isinstance(required, (list, tuple)) or not all(
                isinstance(item, Mapping) for item in required
            ):
                raise AnalysisError("required_sources must be an array of objects")
            try:
                rule = AggregationRule(data["aggregation_rule"])
            except (TypeError, ValueError) as exc:
                raise AnalysisError(
                    f"unsupported aggregation rule {data['aggregation_rule']!r}"
                ) from exc
            return cls(
                boundary_id=_text(data["boundary_id"], "boundary_id"),
                backend=_text(data["backend"], "backend"),
                primary_source_uids=tuple(primary),
                aggregation_rule=rule,
                required_sources=tuple(
                    BoundarySourceSignature.from_dict(item) for item in required
                ),
            )
        except KeyError as exc:
            raise AnalysisError(
                f"measurement boundary is missing {exc.args[0]!r}"
            ) from exc

    def validate_descriptors(
        self, descriptors: Iterable[SourceDescriptor]
    ) -> tuple[SourceDescriptor, ...]:
        """Resolve primary descriptors and require the frozen boundary exactly."""

        by_uid: dict[str, SourceDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.source_uid in by_uid:
                raise BoundaryMismatchError(
                    f"duplicate source uid {descriptor.source_uid!r} in trial"
                )
            by_uid[descriptor.source_uid] = descriptor
        for required in self.required_sources:
            descriptor = by_uid.get(required.source_uid)
            if descriptor is None:
                raise BoundaryMismatchError(
                    f"trial is missing required source {required.source_uid!r}"
                )
            if descriptor.backend != self.backend:
                raise BoundaryMismatchError(
                    f"backend mismatch for source {required.source_uid!r}"
                )
            if BoundarySourceSignature.from_descriptor(descriptor) != required:
                raise BoundaryMismatchError(
                    f"source semantics, evidence class, topology, or clock domain "
                    f"changed for {required.source_uid!r}"
                )
            if descriptor.temporality == "INTERVAL_AVERAGE":
                _validate_interval_average_descriptor(descriptor, by_uid)
        return tuple(by_uid[uid] for uid in self.primary_source_uids)

    def assert_compatible(self, other: "MeasurementBoundary") -> None:
        """Reject raw-joule comparison across different boundaries."""

        if not isinstance(other, MeasurementBoundary):
            raise BoundaryMismatchError("comparison has no measurement boundary")
        if self != other:
            differences: list[str] = []
            if self.boundary_id != other.boundary_id:
                differences.append("boundary_id")
            if self.backend != other.backend:
                differences.append("backend")
            if self.primary_source_uids != other.primary_source_uids:
                differences.append("primary_source_uids")
            if self.aggregation_rule != other.aggregation_rule:
                differences.append("aggregation_rule")
            if self.required_sources != other.required_sources:
                differences.append("required_sources")
            raise BoundaryMismatchError(
                "incompatible measurement boundaries ("
                + ", ".join(differences)
                + "); compare effect direction or standardized effects across "
                "backends instead of raw joules"
            )

    def to_dict(self) -> dict[str, object]:
        """Return the exact Rust ``MeasurementBoundary`` JSON shape."""

        return {
            "boundary_id": self.boundary_id,
            "backend": self.backend,
            "primary_source_uids": list(self.primary_source_uids),
            "aggregation_rule": self.aggregation_rule.value,
            "required_sources": [source.to_dict() for source in self.required_sources],
        }


@dataclass(frozen=True)
class SemanticBoundaryRebind:
    """Explicit same-backend semantic mapping between two host boundaries.

    The declaration permits target-host recalibration.  It does not make raw
    joules poolable and it does not make the boundaries directly compatible.
    """

    source_boundary: MeasurementBoundary
    target_boundary: MeasurementBoundary
    source_to_target_uids: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.source_boundary.backend != self.target_boundary.backend:
            raise BoundaryMismatchError(
                "semantic rebind requires the same daemon backend"
            )
        if (
            self.source_boundary.aggregation_rule
            != self.target_boundary.aggregation_rule
        ):
            raise BoundaryMismatchError(
                "semantic rebind requires the same aggregation rule"
            )
        mapping = dict(self.source_to_target_uids)
        if len(mapping) != len(self.source_to_target_uids):
            raise BoundaryMismatchError("semantic rebind source uids must be unique")
        if len(set(mapping.values())) != len(mapping):
            raise BoundaryMismatchError("semantic rebind target uids must be unique")
        source_required = {
            item.source_uid: item for item in self.source_boundary.required_sources
        }
        target_required = {
            item.source_uid: item for item in self.target_boundary.required_sources
        }
        if set(mapping) != set(source_required):
            raise BoundaryMismatchError(
                "semantic rebind must map every source required by the source boundary"
            )
        if set(mapping.values()) != set(target_required):
            raise BoundaryMismatchError(
                "semantic rebind must map every source required by the target boundary"
            )
        mapped_primaries = tuple(
            mapping[uid] for uid in self.source_boundary.primary_source_uids
        )
        if mapped_primaries != self.target_boundary.primary_source_uids:
            raise BoundaryMismatchError(
                "semantic rebind must preserve primary source ordering"
            )
        for source_uid, target_uid in mapping.items():
            source = source_required[source_uid]
            target = target_required[target_uid]
            if (
                source.kind,
                source.unit,
                source.temporality,
                source.evidence_class,
                source.clock_domain,
                source.disjoint_group,
                source.disjoint_member,
                source.interval_duration_source_uid is not None,
            ) != (
                target.kind,
                target.unit,
                target.temporality,
                target.evidence_class,
                target.clock_domain,
                target.disjoint_group,
                target.disjoint_member,
                target.interval_duration_source_uid is not None,
            ):
                raise BoundaryMismatchError(
                    f"semantic source signature changed for {source_uid!r}"
                )
            mapped_parent = (
                None
                if source.parent_source_uid is None
                else mapping[source.parent_source_uid]
            )
            if target.parent_source_uid != mapped_parent:
                raise BoundaryMismatchError(
                    f"semantic topology changed for {source_uid!r}"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_boundary_id": self.source_boundary.boundary_id,
            "target_boundary_id": self.target_boundary.boundary_id,
            "backend": self.source_boundary.backend,
            "aggregation_rule": self.source_boundary.aggregation_rule.value,
            "raw_joule_pooling_permitted": False,
            "source_to_target_uids": [
                {"source_uid": source_uid, "target_uid": target_uid}
                for source_uid, target_uid in self.source_to_target_uids
            ],
        }


def validate_semantic_rebind(
    *,
    source_boundary: MeasurementBoundary,
    target_boundary: MeasurementBoundary,
    source_descriptors: Iterable[SourceDescriptor],
    target_descriptors: Iterable[SourceDescriptor],
    source_to_target_uids: Mapping[str, str],
) -> SemanticBoundaryRebind:
    """Validate an explicit second-host mapping, including ``source_path``.

    Node ids and stable uids may differ.  Backend, source semantics, evidence,
    clock domain, topology, and descriptor ``source_path`` must match.
    """

    source_descriptor_list = tuple(source_descriptors)
    target_descriptor_list = tuple(target_descriptors)
    source_boundary.validate_descriptors(source_descriptor_list)
    target_boundary.validate_descriptors(target_descriptor_list)
    rebind = SemanticBoundaryRebind(
        source_boundary=source_boundary,
        target_boundary=target_boundary,
        source_to_target_uids=tuple(source_to_target_uids.items()),
    )
    source_by_uid = {
        descriptor.source_uid: descriptor for descriptor in source_descriptor_list
    }
    target_by_uid = {
        descriptor.source_uid: descriptor for descriptor in target_descriptor_list
    }
    for source_uid, target_uid in rebind.source_to_target_uids:
        source = source_by_uid[source_uid]
        target = target_by_uid[target_uid]
        if source.source_path != target.source_path:
            raise BoundaryMismatchError(
                f"semantic source_path changed for {source_uid!r}: "
                f"{source.source_path!r} != {target.source_path!r}"
            )
        if source.temporality == "INTERVAL_AVERAGE":
            source_duration = _validate_interval_average_descriptor(
                source, source_by_uid
            )
            target_duration = _validate_interval_average_descriptor(
                target, target_by_uid
            )
            if (
                source_duration.kind,
                source_duration.unit,
                source_duration.temporality,
                source_duration.evidence_class,
                source_duration.source_path,
            ) != (
                target_duration.kind,
                target_duration.unit,
                target_duration.temporality,
                target_duration.evidence_class,
                target_duration.source_path,
            ):
                raise BoundaryMismatchError(
                    f"interval duration semantics changed for {source_uid!r}"
                )
    return rebind
