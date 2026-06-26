import * as vscode from 'vscode';
import { SidecarClient } from '../context_engineClient';
import { AskHistoryInput, buildAskHistoryRecord } from '../historyRecorder';
import { OverlayManager } from '../overlayManager';
import { SSECallbacks, getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import { readSettings, saveSettings as persistSettings, graphStatusFromCloud, updateSetting } from '../settings';
import { buildContextSummary } from '../contextSummary';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';
import { PromptContextPayload } from '../context_engineClient';
import { isFileNameSymbol } from '../symbolResolution';

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
      this.fetchAndPostIntent(state.lastRequest.question);
    } else {
      this.postMessage({
        type: 'inspector.notAvailable',
        message: 'No context available. Ask about a symbol first.',
      });
    }
  }

  /**
   * Enrich the inspector with a cheap, classify-only intent preview. Posted as a
   * separate message so the inspector renders instantly; the Intent tab fills in
   * when this resolves. Failures are silent — intent is optional enrichment.
   */
  private fetchAndPostIntent(question?: string): void {
    if (!question) return;
    void (async () => {
      try {
        const res = await SidecarClient.intent(question);
        this.postMessage({ type: 'inspector.intentLoaded', intentMatches: res.intent_matches });
      } catch {
        // intent is optional enrichment; ignore
      }
    })();
  }

  public async showImpact(symbol?: string, maxDepth = 3, filePath?: string): Promise<void> {
    // Priority: explicit symbol > lastRequest.symbol > editor cursor > fail
    let targetSymbol = symbol;
    let targetFilePath = filePath;

    if (!targetSymbol) {
      const lastRequest = stateManager.getState().lastRequest;
      if (lastRequest?.symbol) {
        targetSymbol = lastRequest.symbol;
      } else {
        targetSymbol = (await this.currentEditorSymbolAsync()) || undefined;
      }
    }

    if (!targetFilePath) {
      const editor = vscode.window.activeTextEditor;
      targetFilePath = editor?.document.fileName;
    }

    if (!targetSymbol) {
      this.postMessage({ type: 'surface.showImpact' });
      this.postMessage({
        type: 'impact.loadFailed',
        error: 'No symbol selected. Position your cursor on a symbol or ask about it first.',
      });
      return;
    }

    this.postMessage({ type: 'surface.showImpact' });
    await this.loadImpact(targetSymbol, maxDepth, targetFilePath);
  }

  public showSettings(): void {
    this.postMessage({ type: 'surface.showSettings' });
    void this.pushSettings();
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
          context_engineHealth: state.context_engineHealth,
          cloudStatus: state.cloudStatus,
        },
      },
    });
  }

  private pushStateToWebview(): void {
    this.pushWorkspaceState();
  }

  private async resolveAskTarget(
    messageSymbol?: string,
    messageFilePath?: string
  ): Promise<{ symbol: string | undefined; activeFile: string | undefined }> {
    const hostState = stateManager.getState();
    const editor = vscode.window.activeTextEditor;
    const editorFile = editor?.document.fileName;
    const cursorSymbol = editor ? await this.currentEditorSymbolAsync() : null;
    const editorMatches =
      Boolean(editorFile) &&
      (!hostState.activeFile || hostState.activeFile === editorFile);

    const symbol =
      messageSymbol ||
      (editorMatches && cursorSymbol) ||
      hostState.selectedSymbol ||
      cursorSymbol ||
      undefined;
    const activeFile =
      messageFilePath ||
      (editorMatches && editorFile) ||
      hostState.activeFile ||
      editorFile ||
      undefined;

    return { symbol, activeFile };
  }

  public pushWorkspaceTarget(target: { symbol?: string; filePath?: string }): void {
    if (!target.symbol && !target.filePath) {
      return;
    }
    this.postMessage({
      type: 'workspace.updated',
      activeFile: target.filePath || null,
      symbol: target.symbol || null,
      isDirty: vscode.window.activeTextEditor?.document.isDirty ?? false,
    });
  }

  private pushWorkspaceState(): void {
    const editor = vscode.window.activeTextEditor;
    const hostState = stateManager.getState();
    const editorFile = editor?.document.fileName;
    const cursorSymbol = editor ? this.overlayManager.getSymbolAtCursor(editor) : null;
    const editorMatches =
      Boolean(editorFile) &&
      (!hostState.activeFile || hostState.activeFile === editorFile);
    const symbol =
      (editorMatches && cursorSymbol) ||
      hostState.selectedSymbol ||
      cursorSymbol ||
      null;
    const activeFile =
      (editorMatches && editorFile) || hostState.activeFile || editorFile || null;
    const isDirty = editor?.document.isDirty ?? false;

    this.postMessage({
      type: 'workspace.updated',
      activeFile,
      symbol,
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
      case 'surface.ready':
        this.webviewReady = true;
        this.initializeSurfaceState();
        this.flushQueuedMessages();
        this.pushWorkspaceState();
        break;

      case 'chat.ask':
        await this.handleAsk(
          message.prompt,
          message.symbol,
          message.conversationId,
          message.filePath
        );
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
        await this.showImpact(message.symbol, message.maxDepth, message.filePath);
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
        void this.pushSettings();
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

      case 'clipboard.write':
        try {
          await vscode.env.clipboard.writeText(message.text);
          this.postMessage({ type: 'toast.show', level: 'info', message: 'Copied to clipboard.' });
        } catch (error) {
          console.error('Failed to copy text to clipboard:', error);
          this.postMessage({ type: 'toast.show', level: 'error', message: 'Could not copy to clipboard.' });
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

  private async handleAsk(
    prompt: string,
    symbol?: string,
    conversationId?: string,
    filePath?: string
  ): Promise<void> {
    if (!this.webviewView) return;

    const { symbol: targetSymbol, activeFile } = await this.resolveAskTarget(symbol, filePath);

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
      this.fetchAndPostIntent(state.lastRequest?.question);
    } else {
      this.postMessage({
        type: 'inspector.notAvailable',
        message: 'No context available. Ask about a symbol first.',
      });
    }
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
        message: ok ? 'Connection successful' : 'Could not connect to context_engine',
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

  private async currentEditorSymbolAsync(): Promise<string | null> {
    const editor = vscode.window.activeTextEditor;
    return editor ? this.overlayManager.getSymbolAtCursorAsync(editor) : null;
  }

  private async loadImpact(symbol: string, maxDepth = 3, filePath?: string): Promise<void> {
    if (isFileNameSymbol(symbol, filePath)) {
      const message = 'No code symbol selected. Position the cursor inside a method or function.';
      this.postMessage({ type: 'impact.loadFailed', error: message });
      await vscode.window.showWarningMessage(`Impact analysis: ${message}`);
      return;
    }
    try {
      this.postMessage({ type: 'impact.loading' });
      const impact = await SidecarClient.impact(symbol, maxDepth, filePath);
      this.postMessage({
        type: 'impact.loaded',
        symbol,
        impact,
      });
      if ((impact.affected_count ?? 0) === 0 && (impact.affected_symbols?.length ?? 0) === 0) {
        await vscode.window.showInformationMessage(
          `No downstream dependents found for ${symbol}. Try increasing depth or reindexing the workspace.`
        );
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      this.postMessage({
        type: 'impact.loadFailed',
        error: message,
      });
      await vscode.window.showErrorMessage(`Impact analysis failed: ${message}`);
    }
  }
}
