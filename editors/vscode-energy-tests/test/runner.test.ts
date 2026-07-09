import assert from "node:assert/strict";
import { test } from "node:test";
// Use `import ... = require(...)` (not `import * as`) so we get the same
// raw CommonJS module object `runner.ts` calls into — `import *` wraps the
// module in getter-based bindings that node:test's mock.method rejects.
import childProcess = require("child_process");
import { RunnerConfig, runTests } from "../src/runner";

const BASE_CONFIG: Omit<RunnerConfig, "socketPath"> = {
  pythonPath: "python3",
  rootDir: "/tmp/project",
  intervalMs: 10,
  repeat: 0,
};

const MINIMAL_RUN_OUTPUT = JSON.stringify({
  schema_version: 1,
  socket: "",
  results: [],
  discovery_errors: [],
  summary: {},
});

function mockExecFile(t: import("node:test").TestContext): { args?: string[] } {
  const capture: { args?: string[] } = {};
  t.mock.method(
    childProcess,
    "execFile",
    (
      _file: string,
      args: string[],
      _options: unknown,
      callback: (error: Error | null, stdout: string, stderr: string) => void
    ) => {
      capture.args = args;
      callback(null, MINIMAL_RUN_OUTPUT, "");
    }
  );
  return capture;
}

test("runTests omits --socket when socketPath is empty", async (t) => {
  const capture = mockExecFile(t);
  await runTests({ ...BASE_CONFIG, socketPath: "" });
  assert.ok(capture.args, "execFile should have been called");
  assert.equal(capture.args!.includes("--socket"), false);
});

test("runTests passes --socket when socketPath is configured", async (t) => {
  const capture = mockExecFile(t);
  await runTests({ ...BASE_CONFIG, socketPath: "/run/wattch/wattch-powerd.sock" });
  assert.ok(capture.args, "execFile should have been called");
  const idx = capture.args!.indexOf("--socket");
  assert.notEqual(idx, -1);
  assert.equal(capture.args![idx + 1], "/run/wattch/wattch-powerd.sock");
});
