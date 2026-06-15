import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { AskHistoryInput, buildAskHistoryRecord } from '../historyRecorder';
import { OverlayManager } from '../overlayManager';
import { SSECallbacks, getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import { readSettings, saveSettings as persistSettings, updateSetting } from '../settings';
import { buildContextSummary } from '../contextSummary';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';
import { PromptContextPayload } from '../sidecarClient';

export class SurgicalContextViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'surgicalContext.main';

  private webviewView: vscode.WebviewView | undefined;
  private disposables: vscode.Disposable[] = [];
  private currentAbortController: AbortController | null = null;
  private webviewReady = false;
  private queuedMessages: HostToWebviewMessage[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly overlayManager: OverlayManager
  ) {}

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.webviewView = webviewView;
    this.webviewReady = false;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
    };

    webviewView.webview.onDidReceiveMessage(
      (message: WebviewToHostMessage) => this.handleWebviewMessage(message),
      null,
      this.disposables
    );

    webviewView.webview.html = getWebviewContent(
      webviewView.webview,
      this.extensionUri,
      'main.js',
      'styles.css'
    );

    const unsubscribeState = stateManager.subscribe(() => this.pushStateToWebview());
    this.disposables.push(new vscode.Disposable(unsubscribeState));

    // Listen to editor changes
    this.disposables.push(
      vscode.window.onDidChangeActiveTextEditor(() => this.pushWorkspaceState()),
      vscode.window.onDidChangeTextEditorSelection(() => this.pushWorkspaceState()),
      vscode.workspace.onDidChangeTextDocument(() => this.pushWorkspaceState()),
      vscode.workspace.onDidSaveTextDocument(() => this.pushWorkspaceState())
    );
  }

  public showChat(): void {
    this.postMessage({ type: 'surface.showChat' });
    this.pushWorkspaceState();
  }

  public showInspector(): void {
    this.postMessage({ type: 'surface.showInspector' });

    // Read from shared lastRequest state (stored by Ask)
    const state = stateManager.getState();
    if (state.lastRequest?.context) {
      this.postMessage({
        type: 'inspector.loaded',
        context: state.lastRequest.context,
        symbol: state.lastRequest.symbol,
        question: state.lastRequest.question,
      });
    } else {
      this.postMessage({
        type: 'inspector.notAvailable',
        message: 'No context available. Ask about a symbol first.',
      });
    }
  }

  public async showImpact(symbol?: string): Promise<void> {
    // Priority: explicit symbol > lastRequest.symbol > editor cursor > fail
    let targetSymbol = symbol;

    if (!targetSymbol) {
      const lastRequest = stateManager.getState().lastRequest;
      if (lastRequest?.symbol) {
        targetSymbol = lastRequest.symbol;
      } else {
        targetSymbol = this.currentEditorSymbol() || undefined;
      }
    }

    if (!targetSymbol) {
      this.postMessage({
        type: 'impact.loadFailed',
        error: 'No symbol selected. Position your cursor on a symbol or ask about it first.',
      });
      return;
    }

    await this.loadImpact(targetSymbol);
  }

  public showSettings(): void {
    this.postMessage({ type: 'surface.showSettings' });
    this.pushSettings();
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
        lastContext: state.lastContext || null,
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
      case 'surface.ready':
        this.webviewReady = true;
        this.initializeSurfaceState();
        this.flushQueuedMessages();
        this.pushWorkspaceState();
        break;

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

      case 'request.selected':
        stateManager.setState({
          lastContext: message.context,
          lastRequest: {
            requestId: message.requestId,
            symbol: message.symbol,
            question: message.question,
            timestamp: Date.now(),
            context: message.context,
            answer: message.answer || '',
          },
        });
        break;

      case 'action.openInspector':
        this.showInspector();
        break;

      case 'action.openSettings':
        this.showSettings();
        break;

      case 'action.showImpact':
        await this.showImpact(message.symbol);
        break;

      case 'action.openChat':
        this.showChat();
        break;

      case 'action.openDashboard':
        await vscode.commands.executeCommand('surgicalContext.openDashboard');
        break;

      case 'feedback.submit':
        SidecarClient.submitFeedback({
          message_id: message.messageId,
          feedback_token: message.feedbackToken,
          rating: message.rating,
        });
        break;

      case 'settings.loaded':
        this.pushSettings();
        break;

      case 'settings.save':
        await this.saveSettings(message.settings);
        break;

      case 'settings.update':
        await this.updateSetting(message.key, message.value);
        break;

      case 'settings.testUrl':
        await this.testSettingsUrl(message.url, message.authToken || '');
        break;

      case 'settings.openKeybindings':
        vscode.commands.executeCommand('workbench.action.openGlobalKeybindings');
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

      case 'impact.openFiles':
        await this.openImpactFiles(message.filePaths);
        break;
    }
  }

  private async openImpactFiles(filePaths: string[]): Promise<void> {
    const uniquePaths = Array.from(new Set(filePaths.filter(Boolean))).slice(0, 12);
    for (const filePath of uniquePaths) {
      const document = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
      await vscode.window.showTextDocument(document, {
        preview: false,
        preserveFocus: true,
      });
    }
  }

  private async handleAsk(prompt: string, symbol?: string, conversationId?: string): Promise<void> {
    if (!this.webviewView) return;

    let targetSymbol = symbol || this.currentEditorSymbol() || undefined;
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

        this.postMessage({
          type: 'chat.contextSummary',
          summary: buildContextSummary(payload),
        });
      },
      onDone: (traceId: string) => {
        const answer = answerParts.join('');
        const context = latestContext || stateManager.getState().lastContext || null;

        // Store full request in shared state for Inspector/Impact to read
        if (context) {
          stateManager.setState({
            lastRequest: {
              requestId,
              symbol: targetSymbol,
              question: prompt,
              timestamp: Date.now(),
              context,
              answer,
            },
          });

          this.postMessage({
            type: 'chat.requestCompleted',
            requestId,
            answer: '',
            context,
          });
        }

        void this.persistAskHistory({
          conversationId,
          requestId,
          prompt,
          answer,
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

  private async persistAskHistory(input: AskHistoryInput): Promise<void> {
    try {
      await SidecarClient.recordAskHistory(buildAskHistoryRecord(input));
    } catch (error) {
      console.warn('Failed to persist ask history:', error);
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    if (!this.webviewView || !this.webviewReady) {
      this.queuedMessages.push(message);
      return;
    }

    this.webviewView.webview.postMessage(message);
  }

  private flushQueuedMessages(): void {
    if (!this.webviewView || !this.webviewReady || this.queuedMessages.length === 0) {
      return;
    }

    const queuedMessages = this.queuedMessages.splice(0);
    for (const message of queuedMessages) {
      this.webviewView.webview.postMessage(message);
    }
  }

  private pushInspectorContext(): void {
    // Prefer lastRequest.context (from Ask), fall back to lastContext for backward compat
    const state = stateManager.getState();
    const context = state.lastRequest?.context || state.lastContext || null;

    if (context) {
      this.postMessage({
        type: 'inspector.loaded',
        context,
        symbol: state.lastRequest?.symbol,
        question: state.lastRequest?.question,
      });
    } else {
      this.postMessage({
        type: 'inspector.notAvailable',
        message: 'No context available. Ask about a symbol first.',
      });
    }
  }

  private pushSettings(): void {
    this.postMessage({
      type: 'settings.loaded',
      settings: readSettings(),
    });
  }

  private async saveSettings(settings: ReturnType<typeof readSettings>): Promise<void> {
    try {
      await persistSettings(settings);
      this.postMessage({
        type: 'settings.saved',
        message: 'Settings saved.',
      });
    } catch (error) {
      this.postMessage({
        type: 'settings.saveFailed',
        error: `Failed to save settings: ${error instanceof Error ? error.message : String(error)}`,
      });
    }
  }

  private async updateSetting(key: string, value: unknown): Promise<void> {
    try {
      await updateSetting(key, value);
      this.postMessage({
        type: 'settings.saved',
        message: `Setting updated: ${key}`,
      });
    } catch (error) {
      this.postMessage({
        type: 'settings.saveFailed',
        error: `Failed to save setting: ${error instanceof Error ? error.message : String(error)}`,
      });
    }
  }

  private async testSettingsUrl(url: string, authToken: string): Promise<void> {
    try {
      const ok = await SidecarClient.health(url, authToken);
      this.postMessage({
        type: 'settings.testUrlComplete',
        success: ok,
        message: ok ? 'Connection successful' : 'Could not connect to sidecar',
      });
    } catch (error) {
      this.postMessage({
        type: 'settings.testUrlComplete',
        success: false,
        message: `Connection failed: ${error instanceof Error ? error.message : String(error)}`,
      });
    }
  }

  private currentEditorSymbol(): string | null {
    const editor = vscode.window.activeTextEditor;
    return editor ? this.overlayManager.getSymbolAtCursor(editor) : null;
  }

  private async loadImpact(symbol: string): Promise<void> {
    try {
      this.postMessage({ type: 'impact.loading' });
      const impact = await SidecarClient.impact(symbol);
      this.postMessage({
        type: 'impact.loaded',
        symbol,
        impact,
      });
    } catch (error) {
      this.postMessage({
        type: 'impact.loadFailed',
        error: error instanceof Error ? error.message : 'Unknown error',
      });
    }
  }
}
