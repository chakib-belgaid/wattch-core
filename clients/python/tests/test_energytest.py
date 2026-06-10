import json
import subprocess
import sys
from pathlib import Path

from pywattch.energytest import discover_tests, run_tests
from pywattch.energytest.runner import summarize

SUITE = str(Path(__file__).parent / "fixtures" / "energy_suite")


class TestDiscovery:
    def test_finds_named_and_decorated_tests(self):
        tests, errors = discover_tests(SUITE)
        assert errors == []
        ids = {t.test_id for t in tests}
        assert ids == {
            "energy_test_sample.py::energy_test_by_name",
            "energy_test_sample.py::decorated_within_budget",
            "energy_test_sample.py::decorated_over_budget",
            "energy_test_sample.py::decorated_failing",
            "energy_test_sample.py::decorated_erroring",
            "helpers_energy_test.py::energy_test_second_file",
        }

    def test_metadata_is_collected(self):
        tests, _ = discover_tests(SUITE)
        by_name = {t.name: t for t in tests}
        assert by_name["decorated_within_budget"].budget_joules == 1000.0
        assert by_name["energy_test_by_name"].budget_joules is None
        assert by_name["decorated_over_budget"].line > 0

    def test_import_error_is_reported_not_raised(self, tmp_path):
        (tmp_path / "energy_test_broken.py").write_text("import does_not_exist\n")
        tests, errors = discover_tests(str(tmp_path))
        assert tests == []
        assert len(errors) == 1
        assert "does_not_exist" in errors[0].message


class TestRunner:
    def test_statuses_and_energy(self, fake_daemon):
        tests, _ = discover_tests(SUITE)
        results = run_tests(tests, socket_path=fake_daemon.socket_path, interval_ms=5)
        by_name = {r.name: r for r in results}

        assert by_name["energy_test_by_name"].status == "passed"
        assert by_name["energy_test_by_name"].total_joules > 0
        assert by_name["energy_test_by_name"].energy_joules["cpu/package/0/energy"] > 0
        assert by_name["energy_test_by_name"].wall_time_s > 0

        assert by_name["decorated_within_budget"].status == "passed"

        over = by_name["decorated_over_budget"]
        assert over.status == "over_budget"
        assert "exceeds" in over.error

        failed = by_name["decorated_failing"]
        assert failed.status == "failed"
        assert "intentional assertion failure" in failed.error

        errored = by_name["decorated_erroring"]
        assert errored.status == "error"
        assert "intentional error" in errored.error

        summary = summarize(results)
        assert summary == {
            "total": 6,
            "passed": 3,
            "failed": 1,
            "error": 1,
            "over_budget": 1,
        }

    def test_repeat_override_runs_multiple_measurements(self, fake_daemon):
        tests, _ = discover_tests(SUITE)
        target = [t for t in tests if t.name == "decorated_within_budget"]
        results = run_tests(
            target,
            socket_path=fake_daemon.socket_path,
            interval_ms=5,
            repeat_override=3,
        )
        assert len(results[0].runs) == 3
        assert results[0].repeats == 3


class TestCli:
    def test_discover_json_schema(self):
        process = subprocess.run(
            [sys.executable, "-m", "pywattch.energytest", "discover", "--root", SUITE, "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(process.stdout)
        assert data["schema_version"] == 1
        assert len(data["tests"]) == 6
        assert data["errors"] == []
        sample = data["tests"][0]
        assert sample.keys() == {"id", "file", "name", "line", "budget_joules", "repeat"}

    def test_run_json_schema_and_exit_code(self, fake_daemon):
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pywattch.energytest",
                "run",
                "--root",
                SUITE,
                "--socket",
                fake_daemon.socket_path,
                "--interval-ms",
                "5",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        # Suite contains failing tests, so the exit code must be 1.
        assert process.returncode == 1, process.stderr
        data = json.loads(process.stdout)
        assert data["schema_version"] == 1
        assert data["summary"]["total"] == 6
        assert data["summary"]["passed"] == 3
        result = data["results"][0]
        assert {
            "id",
            "file",
            "name",
            "line",
            "status",
            "budget_joules",
            "repeats",
            "wall_time_s",
            "energy_joules",
            "total_joules",
            "error",
            "runs",
        } == result.keys()

    def test_run_single_test_passes(self, fake_daemon):
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pywattch.energytest",
                "run",
                "--root",
                SUITE,
                "--socket",
                fake_daemon.socket_path,
                "--interval-ms",
                "5",
                "--test",
                "energy_test_sample.py::energy_test_by_name",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert process.returncode == 0, process.stderr
        data = json.loads(process.stdout)
        assert [r["status"] for r in data["results"]] == ["passed"]
