from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import candidate  # noqa: E402


class CandidateContractTest(unittest.TestCase):
    def test_generator_is_deterministic(self) -> None:
        first = candidate.generate_events(12, 4, 7)
        self.assertEqual(first, candidate.generate_events(12, 4, 7))
        self.assertNotEqual(first, candidate.generate_events(12, 4, 8))
        self.assertTrue(all(0 <= account < 4 for account, _, _ in first))

    def test_summary_contract(self) -> None:
        events = [
            (1, 0, 4),
            (0, 1, 8),
            (1, 2, 3),
            (1, 0, 5),
        ]
        summary = candidate.summarize(events)
        self.assertEqual([item["account"] for item in summary], [0, 1])
        self.assertEqual(summary[0]["count"], 1)
        self.assertEqual(summary[0]["total_value"], 8)
        self.assertEqual(summary[0]["kind_counts"]["write"], 1)
        self.assertEqual(summary[1]["count"], 3)
        self.assertEqual(summary[1]["kind_values"]["read"], 9)
        self.assertEqual(summary[1]["rolling"], 4002045006104)

    def test_cli_stdout_is_canonical(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "candidate.py"),
                "--events",
                "64",
                "--accounts",
                "8",
                "--seed",
                "17",
                "--repeat",
                "2",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stderr, "")
        parsed = json.loads(completed.stdout)
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["events"], 64)
        self.assertEqual(parsed["accounts"], 8)
        self.assertEqual(parsed["repeat"], 2)
        self.assertRegex(parsed["result_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            completed.stdout,
            json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n",
        )


if __name__ == "__main__":
    unittest.main()
