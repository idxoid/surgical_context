import * as vscode from 'vscode';
import { getWebviewContent } from '../utils';
import { WebviewToHostMessage, HostToWebviewMessage } from '../webview/shared/protocol';
import { SidecarClient } from '../sidecarClient';
import { readSettings, saveSettings, updateSetting } from '../settings';

export class SettingsViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'surgicalContext.settings';

  private webviewView: vscode.WebviewView | undefined;

  constructor(private extensionUri: vscode.Uri) {}

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.webviewView = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
    };

    webviewView.webview.html = getWebviewContent(
      webviewView.webview,
      this.extensionUri,
      'settings.js',
      'styles.css'
    );

    webviewView.webview.onDidReceiveMessage((message: WebviewToHostMessage) => {
      this.handleWebviewMessage(message);
    });

    webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible) {
        this.loadSettings();
      }
    });

    this.loadSettings();
  }

  private loadSettings(): void {
    setTimeout(() => {
      this.postMessage({ type: 'settings.loaded', settings: readSettings() });
    }, 100);
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    switch (message.type) {
      case 'settings.loaded':
        this.postMessage({
          type: 'settings.loaded',
          settings: readSettings(),
        });
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
    this.webviewView?.webview.postMessage(message);
  }
}
