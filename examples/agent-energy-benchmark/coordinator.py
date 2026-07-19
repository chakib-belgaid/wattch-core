#!/usr/bin/env python3
"""Coordinator-owned Luna/Terra x skill benchmark controls.

This standard-library harness deliberately does not create worktrees. The
coordinator prepares and verifies already-isolated, clean checkouts, injects the
skill only as a treatment, passes the exact prompt bytes to Codex, and judges
the exact candidate bytes captured by ``wattch run``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from judge import candidate_argv, expected_stdout, judge_artifact, sha256_bytes  # noqa: E402


SCHEMA_VERSION = 1
CANDIDATE_REL = Path("examples/agent-energy-benchmark/candidate.py")
PROMPT_REL = Path("examples/agent-energy-benchmark/PROMPT.txt")
SKILL_INSTALL_REL = Path(".agents/skills/wattch-energy-optimize")
DEFAULT_WORKLOAD = {
    "events": 300_000,
    "accounts": 512,
    "seed": 20260719,
    "repeat": 6,
}
CONDITIONS = {
    "luna-control": ("gpt-5.6-luna", False),
    "luna-skill": ("gpt-5.6-luna", True),
    "terra-control": ("gpt-5.6-terra", False),
    "terra-skill": ("gpt-5.6-terra", True),
}
EFFORT = "medium"
CORE_ARTIFACT_FILES = (
    "manifest.json",
    "sources.json",
    "raw.trace.wch",
    "events.trace.wch",
    "command.json",
    "target.stdout.log",
    "target.stderr.log",
)


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(canonical_json_bytes(value))
        output.flush()
        os.fsync(output.fileno())


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return {"type": "symlink", "mode": mode, "target": os.readlink(path)}
    if stat.S_ISDIR(metadata.st_mode):
        return {"type": "directory", "mode": mode}
    if stat.S_ISREG(metadata.st_mode):
        return {
            "type": "file",
            "mode": mode,
            "bytes": metadata.st_size,
            "sha256": hash_file(path),
        }
    return {"type": "other", "mode": mode}


def snapshot_tree(root: Path) -> dict[str, dict[str, object]]:
    """Hash every path, including ignored files; never follow symlinks."""

    root = root.resolve()
    result: dict[str, dict[str, object]] = {}

    def visit(directory: Path) -> None:
        for item in sorted(os.scandir(directory), key=lambda value: value.name):
            if directory == root and item.name == ".git":
                continue
            path = Path(item.path)
            relative = path.relative_to(root).as_posix()
            result[relative] = _entry(path)
            if item.is_dir(follow_symlinks=False):
                visit(path)

    visit(root)
    return result


def snapshot_sha256(snapshot: dict[str, dict[str, object]]) -> str:
    return sha256_bytes(canonical_json_bytes(snapshot))


def _git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def git_identity(checkout: Path) -> dict[str, str]:
    status = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ValueError(f"checkout must be clean before treatment injection:\n{status}")
    return {
        "commit_sha": _git(checkout, "rev-parse", "HEAD"),
        "tree_sha": _git(checkout, "rev-parse", "HEAD^{tree}"),
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _global_skill_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".codex/skills/wattch-energy-optimize",
        home / ".agents/skills/wattch-energy-optimize",
    ]


def prepare_condition(
    *,
    checkout: Path,
    state_path: Path,
    condition_id: str,
    skill_package: Path,
    trial_order: int,
    workload: dict[str, int] | None = None,
) -> dict[str, object]:
    checkout = checkout.resolve()
    state_path = state_path.resolve()
    skill_package = skill_package.resolve()
    if condition_id not in CONDITIONS:
        raise ValueError(f"unknown condition {condition_id!r}")
    if _inside(state_path, checkout):
        raise ValueError("state must be outside the measured checkout")
    if any(path.exists() for path in _global_skill_paths()):
        raise ValueError("global wattch-energy-optimize install would contaminate controls")
    if not skill_package.is_dir():
        raise ValueError(f"skill package is not a directory: {skill_package}")

    git = git_identity(checkout)
    base_snapshot = snapshot_tree(checkout)
    candidate = checkout / CANDIDATE_REL
    prompt = checkout / PROMPT_REL
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError("candidate must be one regular file")
    prompt_bytes = prompt.read_bytes()
    prompt_text = prompt_bytes.decode("utf-8", errors="strict")
    if prompt_text.encode("utf-8") != prompt_bytes:
        raise ValueError("prompt UTF-8 round trip changed bytes")

    model, skill_enabled = CONDITIONS[condition_id]
    install = checkout / SKILL_INSTALL_REL
    if install.exists() or install.is_symlink():
        raise ValueError(f"skill install path must initially be absent: {install}")
    skill_bundle_snapshot = snapshot_tree(skill_package)
    if skill_enabled:
        install.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_package, install, symlinks=True)
        if snapshot_tree(install) != skill_bundle_snapshot:
            raise ValueError("installed treatment skill differs from frozen package")

    prepared_snapshot = snapshot_tree(checkout)
    workload = dict(workload or DEFAULT_WORKLOAD)
    expected = expected_stdout(
        workload["events"],
        workload["accounts"],
        workload["seed"],
        workload["repeat"],
    )
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "wattch-agent-condition",
        "condition": {
            "id": condition_id,
            "model": model,
            "reasoning_effort": EFFORT,
            "skill_enabled": skill_enabled,
            "trial_order": trial_order,
        },
        "checkout": str(checkout),
        "base": {
            **git,
            "snapshot_sha256": snapshot_sha256(base_snapshot),
        },
        "prompt": {
            "relative_path": PROMPT_REL.as_posix(),
            "bytes": len(prompt_bytes),
            "sha256": sha256_bytes(prompt_bytes),
        },
        "candidate": {
            "relative_path": CANDIDATE_REL.as_posix(),
            "initial_sha256": hash_file(candidate),
        },
        "skill": {
            "install_relative_path": SKILL_INSTALL_REL.as_posix(),
            "bundle_sha256": snapshot_sha256(skill_bundle_snapshot),
            "installed": skill_enabled,
        },
        "workload": workload,
        "expected_output": {
            "stdout_bytes": len(expected),
            "stdout_sha256": sha256_bytes(expected),
            "stderr_sha256": sha256_bytes(b""),
        },
        "prepared_snapshot": prepared_snapshot,
        "prepared_snapshot_sha256": snapshot_sha256(prepared_snapshot),
        "mutable_paths": [CANDIDATE_REL.as_posix()],
    }
    write_new_json(state_path, manifest)
    return manifest


def verify_mutation(
    manifest: dict[str, Any], *, require_initial_candidate: bool = False
) -> dict[str, object]:
    checkout = Path(manifest["checkout"])
    before = manifest["prepared_snapshot"]
    after = snapshot_tree(checkout)
    candidate_rel = manifest["candidate"]["relative_path"]
    candidate = checkout / candidate_rel
    failures: list[str] = []

    before_without_candidate = {key: value for key, value in before.items() if key != candidate_rel}
    after_without_candidate = {key: value for key, value in after.items() if key != candidate_rel}
    all_paths = sorted(set(before_without_candidate) | set(after_without_candidate))
    for path in all_paths:
        if before_without_candidate.get(path) != after_without_candidate.get(path):
            failures.append(path)

    if not candidate.is_file() or candidate.is_symlink():
        failures.append(candidate_rel)
        candidate_sha = None
    else:
        candidate_sha = hash_file(candidate)
        if require_initial_candidate and candidate_sha != manifest["candidate"]["initial_sha256"]:
            failures.append(candidate_rel)

    return {
        "valid": not failures,
        "unexpected_changes": sorted(set(failures)),
        "candidate_sha256": candidate_sha,
        "snapshot_sha256": snapshot_sha256(after),
    }


def _parse_wrapper(stdout: bytes) -> dict[str, Any] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _labels(manifest: dict[str, Any], *, stage: str, extra: dict[str, str]) -> list[str]:
    condition = manifest["condition"]
    labels = {
        "study": "agent-energy-v1",
        "condition": condition["id"],
        "model": condition["model"],
        "reasoning_effort": condition["reasoning_effort"],
        "skill": "on" if condition["skill_enabled"] else "off",
        "prompt_sha256": manifest["prompt"]["sha256"],
        "stage": stage,
        **extra,
    }
    result: list[str] = []
    for key, value in sorted(labels.items()):
        result.extend(("--label", f"{key}={value}"))
    return result


def build_agent_argv(
    manifest: dict[str, Any],
    *,
    wattch: Path,
    socket: Path,
    out: Path,
    codex: Path,
    mcp: Path,
    mcp_runs: Path,
    interval_ms: int,
) -> tuple[list[str], str]:
    checkout = Path(manifest["checkout"])
    prompt_bytes = checkout.joinpath(manifest["prompt"]["relative_path"]).read_bytes()
    if sha256_bytes(prompt_bytes) != manifest["prompt"]["sha256"]:
        raise ValueError("prompt bytes changed after preparation")
    prompt_text = prompt_bytes.decode("utf-8", errors="strict")
    condition = manifest["condition"]
    run_id = f"{condition['id']}-agent"
    mcp_args = [
        "--workspace-root",
        str(checkout),
        "--runs-dir",
        str(mcp_runs.resolve()),
        "--socket",
        str(socket.resolve()),
    ]
    codex_argv = [
        str(codex.resolve()),
        "exec",
        "--ephemeral",
        "--json",
        "-C",
        str(checkout),
        "-m",
        condition["model"],
        "-c",
        f"model_reasoning_effort={json.dumps(condition['reasoning_effort'])}",
        "-s",
        "workspace-write",
        "-a",
        "never",
        "-c",
        f"mcp_servers.wattch.command={json.dumps(str(mcp.resolve()))}",
        "-c",
        f"mcp_servers.wattch.args={json.dumps(mcp_args, separators=(',', ':'))}",
        prompt_text,
    ]
    outer = [
        str(wattch.resolve()),
        "run",
        "--socket",
        str(socket.resolve()),
        "--out",
        str(out.resolve()),
        "--interval-ms",
        str(interval_ms),
        "--run-id",
        run_id,
        *_labels(manifest, stage="agent-workflow", extra={}),
        "--json",
        "--",
        *codex_argv,
    ]
    return outer, prompt_text


def run_agent(args: argparse.Namespace) -> dict[str, object]:
    state = read_json_object(args.state)
    before = verify_mutation(state, require_initial_candidate=True)
    if not before["valid"]:
        raise ValueError(f"condition is not at its prepared state: {before}")
    command, prompt_text = build_agent_argv(
        state,
        wattch=args.wattch,
        socket=args.socket,
        out=args.out,
        codex=args.codex,
        mcp=args.mcp,
        mcp_runs=args.mcp_runs,
        interval_ms=args.interval_ms,
    )
    if prompt_text.encode("utf-8") != Path(state["checkout"]).joinpath(PROMPT_REL).read_bytes():
        raise AssertionError("agent prompt argument lost bytes")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, check=False, env=environment)
    wrapper_duration_s = time.perf_counter() - started
    wrapper = _parse_wrapper(completed.stdout)
    transcript = b""
    agent_artifact: dict[str, object] | None = None
    if wrapper and isinstance(wrapper.get("run_dir"), str):
        run_dir = Path(wrapper["run_dir"]).resolve()
        path = run_dir / "target.stdout.log"
        if path.is_file():
            transcript = path.read_bytes()
        agent_artifact = {
            "run_id": wrapper.get("run_id"),
            "run_dir": str(run_dir),
            "files": _artifact_hashes(run_dir),
        }
    scope = verify_mutation(state)
    skill_observed = b"wattch-energy-optimize" in transcript
    treatment_gate = (
        skill_observed if state["condition"]["skill_enabled"] else not skill_observed
    )
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "wattch-agent-workflow",
        "condition": state["condition"],
        "prompt": state["prompt"],
        "command_argv_sha256": sha256_bytes(canonical_json_bytes(command)),
        "wrapper_returncode": completed.returncode,
        "wrapper_stdout_sha256": sha256_bytes(completed.stdout),
        "wrapper_stderr_sha256": sha256_bytes(completed.stderr),
        "wrapper_duration_s": wrapper_duration_s,
        "wrapper": wrapper,
        "artifact": agent_artifact,
        "mutation_scope": scope,
        "skill_invocation_observed": skill_observed,
        "treatment_gate_passed": treatment_gate,
        "successful": completed.returncode == 0 and bool(wrapper) and scope["valid"] and treatment_gate,
    }
    output = args.state.resolve().parent / f"{state['condition']['id']}-agent-result.json"
    write_new_json(output, result)
    return result


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in CORE_ARTIFACT_FILES:
        path = run_dir / relative
        if path.is_file() and not path.is_symlink():
            result[relative] = hash_file(path)
    return result


def _candidate_command(state: dict[str, Any], python: Path) -> list[str]:
    workload = state["workload"]
    return candidate_argv(
        python,
        Path(state["checkout"]) / state["candidate"]["relative_path"],
        workload["events"],
        workload["accounts"],
        workload["seed"],
        workload["repeat"],
    )


def _exact_child(command: list[str], cwd: Path) -> dict[str, object]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        check=False,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_trials(args: argparse.Namespace) -> dict[str, object]:
    state = read_json_object(args.state)
    checkout = Path(state["checkout"])
    agent_result_path = (
        args.state.resolve().parent / f"{state['condition']['id']}-agent-result.json"
    )
    if not agent_result_path.is_file():
        raise ValueError("run-agent must complete before generated-code trials")
    agent_result = read_json_object(agent_result_path)
    if agent_result.get("successful") is not True:
        raise ValueError("agent result did not pass execution, scope, and treatment gates")
    scope_before = verify_mutation(state)
    if not scope_before["valid"] or scope_before["candidate_sha256"] is None:
        raise ValueError(f"unexpected checkout mutation before trials: {scope_before}")
    candidate_sha = str(scope_before["candidate_sha256"])
    command = _candidate_command(state, args.python)
    workload = state["workload"]
    expected = expected_stdout(
        workload["events"], workload["accounts"], workload["seed"], workload["repeat"]
    )

    warmup = _exact_child(command, checkout)
    warmup_result = {
        "schema_version": SCHEMA_VERSION,
        "correct": warmup["returncode"] == 0
        and warmup["stdout"] == expected
        and warmup["stderr"] == b"",
        "returncode": warmup["returncode"],
        "stdout_sha256": sha256_bytes(warmup["stdout"]),
        "stderr_sha256": sha256_bytes(warmup["stderr"]),
        "candidate_sha256": candidate_sha,
    }
    write_new_json(
        args.state.resolve().parent / f"{state['condition']['id']}-warmup.json",
        warmup_result,
    )
    if not warmup_result["correct"]:
        raise ValueError("unmeasured warm-up failed exact correctness")
    if verify_mutation(state)["candidate_sha256"] != candidate_sha:
        raise ValueError("candidate changed during warm-up")

    trial_records: list[dict[str, object]] = []
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for index in range(1, args.repetitions + 1):
        run_id = f"{state['condition']['id']}-workload-r{index:03d}"
        wattch_command = [
            str(args.wattch.resolve()),
            "run",
            "--socket",
            str(args.socket.resolve()),
            "--out",
            str(args.out.resolve()),
            "--interval-ms",
            str(args.interval_ms),
            "--run-id",
            run_id,
            *_labels(
                state,
                stage="generated-code-workload",
                extra={"candidate_sha256": candidate_sha, "trial": str(index)},
            ),
            "--json",
            "--",
            *command,
        ]
        started = time.perf_counter()
        completed = subprocess.run(
            wattch_command,
            cwd=checkout,
            env=environment,
            capture_output=True,
            check=False,
        )
        wrapper_duration_s = time.perf_counter() - started
        wrapper = _parse_wrapper(completed.stdout)
        run_dir = (
            Path(wrapper["run_dir"]).resolve()
            if wrapper and isinstance(wrapper.get("run_dir"), str)
            else args.out.resolve() / run_id
        )
        child_exit_code = wrapper.get("exit_code") if wrapper else None
        judgement: dict[str, object]
        required_logs = ("command.json", "target.stdout.log", "target.stderr.log")
        if all((run_dir / relative).is_file() for relative in required_logs):
            judgement = judge_artifact(
                run_dir,
                expected_argv=command,
                expected_cwd=checkout,
                count=workload["events"],
                accounts=workload["accounts"],
                seed=workload["seed"],
                repeat=workload["repeat"],
                child_exit_code=child_exit_code if isinstance(child_exit_code, int) else None,
            )
        else:
            judgement = {
                "schema_version": 1,
                "correct": False,
                "completed_work": False,
                "failure": "captured command logs are missing",
            }
        inspect = subprocess.run(
            [str(args.wattch.resolve()), "inspect", "--run", str(run_dir)],
            capture_output=True,
            check=False,
        )
        scope = verify_mutation(state)
        candidate_unchanged = scope["candidate_sha256"] == candidate_sha
        correct = bool(judgement.get("correct")) and candidate_unchanged and scope["valid"]
        record: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "wattch-agent-trial",
            "condition": state["condition"],
            "prompt": state["prompt"],
            "base": state["base"],
            "agent_result_sha256": hash_file(agent_result_path),
            "trial": {
                "index": index,
                "execution_order": state["condition"]["trial_order"],
                "outcome_type": "generated-code-workload",
            },
            "candidate": {
                "path": str((checkout / CANDIDATE_REL).resolve()),
                "sha256": candidate_sha,
                "unchanged_during_trial": candidate_unchanged,
            },
            "command": {
                "argv": command,
                "cwd": str(checkout.resolve()),
                "argv_sha256": sha256_bytes(canonical_json_bytes(command)),
            },
            "artifact": {
                "run_id": run_id,
                "run_dir": str(run_dir),
                "files": _artifact_hashes(run_dir),
            },
            "acquisition": {
                "complete": wrapper is not None and completed.returncode == 0,
                "wrapper_returncode": completed.returncode,
                "wrapper": wrapper,
                "wrapper_duration_s": wrapper_duration_s,
                "inspect_passed": inspect.returncode == 0,
                "inspect_stdout_sha256": sha256_bytes(inspect.stdout),
                "inspect_stderr_sha256": sha256_bytes(inspect.stderr),
            },
            "correctness": {**judgement, "correct": correct},
            "mutation_scope": scope,
            "physical_claim_eligible": False,
            "physical_claim_blockers": [
                "QUALITY_NOT_ACCEPTED",
                "CALIBRATION_NOT_BOUND",
                "SINGLE_RUN_CANNOT_ESTABLISH_COMPARISON",
                "REMOTE_INFERENCE_UNOBSERVED",
            ],
        }
        record_path = args.state.resolve().parent / "trials" / f"{run_id}.json"
        write_new_json(record_path, record)
        trial_records.append(record)

    with tempfile.TemporaryDirectory(prefix="wattch-candidate-probe-") as temporary:
        probe = subprocess.run(
            [
                str(args.python.resolve()),
                "-I",
                "-B",
                str((checkout / "examples/agent-energy-benchmark/judge.py").resolve()),
                "probe",
                "--candidate",
                str((checkout / CANDIDATE_REL).resolve()),
                "--python",
                str(args.python.resolve()),
            ],
            cwd=temporary,
            env=environment,
            capture_output=True,
            check=False,
        )
    probe_value = _parse_wrapper(probe.stdout)
    final_scope = verify_mutation(state)
    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "wattch-agent-condition-result",
        "condition": state["condition"],
        "candidate_sha256": candidate_sha,
        "repetitions": args.repetitions,
        "all_measured_trials_correct": all(
            bool(record["correctness"]["correct"]) for record in trial_records
        ),
        "api_probe_passed": probe.returncode == 0
        and bool(probe_value)
        and bool(probe_value.get("correct")),
        "api_probe": probe_value,
        "final_mutation_scope": final_scope,
        "physical_comparison_claim_eligible": False,
        "comparison_limitation": (
            "Requires claim-eligible repeated local-source trials and uncertainty; "
            "local captures never measure remote model inference."
        ),
    }
    write_new_json(
        args.state.resolve().parent / f"{state['condition']['id']}-result.json", summary
    )
    return summary


def compare_states(paths: Iterable[Path]) -> dict[str, object]:
    states = [read_json_object(path) for path in paths]
    if len(states) != 4:
        raise ValueError("comparison requires exactly four condition states")
    condition_ids = {state["condition"]["id"] for state in states}
    failures: list[str] = []
    if condition_ids != set(CONDITIONS):
        failures.append("the four canonical conditions are not present exactly once")
    reference = states[0]
    for state in states[1:]:
        for field in ("base", "prompt", "candidate", "workload", "expected_output"):
            if state[field] != reference[field]:
                failures.append(f"condition {state['condition']['id']} differs in {field}")
    return {
        "valid": not failures,
        "conditions": sorted(condition_ids),
        "failures": failures,
        "base_commit": reference["base"]["commit_sha"],
        "base_tree": reference["base"]["tree_sha"],
        "prompt_sha256": reference["prompt"]["sha256"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--checkout", type=Path, required=True)
    prepare.add_argument("--state", type=Path, required=True)
    prepare.add_argument("--condition", choices=sorted(CONDITIONS), required=True)
    prepare.add_argument("--skill-package", type=Path, required=True)
    prepare.add_argument("--trial-order", type=int, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--state", type=Path, required=True)

    compare = subparsers.add_parser("compare")
    compare.add_argument("states", type=Path, nargs=4)

    agent = subparsers.add_parser("run-agent")
    agent.add_argument("--state", type=Path, required=True)
    agent.add_argument("--wattch", type=Path, required=True)
    agent.add_argument("--socket", type=Path, required=True)
    agent.add_argument("--out", type=Path, required=True)
    agent.add_argument("--codex", type=Path, required=True)
    agent.add_argument("--mcp", type=Path, required=True)
    agent.add_argument("--mcp-runs", type=Path, required=True)
    agent.add_argument("--interval-ms", type=int, default=100)

    trials = subparsers.add_parser("run-trials")
    trials.add_argument("--state", type=Path, required=True)
    trials.add_argument("--wattch", type=Path, required=True)
    trials.add_argument("--socket", type=Path, required=True)
    trials.add_argument("--out", type=Path, required=True)
    trials.add_argument("--python", type=Path, default=Path(sys.executable))
    trials.add_argument("--interval-ms", type=int, default=100)
    trials.add_argument("--repetitions", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "prepare":
        result = prepare_condition(
            checkout=args.checkout,
            state_path=args.state,
            condition_id=args.condition,
            skill_package=args.skill_package,
            trial_order=args.trial_order,
        )
        output: object = {
            "state": str(args.state.resolve()),
            "condition": result["condition"],
            "prompt": result["prompt"],
            "base": result["base"],
        }
    elif args.command == "verify":
        output = verify_mutation(read_json_object(args.state))
    elif args.command == "compare":
        output = compare_states(args.states)
    elif args.command == "run-agent":
        output = run_agent(args)
    else:
        if args.repetitions <= 0:
            raise ValueError("repetitions must be positive")
        output = run_trials(args)
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    if isinstance(output, dict) and output.get("valid") is False:
        raise SystemExit(1)
    if isinstance(output, dict) and output.get("successful") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
