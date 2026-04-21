import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { OverlayManager } from '../overlayManager';
import { SSECallbacks, getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  ChatSurfaceState,
} from '../webview/shared/protocol';
import { PromptContextPayload } from '../sidecarClient';

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'surgicalContext.chat';

  private webviewView: vscode.WebviewView | undefined;
  private currentAbortController: AbortController | null = null;

  constructor(
    private extensionUri: vscode.Uri,
    private overlayManager: OverlayManager
  ) {}

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
      'chat.js',
      'styles.css'
    );

    this.initializeSurfaceState();

    webviewView.webview.onDidReceiveMessage((message: WebviewToHostMessage) => {
      this.handleWebviewMessage(message);
    });

    stateManager.subscribe(() => {
      this.pushStateToWebview();
    });

    vscode.window.onDidChangeActiveTextEditor(() => {
      this.pushWorkspaceState();
    });

    vscode.workspace.onDidChangeTextDocument(() => {
      this.pushWorkspaceState();
    });

    vscode.workspace.onDidSaveTextDocument(() => {
      this.pushWorkspaceState();
    });

    this.pushStateToWebview();
  }

  private initializeSurfaceState(): void {
    const state = stateManager.getState();
    const surfaceState: ChatSurfaceState = {
      expandedAccordions: {
        environment: false,
        contextSummary: false,
        advancedInfo: false,
      },
      composerDraft: '',
      workspace: {
        activeFile: state.activeFile || null,
        selectedSymbol: state.selectedSymbol || null,
        isDirty: state.isDirty,
      },
      backend: {
        sidecarHealth: state.sidecarHealth,
        cloudStatus: state.cloudStatus,
      },
    };

    this.postMessage({
      type: 'surface.init',
      state: surfaceState,
    });
  }

  private pushStateToWebview(): void {
    this.pushWorkspaceState();
  }

  private pushWorkspaceState(): void {
    const editor = vscode.window.activeTextEditor;
    const symbol = editor ? this.overlayManager.getSymbolAtCursor(editor) : null;
    const isDirty = editor?.document.isDirty ?? false;
    const activeFile = editor?.document.fileName ?? null;

    this.postMessage({
      type: 'workspace.updated',
      activeFile,
      symbol: symbol || null,
      isDirty,
    });

    const state = stateManager.getState();
    this.postMessage({
      type: 'backend.updated',
      sidecarHealth: state.sidecarHealth,
      cloudStatus: state.cloudStatus,
    });
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    switch (message.type) {
      case 'chat.ask':
        await this.handleAsk(message.prompt, message.symbol);
        break;

      case 'chat.stop':
        if (this.currentAbortController) {
          this.currentAbortController.abort();
          this.currentAbortController = null;
          this.postMessage({
            type: 'chat.requestStopped',
            requestId: message.requestId,
          });
        }
        break;

      case 'action.openInspector':
        vscode.commands.executeCommand('surgicalContext.openInspector');
        break;

      case 'action.showImpact':
        vscode.commands.executeCommand('surgicalContext.showImpact', message.symbol);
        break;

      case 'feedback.submit':
        SidecarClient.submitFeedback({
          message_id: message.messageId,
          rating: message.rating,
        });
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

  private async handleAsk(prompt: string, symbol?: string): Promise<void> {
    if (!this.webviewView) return;

    let targetSymbol = symbol;
    if (!targetSymbol) {
      const editor = vscode.window.activeTextEditor;
      if (editor) {
        const sym = this.overlayManager.getSymbolAtCursor(editor);
        targetSymbol = sym || undefined;
      }
    }

    if (!targetSymbol) {
      this.postMessage({
        type: 'chat.requestFailed',
        requestId: `req-${Date.now()}`,
        error: 'No symbol selected. Please position your cursor on a code symbol.',
      });
      return;
    }

    const requestId = `req-${Date.now()}`;

    this.postMessage({
      type: 'chat.requestStarted',
      requestId,
      symbol: targetSymbol,
    });

    const callbacks: SSECallbacks = {
      onChunk: (chunk: string) => {
        this.postMessage({
          type: 'chat.streamChunk',
          requestId,
          chunk,
        });
      },
      onContext: (context: unknown) => {
        const payload = context as PromptContextPayload;
        stateManager.setState({ lastContext: payload });

        const metadata = payload.metadata;
        const tierTokens = metadata.tier_tokens || {};
        const totalTokens = Object.values(tierTokens).reduce((sum, val) => sum + (val as number), 0);

        this.postMessage({
          type: 'chat.contextSummary',
          summary: {
            primaryLabel: `${payload.primary_source.symbol} in ${payload.primary_source.file_path}`,
            graphCount: payload.graph_context.length,
            docsCount: payload.documentation.length,
            tokenText: `${totalTokens} tokens`,
            chips: metadata.tiers_used || [],
          },
        });
      },
      onDone: (traceId: string) => {
        const context = stateManager.getState().lastContext;
        if (context) {
          this.postMessage({
            type: 'chat.requestCompleted',
            requestId,
            answer: '✓ Response complete',
            context,
          });
        }
      },
      onError: (error: string) => {
        this.postMessage({
          type: 'chat.requestFailed',
          requestId,
          error,
        });
      },
    };

    try {
      this.currentAbortController = await SidecarClient.askStream(
        targetSymbol,
        prompt,
        callbacks
      );
    } catch (error) {
      this.postMessage({
        type: 'chat.requestFailed',
        requestId,
        error: error instanceof Error ? error.message : 'Unknown error',
      });
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.webviewView?.webview.postMessage(message);
  }
}
