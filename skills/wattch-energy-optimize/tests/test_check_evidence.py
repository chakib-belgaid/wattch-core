from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_evidence.py"
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_contract import classify_source  # noqa: E402


def source(
    *,
    backend: str = "linux-rapl",
    sampling_mode: str = "POLLED",
    evidence_class: str = "RAW_HARDWARE_COUNTER",
    kind: str = "CPU_PACKAGE_ENERGY",
    unit: str = "MICROJOULE",
    temporality: str = "CUMULATIVE",
) -> dict[str, object]:
    return {
        "local_source_id": 1,
        "source_uid": f"{backend}://node/cpu",
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
        "labels": {},
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_artifact(run: Path, sources: list[dict[str, object]]) -> None:
    manifest = {
        "run_id": "run-1",
        "node_id": "node",
        "protocol_version": "0.1",
        "clock_instance_id": "clock",
        "wattch_version": "test",
        "created_unix_ns": 1,
        "sources": sources,
        "labels": {},
    }
    (run / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (run / "sources.json").write_text(json.dumps(sources), "utf-8")
    (run / "raw.trace.wch").write_bytes(b"raw")
    (run / "events.trace.wch").write_bytes(b"events")


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

    def test_all_explicit_gates_can_enable_one_local_run(self) -> None:
        item = source()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            run.mkdir()
            write_artifact(run, [item])
            (run / "command.json").write_text("{}", "utf-8")
            (run / "target.stdout.log").write_bytes(b"expected\n")
            (run / "target.stderr.log").write_bytes(b"")
            quality = {
                "disposition": "ACCEPTED",
                "issues": [],
                "sources": [
                    {
                        "source_uid": item["source_uid"],
                        "observed_samples": 10,
                        "sequence_gap_count": 0,
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
                        "artifact": {"run_dir": str(run), "files": files},
                        "acquisition": {"complete": True, "inspect_passed": True},
                        "correctness": {
                            "correct": True,
                            "completed_work": True,
                            "stdout_exact": True,
                            "stderr_exact": True,
                            "command_exact": True,
                            "cwd_exact": True,
                            "exit_exact": True,
                        },
                        "calibration_bound": True,
                    }
                ),
                "utf-8",
            )
            result = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )
            self.assertTrue(result["physical_run_claim_eligible"], result)
            (run / "target.stdout.log").write_bytes(b"tampered\n")
            tampered = self.classify(
                run, source_uid=str(item["source_uid"]), trial=trial
            )
        self.assertFalse(tampered["physical_run_claim_eligible"])
        self.assertIn("CHECKSUM_MISMATCH", tampered["blockers"])

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
