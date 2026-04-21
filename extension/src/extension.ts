import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';
import { ChatPanel } from './chatPanel';
import { OverlayManager } from './overlayManager';
import { ImpactViewProvider } from './providers/ImpactViewProvider';
import { SurgicalContextCodeLensProvider } from './providers/CodeLensProvider';
import { SurgicalContextHoverProvider } from './providers/HoverProvider';
import { ChatPanel } from './panels/ChatPanel';
import { InspectorPanel } from './panels/InspectorPanel';
import { DashboardPanel } from './panels/DashboardPanel';
import { SettingsPanel } from './panels/SettingsPanel';
import { stateManager } from './state/ExtensionState';

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
    const cloudStatus = status.using_fallback ? 'fallback-local' : status.using_aura ? 'connected' : 'offline';
    stateManager.setState({ cloudStatus });
  }).catch(err => {
    console.warn('Failed to fetch cloud status:', err);
  });

  // Instantiate managers
  const overlayManager = new OverlayManager();

  // Register document lifecycle subscriptions
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument(e => overlayManager.onDocumentChanged(e)),
    vscode.workspace.onDidSaveTextDocument(doc => overlayManager.onDocumentSaved(doc)),
    vscode.workspace.onDidCloseTextDocument(doc => overlayManager.onDocumentClosed(doc))
  );

  // Register Impact sidebar view provider
  const impactViewProvider = new ImpactViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ImpactViewProvider.viewType, impactViewProvider)
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
    vscode.commands.registerCommand('surgicalContext.askCurrentSymbol', () => {
      ChatPanel.createOrReveal(context.extensionUri, overlayManager);
    }),

    vscode.commands.registerCommand('surgicalContext.askSelection', () => {
      ChatPanel.createOrReveal(context.extensionUri, overlayManager);
    }),

    vscode.commands.registerCommand('surgicalContext.openInspector', async () => {
      InspectorPanel.createOrReveal(context.extensionUri);
    }),

    vscode.commands.registerCommand('surgicalContext.showImpact', async (symbol?: string) => {
      vscode.commands.executeCommand('workbench.view.extension.surgicalContext');
      // TODO: Focus impact view and trigger load
    }),

    vscode.commands.registerCommand('surgicalContext.findDocs', async () => {
      vscode.window.showInformationMessage('Find Docs coming in Phase 3');
      // TODO: Implement doc search
    }),

    vscode.commands.registerCommand('surgicalContext.openDashboard', async () => {
      DashboardPanel.createOrReveal(context.extensionUri);
    }),

    vscode.commands.registerCommand('surgicalContext.openSettings', async () => {
      SettingsPanel.createOrReveal(context.extensionUri);
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
    vscode.commands.registerCommand('surgicalContext.openChat', () => {
      ChatPanel.createOrReveal(context.extensionUri, overlayManager);
    }),

    vscode.commands.registerCommand('surgicalContext.askAboutCursor', () => {
      ChatPanel.createOrReveal(context.extensionUri, overlayManager);
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
}

export function deactivate(): void {
  // cleanup handled by context.subscriptions
}
