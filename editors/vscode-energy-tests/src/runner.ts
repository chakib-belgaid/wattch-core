import { execFile } from "child_process";
import {
  Discovery,
  RunReport,
  parseDiscoverOutput,
  parseRunOutput,
} from "./model";

export interface RunnerConfig {
  pythonPath: string;
  rootDir: string;
  socketPath: string;
  intervalMs: number;
  /** 0 keeps each test's own repeat setting. */
  repeat: number;
}

export class RunnerProcessError extends Error {
  constructor(message: string, public readonly stderr: string) {
    super(message);
  }
}

function execRunner(
  config: RunnerConfig,
  args: string[]
): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    execFile(
      config.pythonPath,
      ["-m", "pywattch.energytest", ...args],
      { cwd: config.rootDir, maxBuffer: 64 * 1024 * 1024, timeout: 600_000 },
      (error, stdout, stderr) => {
        // The runner exits 1 when tests fail; that still carries a report,
        // so only treat missing stdout as a process failure.
        if (error && !stdout) {
          reject(
            new RunnerProcessError(
              `energy test runner failed: ${error.message}. ` +
                `Is pywattch installed for '${config.pythonPath}'?`,
              stderr
            )
          );
          return;
        }
        resolve({ stdout, stderr });
      }
    );
  });
}

export async function discoverTests(config: RunnerConfig): Promise<Discovery> {
  const { stdout } = await execRunner(config, [
    "discover",
    "--root",
    config.rootDir,
    "--json",
  ]);
  return parseDiscoverOutput(stdout);
}

export async function runTests(
  config: RunnerConfig,
  testIds?: string[]
): Promise<RunReport> {
  const args = [
    "run",
    "--root",
    config.rootDir,
    "--socket",
    config.socketPath,
    "--interval-ms",
    String(config.intervalMs),
    "--json",
  ];
  if (config.repeat > 0) {
    args.push("--repeat", String(config.repeat));
  }
  for (const id of testIds ?? []) {
    args.push("--test", id);
  }
  const { stdout } = await execRunner(config, args);
  return parseRunOutput(stdout);
}
