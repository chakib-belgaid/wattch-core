from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_evidence.py"
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_contract import (  # noqa: E402
    _calibration_gate,
    _cli_lifecycle,
    _descriptor_set,
    _quality_gate,
    _trace_gate,
    classify_source,
)


def source(
    *,
    backend: str = "linux-rapl",
    sampling_mode: str = "POLLED",
    evidence_class: str = "RAW_HARDWARE_COUNTER",
    kind: str = "CPU_PACKAGE_ENERGY",
    unit: str = "MICROJOULE",
    temporality: str = "CUMULATIVE",
    local_source_id: int = 1,
    source_uid: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "local_source_id": local_source_id,
        "source_uid": source_uid or f"{backend}://node/cpu",
        "node_id": "node",
        "backend": backend,
        "kind": kind,
        "unit": unit,
        "temporality": temporality,
        "evidence_class": evidence_class,
        "source_path": "cpu/package/0/energy",
        "parent_source_uid": None,
        "clock_domain": "monotonic",
        "sampling_mode": sampling_mode,
        "nominal_period_ns": 100_000_000,
        "max_value": None,
        "resolution": None,
        "labels": labels or {},
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frame(kind: int, document: object) -> bytes:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    return struct.pack("<4sHHHHI", b"WCH0", 0, 2, kind, 0, len(payload)) + payload


def write_artifact(
    run: Path,
    sources: list[dict[str, object]],
    *,
    labels: dict[str, str] | None = None,
    marker_end_ns: int = 1_000_000_000,
    sequence_step: int = 1,
    sequence_steps: dict[int, int] | None = None,
) -> None:
    manifest = {
        "run_id": "run-1",
        "node_id": "node",
        "protocol_version": "0.2.0",
        "clock_instance_id": "clock",
        "wattch_version": "test",
        "created_unix_ns": 1,
        "sources": sources,
        "labels": labels or {},
    }
    (run / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (run / "sources.json").write_text(json.dumps(sources), "utf-8")
    raw = bytearray()
    for index in range(10):
        samples = [
            {
                "source_id": item["local_source_id"],
                "sequence_no": index
                * (sequence_steps or {}).get(
                    int(item["local_source_id"]), sequence_step
                ),
                "monotonic_ns": (index + 1) * 100_000_000,
                "value": {"type": "i64", "value": index},
            }
            for item in sources
        ]
        raw.extend(
            frame(
                10,
                {
                    "run_id": "run-1",
                    "node_id": "node",
                    "batch_sequence_no": index,
                    "samples": samples,
                },
            )
        )
    (run / "raw.trace.wch").write_bytes(raw)
    events = {
        "run_id": "run-1",
        "node_id": "node",
        "batch_sequence_no": 0,
        "events": [
            {
                "event_id": "target-spawn",
                "run_id": "run-1",
                "node_id": "node",
                "monotonic_ns": 0,
                "kind": "TARGET_SPAWN",
                "labels": {},
            },
            {
                "event_id": "target-exit",
                "run_id": "run-1",
                "node_id": "node",
                "monotonic_ns": marker_end_ns,
                "kind": "TARGET_EXIT",
                "labels": {},
            },
        ],
    }
    (run / "events.trace.wch").write_bytes(frame(11, events))


def candidate_binding(root: Path, digest_label: dict[str, str]) -> dict[str, object]:
    path = root / "candidate.py"
    path.write_text("def run(*args): return []\n", "utf-8")
    digest = sha256(path)
    digest_label["candidate_sha256"] = digest
    scope = {
        "valid": True,
        "unexpected_changes": [],
        "candidate_sha256": digest,
        "candidate_path_matches": True,
        "candidate_hash_matches": True,
    }
    return {
        "path": str(path),
        "sha256": digest,
        "unchanged_during_trial": True,
        "scope_before": scope,
        "scope_after": scope,
    }


def write_calibration(root: Path, item: dict[str, object]) -> tuple[Path, str]:
    source_labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
    signature: dict[str, object] = {
        "source_uid": item["source_uid"],
        "kind": item["kind"],
        "unit": item["unit"],
        "temporality": item["temporality"],
        "evidence_class": item["evidence_class"],
        "parent_source_uid": item["parent_source_uid"],
        "clock_domain": item["clock_domain"],
        "disjoint_group": source_labels.get("wattch.disjoint_group"),
        "disjoint_member": source_labels.get("wattch.disjoint_member"),
    }
    boundary = {
        "boundary_id": "test-boundary",
        "backend": item["backend"],
        "primary_source_uids": [item["source_uid"]],
        "aggregation_rule": "SINGLE_SOURCE",
        "required_sources": [signature],
    }
    boundary_sha = hashlib.sha256(
        json.dumps(
            boundary,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    calibration = {
        "schema_version": 1,
        "kind": "METER_CALIBRATION",
        "calibration_id": "test-calibration",
        "measurement_boundary": boundary,
        "measurement_boundary_sha256": boundary_sha,
        "selected_interval_ns": 100_000_000,
        "required_regimes": ["IDLE", "FIXED_LOAD", "AA"],
        "selected_metrics": {
            "measurement_overhead_ratio": 0.001,
            "missing_sample_ratio": 0.0,
            "phase_sensitivity_ratio": 0.0,
        },
        "thresholds": {
            "max_measurement_overhead_ratio": 0.01,
            "max_missing_sample_ratio": 0.001,
            "max_phase_sensitivity_ratio": 0.01,
        },
    }
    path = root / "calibration.json"
    path.write_text(json.dumps(calibration), "utf-8")
    return path, sha256(path)


class CheckEvidenceTest(unittest.TestCase):
    def classify(
        self,
        run: Path,
        *,
        source_uid: str | None = None,
        trial: Path | None = None,
    ) -> dict[str, object]:
        command = [sys.executable, str(SCRIPT), str(run)]
        if source_uid:
            command.extend(("--source-uid", source_uid))
        if trial:
            command.extend(("--trial-record", str(trial)))
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return json.loads(completed.stdout)

    def test_empty_source_set_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            write_artifact(run, [])
            result = self.classify(run)
        self.assertFalse(result["physical_run_claim_eligible"])
        self.assertIn("EMPTY_SOURCE_SET", result["blockers"])

    def test_synthetic_source_is_plumbing_only(self) -> None:
        classified = classify_source(
            source(backend="synthetic", sampling_mode="SYNTHETIC")
        )
        self.assertEqual(classified["evidence_tier"], "PLUMBING_ONLY")
        self.assertFalse(classified["physical_source_candidate"])
        self.assertIn("SYNTHETIC_SOURCE", classified["blockers"])

    def test_non_energy_and_model_derived_sources_are_ineligible(self) -> None:
        gauge = classify_source(
            source(kind="C_STATE_RESIDENCY", unit="PERCENT", temporality="GAUGE")
        )
        model = classify_source(source(evidence_class="MODEL_DERIVED_ESTIMATE"))
        self.assertFalse(gauge["outcome_semantics_supported"])
        self.assertFalse(model["physical_source_candidate"])
        self.assertEqual(model["evidence_tier"], "MODEL_ESTIMATE")

    def test_real_source_without_explicit_run_gates_remains_ineligible(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            write_artifact(run, [item])
            result = self.classify(run, source_uid=str(item["source_uid"]))
        self.assertEqual(result["source"]["evidence_tier"], "RAW_LOCAL_MEASUREMENT")
        self.assertFalse(result["physical_run_claim_eligible"])
        self.assertIn("CORRECTNESS_UNBOUND", result["blockers"])
        self.assertIn("QUALITY_MISSING", result["blockers"])

    def test_nonzero_cli_exit_is_still_a_complete_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "command.json").write_text("{}", "utf-8")
            (run / "command.lifecycle.jsonl").write_text(
                json.dumps({"state": "STARTED", "run_id": "run-1"})
                + "\n"
                + json.dumps(
                    {"state": "COMPLETE", "run_id": "run-1", "exit_code": 23}
                )
                + "\n",
                "utf-8",
            )
            managed, blockers = _cli_lifecycle(run, {"run_id": "run-1"})

        self.assertTrue(managed)
        self.assertEqual(blockers, [])

    def test_trace_decoder_rejects_corruption_and_derives_sequence_gaps(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            write_artifact(run, [item], sequence_step=2)
            manifest = json.loads((run / "manifest.json").read_text("utf-8"))
            valid, evidence, blockers = _trace_gate(run, manifest, [item])
            self.assertTrue(valid, blockers)
            self.assertEqual(evidence["sequence_gaps"][1], 9)

            (run / "raw.trace.wch").write_bytes(b"not-a-frame")
            valid, _evidence, blockers = _trace_gate(run, manifest, [item])

        self.assertFalse(valid)
        self.assertEqual(blockers, ["TRACE_DECODE_FAILED"])

    def test_interval_average_requires_canonical_duration_descriptor(self) -> None:
        duration_uid = "apple-powermetrics://node/frame-duration"
        labels = {
            "wattch.interval_duration_source_uid": duration_uid,
            "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
            "wattch.interval_timeline_reconstruction": (
                "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"
            ),
        }
        power = source(
            backend="apple-powermetrics",
            evidence_class="TOOL_DERIVED_ESTIMATE",
            kind="CPU_POWER",
            unit="MILLIWATT",
            temporality="INTERVAL_AVERAGE",
            labels=labels,
        )
        duration = source(
            backend="apple-powermetrics",
            evidence_class="TOOL_DERIVED_ESTIMATE",
            kind="SAMPLE_INTERVAL_DURATION",
            unit="NANOSECOND",
            temporality="DELTA",
            local_source_id=2,
            source_uid=duration_uid,
        )
        _sources, blockers = _descriptor_set([power, duration])
        self.assertEqual(blockers, [])

        for key, bad_value in (
            ("backend", "other-backend"),
            ("evidence_class", "RAW_SENSOR_READING"),
            ("kind", "sample_interval_duration"),
            ("unit", "nanosecond"),
            ("temporality", "delta"),
        ):
            bad_duration = {**duration, key: bad_value}
            _sources, blockers = _descriptor_set([power, bad_duration])
            self.assertIn("INTERVAL_DURATION_UNBOUND", blockers)

        bad_power = {
            **power,
            "labels": {**labels, "wattch.interval_anchor": "UNKNOWN"},
        }
        _sources, blockers = _descriptor_set([bad_power, duration])
        self.assertIn("INTERVAL_DURATION_UNBOUND", blockers)

    def test_interval_duration_gap_fails_quality_gate(self) -> None:
        duration_uid = "apple-powermetrics://node/frame-duration"
        power = source(
            backend="apple-powermetrics",
            evidence_class="TOOL_DERIVED_ESTIMATE",
            kind="CPU_POWER",
            unit="MILLIWATT",
            temporality="INTERVAL_AVERAGE",
            labels={
                "wattch.interval_duration_source_uid": duration_uid,
                "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
                "wattch.interval_timeline_reconstruction": (
                    "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"
                ),
            },
        )
        duration = source(
            backend="apple-powermetrics",
            evidence_class="TOOL_DERIVED_ESTIMATE",
            kind="SAMPLE_INTERVAL_DURATION",
            unit="NANOSECOND",
            temporality="DELTA",
            local_source_id=2,
            source_uid=duration_uid,
        )
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            sources = [power, duration]
            write_artifact(run, sources, sequence_steps={2: 2})
            manifest = json.loads((run / "manifest.json").read_text("utf-8"))
            valid, trace_evidence, trace_blockers = _trace_gate(
                run, manifest, sources
            )
            self.assertTrue(valid, trace_blockers)
            self.assertEqual(trace_evidence["sequence_gaps"][2], 9)
            quality = {
                "disposition": "ACCEPTED",
                "issues": [],
                "sources": [
                    {
                        "source_uid": power["source_uid"],
                        "observed_samples": 10,
                        "sequence_gaps": 0,
                    },
                    {
                        "source_uid": duration_uid,
                        "observed_samples": 10,
                        "sequence_gaps": 0,
                    },
                ],
            }
            quality_path = run / "quality.json"
            quality_path.write_text(json.dumps(quality), "utf-8")
            trial = {
                "acquisition": {"marker_duration_s": 1.0},
                "artifact": {"files": {"quality.json": sha256(quality_path)}},
            }
            accepted, blockers = _quality_gate(
                run, power, trial, trace_evidence, sources
            )

        self.assertFalse(accepted)
        self.assertEqual(blockers, ["QUALITY_NOT_ACCEPTED"])

    def test_calibration_interval_must_match_native_source_cadence(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            calibration_path, _old_sha = write_calibration(root, item)
            calibration = json.loads(calibration_path.read_text("utf-8"))
            calibration["selected_interval_ns"] = 200_000_000
            calibration_path.write_text(json.dumps(calibration), "utf-8")
            calibration_sha = sha256(calibration_path)
            (run / "quality.json").write_text(
                json.dumps({"measurement_overhead_ratio": 0.001}), "utf-8"
            )
            trial = {
                "calibration_bound": True,
                "calibration": {
                    "path": str(calibration_path),
                    "sha256": calibration_sha,
                },
            }
            manifest = {
                "labels": {
                    "interval_ms": "200",
                    "measurement_overhead_calibration_sha256": calibration_sha,
                }
            }
            accepted, blockers = _calibration_gate(
                run, item, trial, manifest
            )

        self.assertFalse(accepted)
        self.assertEqual(blockers, ["CALIBRATION_MISMATCH"])

    def test_all_explicit_gates_can_enable_one_local_run(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            calibration_path, calibration_sha = write_calibration(root, item)
            labels = {
                "interval_ms": "100",
                "measurement_overhead_calibration_sha256": calibration_sha,
            }
            candidate = candidate_binding(root, labels)
            write_artifact(
                run,
                [item],
                labels=labels,
            )
            (run / "command.json").write_text("{}", "utf-8")
            (run / "command.lifecycle.jsonl").write_text(
                json.dumps({"state": "STARTED", "run_id": "run-1"})
                + "\n"
                + json.dumps(
                    {"state": "COMPLETE", "run_id": "run-1", "exit_code": 0}
                )
                + "\n",
                "utf-8",
            )
            (run / "target.stdout.log").write_bytes(b"expected\n")
            (run / "target.stderr.log").write_bytes(b"")
            quality = {
                "disposition": "ACCEPTED",
                "issues": [],
                "measurement_overhead_ratio": 0.001,
                "sources": [
                    {
                        "source_uid": item["source_uid"],
                        "observed_samples": 10,
                        "sequence_gaps": 0,
                    }
                ],
            }
            (run / "quality.json").write_text(json.dumps(quality), "utf-8")
            files = {
                path.name: sha256(path)
                for path in run.iterdir()
                if path.is_file()
            }
            trial = root / "trial.json"
            trial.write_text(
                json.dumps(
                    {
                        "record_type": "wattch-agent-trial",
                        "artifact": {
                            "run_id": "run-1",
                            "run_dir": str(run),
                            "files": files,
                        },
                        "acquisition": {
                            "complete": True,
                            "inspect_passed": True,
                            "marker_duration_s": 1.0,
                        },
                        "correctness": {
                            "correct": True,
                            "completed_work": True,
                            "stdout_exact": True,
                            "stderr_exact": True,
                            "command_exact": True,
                            "cwd_exact": True,
                            "exit_exact": True,
                        },
                        "candidate": candidate,
                        "calibration_bound": True,
                        "calibration": {
                            "path": str(calibration_path),
                            "sha256": calibration_sha,
                        },
                    }
                ),
                "utf-8",
            )
            result = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )
            self.assertTrue(result["descriptive_local_run_eligible"], result)
            self.assertTrue(result["descriptive_local_outcome_eligible"], result)
            self.assertFalse(result["confirmatory_calibration_verified"])
            self.assertFalse(result["physical_run_claim_eligible"])

            lowercase_item = {
                **item,
                "kind": "cpu_package_energy",
                "unit": "microjoule",
                "temporality": "cumulative",
                "evidence_class": "raw_hardware_counter",
                "sampling_mode": "polled",
            }
            write_artifact(run, [lowercase_item], labels=labels)
            trial_document = json.loads(trial.read_text("utf-8"))
            trial_document["artifact"]["files"] = {
                path.name: sha256(path) for path in run.iterdir() if path.is_file()
            }
            trial.write_text(json.dumps(trial_document), "utf-8")
            lowercase = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )
            self.assertFalse(lowercase["descriptive_local_run_eligible"])
            self.assertIn("INVALID_SOURCE_DESCRIPTOR", lowercase["blockers"])

            write_artifact(run, [item], labels=labels)
            trial_document["artifact"]["files"] = {
                path.name: sha256(path) for path in run.iterdir() if path.is_file()
            }
            trial.write_text(json.dumps(trial_document), "utf-8")
            calibration_bytes = calibration_path.read_bytes()
            calibration_path.write_text("{}", "utf-8")
            bad_calibration = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )
            self.assertFalse(bad_calibration["descriptive_local_run_eligible"])
            self.assertIn("CALIBRATION_MISMATCH", bad_calibration["blockers"])
            calibration_path.write_bytes(calibration_bytes)
            (run / "target.stdout.log").write_bytes(b"tampered\n")
            tampered = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )
        self.assertFalse(tampered["physical_run_claim_eligible"])
        self.assertFalse(tampered["descriptive_local_run_eligible"])
        self.assertIn("CHECKSUM_MISMATCH", tampered["blockers"])

    def test_short_marker_window_fails_native_cadence_gate(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            calibration_path, calibration_sha = write_calibration(root, item)
            labels = {
                "interval_ms": "100",
                "measurement_overhead_calibration_sha256": calibration_sha,
            }
            candidate = candidate_binding(root, labels)
            write_artifact(
                run,
                [item],
                labels=labels,
                marker_end_ns=900_000_000,
            )
            (run / "command.json").write_text("{}", "utf-8")
            (run / "command.lifecycle.jsonl").write_text(
                json.dumps({"state": "STARTED", "run_id": "run-1"})
                + "\n"
                + json.dumps(
                    {"state": "COMPLETE", "run_id": "run-1", "exit_code": 0}
                )
                + "\n",
                "utf-8",
            )
            (run / "target.stdout.log").write_bytes(b"expected\n")
            (run / "target.stderr.log").write_bytes(b"")
            quality = {
                "disposition": "ACCEPTED",
                "issues": [],
                "measurement_overhead_ratio": 0.001,
                "sources": [
                    {
                        "source_uid": item["source_uid"],
                        "observed_samples": 10,
                        "sequence_gaps": 0,
                    }
                ],
            }
            (run / "quality.json").write_text(json.dumps(quality), "utf-8")
            files = {
                path.name: sha256(path)
                for path in run.iterdir()
                if path.is_file()
            }
            trial = root / "trial.json"
            trial.write_text(
                json.dumps(
                    {
                        "record_type": "wattch-agent-trial",
                        "artifact": {
                            "run_id": "run-1",
                            "run_dir": str(run),
                            "files": files,
                        },
                        "acquisition": {
                            "complete": True,
                            "inspect_passed": True,
                            "marker_duration_s": 0.9,
                        },
                        "correctness": {
                            "correct": True,
                            "completed_work": True,
                            "stdout_exact": True,
                            "stderr_exact": True,
                            "command_exact": True,
                            "cwd_exact": True,
                            "exit_exact": True,
                        },
                        "candidate": candidate,
                        "calibration_bound": True,
                        "calibration": {
                            "path": str(calibration_path),
                            "sha256": calibration_sha,
                        },
                    }
                ),
                "utf-8",
            )
            result = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )

        self.assertFalse(result["physical_run_claim_eligible"])
        self.assertIn("NATIVE_CADENCE_UNDERQUALIFIED", result["blockers"])

    def test_single_run_never_enables_comparison_or_remote_inference(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            write_artifact(run, [item])
            result = self.classify(run, source_uid=str(item["source_uid"]))
        self.assertFalse(result["physical_comparison_claim_eligible"])
        self.assertFalse(result["remote_model_energy_claim_eligible"])


if __name__ == "__main__":
    unittest.main()
