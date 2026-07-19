from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


WORKER = Path(__file__).resolve().parents[1] / "polling_batching_worker.py"
BENCHMARK = Path(__file__).resolve().parents[2] / "bench" / "polling_vs_batching.toml"


def run_worker(mode: str, metrics_path: Path, *, seed: int = 12345):
    environment = dict(os.environ)
    environment.update(
        {
            "WATTCH_INPUT_ID": "wattch-polling-batching-v1",
            "WATTCH_REPLAY_SEED": str(seed),
            "WATTCH_ARRIVAL_TRACE_ID": "unit-test-trace",
        }
    )
    process = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "--mode",
            mode,
            "--items",
            "24",
            "--arrival-interval-ms",
            "1",
            "--batch-size",
            "6",
            "--work-rounds",
            "3",
            "--timeout-s",
            "3",
            "--latency-slo-ms",
            "1000",
            "--metrics-path",
            str(metrics_path),
        ],
        check=True,
        capture_output=True,
        env=environment,
        shell=False,
    )
    return process, json.loads(metrics_path.read_text(encoding="ascii"))


class PollingBatchingWorkerTest(unittest.TestCase):
    def test_variants_have_identical_output_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            polling, polling_metrics = run_worker(
                "polling", root / "polling.json"
            )
            batching, batching_metrics = run_worker(
                "batching", root / "batching.json"
            )

        self.assertEqual(polling.stdout, batching.stdout)
        self.assertEqual(polling.stderr, b"")
        self.assertEqual(batching.stderr, b"")
        output_sha256 = hashlib.sha256(polling.stdout).hexdigest()
        self.assertEqual(polling_metrics["output_sha256"], output_sha256)
        self.assertEqual(batching_metrics["output_sha256"], output_sha256)
        self.assertEqual(
            polling_metrics["arrival_trace_sha256"],
            batching_metrics["arrival_trace_sha256"],
        )
        self.assertEqual(polling_metrics["completed_work"], 24)
        self.assertEqual(batching_metrics["completed_work"], 24)
        self.assertEqual(polling_metrics["backlog_at_end"], 0)
        self.assertEqual(batching_metrics["backlog_at_end"], 0)
        self.assertTrue(polling_metrics["arrival_trace_valid"])
        self.assertTrue(batching_metrics["arrival_trace_valid"])
        self.assertGreater(polling_metrics["throughput_per_s"], 0)
        self.assertGreater(batching_metrics["throughput_per_s"], 0)
        self.assertEqual(
            set(polling_metrics["latency_ms"]), {"max", "p50", "p95", "p99"}
        )

    def test_modes_use_distinct_wait_and_dispatch_strategies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, polling = run_worker("polling", root / "polling.json")
            _, batching = run_worker("batching", root / "batching.json")

        self.assertEqual(polling["wait_strategy"], "nonblocking_busy_poll")
        self.assertGreater(polling["empty_poll_count"], 0)
        self.assertEqual(polling["batches_processed"], 24)
        self.assertEqual(batching["wait_strategy"], "blocking_batch")
        self.assertEqual(batching["empty_poll_count"], 0)
        self.assertEqual(batching["batch_size"], 6)
        self.assertEqual(batching["batches_processed"], 4)

    def test_replay_seed_changes_arrival_trace_but_not_work_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, first_metrics = run_worker(
                "polling", root / "first.json", seed=1
            )
            second, second_metrics = run_worker(
                "polling", root / "second.json", seed=2
            )

        self.assertEqual(first.stdout, second.stdout)
        self.assertNotEqual(
            first_metrics["arrival_trace_sha256"],
            second_metrics["arrival_trace_sha256"],
        )

    def test_sham_drains_same_trace_without_equivalent_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            polling, polling_metrics = run_worker(
                "polling", root / "polling.json"
            )
            sham, sham_metrics = run_worker("sham", root / "sham.json")

        self.assertNotEqual(polling.stdout, sham.stdout)
        self.assertEqual(
            polling_metrics["arrival_trace_sha256"],
            sham_metrics["arrival_trace_sha256"],
        )
        self.assertEqual(sham_metrics["completed_work"], 24)
        self.assertEqual(sham_metrics["backlog_at_end"], 0)
        self.assertTrue(sham_metrics["arrival_trace_valid"])
        self.assertEqual(sham_metrics["wait_strategy"], "blocking_sham_drain")

    def test_benchmark_freezes_the_real_canonical_output_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metrics_path = Path(directory) / "metrics.json"
            environment = dict(os.environ)
            environment.update(
                {
                    "WATTCH_INPUT_ID": "wattch-polling-batching-v1",
                    "WATTCH_REPLAY_SEED": "20260719",
                }
            )
            common = [
                sys.executable,
                str(WORKER),
                "--items",
                "4096",
                "--arrival-interval-ms",
                "0",
                "--batch-size",
                "32",
                "--work-rounds",
                "64",
                "--timeout-s",
                "20",
            ]
            batching = subprocess.run(
                [
                    *common,
                    "--mode",
                    "batching",
                    "--metrics-path",
                    str(metrics_path),
                ],
                check=True,
                capture_output=True,
                env=environment,
                shell=False,
            )
            polling = subprocess.run(
                [
                    *common,
                    "--mode",
                    "polling",
                    "--metrics-path",
                    str(Path(directory) / "polling-metrics.json"),
                ],
                check=True,
                capture_output=True,
                env=environment,
                shell=False,
            )

        configured = re.findall(
            r'^expected_output_sha256 = "([0-9a-f]{64})"$',
            BENCHMARK.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
        self.assertEqual(batching.stdout, polling.stdout)
        actual = hashlib.sha256(batching.stdout).hexdigest()
        self.assertEqual(configured, [actual, actual])

    def test_runner_metrics_environment_creates_exclusive_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metrics_path = Path(directory) / "target.metrics.json"
            environment = dict(os.environ)
            environment.update(
                {
                    "WATTCH_INPUT_ID": "wattch-polling-batching-v1",
                    "WATTCH_REPLAY_SEED": "7",
                    "WATTCH_METRICS_PATH": str(metrics_path),
                }
            )
            command = [
                sys.executable,
                str(WORKER),
                "--mode",
                "batching",
                "--items",
                "8",
                "--arrival-interval-ms",
                "0.5",
                "--batch-size",
                "4",
                "--work-rounds",
                "2",
                "--timeout-s",
                "2",
            ]
            first = subprocess.run(
                command,
                check=True,
                capture_output=True,
                env=environment,
                shell=False,
            )
            second = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment,
                shell=False,
            )
            metrics = json.loads(metrics_path.read_text("ascii"))

        self.assertEqual(first.stderr, b"")
        self.assertEqual(metrics["schema_version"], 1)
        self.assertEqual(second.returncode, 2)
        self.assertIn(b"FileExistsError", second.stderr)


if __name__ == "__main__":
    unittest.main()
