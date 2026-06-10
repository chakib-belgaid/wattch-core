import * as vscode from "vscode";
import { RunnerConfig, discoverTests, runTests } from "./runner";
import { ResultsPanel } from "./resultsPanel";
import { EnergyTestTreeProvider, TestNode } from "./tree";

function readConfig(): RunnerConfig | undefined {
  const workspace = vscode.workspace.workspaceFolders?.[0];
  const settings = vscode.workspace.getConfiguration("energyTests");
  const rootSetting = settings.get<string>("rootDir", "");
  const rootDir = rootSetting || workspace?.uri.fsPath;
  if (!rootDir) {
    void vscode.window.showWarningMessage(
      "Energy Tests: open a folder or set energyTests.rootDir."
    );
    return undefined;
  }
  return {
    pythonPath: settings.get<string>("pythonPath", "python3"),
    socketPath: settings.get<string>("socketPath", "/tmp/wattch-powerd.sock"),
    intervalMs: settings.get<number>("intervalMs", 10),
    repeat: settings.get<number>("repeat", 0),
    rootDir,
  };
}

export function activate(context: vscode.ExtensionContext): void {
  const tree = new EnergyTestTreeProvider();
  const panel = new ResultsPanel();

  const treeView = vscode.window.createTreeView("energyTests.testTree", {
    treeDataProvider: tree,
  });
  context.subscriptions.push(treeView);

  async function refresh(): Promise<void> {
    const config = readConfig();
    if (!config) {
      return;
    }
    try {
      const discovery = await discoverTests(config);
      tree.setTests(discovery.tests);
      if (discovery.errors.length) {
        void vscode.window.showWarningMessage(
          `Energy Tests: ${discovery.errors.length} file(s) failed to import: ` +
            discovery.errors.map((e) => e.file).join(", ")
        );
      }
    } catch (err) {
      tree.setTests([]);
      void vscode.window.showErrorMessage(
        `Energy Tests: discovery failed — ${(err as Error).message}`
      );
    }
  }

  async function run(testIds?: string[]): Promise<void> {
    const config = readConfig();
    if (!config) {
      return;
    }
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: testIds?.length
          ? `Measuring energy of ${testIds.length} test(s)…`
          : "Measuring energy of all tests…",
        cancellable: false,
      },
      async () => {
        try {
          const report = await runTests(config, testIds);
          tree.applyResults(report.results);
          panel.show(report);
        } catch (err) {
          panel.showError((err as Error).message);
        }
      }
    );
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("energyTests.refresh", refresh),
    vscode.commands.registerCommand("energyTests.runAll", () => run()),
    vscode.commands.registerCommand("energyTests.runTest", (node?: TestNode) => {
      if (node instanceof TestNode) {
        return run([node.info.id]);
      }
      return run();
    }),
    vscode.commands.registerCommand("energyTests.openResults", () => panel.show())
  );

  const watcher = vscode.workspace.createFileSystemWatcher(
    "**/*energy_test*.py"
  );
  watcher.onDidCreate(refresh);
  watcher.onDidDelete(refresh);
  watcher.onDidChange(refresh);
  context.subscriptions.push(watcher);

  void refresh();
}

export function deactivate(): void {}
