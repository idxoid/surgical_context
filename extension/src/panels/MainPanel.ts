import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { AskHistoryInput, buildAskHistoryRecord } from '../historyRecorder';
import { OverlayManager } from '../overlayManager';
import { SSECallbacks, getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';
import { PromptContextPayload } from '../sidecarClient';

export class MainPanel {
  public static readonly viewType = 'surgicalContext.main';
  private static instance: MainPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly extensionUri: vscode.Uri;
  private readonly overlayManager: OverlayManager;
  private disposables: vscode.Disposable[] = [];
  private currentAbortController: AbortController | null = null;
  private onDisposeCallback: (() => void) | null = null;

  private constructor(extensionUri: vscode.Uri, overlayManager: OverlayManager) {
    this.extensionUri = extensionUri;
    this.overlayManager = overlayManager;

    this.panel = vscode.window.createWebviewPanel(
      MainPanel.viewType,
      'Surgical Context',
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      }
    );

    this.panel.webview.html = getWebviewContent(
      this.panel.webview,
      extensionUri,
      'main.js',
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

    this.initializeSurfaceState();
  }

  public static createOrReveal(extensionUri: vscode.Uri, overlayManager: OverlayManager): void {
    if (MainPanel.instance) {
      MainPanel.instance.panel.reveal(vscode.ViewColumn.Beside);
      return;
    }

    MainPanel.instance = new MainPanel(extensionUri, overlayManager);
  }

  public onDispose(callback: () => void): void {
    this.onDisposeCallback = callback;
  }

  public reveal(column: vscode.ViewColumn): void {
    this.panel.reveal(column);
  }

  private initializeSurfaceState(): void {
    const state = stateManager.getState();
    this.postMessage({
      type: 'surface.init',
      state: {
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
      },
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
        await this.handleAsk(message.prompt, message.symbol, message.conversationId);
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

      case 'feedback.submit':
        SidecarClient.submitFeedback({
          message_id: message.messageId,
          feedback_token: message.feedbackToken,
          rating: message.rating,
        });
        break;

      case 'action.openDashboard':
        await vscode.commands.executeCommand('surgicalContext.openDashboard');
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

  private async handleAsk(prompt: string, symbol?: string, conversationId?: string): Promise<void> {
    if (!this.panel) return;

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
    const answerParts: string[] = [];
    let streamTraceId = '';
    let latestContext: PromptContextPayload | null = null;

    this.postMessage({
      type: 'chat.requestStarted',
      requestId,
      symbol: targetSymbol || activeFile || 'workspace',
    });

    const callbacks: SSECallbacks = {
      onTrace: (traceId: string) => {
        streamTraceId = traceId;
      },
      onChunk: (chunk: string) => {
        answerParts.push(chunk);
        this.postMessage({
          type: 'chat.streamChunk',
          requestId,
          chunk,
        });
      },
      onContext: (context: unknown) => {
        const payload = context as PromptContextPayload;
        latestContext = payload;
        stateManager.setState({ lastContext: payload });

        const metadata = payload.metadata;
        const tierTokens = metadata.tier_tokens || {};
        const totalTokens = Object.values(tierTokens).reduce((sum, val) => sum + (val as number), 0);
        const askLevel = typeof payload.budget?.ask_level === 'string'
          ? [`level:${payload.budget.ask_level}`]
          : [];

        this.postMessage({
          type: 'chat.contextSummary',
          summary: {
            primaryLabel: `${payload.primary_source.symbol} in ${payload.primary_source.file_path}`,
            graphCount: payload.graph_context.length,
            docsCount: payload.documentation.length,
            tokenText: `${totalTokens} tokens`,
            chips: [...askLevel, ...(metadata.tiers_used || [])],
          },
        });
      },
      onDone: (traceId: string) => {
        const context = latestContext || stateManager.getState().lastContext || null;
        if (context) {
          this.postMessage({
            type: 'chat.requestCompleted',
            requestId,
            answer: '✓ Response complete',
            context,
          });
        }
        void this.persistAskHistory({
          conversationId,
          requestId,
          prompt,
          answer: answerParts.join(''),
          symbol: targetSymbol,
          activeFile,
          traceId: traceId || streamTraceId,
          context,
        });
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
        callbacks,
        undefined,
        activeFile
      );
    } catch (error) {
      this.postMessage({
        type: 'chat.requestFailed',
        requestId,
        error: error instanceof Error ? error.message : 'Unknown error',
      });
    }
  }

  private async persistAskHistory(input: AskHistoryInput): Promise<void> {
    try {
      await SidecarClient.recordAskHistory(buildAskHistoryRecord(input));
    } catch (error) {
      console.warn('Failed to persist ask history:', error);
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.panel.webview.postMessage(message);
  }

  private dispose(): void {
    MainPanel.instance = undefined;
    this.panel.dispose();

    while (this.disposables.length) {
      const x = this.disposables.pop();
      if (x) {
        x.dispose();
      }
    }

    if (this.onDisposeCallback) {
      this.onDisposeCallback();
    }
  }
}
