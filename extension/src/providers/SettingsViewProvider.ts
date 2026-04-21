import * as vscode from 'vscode';
import { getWebviewContent } from '../utils';
import { WebviewToHostMessage, HostToWebviewMessage, SettingsData } from '../webview/shared/protocol';
import { SidecarClient } from '../sidecarClient';

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
      this.webviewView?.webview.postMessage({ type: 'settings.loaded' } as any);
    }, 100);
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    const config = vscode.workspace.getConfiguration('surgicalContext');

    switch (message.type) {
      case 'settings.loaded':
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
    this.webviewView?.webview.postMessage(message);
  }
}
