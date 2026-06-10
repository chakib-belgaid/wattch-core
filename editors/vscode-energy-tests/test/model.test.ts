import assert from "node:assert/strict";
import { test } from "node:test";
import {
  RunnerOutputError,
  formatJoules,
  parseDiscoverOutput,
  parseRunOutput,
} from "../src/model";

const DISCOVER_OUTPUT = JSON.stringify({
  schema_version: 1,
  root: ".",
  tests: [
    {
      id: "energy_test_sample.py::energy_test_by_name",
      file: "energy_test_sample.py",
      name: "energy_test_by_name",
      line: 8,
      budget_joules: null,
      repeat: 1,
    },
    {
      id: "energy_test_sample.py::decorated_within_budget",
      file: "energy_test_sample.py",
      name: "decorated_within_budget",
      line: 13,
      budget_joules: 1000.0,
      repeat: 3,
    },
  ],
  errors: [{ file: "energy_test_broken.py", message: "No module named 'x'" }],
});

const RUN_OUTPUT = JSON.stringify({
  schema_version: 1,
  root: ".",
  socket: "/tmp/wattch-powerd.sock",
  results: [
    {
      id: "energy_test_sample.py::energy_test_by_name",
      file: "energy_test_sample.py",
      name: "energy_test_by_name",
      line: 8,
      status: "passed",
      budget_joules: null,
      repeats: 1,
      wall_time_s: 0.021,
      energy_joules: { "cpu/package/0/energy": 0.004, "cpu/package/0/dram/energy": 0.001 },
      total_joules: 0.005,
      error: null,
      runs: [],
    },
    {
      id: "energy_test_sample.py::decorated_over_budget",
      file: "energy_test_sample.py",
      name: "decorated_over_budget",
      line: 18,
      status: "over_budget",
      budget_joules: 0.0,
      repeats: 1,
      wall_time_s: 0.02,
      energy_joules: { "cpu/package/0/energy": 0.004 },
      total_joules: 0.004,
      error: "median energy 0.004 J exceeds budget 0.0 J",
      runs: [],
    },
  ],
  discovery_errors: [],
  summary: { total: 2, passed: 1, failed: 0, error: 0, over_budget: 1 },
});

test("parses discover output", () => {
  const discovery = parseDiscoverOutput(DISCOVER_OUTPUT);
  assert.equal(discovery.tests.length, 2);
  assert.equal(discovery.tests[0].name, "energy_test_by_name");
  assert.equal(discovery.tests[0].budgetJoules, null);
  assert.equal(discovery.tests[1].budgetJoules, 1000.0);
  assert.equal(discovery.tests[1].repeat, 3);
  assert.equal(discovery.errors.length, 1);
  assert.match(discovery.errors[0].message, /No module named/);
});

test("parses run output with statuses and energy", () => {
  const report = parseRunOutput(RUN_OUTPUT);
  assert.equal(report.socket, "/tmp/wattch-powerd.sock");
  assert.equal(report.results.length, 2);

  const passed = report.results[0];
  assert.equal(passed.status, "passed");
  assert.equal(passed.totalJoules, 0.005);
  assert.equal(passed.energyJoules["cpu/package/0/energy"], 0.004);

  const overBudget = report.results[1];
  assert.equal(overBudget.status, "over_budget");
  assert.match(overBudget.error ?? "", /exceeds budget/);

  assert.deepEqual(report.summary, {
    total: 2,
    passed: 1,
    failed: 0,
    error: 0,
    overBudget: 1,
  });
});

test("rejects non-JSON runner output with a helpful error", () => {
  assert.throws(
    () => parseRunOutput("Traceback (most recent call last): ..."),
    (err: unknown) =>
      err instanceof RunnerOutputError &&
      /did not produce JSON/.test(err.message) &&
      /Traceback/.test(err.message)
  );
});

test("rejects unsupported schema versions", () => {
  const future = JSON.stringify({ schema_version: 99, results: [] });
  assert.throws(
    () => parseRunOutput(future),
    (err: unknown) =>
      err instanceof RunnerOutputError && /schema_version 99/.test(err.message)
  );
});

test("rejects malformed result entries", () => {
  const bad = JSON.stringify({
    schema_version: 1,
    results: [{ id: 42, status: "passed" }],
    summary: {},
  });
  assert.throws(() => parseRunOutput(bad), RunnerOutputError);
});

test("formatJoules picks sensible units", () => {
  assert.equal(formatJoules(0), "0 J");
  assert.equal(formatJoules(0.0000005), "0.5 µJ");
  assert.equal(formatJoules(0.0042), "4.20 mJ");
  assert.equal(formatJoules(1.5), "1.500 J");
});
