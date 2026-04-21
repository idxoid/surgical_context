import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';
import { ChatPanel } from './chatPanel';
import { OverlayManager } from './overlayManager';
import { ChatViewProvider } from './providers/ChatViewProvider';
import { InspectorPanel } from './panels/InspectorPanel';
import { stateManager } from './state/ExtensionState';

export function activate(context: vscode.ExtensionContext): void {
  // Verify sidecar is reachable and update state (non-blocking)
  SidecarClient.health().then(ok => {
    stateManager.setState({
      sidecarHealth: ok ? 'up' : 'down',
    });
    vscode.window.setStatusBarMessage(
      ok ? '$(check) Surgical Context: sidecar ready' : '$(warning) Surgical Context: sidecar unreachable',
      5000
    );
  });

  // Poll cloud status
  SidecarClient.cloudStatus().then(status => {
    const cloudStatus = status.using_fallback ? 'fallback-local' : status.using_aura ? 'connected' : 'offline';
    stateManager.setState({ cloudStatus });
  });

  // Instantiate managers
  const overlayManager = new OverlayManager();

  // Register document lifecycle subscriptions
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument(e => overlayManager.onDocumentChanged(e)),
    vscode.workspace.onDidSaveTextDocument(doc => overlayManager.onDocumentSaved(doc)),
    vscode.workspace.onDidCloseTextDocument(doc => overlayManager.onDocumentClosed(doc))
  );

  // Register Chat sidebar view provider
  const chatViewProvider = new ChatViewProvider(context.extensionUri, overlayManager);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chatViewProvider)
  );

  // Register commands
  context.subscriptions.push(

    // Spec commands
    vscode.commands.registerCommand('surgicalContext.askCurrentSymbol', () => {
      // Focus the chat view (which has its own ask logic)
      vscode.commands.executeCommand('workbench.view.extension.surgicalContext');
    }),

    vscode.commands.registerCommand('surgicalContext.askSelection', () => {
      // Similar to askCurrentSymbol but uses selection instead of word
      vscode.commands.executeCommand('workbench.view.extension.surgicalContext');
    }),

    vscode.commands.registerCommand('surgicalContext.openInspector', async () => {
      InspectorPanel.createOrReveal(context.extensionUri);
    }),

    vscode.commands.registerCommand('surgicalContext.showImpact', async (symbol?: string) => {
      vscode.window.showInformationMessage('Impact Explorer coming in Phase 4');
      // TODO: Implement ImpactViewProvider
    }),

    vscode.commands.registerCommand('surgicalContext.findDocs', async () => {
      vscode.window.showInformationMessage('Find Docs coming in Phase 3');
      // TODO: Implement doc search
    }),

    vscode.commands.registerCommand('surgicalContext.openDashboard', async () => {
      vscode.window.showInformationMessage('Dashboard coming in Phase 5');
      // TODO: Implement DashboardPanel
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
      ChatPanel.createOrReveal(context.extensionUri);
    }),

    vscode.commands.registerCommand('surgicalContext.askAboutCursor', () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showErrorMessage('No active editor.');
        return;
      }
      const symbol = overlayManager.getSymbolAtCursor(editor);
      ChatPanel.createOrReveal(context.extensionUri, symbol ?? undefined);
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
