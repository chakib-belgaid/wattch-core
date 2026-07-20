from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
from statistics import NormalDist
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import physical_study  # noqa: E402
from pywattch._messages import SourceDescriptor  # noqa: E402
from pywattch.analysis.boundary import MeasurementBoundary  # noqa: E402


def no_fork_sandbox_available() -> bool:
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if not sandbox_exec.is_file():
        return False
    completed = subprocess.run(
        [
            str(sandbox_exec),
            "-p",
            physical_study.NO_FORK_SANDBOX_PROFILE,
            sys.executable,
            "-I",
            "-B",
            "-c",
            physical_study.NO_FORK_SANDBOX_PROBE,
        ],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def apple_source(uid: str = "powermetrics://agent-energy-mac/processor.cpu_power") -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=1,
        source_uid=uid,
        node_id="agent-energy-mac",
        backend="apple-powermetrics",
        kind="CPU_POWER",
        unit="MILLIWATT",
        temporality="INTERVAL_AVERAGE",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="collector/powermetrics/processor.cpu_power",
        parent_source_uid=None,
        clock_domain="monotonic",
        sampling_mode="POLLED",
        nominal_period_ns=250_000_000,
        max_value=None,
        resolution=None,
        labels={
            "wattch.interval_duration_source_uid": "powermetrics://frame/elapsed",
            "wattch.interval_anchor": "ENDS_AT_DAEMON_RECEIPT_TIMESTAMP",
            "wattch.interval_timeline_reconstruction": (
                "AFFINE_NATIVE_ELAPSED_TO_DAEMON_RECEIPTS_V1"
            ),
        },
    )


def apple_duration() -> SourceDescriptor:
    return SourceDescriptor(
        local_source_id=99,
        source_uid="powermetrics://frame/elapsed",
        node_id="agent-energy-mac",
        backend="apple-powermetrics",
        kind="SAMPLE_INTERVAL_DURATION",
        unit="NANOSECOND",
        temporality="DELTA",
        evidence_class="TOOL_DERIVED_ESTIMATE",
        source_path="collector/powermetrics/frame/elapsed_ns",
        parent_source_uid=None,
        clock_domain="monotonic",
        sampling_mode="POLLED",
        nominal_period_ns=250_000_000,
        max_value=None,
        resolution=1.0,
        labels={},
    )


class PhysicalStudyTest(unittest.TestCase):
    def test_williams_schedule_is_deterministic_and_balanced(self) -> None:
        first = physical_study.build_schedule(
            physical_study.CANONICAL_CONDITIONS,
            blocks=8,
            seed=91,
            plan_id="physical-v1",
        )
        second = physical_study.build_schedule(
            physical_study.CANONICAL_CONDITIONS,
            blocks=8,
            seed=91,
            plan_id="physical-v1",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        physical_study.validate_schedule(
            first,
            condition_ids=physical_study.CANONICAL_CONDITIONS,
            blocks=8,
        )
        positions = {
            (condition, position): sum(
                item["condition"] == condition and item["position"] == position
                for item in first
            )
            for condition in physical_study.CANONICAL_CONDITIONS
            for position in range(1, 5)
        }
        self.assertEqual(set(positions.values()), {2})

    def test_schedule_rejects_incomplete_block(self) -> None:
        schedule = physical_study.build_schedule(
            physical_study.CANONICAL_CONDITIONS,
            blocks=4,
            seed=3,
            plan_id="physical-v1",
        )
        schedule.pop()

        with self.assertRaisesRegex(ValueError, "four trials per block"):
            physical_study.validate_schedule(
                schedule,
                condition_ids=physical_study.CANONICAL_CONDITIONS,
                blocks=4,
            )

    def test_schedule_rejects_zero_blocks(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive multiple"):
            physical_study.validate_schedule(
                [],
                condition_ids=physical_study.CANONICAL_CONDITIONS,
                blocks=0,
            )

    def test_schedule_rejects_list_order_that_disagrees_with_labels(self) -> None:
        schedule = physical_study.build_schedule(
            physical_study.CANONICAL_CONDITIONS,
            blocks=4,
            seed=3,
            plan_id="physical-v1",
        )
        schedule[:4] = reversed(schedule[:4])

        with self.assertRaisesRegex(ValueError, "list order"):
            physical_study.validate_schedule(
                schedule,
                condition_ids=physical_study.CANONICAL_CONDITIONS,
                blocks=4,
            )

    def test_source_preflight_accepts_only_unique_physical_cpu_power(self) -> None:
        acquisition = {
            "backend": "apple-powermetrics",
            "source_kind": "CPU_POWER",
            "unit": "MILLIWATT",
            "temporality": "INTERVAL_AVERAGE",
            "evidence_class": "TOOL_DERIVED_ESTIMATE",
            "interval_ms": 250,
        }
        selected = physical_study.select_physical_source(
            [apple_source(), apple_duration()], acquisition
        )
        self.assertEqual(selected.source_uid, apple_source().source_uid)

        synthetic = SourceDescriptor.from_dict(
            {
                **apple_source().to_dict(),
                "backend": "synthetic",
                "sampling_mode": "SYNTHETIC",
            }
        )
        with self.assertRaisesRegex(ValueError, "found 0"):
            physical_study.select_physical_source([synthetic], acquisition)
        with self.assertRaisesRegex(ValueError, "found 2"):
            physical_study.select_physical_source(
                [
                    apple_source(),
                    apple_source("powermetrics://other/cpu"),
                    apple_duration(),
                ],
                acquisition,
            )

    def test_calibration_must_bind_source_interval_and_strict_metrics(self) -> None:
        source = apple_source()
        descriptors = [source, apple_duration()]
        boundary = MeasurementBoundary.from_descriptors(
            descriptors,
            [source.source_uid],
            boundary_id=physical_study.PHYSICAL_BOUNDARY_ID,
        ).to_dict()
        boundary_sha = hashlib.sha256(
            json.dumps(
                boundary,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        calibration = {
            "schema_version": 1,
            "kind": "METER_CALIBRATION",
            "calibration_id": "apple-cpu-v1",
            "measurement_boundary": boundary,
            "measurement_boundary_sha256": boundary_sha,
            "selected_interval_ns": 250_000_000,
            "required_regimes": ["IDLE", "FIXED_LOAD", "AA"],
            "selected_metrics": {
                "measurement_overhead_ratio": 0.004,
                "missing_sample_ratio": 0.0002,
                "phase_sensitivity_ratio": 0.003,
            },
            "thresholds": {
                "max_measurement_overhead_ratio": 0.01,
                "max_missing_sample_ratio": 0.001,
                "max_phase_sensitivity_ratio": 0.01,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            path.write_text(json.dumps(calibration), "utf-8")
            accepted = physical_study.validate_calibration(
                path,
                source=source,
                descriptors=descriptors,
                interval_ms=250,
            )
            self.assertEqual(accepted["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            calibration["selected_interval_ns"] = 1_000_000_000
            path.write_text(json.dumps(calibration), "utf-8")
            with self.assertRaisesRegex(ValueError, "interval"):
                physical_study.validate_calibration(
                    path,
                    source=source,
                    descriptors=descriptors,
                    interval_ms=250,
                )

    def test_differential_readiness_recomputes_bound_aa_floor(self) -> None:
        source = apple_source()
        descriptors = [source, apple_duration()]
        boundary = MeasurementBoundary.from_descriptors(
            descriptors,
            [source.source_uid],
            boundary_id=physical_study.PHYSICAL_BOUNDARY_ID,
        ).to_dict()
        energies = {
            "idle": ((50.0, 51.0), (60.0, 61.0)),
            "fixed-load": ((100.0, 102.0), (110.0, 112.0)),
            "aa": ((90.0, 92.0), (100.0, 102.0)),
        }
        observations = []
        for regime, phases in energies.items():
            for phase, repetitions in enumerate(phases):
                for repetition, energy in enumerate(repetitions, start=1):
                    observations.append(
                        {
                            "regime": regime,
                            "phase_index": phase,
                            "repetition": repetition,
                            "marker_duration_s": 30.0,
                            "energy_outcome": {"total_joules": energy},
                        }
                    )
        differences = [10.0, 10.0, 10.0, 10.0]
        mean = statistics.fmean(differences)
        deviation = statistics.stdev(differences)
        standard_error = deviation / len(differences) ** 0.5
        critical = NormalDist().inv_cdf(0.975)
        uncertainty = physical_study._linear_quantile(
            [abs(value) for value in differences], 0.95
        )
        controls = [100.0, 102.0, 110.0, 112.0]
        control_mean = statistics.fmean(controls)
        threshold = max(0.05 * control_mean, 2.0 * uncertainty)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instrumented_path = root / "instrumented.json"
            instrumented_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_type": "wattch-meter-calibration-instrumented",
                        "interval_ns": 250_000_000,
                        "parameters": {"poll_interval_ms": 50},
                        "measurement_boundary": boundary,
                        "observations": observations,
                    }
                ),
                "utf-8",
            )
            readiness = {
                "schema_version": 1,
                "record_type": physical_study.DIFFERENTIAL_READINESS_KIND,
                "instrumented_path": "instrumented.json",
                "instrumented_sha256": hashlib.sha256(
                    instrumented_path.read_bytes()
                ).hexdigest(),
                "method": "pywattch.analysis.calibration.compare_sham_trials",
                "comparison": {
                    "contrast": "fixed-load-aa",
                    "block_ids": ["p0-r1", "p0-r2", "p1-r1", "p1-r2"],
                    "paired_differences_joules": differences,
                    "mean_difference_joules": mean,
                    "sample_standard_deviation_joules": deviation,
                    "standard_error_joules": standard_error,
                    "confidence": 0.95,
                    "ci_lower_joules": mean - critical * standard_error,
                    "ci_upper_joules": mean + critical * standard_error,
                    "uncertainty_quantile": 0.95,
                    "uncertainty_joules": uncertainty,
                    "centers_on_zero": False,
                },
                "meaningful_effect": {
                    "control_mean_joules": control_mean,
                    "relative_floor_fraction": 0.05,
                    "relative_floor_joules": 0.05 * control_mean,
                    "aa_uncertainty_multiplier": 2.0,
                    "aa_floor_joules": 2.0 * uncertainty,
                    "meaningful_effect_joules": threshold,
                },
                "eligible_for_comparison": False,
                "blockers": [
                    "METER_PHASE_SENSITIVITY_REJECTED",
                    "MEASUREMENT_OVERHEAD_NOT_EVALUATED",
                ],
            }
            readiness_path = root / "readiness.json"
            readiness_path.write_text(json.dumps(readiness), "utf-8")

            accepted = physical_study.validate_differential_readiness(
                readiness_path,
                source=source,
                descriptors=descriptors,
                interval_ms=250,
            )
            self.assertEqual(accepted["meaningful_effect_joules"], threshold)
            self.assertEqual(
                accepted["evidence_level"], "EXPLORATORY_PAIRED_DIFFERENTIAL"
            )

            readiness["comparison"]["mean_difference_joules"] = 11.0
            readiness_path.write_text(json.dumps(readiness), "utf-8")
            with self.assertRaisesRegex(ValueError, "mean_difference_joules"):
                physical_study.validate_differential_readiness(
                    readiness_path,
                    source=source,
                    descriptors=descriptors,
                    interval_ms=250,
                )

    def test_inspection_gate_rejects_incomplete_or_sparse_capture(self) -> None:
        source = apple_source().to_dict()
        inspection = {
            "command_lifecycle": {
                "status": "INCOMPLETE",
                "exit_code": None,
            },
            "unknown_source_samples": 1,
            "per_source": {
                "1": {
                    "samples": 9,
                    "sequence_gaps": 1,
                }
            },
        }

        self.assertEqual(
            physical_study.inspection_blockers(
                inspection, source=source, minimum_samples=20
            ),
            [
                "COMMAND_LIFECYCLE_INCOMPLETE",
                "UNKNOWN_SOURCE_SAMPLES",
                "TOO_FEW_NATIVE_SAMPLES",
                "SEQUENCE_GAPS",
            ],
        )

        inspection["command_lifecycle"] = {"status": "COMPLETE", "exit_code": 0}
        inspection["unknown_source_samples"] = 0
        inspection["per_source"]["1"] = {"samples": 20, "sequence_gaps": 0}
        self.assertEqual(
            physical_study.inspection_blockers(
                inspection, source=source, minimum_samples=20
            ),
            [],
        )

    def test_quality_requires_interval_duration_companion(self) -> None:
        source = apple_source()
        duration = apple_duration()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "sources.json").write_text(
                json.dumps([source.to_dict(), duration.to_dict()]), "utf-8"
            )
            with (
                mock.patch.object(physical_study, "_read_trace", return_value=[]),
                mock.patch.object(
                    physical_study, "summarize_source_quality", return_value=()
                ),
                mock.patch.object(
                    physical_study,
                    "build_trial_quality_metadata",
                    return_value={"disposition": "ACCEPTED"},
                ) as build_quality,
            ):
                physical_study._write_quality(
                    run,
                    run_id="run-1",
                    source_uid=source.source_uid,
                    measurement_overhead_ratio=0.001,
                    max_overhead_ratio=0.01,
                    max_missing_sample_ratio=0.001,
                )

        self.assertEqual(
            build_quality.call_args.kwargs["required_source_uids"],
            [source.source_uid, duration.source_uid],
        )

    def test_aggregate_study_fails_closed_without_complete_eligible_matrix(self) -> None:
        plan = {
            "plan_id": "physical-v1",
            "design": {"blocks": 8, "schedule_seed": 11, "schedule": []},
        }
        readiness = {
            "selected_source": apple_source().to_dict(),
            "descriptive_comparison_ready": True,
            "limitations": ["local only"],
        }

        result = physical_study.aggregate_study(
            plan, readiness=readiness, trials=[]
        )

        self.assertFalse(result["claims"]["physical_comparison_claim_eligible"])
        self.assertFalse(
            result["claims"]["confirmatory_comparison_claim_eligible"]
        )
        self.assertFalse(result["claims"]["remote_model_energy_claim_eligible"])
        self.assertTrue(
            all(cell["median_energy_joules"] is None for cell in result["cells"])
        )

    def test_aggregate_requires_exact_schedule_bijection(self) -> None:
        schedule = physical_study.build_schedule(
            physical_study.CANONICAL_CONDITIONS,
            blocks=4,
            seed=11,
            plan_id="physical-v1",
        )
        plan = {
            "plan_id": "physical-v1",
            "design": {"blocks": 4, "schedule_seed": 11, "schedule": schedule},
        }
        readiness = {
            "selected_source": apple_source().to_dict(),
            "descriptive_comparison_ready": True,
            "limitations": ["local only"],
        }

        def trial(item: dict[str, object]) -> dict[str, object]:
            return {
                "schedule": item,
                "record": {
                    "descriptive_local_run_eligible": True,
                    "run_blockers": [],
                },
                "summary": {
                    "descriptive_local_run_eligible": True,
                    "outcome": {"total_joules": 1.0, "duration_s": 1.0},
                },
            }

        duplicated = [trial(schedule[0]) for _ in schedule]
        rejected = physical_study.aggregate_study(
            plan, readiness=readiness, trials=duplicated
        )
        self.assertFalse(rejected["matrix_complete"])
        self.assertFalse(
            rejected["claims"]["descriptive_local_comparison_eligible"]
        )

        accepted = physical_study.aggregate_study(
            plan, readiness=readiness, trials=[trial(item) for item in schedule]
        )
        self.assertTrue(accepted["matrix_complete"])
        self.assertTrue(
            accepted["claims"]["descriptive_local_comparison_eligible"]
        )
        self.assertFalse(accepted["claims"]["physical_comparison_claim_eligible"])

    def test_differential_aggregate_applies_frozen_floor_and_interval(self) -> None:
        schedule = physical_study.build_schedule(
            physical_study.CANONICAL_CONDITIONS,
            blocks=4,
            seed=13,
            plan_id="paired-v1",
        )
        plan = {
            "plan_id": "paired-v1",
            "design": {"blocks": 4, "schedule_seed": 13, "schedule": schedule},
            "analysis": {
                "mode": physical_study.PAIRED_DIFFERENTIAL_MODE,
                "meaningful_effect_joules": 20.0,
                "interaction_threshold_multiplier": 2.0,
            },
        }
        readiness = {
            "selected_source": apple_source().to_dict(),
            "comparison_mode": physical_study.PAIRED_DIFFERENTIAL_MODE,
            "descriptive_comparison_ready": False,
            "differential_comparison_ready": True,
            "limitations": ["paired only"],
        }
        energy = {
            "luna-control": 100.0,
            "luna-skill": 200.0,
            "terra-control": 150.0,
            "terra-skill": 120.0,
        }

        def trial(item: dict[str, object]) -> dict[str, object]:
            value = energy[str(item["condition"])]
            return {
                "schedule": item,
                "record": {
                    "descriptive_local_run_eligible": False,
                    "differential_local_run_eligible": True,
                    "run_blockers": [],
                },
                "summary": {
                    "descriptive_local_run_eligible": False,
                    "differential_local_run_eligible": True,
                    "outcome": {"total_joules": value, "duration_s": 10.0},
                },
            }

        result = physical_study.aggregate_study(
            plan,
            readiness=readiness,
            trials=[trial(item) for item in schedule],
        )

        self.assertTrue(result["matrix_complete"])
        self.assertFalse(
            result["claims"]["descriptive_local_comparison_eligible"]
        )
        self.assertTrue(
            result["claims"][
                "exploratory_paired_differential_comparison_eligible"
            ]
        )
        self.assertTrue(
            result["paired_contrasts"]["luna_skill_minus_control"][
                "directional_difference_detected"
            ]
        )
        self.assertFalse(result["claims"]["physical_comparison_claim_eligible"])

    def test_aggregate_rejects_an_empty_zero_block_matrix(self) -> None:
        plan = {
            "plan_id": "physical-v1",
            "design": {"blocks": 0, "schedule_seed": 11, "schedule": []},
        }
        readiness = {
            "selected_source": apple_source().to_dict(),
            "descriptive_comparison_ready": True,
            "limitations": ["local only"],
        }

        result = physical_study.aggregate_study(
            plan, readiness=readiness, trials=[]
        )

        self.assertFalse(result["matrix_complete"])
        self.assertFalse(
            result["claims"]["descriptive_local_comparison_eligible"]
        )

    def test_plan_bounds_require_native_updates_and_a_target_timeout(self) -> None:
        plan = {
            "workload": {
                "events": 10,
                "accounts": 2,
                "seed": 1,
                "repeat": 1,
                "iterations": 1,
                "target_timeout_ms": 2_000,
            },
            "acquisition": {
                "interval_ms": 100,
                "poll_interval_ms": 50,
                "minimum_target_duration_s": 1.0,
                "minimum_selected_source_samples": 10,
                "cooldown_s": 0.0,
            },
            "design": {"blocks": 4, "schedule_seed": 1},
        }
        physical_study._validate_plan_bounds(plan)

        plan["acquisition"]["minimum_selected_source_samples"] = 0
        with self.assertRaisesRegex(ValueError, "ten native samples"):
            physical_study._validate_plan_bounds(plan)
        plan["acquisition"]["minimum_selected_source_samples"] = 10
        plan["acquisition"]["poll_interval_ms"] = 60
        with self.assertRaisesRegex(ValueError, "poll at least twice"):
            physical_study._validate_plan_bounds(plan)
        plan["acquisition"]["poll_interval_ms"] = 50
        plan["workload"]["target_timeout_ms"] = 1_000
        with self.assertRaisesRegex(ValueError, "timeout"):
            physical_study._validate_plan_bounds(plan)

    def test_condition_record_rejects_relabeling_bound_candidate_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            candidate = checkout / "candidate.py"
            candidate.parent.mkdir()
            candidate.write_text("def run(*args): return []\n", "utf-8")
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            condition = dict(
                physical_study.CANONICAL_CONDITION_METADATA["luna-control"]
            )
            condition["trial_order"] = 7
            state = {
                "checkout": str(checkout),
                "condition": condition,
                "candidate": {"relative_path": "candidate.py"},
            }
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state), "utf-8")
            (root / "luna-control-agent-result.json").write_text(
                json.dumps({"successful": True, "condition": condition}), "utf-8"
            )
            (root / "luna-control-result.json").write_text(
                json.dumps(
                    {
                        "all_measured_trials_correct": True,
                        "api_probe_passed": True,
                        "candidate_sha256": digest,
                        "condition": condition,
                    }
                ),
                "utf-8",
            )
            mutation = {"valid": True, "candidate_sha256": digest}
            with mock.patch.object(
                physical_study.coordinator,
                "verify_mutation",
                return_value=mutation,
            ):
                physical_study._condition_record(state_path)
                state["condition"] = {**condition, "model": "gpt-5.6-terra"}
                state_path.write_text(json.dumps(state), "utf-8")
                with self.assertRaisesRegex(ValueError, "not canonical"):
                    physical_study._condition_record(state_path)

    def test_runtime_guard_detects_plan_or_wattch_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            wattch_path = root / "wattch"
            plan_path.write_text("{}", "utf-8")
            wattch_path.write_bytes(b"binary")
            plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            wattch_sha = hashlib.sha256(wattch_path.read_bytes()).hexdigest()
            plan = {
                "conditions": [],
                "workload": {
                    key: value
                    for path_key, sha_key in (
                        ("driver_path", "driver_sha256"),
                        ("python_path", "python_sha256"),
                        ("sandbox_exec_path", "sandbox_exec_sha256"),
                        ("coordinator_path", "coordinator_sha256"),
                        ("summarizer_path", "summarizer_sha256"),
                        ("evidence_contract_path", "evidence_contract_sha256"),
                    )
                    for key, value in (
                        (path_key, str(wattch_path)),
                        (sha_key, wattch_sha),
                    )
                },
            }
            with mock.patch.object(physical_study, "verify_plan") as verify:
                physical_study.verify_runtime_bindings(
                    plan,
                    plan_path=plan_path,
                    plan_sha256=plan_sha,
                    wattch_path=wattch_path,
                    wattch_sha256=wattch_sha,
                )
                verify.assert_not_called()
                plan_path.write_text('{"tampered":true}', "utf-8")
                with self.assertRaisesRegex(ValueError, "plan changed"):
                    physical_study.verify_runtime_bindings(
                        plan,
                        plan_path=plan_path,
                        plan_sha256=plan_sha,
                        wattch_path=wattch_path,
                        wattch_sha256=wattch_sha,
                    )

    @unittest.skipUnless(hasattr(os, "killpg"), "requires Unix process groups")
    def test_warmup_runner_kills_term_ignoring_descendant(self) -> None:
        script = (
            "import os, signal, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: pass\n"
        )
        completed, timed_out, cleanup = physical_study._run_process_tree(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=os.environ.copy(),
            timeout_s=0.1,
        )

        child_pid = int(completed.stdout.strip())
        self.assertTrue(timed_out)
        self.assertTrue(cleanup["term_sent"])
        self.assertTrue(cleanup["kill_sent"])
        self.assertTrue(cleanup["clean"])
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, signal.SIGCONT)

    @unittest.skipUnless(
        no_fork_sandbox_available(), "macOS no-fork sandbox unavailable"
    )
    def test_workload_sandbox_blocks_detached_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "detached-child.pid"
            candidate = root / "candidate.py"
            candidate.write_text(
                "import pathlib, subprocess, sys\n"
                "def run(*_args):\n"
                "    child = subprocess.Popen(\n"
                "        [sys.executable, '-c', 'while True: pass'],\n"
                "        start_new_session=True,\n"
                "    )\n"
                f"    pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
                "    return []\n",
                "utf-8",
            )
            condition = {
                "candidate_path": str(candidate),
                "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            }
            plan = {
                "workload": {
                    "sandbox_exec_path": "/usr/bin/sandbox-exec",
                    "python_path": sys.executable,
                    "driver_path": str(ROOT / "physical_workload.py"),
                    "events": 1,
                    "accounts": 1,
                    "seed": 1,
                    "repeat": 1,
                    "iterations": 1,
                    "target_timeout_ms": 2_000,
                }
            }
            completed, timed_out, cleanup = physical_study._run_process_tree(
                physical_study.workload_command(plan, condition),
                cwd=str(root),
                env=os.environ.copy(),
                timeout_s=5,
            )
            escaped_pid = int(marker.read_text("utf-8")) if marker.exists() else None
            try:
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(timed_out)
                self.assertTrue(cleanup["clean"])
                self.assertIsNone(escaped_pid)
            finally:
                if escaped_pid is not None:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
