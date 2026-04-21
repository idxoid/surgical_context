import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';
import { ChatPanel } from './chatPanel';
import { OverlayManager } from './overlayManager';
import { ChatViewProvider } from './providers/ChatViewProvider';
import { InspectorViewProvider } from './providers/InspectorViewProvider';
import { ImpactViewProvider } from './providers/ImpactViewProvider';
import { DashboardViewProvider } from './providers/DashboardViewProvider';
import { SettingsViewProvider } from './providers/SettingsViewProvider';
import { SurgicalContextCodeLensProvider } from './providers/CodeLensProvider';
import { SurgicalContextHoverProvider } from './providers/HoverProvider';
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

  // Register sidebar view providers
  const chatViewProvider = new ChatViewProvider(context.extensionUri, overlayManager);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chatViewProvider)
  );

  const inspectorViewProvider = new InspectorViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(InspectorViewProvider.viewType, inspectorViewProvider)
  );

  const impactViewProvider = new ImpactViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ImpactViewProvider.viewType, impactViewProvider)
  );

  const dashboardViewProvider = new DashboardViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(DashboardViewProvider.viewType, dashboardViewProvider)
  );

  const settingsViewProvider = new SettingsViewProvider(context.extensionUri);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(SettingsViewProvider.viewType, settingsViewProvider)
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
      vscode.commands.executeCommand('surgicalContext.chat.focus');
    }),

    vscode.commands.registerCommand('surgicalContext.askSelection', () => {
      vscode.commands.executeCommand('surgicalContext.chat.focus');
    }),

    vscode.commands.registerCommand('surgicalContext.openInspector', async () => {
      vscode.commands.executeCommand('surgicalContext.inspector.focus');
    }),

    vscode.commands.registerCommand('surgicalContext.showImpact', async (symbol?: string) => {
      vscode.commands.executeCommand('surgicalContext.impact.focus');
    }),

    vscode.commands.registerCommand('surgicalContext.findDocs', async () => {
      vscode.window.showInformationMessage('Find Docs coming in Phase 3');
      // TODO: Implement doc search
    }),

    vscode.commands.registerCommand('surgicalContext.openDashboard', async () => {
      vscode.commands.executeCommand('surgicalContext.dashboard.focus');
    }),

    vscode.commands.registerCommand('surgicalContext.openSettings', async () => {
      vscode.commands.executeCommand('surgicalContext.settings.focus');
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
      vscode.commands.executeCommand('surgicalContext.chat.focus');
    }),

    vscode.commands.registerCommand('surgicalContext.askAboutCursor', () => {
      vscode.commands.executeCommand('surgicalContext.chat.focus');
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
