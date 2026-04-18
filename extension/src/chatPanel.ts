import * as vscode from 'vscode';
import { SidecarClient } from './sidecarClient';

export class ChatPanel {
  private static instance: ChatPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];

  static createOrReveal(extensionUri: vscode.Uri, prefillSymbol?: string): void {
    if (ChatPanel.instance) {
      ChatPanel.instance.panel.reveal();
      if (prefillSymbol) ChatPanel.instance.sendPrefill(prefillSymbol);
      return;
    }
    ChatPanel.instance = new ChatPanel(extensionUri, prefillSymbol);
  }

  private constructor(extensionUri: vscode.Uri, prefillSymbol?: string) {
    this.panel = vscode.window.createWebviewPanel(
      'surgicalContext',
      'Surgical Context',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')]
      }
    );

    this.panel.webview.html = this.buildHtml(extensionUri);

    this.panel.webview.onDidReceiveMessage(
      async (msg) => {
        if (msg.type === 'ask') {
          try {
            const result = await SidecarClient.ask(msg.symbol, msg.question);
            this.panel.webview.postMessage({ type: 'answer', payload: result });
          } catch (err) {
            const errMsg = err instanceof Error ? err.message : 'Unknown error';
            this.panel.webview.postMessage({ type: 'error', message: errMsg });
          }
        }
      },
      undefined,
      this.disposables
    );

    this.panel.onDidDispose(() => {
      ChatPanel.instance = undefined;
      this.disposables.forEach(d => d.dispose());
    }, null, this.disposables);

    if (prefillSymbol) {
      setTimeout(() => this.sendPrefill(prefillSymbol), 300);
    }
  }

  private sendPrefill(symbol: string): void {
    this.panel.webview.postMessage({ type: 'prefill', symbol });
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const scriptUri = this.panel.webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'media', 'webview.js')
    );
    const nonce = this.getNonce();

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline';">
  <title>Surgical Context</title>
  <style>
    body {
      font-family: var(--vscode-font-family);
      padding: 12px;
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      margin: 0;
    }
    #chat-log {
      height: 60vh;
      overflow-y: auto;
      border: 1px solid var(--vscode-panel-border);
      padding: 8px;
      margin-bottom: 10px;
    }
    .entry {
      margin-bottom: 12px;
    }
    .q {
      font-weight: bold;
      margin-bottom: 4px;
      color: var(--vscode-textLink-foreground);
    }
    .a {
      white-space: pre-wrap;
      font-size: 0.9em;
    }
    .ctx {
      font-size: 0.8em;
      opacity: 0.7;
      margin-top: 4px;
    }
    .error {
      color: var(--vscode-inputValidation-errorBorder);
      font-size: 0.9em;
      margin: 8px 0;
    }
    input, textarea, button {
      width: 100%;
      box-sizing: border-box;
      margin-bottom: 6px;
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border);
      padding: 6px;
      font-family: var(--vscode-font-family);
    }
    button {
      cursor: pointer;
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      padding: 8px;
    }
    button:hover {
      background: var(--vscode-button-hoverBackground);
    }
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
  </style>
</head>
<body>
  <div id="chat-log"></div>
  <input id="symbol-input" type="text" placeholder="Symbol (e.g. MyClass, parse_token)" />
  <textarea id="question-input" rows="3" placeholder="Ask a question about this symbol…"></textarea>
  <button id="submit-btn">Ask</button>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }

  private getNonce(): string {
    let text = '';
    const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (let i = 0; i < 32; i++) {
      text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
  }
}
