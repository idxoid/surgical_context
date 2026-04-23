import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';

export class ImpactViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'surgicalContext.impact';

  private webviewView: vscode.WebviewView | undefined;

  constructor(private extensionUri: vscode.Uri) {}

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.webviewView = webviewView;

    // Configure webview
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
    };

    // Load HTML
    webviewView.webview.html = getWebviewContent(
      webviewView.webview,
      this.extensionUri,
      'impact.js',
      'styles.css'
    );

    // Register message handlers
    webviewView.webview.onDidReceiveMessage((message: WebviewToHostMessage) => {
      this.handleWebviewMessage(message);
    });

    // Listen to state changes
    stateManager.subscribe(() => {
      this.pushStateToWebview();
    });

    // Initial state push
    this.pushStateToWebview();
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    switch (message.type) {
      case 'action.showImpact':
        if (message.symbol) {
          await this.loadImpact(message.symbol);
        }
        break;

      case 'link.openFile':
        if (message.filePath) {
          const uri = vscode.Uri.file(message.filePath);
          const opts: vscode.TextDocumentShowOptions = { preview: true };
          if (message.line) {
            opts.selection = new vscode.Range(
              new vscode.Position(message.line - 1, 0),
              new vscode.Position(message.line - 1, 0)
            );
          }
          vscode.window.showTextDocument(uri, opts);
        }
        break;
    }
  }

  private async loadImpact(symbol: string): Promise<void> {
    try {
      this.postMessage({
        type: 'impact.loading',
      });

      const impact = await SidecarClient.impact(symbol);
      this.postMessage({
        type: 'impact.loaded',
        symbol,
        impact,
      });
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : 'Unknown error';
      this.postMessage({
        type: 'impact.loadFailed',
        error: errMsg,
      });
    }
  }

  private pushStateToWebview(): void {
    const state = stateManager.getState();
    this.postMessage({
      type: 'workspace.updated',
      activeFile: state.activeFile || null,
      symbol: state.selectedSymbol || null,
      isDirty: state.isDirty,
    });
  }

  private postMessage(message: HostToWebviewMessage): void {
    if (this.webviewView) {
      this.webviewView.webview.postMessage(message);
    }
  }
}
