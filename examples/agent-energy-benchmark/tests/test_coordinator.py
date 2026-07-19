from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import coordinator  # noqa: E402


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class CoordinatorTest(unittest.TestCase):
    def repository(self, root: Path) -> tuple[Path, Path]:
        repo = root / "repo"
        benchmark = repo / "examples/agent-energy-benchmark"
        benchmark.mkdir(parents=True)
        (benchmark / "candidate.py").write_text("VALUE = 1\n", "utf-8")
        (benchmark / "PROMPT.txt").write_bytes(b"identical prompt\n")
        run("git", "init", "-q", str(repo), cwd=root)
        run("git", "add", ".", cwd=repo)
        run(
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
            cwd=repo,
        )
        skill = root / "skill"
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: wattch-energy-optimize\n---\n", "utf-8")
        (skill / "agents/openai.yaml").write_text("interface: {}\n", "utf-8")
        return repo, skill

    @mock.patch.object(coordinator, "_global_skill_paths", return_value=[])
    def test_prepare_injects_skill_only_for_treatment_and_keeps_prompt_bytes(
        self, _paths: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, skill = self.repository(root)
            treatment = root / "treatment"
            run("git", "clone", "-q", str(repo), str(treatment), cwd=root)
            control_state = root / "control.json"
            treatment_state = root / "treatment.json"
            control = coordinator.prepare_condition(
                checkout=repo,
                state_path=control_state,
                condition_id="luna-control",
                skill_package=skill,
                trial_order=1,
                workload={"events": 64, "accounts": 8, "seed": 17, "repeat": 2},
            )
            treated = coordinator.prepare_condition(
                checkout=treatment,
                state_path=treatment_state,
                condition_id="luna-skill",
                skill_package=skill,
                trial_order=2,
                workload={"events": 64, "accounts": 8, "seed": 17, "repeat": 2},
            )
            self.assertFalse((repo / coordinator.SKILL_INSTALL_REL).exists())
            self.assertTrue((treatment / coordinator.SKILL_INSTALL_REL / "SKILL.md").is_file())
            self.assertEqual(control["base"]["commit_sha"], treated["base"]["commit_sha"])
            self.assertEqual(control["base"]["tree_sha"], treated["base"]["tree_sha"])
            self.assertEqual(control["prompt"], treated["prompt"])
            self.assertEqual(control["prompt"]["bytes"], len(b"identical prompt\n"))

    @mock.patch.object(coordinator, "_global_skill_paths", return_value=[])
    def test_mutation_scope_allows_only_candidate_and_detects_untracked_file(
        self, _paths: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, skill = self.repository(root)
            state_path = root / "state.json"
            state = coordinator.prepare_condition(
                checkout=repo,
                state_path=state_path,
                condition_id="terra-control",
                skill_package=skill,
                trial_order=1,
                workload={"events": 64, "accounts": 8, "seed": 17, "repeat": 2},
            )
            (repo / coordinator.CANDIDATE_REL).write_text("VALUE = 2\n", "utf-8")
            allowed = coordinator.verify_mutation(state)
            self.assertTrue(allowed["valid"], allowed)
            (repo / "unexpected.txt").write_text("not allowed", "utf-8")
            rejected = coordinator.verify_mutation(state)
        self.assertFalse(rejected["valid"])
        self.assertIn("unexpected.txt", rejected["unexpected_changes"])

    @mock.patch.object(coordinator, "_global_skill_paths", return_value=[])
    def test_agent_argv_preserves_exact_prompt_as_one_argument(
        self, _paths: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, skill = self.repository(root)
            state = coordinator.prepare_condition(
                checkout=repo,
                state_path=root / "state.json",
                condition_id="terra-control",
                skill_package=skill,
                trial_order=1,
                workload={"events": 64, "accounts": 8, "seed": 17, "repeat": 2},
            )
            command, prompt = coordinator.build_agent_argv(
                state,
                wattch=root / "wattch",
                socket=root / "wattch.sock",
                out=root / "runs",
                codex=root / "codex",
                mcp=root / "wattch-mcp",
                mcp_runs=root / "mcp-runs",
                interval_ms=100,
            )
        self.assertEqual(prompt, "identical prompt\n")
        self.assertEqual(command[-1], "identical prompt\n")
        self.assertEqual(command.count("identical prompt\n"), 1)
        self.assertLess(command.index("-a"), command.index("exec"))
        self.assertGreater(command.index("--ignore-user-config"), command.index("exec"))


if __name__ == "__main__":
    unittest.main()
