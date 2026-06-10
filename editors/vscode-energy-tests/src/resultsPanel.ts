import * as vscode from "vscode";
import { RunReport, TestStatus, formatJoules } from "./model";

const STATUS_LABELS: Record<TestStatus, string> = {
  passed: "✓ passed",
  failed: "✗ failed",
  error: "⚠ error",
  over_budget: "🔥 over budget",
};

const STATUS_CLASSES: Record<TestStatus, string> = {
  passed: "passed",
  failed: "failed",
  error: "error",
  over_budget: "overBudget",
};

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** The "Energy Tests" results tab: a unit-test report with joule columns. */
export class ResultsPanel {
  private panel: vscode.WebviewPanel | undefined;
  private lastReport: RunReport | undefined;

  show(report?: RunReport): void {
    if (report) {
      this.lastReport = report;
    }
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel(
        "energyTests.results",
        "Energy Tests",
        vscode.ViewColumn.Active,
        { enableScripts: false }
      );
      this.panel.onDidDispose(() => {
        this.panel = undefined;
      });
    } else {
      this.panel.reveal(undefined, true);
    }
    this.panel.webview.html = this.render();
  }

  showError(message: string): void {
    this.lastReport = undefined;
    this.show();
    if (this.panel) {
      this.panel.webview.html = this.renderShell(
        `<p class="banner failedBanner">${escapeHtml(message)}</p>
         <p>Start a daemon and check the <code>energyTests.*</code> settings, then run again.</p>`
      );
    }
  }

  private render(): string {
    if (!this.lastReport) {
      return this.renderShell(
        "<p>No results yet. Run energy tests from the Energy Tests view.</p>"
      );
    }
    const report = this.lastReport;

    const domains = [
      ...new Set(report.results.flatMap((r) => Object.keys(r.energyJoules))),
    ].sort();

    const header = ["Test", "Status", "Time", "Total energy"]
      .concat(domains.map((d) => escapeHtml(d)))
      .concat(["Budget"])
      .map((h) => `<th>${h}</th>`)
      .join("");

    const rows = report.results
      .map((result) => {
        const cells = [
          `<td class="testName">${escapeHtml(result.id)}${
            result.error
              ? `<div class="errorText">${escapeHtml(
                  result.error.trim().split("\n").slice(-1)[0]
                )}</div>`
              : ""
          }</td>`,
          `<td class="${STATUS_CLASSES[result.status]}">${
            STATUS_LABELS[result.status]
          }</td>`,
          `<td>${(result.wallTimeS * 1000).toFixed(1)} ms</td>`,
          `<td><strong>${formatJoules(result.totalJoules)}</strong></td>`,
        ];
        for (const domain of domains) {
          const joules = result.energyJoules[domain];
          cells.push(
            `<td>${joules === undefined ? "—" : formatJoules(joules)}</td>`
          );
        }
        cells.push(
          `<td>${
            result.budgetJoules === null
              ? "—"
              : formatJoules(result.budgetJoules)
          }</td>`
        );
        return `<tr>${cells.join("")}</tr>`;
      })
      .join("\n");

    const summary = report.summary;
    const summaryLine =
      `${summary.total} tests — ` +
      `<span class="passed">${summary.passed} passed</span>, ` +
      `<span class="failed">${summary.failed} failed</span>, ` +
      `<span class="error">${summary.error} errors</span>, ` +
      `<span class="overBudget">${summary.overBudget} over budget</span>` +
      ` · socket ${escapeHtml(report.socket)}`;

    const discoveryErrors = report.discoveryErrors.length
      ? `<p class="banner failedBanner">Discovery errors: ${report.discoveryErrors
          .map((e) => `${escapeHtml(e.file)} (${escapeHtml(e.message)})`)
          .join(", ")}</p>`
      : "";

    return this.renderShell(`
      ${discoveryErrors}
      <p class="banner">${summaryLine}</p>
      <table>
        <thead><tr>${header}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="footnote">Energy from cumulative hardware counters via the
      Wattch daemon. Tag boundaries are quantized to the sampling interval.</p>
    `);
  }

  private renderShell(body: string): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); padding: 0 1rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 4px 10px; border-bottom: 1px solid var(--vscode-widget-border, #444); }
  th { position: sticky; top: 0; background: var(--vscode-editor-background); }
  .passed { color: var(--vscode-testing-iconPassed, #2dba4e); }
  .failed { color: var(--vscode-testing-iconFailed, #f14c4c); }
  .error { color: var(--vscode-testing-iconErrored, #cca700); }
  .overBudget { color: var(--vscode-testing-iconFailed, #f14c4c); font-weight: 600; }
  .banner { padding: 6px 0; }
  .failedBanner { color: var(--vscode-testing-iconFailed, #f14c4c); }
  .errorText { font-size: 0.85em; opacity: 0.8; }
  .testName { font-family: var(--vscode-editor-font-family, monospace); }
  .footnote { opacity: 0.7; font-size: 0.85em; }
</style>
</head>
<body>
<h2>Energy Tests</h2>
${body}
</body>
</html>`;
  }
}
