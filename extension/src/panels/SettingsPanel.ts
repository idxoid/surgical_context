import * as vscode from 'vscode';
import { getWebviewContent } from '../utils';
import { WebviewToHostMessage, HostToWebviewMessage, SettingsData } from '../webview/shared/protocol';
import { SidecarClient } from '../sidecarClient';

export class SettingsPanel {
  public static currentPanel: SettingsPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private readonly extensionUri: vscode.Uri;
  private disposables: vscode.Disposable[] = [];

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri) {
    this.panel = panel;
    this.extensionUri = extensionUri;

    this.panel.webview.html = getWebviewContent(
      this.panel.webview,
      extensionUri,
      'settings.js',
      'styles.css'
    );

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);

    this.panel.webview.onDidReceiveMessage(
      (message: WebviewToHostMessage) => this.handleWebviewMessage(message),
      undefined,
      this.disposables
    );

    // Trigger initial load
    this.loadInitialSettings();
  }

  private loadInitialSettings(): void {
    // Small delay to ensure webview is ready
    setTimeout(() => {
      this.panel.webview.postMessage({ type: 'settings.loaded' } as any);
    }, 100);
  }

  public static createOrReveal(extensionUri: vscode.Uri): void {
    const column = vscode.ViewColumn.One;

    if (SettingsPanel.currentPanel) {
      SettingsPanel.currentPanel.panel.reveal(column);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      'surgicalContextSettings',
      'Surgical Context Settings',
      column,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      }
    );

    SettingsPanel.currentPanel = new SettingsPanel(panel, extensionUri);
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    const config = vscode.workspace.getConfiguration('surgicalContext');

    switch (message.type) {
      case 'settings.loaded':
        // Send current settings to webview
        const settings: SettingsData = {
          backendUrl: config.get('backendUrl') ?? 'http://localhost:8000',
          workspaceId: config.get('workspaceId') ?? 'local/default@main',
          modelPreference: config.get('modelPreference') ?? 'auto',
          authToken: config.get('authToken') ?? '',
          overlaySync: config.get('overlaySync') ?? true,
          autoOpenInspector: config.get('chat.autoOpenInspector') ?? false,
        };

        this.postMessage({
          type: 'settings.loaded',
          settings,
        });
        break;

      case 'settings.update':
        try {
          await config.update(
            message.key,
            message.value,
            vscode.ConfigurationTarget.Workspace
          );
          this.postMessage({
            type: 'settings.saved',
            message: `Setting updated: ${message.key}`,
          });
        } catch (err) {
          this.postMessage({
            type: 'settings.saveFailed',
            error: `Failed to save setting: ${err instanceof Error ? err.message : String(err)}`,
          });
        }
        break;

      case 'settings.testUrl':
        try {
          const ok = await SidecarClient.health();
          this.postMessage({
            type: 'settings.testUrlComplete',
            success: ok,
            message: ok ? '✓ Connection successful' : '✗ Could not connect to sidecar',
          });
        } catch (err) {
          this.postMessage({
            type: 'settings.testUrlComplete',
            success: false,
            message: `✗ Connection failed: ${err instanceof Error ? err.message : String(err)}`,
          });
        }
        break;

      case 'settings.openKeybindings':
        vscode.commands.executeCommand('workbench.action.openGlobalKeybindings');
        break;
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.panel.webview.postMessage(message);
  }

  private dispose(): void {
    SettingsPanel.currentPanel = undefined;

    this.panel.dispose();

    while (this.disposables.length) {
      const x = this.disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }
}
