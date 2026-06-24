import * as vscode from 'vscode';
import { SidecarClient } from '../context_engineClient';
import { OverlayManager } from '../overlayManager';
import { SSECallbacks, getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import { InspectorPanel } from './InspectorPanel';
import { buildContextSummary } from '../contextSummary';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  ChatSurfaceState,
} from '../webview/shared/protocol';
import { PromptContextPayload } from '../context_engineClient';

export class ChatPanel {
  public static readonly viewType = 'surgicalContext.chat';
  private static instance: ChatPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly extensionUri: vscode.Uri;
  private readonly overlayManager: OverlayManager;
  private disposables: vscode.Disposable[] = [];
  private currentAbortController: AbortController | null = null;

  private constructor(extensionUri: vscode.Uri, overlayManager: OverlayManager) {
    this.extensionUri = extensionUri;
    this.overlayManager = overlayManager;

    this.panel = vscode.window.createWebviewPanel(
      ChatPanel.viewType,
      'Surgical Context: Chat',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      }
    );

    this.panel.webview.html = getWebviewContent(
      this.panel.webview,
      extensionUri,
      'chat.js',
      'styles.css'
    );

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (message: WebviewToHostMessage) => this.handleWebviewMessage(message),
      null,
      this.disposables
    );

    // Listen to state changes
    stateManager.subscribe(() => {
      this.pushStateToWebview();
    });

    // Listen to editor changes
    vscode.window.onDidChangeActiveTextEditor(() => {
      this.pushWorkspaceState();
    });

    vscode.workspace.onDidChangeTextDocument(() => {
      this.pushWorkspaceState();
    });

    vscode.workspace.onDidSaveTextDocument(() => {
      this.pushWorkspaceState();
    });

    // Initial state push
    this.initializeSurfaceState();
  }

  public static createOrReveal(extensionUri: vscode.Uri, overlayManager: OverlayManager): void {
    if (ChatPanel.instance) {
      ChatPanel.instance.panel.reveal(vscode.ViewColumn.Beside);
      return;
    }

    ChatPanel.instance = new ChatPanel(extensionUri, overlayManager);
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
        context_engineHealth: state.context_engineHealth,
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
      context_engineHealth: state.context_engineHealth,
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
        InspectorPanel.createOrReveal(this.extensionUri);
        break;

      case 'action.showImpact':
        vscode.commands.executeCommand('surgicalContext.showImpact', message.symbol);
        break;

      case 'feedback.submit':
        SidecarClient.submitFeedback({
          message_id: message.messageId,
          feedback_token: message.feedbackToken,
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
    if (!this.panel) return;

    // Resolve symbol from current editor if not provided
    let targetSymbol = symbol;
    if (!targetSymbol) {
      const editor = vscode.window.activeTextEditor;
      if (editor) {
        const sym = this.overlayManager.getSymbolAtCursor(editor);
        targetSymbol = sym || undefined;
      }
    }

    const activeFile = vscode.window.activeTextEditor?.document.fileName;

    const requestId = `req-${Date.now()}`;

    // Post request started
    this.postMessage({
      type: 'chat.requestStarted',
      requestId,
      symbol: targetSymbol || activeFile || 'workspace',
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

        // Auto-open inspector if setting enabled
        const config = vscode.workspace.getConfiguration('surgicalContext');
        if (config.get('chat.autoOpenInspector')) {
          InspectorPanel.createOrReveal(this.extensionUri);
        }

        this.postMessage({
          type: 'chat.contextSummary',
          summary: buildContextSummary(payload),
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

    let stream: ReturnType<typeof SidecarClient.askStream> | null = null;
    try {
      stream = SidecarClient.askStream(
        targetSymbol,
        prompt,
        callbacks,
        undefined,
        activeFile
      );
      this.currentAbortController = stream.controller;
      await stream.done;
    } catch (error) {
      this.postMessage({
        type: 'chat.requestFailed',
        requestId,
        error: error instanceof Error ? error.message : 'Unknown error',
      });
    } finally {
      if (stream && this.currentAbortController === stream.controller) {
        this.currentAbortController = null;
      }
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.panel.webview.postMessage(message);
  }

  private dispose(): void {
    ChatPanel.instance = undefined;

    this.panel.dispose();

    while (this.disposables.length) {
      const x = this.disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }
}
