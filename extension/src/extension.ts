import * as vscode from 'vscode';
import { SidecarClient } from './context_engineClient';
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

type RevealSurgicalContextView = () => Promise<void>;

async function showChatForCommandTarget(
  overlayManager: OverlayManager,
  view: SurgicalContextViewProvider,
  revealView: RevealSurgicalContextView,
  args: unknown[],
): Promise<void> {
  const target = await resolveSymbolCommandTarget(overlayManager, args);
  await applySymbolCommandTarget(target);
  await revealView();
  pushTargetState(target, view);
  view.showChat();
}

async function showImpactForCommandTarget(
  overlayManager: OverlayManager,
  view: SurgicalContextViewProvider,
  revealView: RevealSurgicalContextView,
  args: unknown[],
): Promise<void> {
  const target = await resolveSymbolCommandTarget(overlayManager, args);
  await applySymbolCommandTarget(target);
  await revealView();
  pushTargetState(target, view);
  await view.showImpact(target.symbol, 3, target.filePath);
}

function registerChatCommand(
  command: string,
  overlayManager: OverlayManager,
  view: SurgicalContextViewProvider,
  revealView: RevealSurgicalContextView,
): vscode.Disposable {
  return vscode.commands.registerCommand(command, async (...args: unknown[]) => {
    await showChatForCommandTarget(overlayManager, view, revealView, args);
  });
}

export function activate(context: vscode.ExtensionContext): void {
  // Create persistent status bar item
  const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBarItem.command = 'surgicalContext.checkHealth';
  statusBarItem.text = '$(loading~spin) Surgical Context';
  statusBarItem.tooltip = 'Click to check context_engine health';
  statusBarItem.show();
  const disposables: vscode.Disposable[] = [statusBarItem];

  // Verify context_engine is reachable and update state (non-blocking)
  SidecarClient.health().then(ok => {
    stateManager.setState({
      context_engineHealth: ok ? 'up' : 'down',
    });
    statusBarItem.text = ok ? '$(check) Surgical Context' : '$(warning) Surgical Context';
    statusBarItem.backgroundColor = ok ? undefined : new vscode.ThemeColor('statusBarItem.warningBackground');
    vscode.window.setStatusBarMessage(
      ok ? '✓ Surgical Context context_engine is ready' : '⚠ Surgical Context context_engine is unreachable',
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

  const codeLensProvider = new SurgicalContextCodeLensProvider();
  const hoverProvider = new SurgicalContextHoverProvider(overlayManager);

  disposables.push(
    vscode.workspace.onDidChangeTextDocument(e => overlayManager.onDocumentChanged(e)),
    vscode.workspace.onDidSaveTextDocument(doc => overlayManager.onDocumentSaved(doc)),
    vscode.workspace.onDidCloseTextDocument(doc => overlayManager.onDocumentClosed(doc)),
    vscode.window.onDidChangeActiveTextEditor(() => {
      stateManager.clearLastRequestIfStale();
    }),
    vscode.window.registerWebviewViewProvider(
      SurgicalContextViewProvider.viewType,
      surgicalContextView,
      { webviewOptions: { retainContextWhenHidden: true } }
    ),
    vscode.languages.registerCodeLensProvider({ scheme: 'file' }, codeLensProvider),
    vscode.languages.registerHoverProvider({ scheme: 'file' }, hoverProvider),

    // Spec commands
    registerChatCommand(
      'surgicalContext.askCurrentSymbol',
      overlayManager,
      surgicalContextView,
      revealSurgicalContextView,
    ),

    registerChatCommand(
      'surgicalContext.askSelection',
      overlayManager,
      surgicalContextView,
      revealSurgicalContextView,
    ),

    vscode.commands.registerCommand('surgicalContext.openInspector', async () => {
      await revealSurgicalContextView();
      surgicalContextView.showInspector();
    }),

    vscode.commands.registerCommand('surgicalContext.showImpact', async (...args: unknown[]) => {
      await showImpactForCommandTarget(
        overlayManager,
        surgicalContextView,
        revealSurgicalContextView,
        args,
      );
    }),

    vscode.commands.registerCommand('surgicalContext.findDocs', async () => {
      await runUnifiedSearch({ title: 'Search documentation', docOnly: true });
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
      const next = !current;
      config.update('overlaySync', next, vscode.ConfigurationTarget.Workspace);
      vscode.window.showInformationMessage(`Overlay sync ${next ? 'enabled' : 'disabled'}.`);
    }),

    vscode.commands.registerCommand('surgicalContext.searchWorkspace', async () => {
      await runUnifiedSearch({ title: 'Search workspace symbols and docs' });
    }),

    // Legacy commands for backward compatibility
    registerChatCommand(
      'surgicalContext.openChat',
      overlayManager,
      surgicalContextView,
      revealSurgicalContextView,
    ),

    registerChatCommand(
      'surgicalContext.askAboutCursor',
      overlayManager,
      surgicalContextView,
      revealSurgicalContextView,
    ),

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
  context.subscriptions.push(...disposables);

  void promptForSecondarySideBarPlacement(context, openSecondarySideBarMovePicker);
}

export function deactivate(): void {
  // cleanup handled by context.subscriptions
}

async function resolveSymbolCommandTarget(
  overlayManager: OverlayManager,
  args: unknown[]
): Promise<SymbolCommandTarget> {
  const explicit = explicitTargetFromArgs(args, overlayManager);
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

function explicitTargetFromArgs(
  args: unknown[],
  overlayManager: OverlayManager,
): SymbolCommandTarget | null {
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

  if (first instanceof vscode.Position && isTextDocument(second)) {
    const document = second;
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

function isTextDocument(value: unknown): value is vscode.TextDocument {
  return Boolean(
    value
    && typeof value === 'object'
    && typeof (value as vscode.TextDocument).fileName === 'string'
    && typeof (value as vscode.TextDocument).lineAt === 'function'
  );
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
  if (editor?.document.fileName !== filePath) return {};

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

function pushTargetState(target: SymbolCommandTarget, view?: SurgicalContextViewProvider): void {
  const editor = vscode.window.activeTextEditor;
  stateManager.setState({
    selectedSymbol: target.symbol || undefined,
    activeFile: target.filePath || editor?.document.fileName,
    isDirty: editor?.document.isDirty ?? false,
  });
  view?.pushWorkspaceTarget(target);
}

function clampLine(line: number, document: vscode.TextDocument): number {
  return Math.max(0, Math.min(line, document.lineCount - 1));
}

async function runUnifiedSearch(options: { title: string; docOnly?: boolean }): Promise<void> {
  const query = await vscode.window.showInputBox({ prompt: options.title });
  if (!query?.trim()) {
    return;
  }

  try {
    const response = await SidecarClient.unifiedSearch(query.trim(), undefined, 15);
    const results = options.docOnly
      ? response.results.filter(result => result.type === 'doc')
      : response.results;
    if (results.length === 0) {
      vscode.window.showInformationMessage('No matches found.');
      return;
    }

    const pick = await vscode.window.showQuickPick(
      results.map(result => ({
        label: result.title || result.file_path,
        description: result.file_path,
        detail: result.content.slice(0, 160),
        result,
      })),
      { placeHolder: options.title, matchOnDescription: true, matchOnDetail: true }
    );
    if (!pick?.result.file_path) {
      return;
    }

    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(pick.result.file_path));
    await vscode.window.showTextDocument(document, { preview: true });
  } catch (error) {
    vscode.window.showErrorMessage(
      `Search failed: ${error instanceof Error ? error.message : String(error)}`
    );
  }
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
