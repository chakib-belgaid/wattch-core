/**
 * Runner-output model: types and validation for the JSON emitted by
 * `python -m pywattch.energytest {discover,run} --json` (schema_version 1).
 *
 * All measurement logic lives in Python; the extension only renders.
 */

export const SUPPORTED_SCHEMA_VERSION = 1;

export type TestStatus = "passed" | "failed" | "error" | "over_budget";

export interface EnergyTestInfo {
  id: string;
  file: string;
  name: string;
  line: number;
  budgetJoules: number | null;
  repeat: number;
}

export interface DiscoveryError {
  file: string;
  message: string;
}

export interface Discovery {
  tests: EnergyTestInfo[];
  errors: DiscoveryError[];
}

export interface EnergyTestResult {
  id: string;
  file: string;
  name: string;
  line: number;
  status: TestStatus;
  budgetJoules: number | null;
  repeats: number;
  wallTimeS: number;
  energyJoules: Record<string, number>;
  totalJoules: number;
  error: string | null;
}

export interface RunReport {
  socket: string;
  results: EnergyTestResult[];
  discoveryErrors: DiscoveryError[];
  summary: {
    total: number;
    passed: number;
    failed: number;
    error: number;
    overBudget: number;
  };
}

export class RunnerOutputError extends Error {}

function parseJson(stdout: string, context: string): any {
  let data: any;
  try {
    data = JSON.parse(stdout);
  } catch (err) {
    throw new RunnerOutputError(
      `${context}: runner did not produce JSON (${(err as Error).message}). ` +
        `Output started with: ${stdout.slice(0, 120)}`
    );
  }
  if (typeof data !== "object" || data === null) {
    throw new RunnerOutputError(`${context}: unexpected non-object output`);
  }
  if (data.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    throw new RunnerOutputError(
      `${context}: unsupported runner schema_version ${data.schema_version}; ` +
        `this extension supports ${SUPPORTED_SCHEMA_VERSION}. Update pywattch or the extension.`
    );
  }
  return data;
}

function asArray(value: unknown, context: string): any[] {
  if (!Array.isArray(value)) {
    throw new RunnerOutputError(`${context}: expected an array`);
  }
  return value;
}

const STATUSES: TestStatus[] = ["passed", "failed", "error", "over_budget"];

export function parseDiscoverOutput(stdout: string): Discovery {
  const data = parseJson(stdout, "discover");
  const tests = asArray(data.tests, "discover.tests").map((t: any): EnergyTestInfo => {
    if (typeof t.id !== "string" || typeof t.file !== "string" || typeof t.name !== "string") {
      throw new RunnerOutputError(`discover: malformed test entry ${JSON.stringify(t)}`);
    }
    return {
      id: t.id,
      file: t.file,
      name: t.name,
      line: typeof t.line === "number" ? t.line : 0,
      budgetJoules: typeof t.budget_joules === "number" ? t.budget_joules : null,
      repeat: typeof t.repeat === "number" ? t.repeat : 1,
    };
  });
  const errors = asArray(data.errors ?? [], "discover.errors").map(
    (e: any): DiscoveryError => ({
      file: String(e.file ?? "<unknown>"),
      message: String(e.message ?? "unknown error"),
    })
  );
  return { tests, errors };
}

export function parseRunOutput(stdout: string): RunReport {
  const data = parseJson(stdout, "run");
  const results = asArray(data.results, "run.results").map((r: any): EnergyTestResult => {
    if (typeof r.id !== "string" || !STATUSES.includes(r.status)) {
      throw new RunnerOutputError(`run: malformed result entry ${JSON.stringify(r)}`);
    }
    return {
      id: r.id,
      file: String(r.file),
      name: String(r.name),
      line: typeof r.line === "number" ? r.line : 0,
      status: r.status,
      budgetJoules: typeof r.budget_joules === "number" ? r.budget_joules : null,
      repeats: typeof r.repeats === "number" ? r.repeats : 1,
      wallTimeS: typeof r.wall_time_s === "number" ? r.wall_time_s : 0,
      energyJoules:
        typeof r.energy_joules === "object" && r.energy_joules !== null ? r.energy_joules : {},
      totalJoules: typeof r.total_joules === "number" ? r.total_joules : 0,
      error: typeof r.error === "string" ? r.error : null,
    };
  });
  const summaryData = data.summary ?? {};
  const discoveryErrors = asArray(data.discovery_errors ?? [], "run.discovery_errors").map(
    (e: any): DiscoveryError => ({
      file: String(e.file ?? "<unknown>"),
      message: String(e.message ?? "unknown error"),
    })
  );
  return {
    socket: String(data.socket ?? ""),
    results,
    discoveryErrors,
    summary: {
      total: Number(summaryData.total ?? results.length),
      passed: Number(summaryData.passed ?? 0),
      failed: Number(summaryData.failed ?? 0),
      error: Number(summaryData.error ?? 0),
      overBudget: Number(summaryData.over_budget ?? 0),
    },
  };
}

export function formatJoules(joules: number): string {
  if (joules === 0) return "0 J";
  if (Math.abs(joules) < 0.001) return `${(joules * 1e6).toFixed(1)} µJ`;
  if (Math.abs(joules) < 1) return `${(joules * 1e3).toFixed(2)} mJ`;
  return `${joules.toFixed(3)} J`;
}
