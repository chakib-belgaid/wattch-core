import * as path from "path";
import * as vscode from "vscode";
import {
  EnergyTestInfo,
  EnergyTestResult,
  TestStatus,
  formatJoules,
} from "./model";

const STATUS_ICONS: Record<TestStatus, vscode.ThemeIcon> = {
  passed: new vscode.ThemeIcon(
    "testing-passed-icon",
    new vscode.ThemeColor("testing.iconPassed")
  ),
  failed: new vscode.ThemeIcon(
    "testing-failed-icon",
    new vscode.ThemeColor("testing.iconFailed")
  ),
  error: new vscode.ThemeIcon(
    "testing-error-icon",
    new vscode.ThemeColor("testing.iconErrored")
  ),
  over_budget: new vscode.ThemeIcon(
    "flame",
    new vscode.ThemeColor("testing.iconFailed")
  ),
};

export type TreeNode = FileNode | TestNode;

export class FileNode {
  constructor(public readonly file: string, public readonly tests: TestNode[]) {}
}

export class TestNode {
  constructor(
    public readonly info: EnergyTestInfo,
    public result?: EnergyTestResult
  ) {}
}

export class EnergyTestTreeProvider implements vscode.TreeDataProvider<TreeNode> {
  private readonly emitter = new vscode.EventEmitter<TreeNode | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  private files: FileNode[] = [];
  private byId = new Map<string, TestNode>();

  setTests(tests: EnergyTestInfo[]): void {
    const grouped = new Map<string, TestNode[]>();
    this.byId.clear();
    for (const info of tests) {
      const node = new TestNode(info);
      this.byId.set(info.id, node);
      const bucket = grouped.get(info.file) ?? [];
      bucket.push(node);
      grouped.set(info.file, bucket);
    }
    this.files = [...grouped.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([file, nodes]) => new FileNode(file, nodes));
    this.emitter.fire(undefined);
  }

  applyResults(results: EnergyTestResult[]): void {
    for (const result of results) {
      const node = this.byId.get(result.id);
      if (node) {
        node.result = result;
      }
    }
    this.emitter.fire(undefined);
  }

  get testIds(): string[] {
    return [...this.byId.keys()];
  }

  getTreeItem(node: TreeNode): vscode.TreeItem {
    if (node instanceof FileNode) {
      const item = new vscode.TreeItem(
        node.file,
        vscode.TreeItemCollapsibleState.Expanded
      );
      item.iconPath = vscode.ThemeIcon.File;
      item.contextValue = "energyTestFile";
      return item;
    }

    const item = new vscode.TreeItem(
      node.info.name,
      vscode.TreeItemCollapsibleState.None
    );
    item.id = node.info.id;
    item.contextValue = "energyTest";
    if (node.result) {
      item.iconPath = STATUS_ICONS[node.result.status];
      item.description = `${formatJoules(node.result.totalJoules)} · ${(
        node.result.wallTimeS * 1000
      ).toFixed(1)} ms`;
      item.tooltip = node.result.error ?? node.result.status;
    } else {
      item.iconPath = new vscode.ThemeIcon("circle-large-outline");
      item.description =
        node.info.budgetJoules !== null
          ? `budget ${formatJoules(node.info.budgetJoules)}`
          : undefined;
    }
    return item;
  }

  getChildren(node?: TreeNode): TreeNode[] {
    if (!node) {
      return this.files;
    }
    if (node instanceof FileNode) {
      return node.tests;
    }
    return [];
  }

  resolveFileUri(rootDir: string, node: TestNode): vscode.Uri {
    return vscode.Uri.file(path.join(rootDir, node.info.file));
  }
}
