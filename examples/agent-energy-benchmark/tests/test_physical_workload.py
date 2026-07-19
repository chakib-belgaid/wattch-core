from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import physical_workload  # noqa: E402


class PhysicalWorkloadTest(unittest.TestCase):
    def candidate(self, root: Path, *, import_output: bool = False) -> tuple[Path, str]:
        path = root / "candidate.py"
        prefix = "print('unexpected import output')\n" if import_output else ""
        path.write_text(
            prefix
            + "def run(count, accounts, seed, repeat):\n"
            + "    return {'count': count, 'accounts': accounts, "
            + "'seed': seed, 'repeat': repeat}\n",
            "utf-8",
        )
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_aggregate_result_is_deterministic(self) -> None:
        result = {"value": 17}
        expected = hashlib.sha256(
            physical_workload.canonical_json_bytes(result) * 3
        ).hexdigest()

        aggregate = physical_workload.aggregate_result(
            [result, result, result],
            events=10,
            accounts=2,
            seed=5,
            repeat=4,
            iterations=3,
        )

        self.assertEqual(aggregate["aggregate_sha256"], expected)
        self.assertEqual(aggregate["total_events"], 30)

    def test_cli_runs_exact_hashed_candidate_repeatedly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.candidate(Path(directory))
            command = [
                sys.executable,
                "-I",
                "-B",
                str(ROOT / "physical_workload.py"),
                "--candidate",
                str(path),
                "--candidate-sha256",
                digest,
                "--events",
                "10",
                "--accounts",
                "2",
                "--seed",
                "5",
                "--repeat",
                "4",
                "--iterations",
                "3",
            ]
            completed = subprocess.run(command, capture_output=True, check=False)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"")
        value = json.loads(completed.stdout)
        self.assertEqual(value["iterations"], 3)
        self.assertEqual(value["total_events"], 30)
        self.assertNotEqual(
            value["iteration_result_sha256"],
            value["last_iteration_result_sha256"],
        )

    def test_cli_rejects_hash_mismatch_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _digest = self.candidate(Path(directory))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "physical_workload.py"),
                    "--candidate",
                    str(path),
                    "--candidate-sha256",
                    "0" * 64,
                    "--iterations",
                    "1",
                ],
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"candidate SHA-256 does not match", completed.stderr)

    def test_import_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self.candidate(Path(directory), import_output=True)
            with self.assertRaisesRegex(ValueError, "import must not write"):
                physical_workload.load_candidate(path, digest)

    def test_symlink_candidate_is_rejected_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, digest = self.candidate(root)
            link = root / "candidate-link.py"
            link.symlink_to(path)

            with self.assertRaisesRegex(ValueError, "non-symlink"):
                physical_workload.load_candidate(link, digest)

    def test_cli_timeout_bounds_a_hanging_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.py"
            path.write_text(
                "import time\n"
                "def run(count, accounts, seed, repeat):\n"
                "    time.sleep(10)\n"
                "    return {}\n",
                "utf-8",
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "physical_workload.py"),
                    "--candidate",
                    str(path),
                    "--candidate-sha256",
                    digest,
                    "--events",
                    "10",
                    "--accounts",
                    "2",
                    "--repeat",
                    "1",
                    "--iterations",
                    "1",
                    "--timeout-ms",
                    "50",
                ],
                capture_output=True,
                check=False,
                timeout=2,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"physical workload exceeded 50 ms", completed.stderr)


if __name__ == "__main__":
    unittest.main()
