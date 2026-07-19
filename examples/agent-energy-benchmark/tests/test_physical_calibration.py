from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import physical_calibration as calibration  # noqa: E402
from physical_calibration_workload import fixed_work  # noqa: E402


class PhysicalCalibrationTests(unittest.TestCase):
    def test_fixed_work_is_deterministic_and_seeded(self) -> None:
        first = fixed_work(500, 7)
        self.assertEqual(first, fixed_work(500, 7))
        self.assertNotEqual(first["digest"], fixed_work(500, 8)["digest"])

    def test_phase_fraction_parser_and_schedule_cover_all_cells(self) -> None:
        phases = calibration.parse_phase_fractions("0,0.25,0.5,0.75")
        schedule = calibration._schedule(
            repetitions=2, phase_fractions=phases, seed=9
        )
        self.assertEqual(len(schedule), 24)
        self.assertEqual(
            {
                (item["regime"], item["phase_index"], item["repetition"])
                for item in schedule
            },
            {
                (regime, phase, repetition)
                for regime in calibration.REGIMES
                for phase in range(4)
                for repetition in (1, 2)
            },
        )
        with self.assertRaises(argparse.ArgumentTypeError):
            calibration.parse_phase_fractions("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            calibration.parse_phase_fractions("0,1")

    def test_phase_sensitivity_uses_phase_means(self) -> None:
        observations = [
            {"phase_index": 0, "energy_outcome": {"total_joules": 100.0}},
            {"phase_index": 0, "energy_outcome": {"total_joules": 102.0}},
            {"phase_index": 1, "energy_outcome": {"total_joules": 101.0}},
            {"phase_index": 1, "energy_outcome": {"total_joules": 103.0}},
        ]
        self.assertAlmostEqual(
            calibration.phase_sensitivity_ratio(observations), 1.0 / 101.5
        )

    def test_no_collector_check_rejects_existing_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            socket = Path(temporary) / "wattch.sock"
            socket.touch()
            with self.assertRaisesRegex(ValueError, "socket still exists"):
                calibration._assert_no_collector(socket)

    def test_finalize_seals_only_passing_paired_observations(self) -> None:
        boundary = {
            "boundary_id": "test-boundary",
            "backend": "test-meter",
            "primary_source_uids": ["test://cpu-power"],
            "aggregation_rule": "SINGLE_SOURCE",
            "required_sources": [
                {
                    "source_uid": "test://cpu-power",
                    "kind": "CPU_POWER",
                    "unit": "WATT",
                    "temporality": "INSTANT",
                    "evidence_class": "DIRECT_HARDWARE_COUNTER",
                    "parent_source_uid": None,
                    "clock_domain": "MONOTONIC",
                    "disjoint_group": None,
                    "disjoint_member": None,
                }
            ],
        }
        instrumented_observations = []
        baseline_observations = []
        for regime in calibration.REGIMES:
            for phase_index, energy in enumerate((100.0, 100.5)):
                observation_id = f"{regime}-{phase_index}"
                instrumented_observations.append(
                    {
                        "observation_id": observation_id,
                        "regime": regime,
                        "phase_index": phase_index,
                        "marker_duration_s": 10.05,
                        "energy_outcome": {"total_joules": energy},
                        "required_source_missing_sample_ratios": {
                            "test://cpu-power": 0.0
                        },
                    }
                )
                baseline_observations.append(
                    {
                        "observation_id": observation_id,
                        "regime": regime,
                        "phase_index": phase_index,
                        "duration_s": 10.0,
                    }
                )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instrumented_path = root / "instrumented.json"
            baseline_path = root / "baseline.json"
            instrumented = {
                "record_type": calibration.INSTRUMENTED_KIND,
                "interval_ns": 250_000_000,
                "measurement_boundary": boundary,
                "observations": instrumented_observations,
            }
            instrumented_path.write_bytes(
                calibration.canonical_json_bytes(instrumented)
            )
            baseline = {
                "record_type": calibration.BASELINE_KIND,
                "instrumented_sha256": hashlib.sha256(
                    instrumented_path.read_bytes()
                ).hexdigest(),
                "observations": baseline_observations,
            }
            baseline_path.write_bytes(calibration.canonical_json_bytes(baseline))
            args = argparse.Namespace(
                instrumented=instrumented_path,
                baseline=baseline_path,
                assessment=root / "assessment.json",
                out=root / "meter-calibration.json",
                calibration_id="test-calibration",
            )
            result = calibration.finalize(args)
            self.assertTrue(result["eligible"])
            document = json.loads((root / "meter-calibration.json").read_text())
            self.assertEqual(document["kind"], "METER_CALIBRATION")
            self.assertLess(
                document["selected_metrics"]["measurement_overhead_ratio"],
                0.01,
            )


if __name__ == "__main__":
    unittest.main()
