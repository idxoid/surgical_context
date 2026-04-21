import * as vscode from 'vscode';
import { getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';

export class InspectorPanel {
  public static readonly viewType = 'surgicalContext.inspector';
  private static instance: InspectorPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly extensionUri: vscode.Uri;
  private disposables: vscode.Disposable[] = [];

  private constructor(extensionUri: vscode.Uri) {
    this.extensionUri = extensionUri;

    this.panel = vscode.window.createWebviewPanel(
      InspectorPanel.viewType,
      'Surgical Context: Inspector',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      }
    );

    this.panel.webview.html = getWebviewContent(
      this.panel.webview,
      extensionUri,
      'inspector.js',
      'styles.css'
    );

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (message: WebviewToHostMessage) => this.handleWebviewMessage(message),
      null,
      this.disposables
    );

    this.loadContext();
  }

  public static createOrReveal(extensionUri: vscode.Uri): void {
    if (InspectorPanel.instance) {
      InspectorPanel.instance.panel.reveal(vscode.ViewColumn.Beside);
      return;
    }

    InspectorPanel.instance = new InspectorPanel(extensionUri);
  }

  private loadContext(): void {
    const state = stateManager.getState();
    if (state.lastContext) {
      this.postMessage({
        type: 'inspector.loaded',
        context: state.lastContext,
      });
    } else {
      this.postMessage({
        type: 'inspector.loaded',
        context: null,
      });
    }
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    switch (message.type) {
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

  private postMessage(message: HostToWebviewMessage): void {
    this.panel.webview.postMessage(message);
  }

  private dispose(): void {
    InspectorPanel.instance = undefined;
    this.disposables.forEach(d => d.dispose());
  }
}
