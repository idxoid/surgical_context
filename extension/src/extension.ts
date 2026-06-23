import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';
import { OverlayManager } from './overlayManager';
import { SurgicalContextCodeLensProvider } from './providers/CodeLensProvider';
import { SurgicalContextHoverProvider } from './providers/HoverProvider';
import { DashboardPanel } from './panels/DashboardPanel';
import { SurgicalContextViewProvider } from './providers/SurgicalContextViewProvider';
import { stateManager } from './state/ExtensionState';

const SECONDARY_SIDEBAR_PROMPT_KEY = 'surgicalContext.secondarySideBarPromptShown';

interface SymbolCommandTarget {
  symbol?: string;
  filePath?: string;
  line?: number;
  character?: number;
}

export function activate(context: vscode.ExtensionContext): void {
  // Create persistent status bar item
  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = 'surgicalContext.checkHealth';
  statusBarItem.text = '$(loading~spin) Surgical Context';
  statusBarItem.tooltip = 'Click to check sidecar health';
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  // Verify sidecar is reachable and update state (non-blocking)
  SidecarClient.health().then(ok => {
    stateManager.setState({
      sidecarHealth: ok ? 'up' : 'down',
    });
    statusBarItem.text = ok ? '$(check) Surgical Context' : '$(warning) Surgical Context';
    statusBarItem.backgroundColor = ok ? undefined : new vscode.ThemeColor('statusBarItem.warningBackground');
    vscode.window.setStatusBarMessage(
      ok ? '✓ Surgical Context sidecar is ready' : '⚠ Surgical Context sidecar is unreachable',
      5000
    );
  }).catch(err => {
    statusBarItem.text = '$(error) Surgical Context';
    statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
  });

  // Poll cloud status
  SidecarClient.cloudStatus().then(status => {
    const cloudStatus = status.using_aura ? 'connected' : 'local';
    stateManager.setState({ cloudStatus });
  }).catch(err => {
    console.warn('Failed to fetch cloud status:', err);
  });

  // Instantiate managers
  const overlayManager = new OverlayManager();
  const surgicalContextView = new SurgicalContextViewProvider(context.extensionUri, overlayManager);

  const revealSurgicalContextView = async (): Promise<void> => {
    await vscode.commands.executeCommand('workbench.view.extension.surgicalContext');
    await vscode.commands.executeCommand(SurgicalContextViewProvider.viewType + '.focus');
  };

  const openSecondarySideBarMovePicker = async (): Promise<void> => {
    await revealSurgicalContextView();
    await vscode.window.showInformationMessage(
      'Choose "Secondary Side Bar" or "New Secondary Side Bar Entry" in the next picker.'
    );
    await vscode.commands.executeCommand(
      'workbench.action.moveFocusedView',
      SurgicalContextViewProvider.viewType
    );
  };

  // Register document lifecycle subscriptions
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument(e => overlayManager.onDocumentChanged(e)),
    vscode.workspace.onDidSaveTextDocument(doc => overlayManager.onDocumentSaved(doc)),
    vscode.workspace.onDidCloseTextDocument(doc => overlayManager.onDocumentClosed(doc)),
    // Clear stale lastRequest when user switches files
    vscode.window.onDidChangeActiveTextEditor(() => {
      stateManager.clearLastRequestIfStale();
    })
  );

  // Register the single sidebar surface used by the mocks for chat and impact.
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      SurgicalContextViewProvider.viewType,
      surgicalContextView,
      { webviewOptions: { retainContextWhenHidden: true } }
    )
  );

  // Register CodeLens provider
  const codeLensProvider = new SurgicalContextCodeLensProvider();
  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ scheme: 'file' }, codeLensProvider)
  );

  // Register Hover provider
  const hoverProvider = new SurgicalContextHoverProvider(overlayManager);
  context.subscriptions.push(
    vscode.languages.registerHoverProvider({ scheme: 'file' }, hoverProvider)
  );

  // Register commands
  context.subscriptions.push(

    // Spec commands
    vscode.commands.registerCommand('surgicalContext.askCurrentSymbol', async (...args: unknown[]) => {
      const target = await resolveSymbolCommandTarget(overlayManager, args);
      await applySymbolCommandTarget(target);
      await revealSurgicalContextView();
      pushTargetState(target);
      surgicalContextView.showChat();
    }),

    vscode.commands.registerCommand('surgicalContext.askSelection', async (...args: unknown[]) => {
      const target = await resolveSymbolCommandTarget(overlayManager, args);
      await applySymbolCommandTarget(target);
      await revealSurgicalContextView();
      pushTargetState(target);
      surgicalContextView.showChat();
    }),

    vscode.commands.registerCommand('surgicalContext.openInspector', async () => {
      await revealSurgicalContextView();
      surgicalContextView.showInspector();
    }),

    vscode.commands.registerCommand('surgicalContext.showImpact', async (...args: unknown[]) => {
      const target = await resolveSymbolCommandTarget(overlayManager, args);
      await applySymbolCommandTarget(target);
      await revealSurgicalContextView();
      pushTargetState(target);
      await surgicalContextView.showImpact(target.symbol, 3, target.filePath);
    }),

    vscode.commands.registerCommand('surgicalContext.findDocs', async () => {
      vscode.window.showInformationMessage('Find Docs coming in Phase 3');
      // TODO: Implement doc search
    }),

    vscode.commands.registerCommand('surgicalContext.openDashboard', async () => {
      DashboardPanel.createOrReveal(context.extensionUri);
    }),

    vscode.commands.registerCommand('surgicalContext.openSettings', async () => {
      await revealSurgicalContextView();
      surgicalContextView.showSettings();
    }),

    vscode.commands.registerCommand('surgicalContext.moveToSecondarySideBar', async () => {
      await openSecondarySideBarMovePicker();
    }),

    vscode.commands.registerCommand('surgicalContext.reindexCurrentFile', async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showErrorMessage('No active editor.');
        return;
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'Surgical Context: Reindexing file…' },
        () => SidecarClient.indexFile(editor.document.fileName)
      );
      vscode.window.showInformationMessage('File reindexed.');
    }),

    vscode.commands.registerCommand('surgicalContext.toggleOverlaySync', () => {
      const config = vscode.workspace.getConfiguration('surgicalContext');
      const current = config.get<boolean>('overlaySync', true);
      config.update('overlaySync', !current, vscode.ConfigurationTarget.Workspace);
      vscode.window.showInformationMessage(`Overlay sync ${!current ? 'enabled' : 'disabled'}.`);
    }),

    vscode.commands.registerCommand('surgicalContext.searchWorkspace', async () => {
      vscode.window.showInformationMessage('Workspace search coming soon');
      // TODO: Implement search
    }),

    // Legacy commands for backward compatibility
    vscode.commands.registerCommand('surgicalContext.openChat', async (...args: unknown[]) => {
      const target = await resolveSymbolCommandTarget(overlayManager, args);
      await applySymbolCommandTarget(target);
      await revealSurgicalContextView();
      pushTargetState(target);
      surgicalContextView.showChat();
    }),

    vscode.commands.registerCommand('surgicalContext.askAboutCursor', async (...args: unknown[]) => {
      const target = await resolveSymbolCommandTarget(overlayManager, args);
      await applySymbolCommandTarget(target);
      await revealSurgicalContextView();
      pushTargetState(target);
      surgicalContextView.showChat();
    }),

    vscode.commands.registerCommand('surgicalContext.indexProject', async () => {
      const folders = vscode.workspace.workspaceFolders;
      if (!folders?.length) {
        vscode.window.showErrorMessage('No workspace folder open.');
        return;
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: 'Surgical Context: Indexing…' },
        () => SidecarClient.index(folders[0].uri.fsPath)
      );
      vscode.window.showInformationMessage('Surgical Context: Index complete.');
    }),

    vscode.commands.registerCommand('surgicalContext.checkHealth', async () => {
      const ok = await SidecarClient.health();
      vscode.window.showInformationMessage(
        ok ? 'Sidecar is healthy.' : 'Sidecar is not reachable at localhost:8000.'
      );
    })
  );

  void promptForSecondarySideBarPlacement(context, openSecondarySideBarMovePicker);
}

export function deactivate(): void {
  // cleanup handled by context.subscriptions
}

async function resolveSymbolCommandTarget(
  overlayManager: OverlayManager,
  args: unknown[]
): Promise<SymbolCommandTarget> {
  const explicit = explicitTargetFromArgs(args);
  const target = explicit || await targetFromActiveEditorAsync(overlayManager);

  if (!target.symbol && target.filePath && target.line !== undefined) {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(target.filePath));
    const position = new vscode.Position(
      clampLine(target.line, document),
      Math.max(0, target.character || 0)
    );
    target.symbol = (
      await overlayManager.getSymbolAtPositionAsync(document, position)
    ) || undefined;
  }

  return target;
}

function explicitTargetFromArgs(args: unknown[]): SymbolCommandTarget | null {
  const first = args[0];
  const second = args[1];

  if (isSymbolCommandTarget(first)) {
    return { ...first };
  }

  if (first instanceof vscode.Uri) {
    return {
      filePath: first.fsPath,
      ...targetFromActiveEditorIfSameFile(first.fsPath),
    };
  }

  if (first instanceof vscode.Position && args[1] instanceof vscode.TextDocument) {
    const document = args[1] as vscode.TextDocument;
    const position = first as vscode.Position;
    return {
      filePath: document.fileName,
      line: position.line,
      character: position.character,
      symbol: overlayManager.getSymbolAtPosition(document, position) || undefined,
    };
  }

  if (typeof first === 'string' && typeof second === 'string') {
    return {
      filePath: first,
      symbol: second,
    };
  }

  if (typeof first === 'string') {
    return { symbol: first };
  }

  return null;
}

function isSymbolCommandTarget(value: unknown): value is SymbolCommandTarget {
  if (!value || typeof value !== 'object') return false;
  const target = value as Record<string, unknown>;
  return (
    typeof target.symbol === 'string' ||
    typeof target.filePath === 'string' ||
    typeof target.line === 'number'
  );
}

function targetFromActiveEditor(overlayManager: OverlayManager): SymbolCommandTarget {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return {};

  const position = editor.selection.active;
  return {
    filePath: editor.document.fileName,
    symbol: overlayManager.getSymbolAtPosition(editor.document, position) || undefined,
    line: position.line,
    character: position.character,
  };
}

async function targetFromActiveEditorAsync(
  overlayManager: OverlayManager
): Promise<SymbolCommandTarget> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) return {};

  const position = editor.selection.active;
  return {
    filePath: editor.document.fileName,
    symbol: (await overlayManager.getSymbolAtPositionAsync(editor.document, position)) || undefined,
    line: position.line,
    character: position.character,
  };
}

function targetFromActiveEditorIfSameFile(filePath: string): SymbolCommandTarget {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.fileName !== filePath) return {};

  const position = editor.selection.active;
  return {
    line: position.line,
    character: position.character,
  };
}

async function applySymbolCommandTarget(target: SymbolCommandTarget): Promise<void> {
  if (!target.filePath || target.line === undefined) return;

  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(target.filePath));
  const editor = await vscode.window.showTextDocument(document, {
    preview: true,
    preserveFocus: false,
  });
  const position = new vscode.Position(
    clampLine(target.line, document),
    Math.max(0, target.character || 0)
  );
  editor.selection = new vscode.Selection(position, position);
  editor.revealRange(
    new vscode.Range(position, position),
    vscode.TextEditorRevealType.InCenterIfOutsideViewport
  );
}

function pushTargetState(target: SymbolCommandTarget): void {
  const editor = vscode.window.activeTextEditor;
  stateManager.setState({
    selectedSymbol: target.symbol || undefined,
    activeFile: target.filePath || editor?.document.fileName,
    isDirty: editor?.document.isDirty ?? false,
  });
}

function clampLine(line: number, document: vscode.TextDocument): number {
  return Math.max(0, Math.min(line, document.lineCount - 1));
}

async function promptForSecondarySideBarPlacement(
  context: vscode.ExtensionContext,
  openMovePicker: () => Promise<void>
): Promise<void> {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  const shouldPrompt = config.get<boolean>('layout.promptForSecondarySideBar', true);
  const alreadyPrompted = context.globalState.get<boolean>(SECONDARY_SIDEBAR_PROMPT_KEY, false);

  if (!shouldPrompt || alreadyPrompted) {
    return;
  }

  const choice = await vscode.window.showInformationMessage(
    'Surgical Context works best in the Secondary Side Bar on the right. VS Code requires a one-time Move View confirmation.',
    'Move View',
    'Later',
    'Do Not Ask Again'
  );

  if (choice === 'Move View') {
    await context.globalState.update(SECONDARY_SIDEBAR_PROMPT_KEY, true);
    await openMovePicker();
  } else if (choice === 'Do Not Ask Again') {
    await context.globalState.update(SECONDARY_SIDEBAR_PROMPT_KEY, true);
  }
}
