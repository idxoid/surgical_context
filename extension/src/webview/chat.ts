// @ts-ignore vscode API injected at runtime
declare const vscode: any;

import {
  WebviewToHostMessage,
  HostToWebviewMessage,
  ChatSurfaceState,
  ChatMessage,
  ContextSummaryDto,
} from './shared/protocol';
import {
  renderMessageCard,
  renderStreamingCursor,
  renderEnvironmentAccordion,
  renderContextSummaryAccordion,
  renderAdvancedInfoAccordion,
  renderActionBar,
  renderComposerDock,
  renderStatusChips,
  resizeComposerToFit,
} from './shared/layout';

class ChatPanel {
  private state: ChatSurfaceState | null = null;
  private messages: Map<string, ChatMessage> = new Map();
  private currentStreamingRequestId: string | null = null;
  private currentContextSummary: ContextSummaryDto | null = null;
  private currentAbortController: AbortController | null = null;

  constructor() {
    this.initializeMessageListener();
    this.initializeUI();
    this.restoreState();
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;

      switch (message.type) {
        case 'surface.init':
          this.state = message.state;
          this.render();
          break;

        case 'chat.requestStarted':
          this.onRequestStarted(message.requestId, message.symbol);
          break;

        case 'chat.streamChunk':
          this.onStreamChunk(message.requestId, message.chunk);
          break;

        case 'chat.requestCompleted':
          this.onRequestCompleted(message.requestId, message.answer, message.context);
          this.currentContextSummary = null;
          break;

        case 'chat.requestFailed':
          this.onRequestFailed(message.requestId, message.error);
          break;

        case 'chat.requestStopped':
          this.onRequestStopped(message.requestId);
          break;

        case 'chat.contextSummary':
          this.currentContextSummary = message.summary;
          break;

        case 'workspace.updated':
          if (this.state) {
            this.state.workspace = {
              activeFile: message.activeFile,
              selectedSymbol: message.symbol,
              isDirty: message.isDirty,
            };
            this.updateStatusChips();
          }
          break;

        case 'backend.updated':
          if (this.state) {
            this.state.backend = {
              sidecarHealth: message.sidecarHealth,
              cloudStatus: message.cloudStatus,
            };
            this.updateHeader();
          }
          break;

        case 'toast.show':
          this.showToast(message.message, message.level);
          break;
      }
    });
  }

  private initializeUI(): void {
    this.setupComposerListeners();
    this.setupAccordionListeners();
    this.setupActionBarListeners();
  }

  private setupComposerListeners(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    const sendBtn = document.getElementById('composer-send') as HTMLButtonElement | null;

    if (!composer || !sendBtn) return;

    // Auto-grow textarea
    composer.addEventListener('input', () => {
      resizeComposerToFit(composer);
      this.persistState();
    });

    // Keyboard shortcuts
    composer.addEventListener('keydown', (e: KeyboardEvent) => {
      // Enter to send (unless Shift+Enter for newline)
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.askAboutSymbol();
      }
    });

    // Send button click
    sendBtn.addEventListener('click', () => this.askAboutSymbol());

    // Global keyboard shortcut: Cmd+L or Ctrl+L to focus composer
    document.addEventListener('keydown', (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'l') {
        e.preventDefault();
        composer.focus();
      }
    });
  }

  private setupAccordionListeners(): void {
    document.querySelectorAll('.accordion-header').forEach(header => {
      header.addEventListener('click', () => {
        const accordionGroup = header.parentElement;
        if (!accordionGroup) return;

        const id = accordionGroup.getAttribute('data-accordion');
        const content = accordionGroup.querySelector('.accordion-content');
        const isExpanded = header.getAttribute('aria-expanded') === 'true';

        if (content) {
          if (isExpanded) {
            header.setAttribute('aria-expanded', 'false');
            content.setAttribute('hidden', '');
            content.classList.remove('expanded');
          } else {
            header.setAttribute('aria-expanded', 'true');
            content.removeAttribute('hidden');
            content.classList.add('expanded');
          }
        }

        if (id) {
          this.postMessage({
            type: 'accordion.toggled',
            id,
            expanded: !isExpanded,
          });
          this.persistState();
        }
      });
    });
  }

  private setupActionBarListeners(): void {
    document.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', (e: Event) => {
        const action = (e.currentTarget as HTMLElement).getAttribute('data-action');

        switch (action) {
          case 'ask':
            this.askAboutSymbol();
            break;
          case 'openInspector':
            this.postMessage({ type: 'action.openInspector' });
            break;
          case 'showImpact':
            this.postMessage({ type: 'action.showImpact' });
            break;
          case 'search':
            // TODO: implement search
            this.showToast('Search coming soon', 'info');
            break;
          case 'feedback':
            const rating = (e.currentTarget as HTMLElement).getAttribute('data-rating') as 'up' | 'down';
            const messageId = (e.currentTarget as HTMLElement).closest('.message-card')?.id;
            if (messageId) {
              this.postMessage({ type: 'feedback.submit', messageId, rating });
              this.showToast('Thanks for your feedback!', 'info');
            }
            break;
          case 'copy':
            const card = (e.currentTarget as HTMLElement).closest('.message-card');
            const content = card?.querySelector('.message-content')?.textContent;
            if (content) {
              navigator.clipboard.writeText(content).then(() => {
                this.showToast('Copied to clipboard', 'info');
              });
            }
            break;
        }
      });
    });
  }

  private askAboutSymbol(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    if (!composer || !composer.value.trim() || !this.state) return;

    const prompt = composer.value.trim();
    const symbol = this.state.workspace.selectedSymbol || undefined;

    composer.value = '';
    resizeComposerToFit(composer);

    this.postMessage({
      type: 'chat.ask',
      prompt,
      symbol,
    });
  }

  private onRequestStarted(requestId: string, symbol?: string): void {
    this.currentStreamingRequestId = requestId;

    // Add user message card
    const userMsg: ChatMessage = {
      id: `msg-${Date.now()}`,
      type: 'user',
      content: (document.getElementById('composer-input') as HTMLTextAreaElement)?.value || '',
      timestamp: Date.now(),
    };
    this.messages.set(userMsg.id, userMsg);

    // Add assistant message card (empty, streaming)
    const assistantMsg: ChatMessage = {
      id: requestId,
      type: 'assistant',
      content: '',
      timestamp: Date.now(),
      status: 'streaming',
    };
    this.messages.set(requestId, assistantMsg);

    this.updateConversationView();
    this.scrollToBottom();
  }

  private onStreamChunk(requestId: string, chunk: string): void {
    if (this.currentStreamingRequestId !== requestId) return;

    const msg = this.messages.get(requestId);
    if (msg) {
      msg.content += chunk;
      msg.status = 'streaming';
      this.updateConversationView();
      this.scrollToBottom();
    }
  }

  private onRequestCompleted(requestId: string, answer: string, context: any): void {
    if (this.currentStreamingRequestId !== requestId) return;
    this.currentStreamingRequestId = null;

    const msg = this.messages.get(requestId);
    if (msg) {
      msg.content = answer;
      msg.context = context;
      msg.status = 'done';
      this.updateConversationView();
    }

    // Auto-open Context Summary accordion if setting is enabled
    if (this.state?.expandedAccordions['contextSummary'] === false) {
      const header = document.querySelector('[data-accordion="contextSummary"] .accordion-header');
      if (header) {
        (header as HTMLElement).click();
      }
    }
  }

  private onRequestFailed(requestId: string, error: string): void {
    if (this.currentStreamingRequestId !== requestId) return;
    this.currentStreamingRequestId = null;

    const msg = this.messages.get(requestId);
    if (msg) {
      msg.status = 'error';
      msg.error = error;
      this.updateConversationView();
    }
  }

  private onRequestStopped(requestId: string): void {
    const msg = this.messages.get(requestId);
    if (msg) {
      msg.status = 'done';
      this.updateConversationView();
    }
    this.currentStreamingRequestId = null;
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root || !this.state) return;

    const environmentAccordion = renderEnvironmentAccordion({
      workspace: this.state.workspace.activeFile || 'none',
      cloud: this.state.backend.cloudStatus,
      mode: 'surgical',
      symbol: this.state.workspace.selectedSymbol || undefined,
    });

    const contextSummaryAccordion = renderContextSummaryAccordion(this.currentContextSummary || undefined);

    const advancedInfoAccordion = renderAdvancedInfoAccordion({
      intent: 'ask',
      tiersUsed: ['code', 'docs'],
      isDirty: this.state.workspace.isDirty,
    });

    const statusChips = renderStatusChips({
      isDirty: this.state.workspace.isDirty,
      graphFirst: true,
      docLinked: true,
    });

    root.innerHTML = `
      <div class="header">
        <span class="header-title">Surgical Context</span>
        <div class="health-indicator ${this.state.backend.sidecarHealth}"></div>
      </div>
      ${renderActionBar()}
      <div class="conversation-viewport" id="conversation"></div>
      ${environmentAccordion}
      ${contextSummaryAccordion}
      ${advancedInfoAccordion}
      ${renderComposerDock()}
      ${statusChips}
    `;

    this.initializeUI();
    this.updateConversationView();
    this.restoreComposerDraft();
  }

  private updateConversationView(): void {
    const viewport = document.getElementById('conversation');
    if (!viewport) return;

    const html = Array.from(this.messages.values())
      .map(msg => `<div id="${msg.id}">${renderMessageCard(msg)}</div>`)
      .join('');

    viewport.innerHTML = html;

    // Reattach event listeners
    this.setupActionBarListeners();
  }

  private updateHeader(): void {
    const indicator = document.querySelector('.health-indicator');
    if (indicator && this.state) {
      indicator.className = `health-indicator ${this.state.backend.sidecarHealth}`;
    }
  }

  private updateStatusChips(): void {
    if (!this.state) return;
    const chipRow = document.querySelector('.status-chip-row');
    if (chipRow) {
      chipRow.outerHTML = renderStatusChips({
        isDirty: this.state.workspace.isDirty,
        graphFirst: true,
        docLinked: true,
      });
    }
  }

  private scrollToBottom(): void {
    const viewport = document.querySelector('.conversation-viewport');
    if (viewport) {
      setTimeout(() => {
        viewport.scrollTop = viewport.scrollHeight;
      }, 0);
    }
  }

  private showToast(message: string, level: 'info' | 'warning' | 'error'): void {
    // TODO: implement toast UI
    console.log(`[${level}] ${message}`);
  }

  private persistState(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    const state = {
      composerDraft: composer?.value || '',
      expandedAccordions: this.state?.expandedAccordions || {},
    };
    vscode.setState(state);
  }

  private restoreState(): void {
    const saved = vscode.getState();
    if (saved?.expandedAccordions) {
      if (!this.state) {
        this.state = {
          expandedAccordions: saved.expandedAccordions,
          composerDraft: saved.composerDraft || '',
          workspace: { activeFile: null, selectedSymbol: null, isDirty: false },
          backend: { sidecarHealth: 'degraded', cloudStatus: 'offline' },
        };
      } else {
        this.state.expandedAccordions = saved.expandedAccordions;
      }
    }
  }

  private restoreComposerDraft(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    const saved = vscode.getState();
    if (composer && saved?.composerDraft) {
      composer.value = saved.composerDraft;
      resizeComposerToFit(composer);
    }
  }

  private postMessage(message: WebviewToHostMessage): void {
    vscode.postMessage(message);
  }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new ChatPanel());
} else {
  new ChatPanel();
}
