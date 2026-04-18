import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';
import { ChatPanel } from './chatPanel';
import { OverlayManager } from './overlayManager';

export function activate(context: vscode.ExtensionContext): void {

  // Verify sidecar is reachable (non-blocking)
  SidecarClient.health().then(ok => {
    vscode.window.setStatusBarMessage(
      ok ? '$(check) Surgical Context: sidecar ready' : '$(warning) Surgical Context: sidecar unreachable',
      5000
    );
  });

  // Instantiate managers
  const overlayManager = new OverlayManager();

  // Register document lifecycle subscriptions
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument(e => overlayManager.onDocumentChanged(e)),
    vscode.workspace.onDidSaveTextDocument(doc => overlayManager.onDocumentSaved(doc)),
    vscode.workspace.onDidCloseTextDocument(doc => overlayManager.onDocumentClosed(doc))
  );

  // Register commands
  context.subscriptions.push(

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
