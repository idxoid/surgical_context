import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { OverlayManager } from '../overlayManager';
import { SSECallbacks } from '../utils';
import { getWebviewContent } from '../utils';
import { stateManager } from '../state/ExtensionState';
import { InspectorPanel } from '../panels/InspectorPanel';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  ChatSurfaceState,
  ContextSummaryDto,
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

    // Configure webview
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
    };

    // Load HTML
    webviewView.webview.html = getWebviewContent(
      webviewView.webview,
      this.extensionUri,
      'chat.js',
      'styles.css'
    );

    // Initialize surface state
    this.initializeSurfaceState();

    // Register message handlers
    webviewView.webview.onDidReceiveMessage((message: WebviewToHostMessage) => {
      this.handleWebviewMessage(message);
    });

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

      case 'composer.changed':
        // State is persisted client-side, no action needed
        break;

      case 'accordion.toggled':
        // State is persisted client-side
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

    // Resolve symbol from current editor if not provided
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

    // Send overlay if file is dirty
    const editor = vscode.window.activeTextEditor;
    if (editor && editor.document.isDirty) {
      const config = vscode.workspace.getConfiguration('surgicalContext');
      const overlaySync = config.get<boolean>('overlaySync', true);

      if (overlaySync) {
        try {
          await SidecarClient.overlay(editor.document.fileName, editor.document.getText());
        } catch (err) {
          console.warn('Failed to sync overlay:', err);
          // Continue anyway, the ask will use stale content
        }
      }
    }

    const requestId = `req-${Date.now()}`;

    // Notify webview that request started
    this.postMessage({
      type: 'chat.requestStarted',
      requestId,
      symbol: targetSymbol,
    });

    // Start streaming
    const callbacks: SSECallbacks = {
      onTrace: () => {
        // trace received, no UI action needed
      },
      onChunk: (chunk: string) => {
        this.postMessage({
          type: 'chat.streamChunk',
          requestId,
          chunk,
        });
      },
      onContext: (context: unknown) => {
        const contextPayload = context as PromptContextPayload;
        // Store context in state
        stateManager.setState({ lastContext: contextPayload });
        // Notify webview
        this.postMessage({
          type: 'chat.contextSummary',
          summary: this.buildContextSummary(contextPayload),
        });
        // Auto-open inspector if setting enabled
        const config = vscode.workspace.getConfiguration('surgicalContext');
        if (config.get<boolean>('chat.autoOpenInspector', false)) {
          InspectorPanel.createOrReveal(this.extensionUri);
        }
      },
      onDone: () => {
        // Request completed
        this.currentAbortController = null;
      },
      onError: (error: string) => {
        this.postMessage({
          type: 'chat.requestFailed',
          requestId,
          error,
        });
        this.currentAbortController = null;
      },
    };

    try {
      this.currentAbortController = await SidecarClient.askStream(
        targetSymbol,
        prompt,
        callbacks
      );
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : 'Unknown error';
      this.postMessage({
        type: 'chat.requestFailed',
        requestId,
        error: errMsg,
      });
    }
  }

  private buildContextSummary(context: any): ContextSummaryDto {
    const primaryLabel = context.primary_source?.symbol || 'unknown';
    const graphCount = (context.graph_context || []).length;
    const docsCount = (context.documentation || []).length;

    const tokens =
      (context.metadata?.tokens_primary || 0) +
      (context.metadata?.tokens_graph || 0) +
      (context.metadata?.tokens_docs || 0);
    const tokenText =
      tokens > 0 ? `${tokens} (vs. est. ${tokens * 3} full-open)` : 'N/A';

    const chips = [];
    if (context.metadata?.tiers_used?.includes('docs')) chips.push('📖 Docs');
    if (context.metadata?.tiers_used?.includes('cross_refs')) chips.push('🔗 Cross-refs');
    if (context.mode === 'surgical_full') chips.push('🎯 Surgical');

    return {
      primaryLabel,
      graphCount,
      docsCount,
      tokenText,
      chips,
    };
  }

  private pushStateToWebview(): void {
    const state = stateManager.getState();
    this.postMessage({
      type: 'backend.updated',
      sidecarHealth: state.sidecarHealth,
      cloudStatus: state.cloudStatus,
    });
  }

  private pushWorkspaceState(): void {
    const editor = vscode.window.activeTextEditor;
    const activeFileStr = editor?.document.fileName;
    const isDirty = editor?.document.isDirty || false;
    const selectedSymbolStr = editor ? this.overlayManager.getSymbolAtCursor(editor) : undefined;

    stateManager.setState({
      activeFile: activeFileStr || undefined,
      isDirty,
      selectedSymbol: selectedSymbolStr || undefined,
    });

    this.postMessage({
      type: 'workspace.updated',
      activeFile: activeFileStr || null,
      symbol: selectedSymbolStr || null,
      isDirty,
    });
  }

  private postMessage(message: HostToWebviewMessage): void {
    if (this.webviewView) {
      this.webviewView.webview.postMessage(message);
    }
  }
}
