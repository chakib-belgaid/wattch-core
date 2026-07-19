import pytest

from pywattch import RawSample, SourceDescriptor
from pywattch.analysis import (
    AggregationRule,
    AnalysisError,
    BoundaryMismatchError,
    Estimand,
    MeasurementBoundary,
    compute_energy_outcome,
)


def descriptor(
    local_id: int,
    uid: str,
    *,
    backend: str = "rapl",
    kind: str = "CPU_PACKAGE_ENERGY",
    unit: str = "MICROJOULE",
    temporality: str = "CUMULATIVE",
    evidence_class: str = "RAW_HARDWARE_COUNTER",
    parent: str | None = None,
    max_value: float | None = 1_000.0,
    labels: dict[str, str] | None = None,
) -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=local_id,
        source_uid=uid,
        node_id="node-a",
        backend=backend,
        kind=kind,
        unit=unit,
        temporality=temporality,
        evidence_class=evidence_class,
        source_path=uid.removeprefix(f"{backend}://"),
        clock_domain="monotonic",
        sampling_mode="POLLED",
        parent_source_uid=parent,
        max_value=max_value,
        labels={} if labels is None else labels,
    )


def samples(source_id: int, values: list[float]) -> list[RawSample]:
    return [
        RawSample(
            source_id=source_id,
            sequence_no=index,
            monotonic_ns=index * 1_000_000_000,
            value=value,
        )
        for index, value in enumerate(values)
    ]


def interval_average_descriptors() -> tuple[SourceDescriptor, SourceDescriptor]:
    duration_uid = "powermetrics://node-a/frame/elapsed_ns"
    power = descriptor(
        7,
        "powermetrics://node-a/cpu/power",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INTERVAL_AVERAGE",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={
            "wattch.interval_duration_source_uid": duration_uid,
            "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
        },
    )
    duration = descriptor(
        8,
        duration_uid,
        backend="powermetrics",
        kind="SAMPLE_INTERVAL_DURATION",
        unit="NANOSECOND",
        temporality="DELTA",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
    )
    return power, duration


def test_boundary_rejects_nested_package_and_dram_sum():
    package_uid = "rapl://node-a/package/0"
    descriptors = [
        descriptor(1, package_uid),
        descriptor(
            2,
            "rapl://node-a/package/0/dram",
            kind="DRAM_ENERGY",
            parent=package_uid,
        ),
    ]

    with pytest.raises(AnalysisError, match="nested source"):
        MeasurementBoundary.from_descriptors(
            descriptors,
            [source.source_uid for source in descriptors],
            AggregationRule.SUM_DECLARED_DISJOINT,
            boundary_id="nested",
        )


def test_boundary_compatibility_ignores_run_local_ids_but_not_evidence_class():
    uid = "rapl://node-a/package/0"
    original = descriptor(1, uid)
    boundary = MeasurementBoundary.from_descriptors(
        [original], [uid], boundary_id="cpu-package"
    )
    renumbered = descriptor(99, uid)
    same = MeasurementBoundary.from_descriptors(
        [renumbered], [uid], boundary_id="cpu-package"
    )
    boundary.assert_compatible(same)
    assert boundary.validate_descriptors([renumbered]) == (renumbered,)

    changed = descriptor(99, uid, evidence_class="TOOL_DERIVED_ESTIMATE")
    changed_boundary = MeasurementBoundary.from_descriptors(
        [changed], [uid], boundary_id="cpu-package"
    )
    with pytest.raises(BoundaryMismatchError, match="required_sources"):
        boundary.assert_compatible(changed_boundary)
    with pytest.raises(BoundaryMismatchError, match="evidence class"):
        boundary.validate_descriptors([changed])

    document = boundary.to_dict()
    assert set(document) == {
        "boundary_id",
        "backend",
        "primary_source_uids",
        "aggregation_rule",
        "required_sources",
    }
    assert document["required_sources"][0]["clock_domain"] == "monotonic"
    assert MeasurementBoundary.from_dict(document) == boundary


def test_wrap_corrected_cumulative_outcome_interpolates_window_edges():
    source = descriptor(1, "rapl://node-a/package/0")
    boundary = MeasurementBoundary.from_descriptors(
        [source], [source.source_uid], boundary_id="cpu-package"
    )

    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[source],
        samples=samples(1, [900.0, 100.0, 300.0]),
        start_monotonic_ns=500_000_000,
        end_monotonic_ns=1_500_000_000,
        estimand=Estimand.FIXED_HORIZON,
    )

    # Unwrapped counter: 0 -> 200 -> 400 uJ; window is 100 -> 300 uJ.
    assert outcome.total_joules == pytest.approx(200e-6)
    assert outcome.integration_methods[source.source_uid] == "COUNTER_DELTA"
    assert outcome.estimated is False


def test_legacy_instant_milliwatts_remain_trapezoidal_and_preserve_evidence():
    source = descriptor(
        7,
        "powermetrics://node-a/cpu/power",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
    )
    boundary = MeasurementBoundary.from_descriptors(
        [source], [source.source_uid], boundary_id="powermetrics-cpu"
    )

    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[source],
        samples=samples(7, [10_000.0, 10_000.0, 10_000.0]),
        start_monotonic_ns=0,
        end_monotonic_ns=2_000_000_000,
        estimand="FIXED_HORIZON",
    )

    assert outcome.total_joules == pytest.approx(20.0)
    assert outcome.estimated is True
    assert boundary.evidence_classes == ("TOOL_DERIVED_ESTIMATE",)
    required = outcome.to_dict()["measurement_boundary"]["required_sources"]
    assert required[0]["evidence_class"] == "TOOL_DERIVED_ESTIMATE"


def test_interval_average_power_uses_exact_overlap_not_trapezoids():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    raw = [
        RawSample(7, 0, 1_000_000_000, 10_000.0),
        RawSample(8, 0, 1_000_000_000, 1_000_000_000),
        RawSample(7, 1, 2_000_000_000, 30_000.0),
        RawSample(8, 1, 2_000_000_000, 1_000_000_000),
    ]

    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[power, duration],
        samples=raw,
        start_monotonic_ns=1_000_000_000,
        end_monotonic_ns=2_000_000_000,
        estimand="FIXED_HORIZON",
    )

    # The second native frame says 30 W averaged over [1 s, 2 s]. A
    # trapezoid between the 10 W and 30 W receipt points would incorrectly
    # produce 20 J.
    assert outcome.total_joules == pytest.approx(30.0)
    assert outcome.integration_methods[power.source_uid] == (
        "INTERVAL_AVERAGE_POWER_OVERLAP"
    )
    assert outcome.estimated is True
    signature = boundary.to_dict()["required_sources"][0]
    assert signature["interval_duration_source_uid"] == duration.source_uid


def test_interval_average_affine_sync_accepts_realistic_receipt_jitter():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    receipts = (
        1_000_000_000,
        2_001_000_000,
        2_999_000_000,
        4_000_500_000,
    )
    raw = []
    for sequence_no, receipt_ns in enumerate(receipts):
        raw.extend(
            (
                RawSample(7, sequence_no, receipt_ns, 10_000.0),
                RawSample(8, sequence_no, receipt_ns, 1_000_000_000),
            )
        )

    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[power, duration],
        samples=raw,
        start_monotonic_ns=1_000_000_000,
        end_monotonic_ns=3_000_000_000,
        estimand="FIXED_HORIZON",
    )

    # Receipt delivery jitter does not create false native gaps or overlaps.
    assert outcome.total_joules == pytest.approx(20.0)


def test_interval_average_affine_sync_rejects_implausible_clock_drift():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    receipts = (1_000_000_000, 2_200_000_000, 3_400_000_000)
    raw = []
    for sequence_no, receipt_ns in enumerate(receipts):
        raw.extend(
            (
                RawSample(7, sequence_no, receipt_ns, 10_000.0),
                RawSample(8, sequence_no, receipt_ns, 1_000_000_000),
            )
        )

    with pytest.raises(AnalysisError, match="synchronization drift"):
        compute_energy_outcome(
            boundary=boundary,
            descriptors=[power, duration],
            samples=raw,
            start_monotonic_ns=1_000_000_000,
            end_monotonic_ns=3_000_000_000,
            estimand="FIXED_HORIZON",
        )


def test_interval_average_affine_sync_rejects_excessive_receipt_residual():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    receipts = (
        1_000_000_000,
        2_100_000_000,
        2_900_000_000,
        4_100_000_000,
        5_000_000_000,
    )
    raw = []
    for sequence_no, receipt_ns in enumerate(receipts):
        raw.extend(
            (
                RawSample(7, sequence_no, receipt_ns, 10_000.0),
                RawSample(8, sequence_no, receipt_ns, 1_000_000_000),
            )
        )

    with pytest.raises(AnalysisError, match="receipt residual"):
        compute_energy_outcome(
            boundary=boundary,
            descriptors=[power, duration],
            samples=raw,
            start_monotonic_ns=1_000_000_000,
            end_monotonic_ns=4_000_000_000,
            estimand="FIXED_HORIZON",
        )


def test_interval_average_clips_partial_native_intervals_at_event_edges():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    raw = [
        RawSample(7, 0, 1_000_000_000, 10_000.0),
        # A collector started before this daemon clock epoch can legitimately
        # report an interval whose inferred start is negative.
        RawSample(8, 0, 1_000_000_000, 2_000_000_000),
    ]
    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[power, duration],
        samples=raw,
        start_monotonic_ns=250_000_000,
        end_monotonic_ns=750_000_000,
        estimand="FIXED_HORIZON",
    )
    assert outcome.total_joules == pytest.approx(5.0)


def test_interval_average_requires_same_frame_duration_evidence():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    with pytest.raises(AnalysisError, match="same-frame duration"):
        compute_energy_outcome(
            boundary=boundary,
            descriptors=[power, duration],
            samples=[
                RawSample(7, 0, 1_000_000_000, 10_000.0),
                RawSample(8, 0, 900_000_000, 1_000_000_000),
            ],
            start_monotonic_ns=250_000_000,
            end_monotonic_ns=750_000_000,
            estimand="FIXED_HORIZON",
        )


def test_interval_average_boundary_requires_linked_duration_descriptor():
    power, _duration = interval_average_descriptors()
    with pytest.raises(AnalysisError, match="missing linked interval duration"):
        MeasurementBoundary.from_descriptors(
            [power], [power.source_uid], boundary_id="powermetrics-cpu"
        )


def test_interval_average_rejects_gaps_in_requested_window():
    power, duration = interval_average_descriptors()
    boundary = MeasurementBoundary.from_descriptors(
        [power, duration], [power.source_uid], boundary_id="powermetrics-cpu"
    )
    raw = [
        RawSample(7, 0, 1_000_000_000, 10_000.0),
        RawSample(8, 0, 1_000_000_000, 500_000_000),
    ]
    with pytest.raises(AnalysisError, match="unmeasured gap"):
        compute_energy_outcome(
            boundary=boundary,
            descriptors=[power, duration],
            samples=raw,
            start_monotonic_ns=250_000_000,
            end_monotonic_ns=750_000_000,
            estimand="FIXED_HORIZON",
        )


def test_disjoint_interval_averages_can_share_one_raw_frame_duration():
    cpu, duration = interval_average_descriptors()
    group = {
        "wattch.disjoint_group": "apple-compute-domains",
        "wattch.interval_duration_source_uid": duration.source_uid,
        "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
    }
    cpu = descriptor(
        cpu.local_source_id,
        cpu.source_uid,
        backend="powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INTERVAL_AVERAGE",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={**group, "wattch.disjoint_member": "cpu"},
    )
    gpu = descriptor(
        9,
        "powermetrics://node-a/gpu/power",
        backend="powermetrics",
        kind="GPU_POWER",
        unit="MILLIWATT",
        temporality="INTERVAL_AVERAGE",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={**group, "wattch.disjoint_member": "gpu"},
    )
    boundary = MeasurementBoundary.from_descriptors(
        [cpu, gpu, duration],
        [cpu.source_uid, gpu.source_uid],
        AggregationRule.SUM_DECLARED_DISJOINT,
        boundary_id="powermetrics-compute",
    )
    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[cpu, gpu, duration],
        samples=[
            RawSample(7, 0, 1_000_000_000, 5_000.0),
            RawSample(9, 0, 1_000_000_000, 3_000.0),
            RawSample(8, 0, 1_000_000_000, 1_000_000_000),
        ],
        start_monotonic_ns=0,
        end_monotonic_ns=1_000_000_000,
        estimand="FIXED_HORIZON",
    )
    assert outcome.per_source_joules == pytest.approx(
        {cpu.source_uid: 5.0, gpu.source_uid: 3.0}
    )


def test_explicit_disjoint_domains_sum_without_parent_child_double_count():
    cpu = descriptor(
        1,
        "powermetrics://node-a/cpu/power",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={
            "wattch.disjoint_group": "apple-compute-domains",
            "wattch.disjoint_member": "cpu",
        },
    )
    gpu = descriptor(
        2,
        "powermetrics://node-a/gpu/power",
        backend="powermetrics",
        kind="GPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={
            "wattch.disjoint_group": "apple-compute-domains",
            "wattch.disjoint_member": "gpu",
        },
    )
    boundary = MeasurementBoundary.from_descriptors(
        [cpu, gpu],
        [cpu.source_uid, gpu.source_uid],
        AggregationRule.SUM_DECLARED_DISJOINT,
        boundary_id="powermetrics-compute",
    )
    outcome = compute_energy_outcome(
        boundary=boundary,
        descriptors=[cpu, gpu],
        samples=samples(1, [5.0, 5.0]) + samples(2, [3.0, 3.0]),
        start_monotonic_ns=0,
        end_monotonic_ns=1_000_000_000,
        estimand=Estimand.FIXED_HORIZON,
    )
    assert outcome.per_source_joules == pytest.approx(
        {cpu.source_uid: 5.0, gpu.source_uid: 3.0}
    )
    assert outcome.total_joules == pytest.approx(8.0)


def test_disjoint_sum_requires_descriptor_proof_and_preserves_it_in_signature():
    cpu = descriptor(
        1,
        "powermetrics://node-a/cpu/power",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
    )
    gpu = descriptor(
        2,
        "powermetrics://node-a/gpu/power",
        backend="powermetrics",
        kind="GPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
    )

    with pytest.raises(AnalysisError, match="wattch.disjoint_group"):
        MeasurementBoundary.from_descriptors(
            [cpu, gpu],
            [cpu.source_uid, gpu.source_uid],
            AggregationRule.SUM_DECLARED_DISJOINT,
            boundary_id="unproven-compute-sum",
        )

    proven_cpu = descriptor(
        1,
        cpu.source_uid,
        backend="powermetrics",
        kind="CPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={
            "wattch.disjoint_group": "apple-compute-domains",
            "wattch.disjoint_member": "cpu",
        },
    )
    proven_gpu = descriptor(
        2,
        gpu.source_uid,
        backend="powermetrics",
        kind="GPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={
            "wattch.disjoint_group": "apple-compute-domains",
            "wattch.disjoint_member": "gpu",
        },
    )
    boundary = MeasurementBoundary.from_descriptors(
        [proven_cpu, proven_gpu],
        [proven_cpu.source_uid, proven_gpu.source_uid],
        AggregationRule.SUM_DECLARED_DISJOINT,
        boundary_id="proven-compute-sum",
    )
    signatures = {source.source_uid: source for source in boundary.required_sources}
    assert signatures[proven_cpu.source_uid].disjoint_group == "apple-compute-domains"
    assert signatures[proven_gpu.source_uid].disjoint_member == "gpu"

    drifted_gpu = descriptor(
        2,
        gpu.source_uid,
        backend="powermetrics",
        kind="GPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels={
            "wattch.disjoint_group": "different-partition",
            "wattch.disjoint_member": "gpu",
        },
    )
    with pytest.raises(BoundaryMismatchError, match="source semantics"):
        boundary.validate_descriptors([proven_cpu, drifted_gpu])


def test_disjoint_sum_rejects_duplicate_member_identity():
    shared_labels = {
        "wattch.disjoint_group": "compute-domains",
        "wattch.disjoint_member": "same-domain",
    }
    cpu = descriptor(
        1,
        "powermetrics://node-a/cpu/power",
        backend="powermetrics",
        kind="CPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels=shared_labels,
    )
    gpu = descriptor(
        2,
        "powermetrics://node-a/gpu/power",
        backend="powermetrics",
        kind="GPU_POWER",
        unit="WATT",
        temporality="INSTANT",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        max_value=None,
        labels=shared_labels,
    )
    with pytest.raises(AnalysisError, match="unique"):
        MeasurementBoundary.from_descriptors(
            [cpu, gpu],
            [cpu.source_uid, gpu.source_uid],
            AggregationRule.SUM_DECLARED_DISJOINT,
            boundary_id="duplicate-member",
        )


def test_outcome_rejects_unbracketed_window():
    source = descriptor(1, "rapl://node-a/package/0")
    boundary = MeasurementBoundary.from_descriptors(
        [source], [source.source_uid], boundary_id="cpu-package"
    )
    with pytest.raises(AnalysisError, match="do not bracket"):
        compute_energy_outcome(
            boundary=boundary,
            descriptors=[source],
            samples=samples(1, [0.0, 100.0]),
            start_monotonic_ns=0,
            end_monotonic_ns=2_000_000_000,
            estimand=Estimand.FIXED_HORIZON,
        )
