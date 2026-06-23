import * as vscode from 'vscode';
import { getWebviewContent } from '../utils';
import { WebviewToHostMessage, HostToWebviewMessage } from '../webview/shared/protocol';
import { SidecarClient } from '../sidecarClient';
import { readSettings, saveSettings, updateSetting, graphStatusFromCloud } from '../settings';

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
    void this.pushSettings();
  }

  private async pushSettings(): Promise<void> {
    const settings = readSettings();
    let graphStatus = graphStatusFromCloud(null);
    try {
      graphStatus = graphStatusFromCloud(await SidecarClient.cloudStatus());
    } catch {
      // keep offline status
    }
    this.postMessage({
      type: 'settings.loaded',
      settings: { ...settings, graphStatus },
    });
  }

  private loadInitialSettings(): void {
    void this.pushSettings();
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
    switch (message.type) {
      case 'settings.loaded':
        void this.pushSettings();
        break;

      case 'settings.save':
        try {
          await saveSettings(message.settings);
          this.postMessage({
            type: 'settings.saved',
            message: 'Settings saved.',
          });
        } catch (err) {
          this.postMessage({
            type: 'settings.saveFailed',
            error: `Failed to save settings: ${err instanceof Error ? err.message : String(err)}`,
          });
        }
        break;

      case 'settings.update':
        try {
          await updateSetting(message.key, message.value);
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
          const ok = await SidecarClient.health(message.url, message.authToken || '');
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
