from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import judge  # noqa: E402


class ArtifactJudgeTest(unittest.TestCase):
    def make_run(
        self,
        root: Path,
        *,
        argv: list[str],
        cwd: Path,
        stdout: bytes,
        stderr: bytes = b"",
    ) -> Path:
        run = root / "run"
        run.mkdir()
        (run / "command.json").write_text(
            json.dumps({"argv": argv, "cwd": str(cwd.resolve())}), "utf-8"
        )
        (run / "target.stdout.log").write_bytes(stdout)
        (run / "target.stderr.log").write_bytes(stderr)
        return run

    def test_exact_captured_bytes_and_command_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.py"
            candidate.write_text("raise AssertionError('must not execute')\n", "utf-8")
            argv = judge.candidate_argv(Path(sys.executable), candidate, 64, 8, 17, 2)
            run = self.make_run(
                root,
                argv=argv,
                cwd=root,
                stdout=judge.expected_stdout(64, 8, 17, 2),
            )
            result = judge.judge_artifact(
                run,
                expected_argv=argv,
                expected_cwd=root,
                count=64,
                accounts=8,
                seed=17,
                repeat=2,
                child_exit_code=0,
            )
        self.assertTrue(result["correct"])

    def test_semantically_equal_json_with_different_bytes_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.py"
            candidate.write_text("", "utf-8")
            argv = judge.candidate_argv(Path(sys.executable), candidate, 64, 8, 17, 2)
            semantic = judge.expected_output(64, 8, 17, 2)
            run = self.make_run(
                root,
                argv=argv,
                cwd=root,
                stdout=(json.dumps(semantic, indent=2) + "\n").encode(),
            )
            result = judge.judge_artifact(
                run,
                expected_argv=argv,
                expected_cwd=root,
                count=64,
                accounts=8,
                seed=17,
                repeat=2,
                child_exit_code=0,
            )
        self.assertFalse(result["correct"])
        self.assertFalse(result["stdout_exact"])

    def test_stderr_command_or_exit_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.py"
            candidate.write_text("", "utf-8")
            argv = judge.candidate_argv(Path(sys.executable), candidate, 64, 8, 17, 2)
            run = self.make_run(
                root,
                argv=[*argv, "extra"],
                cwd=root,
                stdout=judge.expected_stdout(64, 8, 17, 2),
                stderr=b"unexpected",
            )
            result = judge.judge_artifact(
                run,
                expected_argv=argv,
                expected_cwd=root,
                count=64,
                accounts=8,
                seed=17,
                repeat=2,
                child_exit_code=9,
            )
        self.assertFalse(result["correct"])
        self.assertFalse(result["stderr_exact"])
        self.assertFalse(result["command_exact"])
        self.assertFalse(result["exit_exact"])

    def test_public_api_probe_passes_frozen_candidate(self) -> None:
        result = judge.probe_candidate(ROOT / "candidate.py", Path(sys.executable))
        self.assertTrue(result["correct"], result)

    def test_reference_preserves_nonpositive_repeat_behavior(self) -> None:
        for repeat in (0, -2):
            result = judge.expected_output(73, 9, 17, repeat)
            self.assertEqual(result["accounts"], 0)
            self.assertEqual(
                result["result_sha256"],
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )

    def test_reference_preserves_out_of_range_integer_kinds(self) -> None:
        events = [(0, -1, 5), (0, 4, 7), (1, 0, 11)]
        summary = judge.reference_summary(events)
        self.assertEqual(summary[0]["count"], 2)
        self.assertEqual(
            summary[0]["kind_counts"], dict.fromkeys(judge.EVENT_KINDS, 0)
        )
        self.assertEqual(summary[0]["rolling"], 18446744072705552647)


if __name__ == "__main__":
    unittest.main()
