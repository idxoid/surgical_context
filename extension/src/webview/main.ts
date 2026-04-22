declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  ChatMessage,
  ChatSurfaceState,
  ContextSummaryDto,
  HostToWebviewMessage,
  ImpactResponse,
  PromptContextPayload,
  SettingsData,
  WebviewToHostMessage,
} from './shared/protocol';
import {
  escapeHtml,
  renderAdvancedInfoAccordion,
  renderComposerDock,
  renderContextSummaryAccordion,
  renderEnvironmentAccordion,
  renderMessageCard,
  renderStatusChips,
  resizeComposerToFit,
} from './shared/layout';
import {
  renderActionButtonRow,
  renderAffectsGroup,
  renderFilesGroup,
  renderSymbolSummaryCard,
} from './shared/impactLayout';
import {
  renderDocumentationTab,
  renderGraphContextTab,
  renderPrimarySourceTab,
  renderPromptJsonTab,
  renderTokenBreakdownTab,
} from './shared/inspectorLayout';
import {
  renderSettingsForm,
  showFeedback,
  showFieldStatus,
} from './shared/settingsLayout';

type Surface = 'chat' | 'inspector' | 'impact' | 'settings';
type InspectorTab = 'primary' | 'graph' | 'docs' | 'tokens' | 'json';

class MainSurface {
  private surface: Surface = 'chat';
  private state: ChatSurfaceState | null = null;
  private messages: Map<string, ChatMessage> = new Map();
  private currentStreamingRequestId: string | null = null;
  private currentContextSummary: ContextSummaryDto | null = null;
  private currentPromptContext: PromptContextPayload | null = null;
  private selectedPromptRequestId: string | null = null;
  private inspectorTab: InspectorTab = 'primary';
  private pendingPrompt: string | null = null;
  private currentImpact: ImpactResponse | null = null;
  private currentImpactSymbol: string | null = null;
  private currentImpactSource: 'prompt' | 'graph' | null = null;
  private impactError: string | null = null;
  private impactLoading = false;
  private settings: SettingsData | null = null;
  private keyboardListenerAttached = false;

  constructor() {
    this.initializeMessageListener();
    this.restoreState();
    this.renderLoadingShell();
    this.postMessage({ type: 'surface.ready' });
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;

      switch (message.type) {
        case 'surface.init':
          this.state = message.state;
          if (message.state.lastContext && !this.currentPromptContext) {
            this.currentPromptContext = message.state.lastContext;
            this.currentContextSummary = this.summaryFromContext(message.state.lastContext);
            this.currentImpact = this.impactFromContext(message.state.lastContext);
            this.currentImpactSymbol = message.state.lastContext.primary_source.symbol;
            this.currentImpactSource = 'prompt';
          }
          this.render();
          break;

        case 'surface.showChat':
          this.surface = 'chat';
          this.render();
          break;

        case 'surface.showInspector':
          this.surface = 'inspector';
          this.render();
          break;

        case 'surface.showSettings':
          this.surface = 'settings';
          this.render();
          this.requestSettings();
          break;

        case 'chat.requestStarted':
          this.surface = 'chat';
          this.onRequestStarted(message.requestId, message.symbol);
          break;

        case 'chat.streamChunk':
          this.onStreamChunk(message.requestId, message.chunk);
          break;

        case 'chat.requestCompleted':
          this.onRequestCompleted(message.requestId, message.answer, message.context);
          break;

        case 'chat.requestFailed':
          this.onRequestFailed(message.requestId, message.error);
          break;

        case 'chat.requestStopped':
          this.onRequestStopped(message.requestId);
          break;

        case 'chat.contextSummary':
          this.currentContextSummary = message.summary;
          this.refreshAccordions();
          break;

        case 'workspace.updated':
          if (this.state) {
            this.state.workspace = {
              activeFile: message.activeFile,
              selectedSymbol: message.symbol,
              isDirty: message.isDirty,
            };
            this.refreshWorkspaceBits();
          }
          break;

        case 'backend.updated':
          if (this.state) {
            this.state.backend = {
              sidecarHealth: message.sidecarHealth,
              cloudStatus: message.cloudStatus,
            };
            this.refreshWorkspaceBits();
          }
          break;

        case 'impact.loading':
          this.surface = 'impact';
          this.impactLoading = true;
          this.impactError = null;
          this.render();
          break;

        case 'impact.loaded':
          this.surface = 'impact';
          this.impactLoading = false;
          this.currentImpactSymbol = message.symbol;
          this.currentImpact = message.impact;
          this.currentImpactSource = 'graph';
          this.impactError = null;
          this.render();
          break;

        case 'impact.loadFailed':
          this.surface = 'impact';
          this.impactLoading = false;
          this.impactError = message.error;
          this.render();
          break;

        case 'inspector.loaded':
          this.surface = 'inspector';
          this.currentPromptContext = message.context;
          if (message.context) {
            this.currentContextSummary = this.summaryFromContext(message.context);
            this.currentImpact = this.impactFromContext(message.context);
            this.currentImpactSymbol = message.context.primary_source.symbol;
            this.currentImpactSource = 'prompt';
          }
          this.render();
          break;

        case 'settings.loaded':
          this.settings = message.settings;
          if (this.surface === 'settings') {
            this.render();
          }
          break;

        case 'settings.saved':
          showFeedback(message.message, 'success');
          break;

        case 'settings.saveFailed':
          showFeedback(message.error, 'error');
          break;

        case 'settings.testUrlComplete':
          showFieldStatus('backendUrl', message.success, message.message);
          break;

        case 'toast.show':
          this.showToast(message.message, message.level);
          break;
      }
    });
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    if (!this.state) {
      this.renderLoadingShell();
      return;
    }

    root.innerHTML = this.renderCurrentSurface();

    this.attachEventListeners();
    this.restoreComposerDraft();
    this.updateConversationView();
  }

  private renderLoadingShell(): void {
    const root = document.getElementById('root');
    if (!root) return;

    root.innerHTML = `
      <section class="surface surface-chat" aria-label="Surgical Context loading">
        ${this.renderSurfaceTabs()}
        <div class="loading-state">Loading Surgical Context...</div>
      </section>
    `;

    this.attachEventListeners();
  }

  private renderCurrentSurface(): string {
    switch (this.surface) {
      case 'inspector':
        return this.renderInspectorSurface();
      case 'impact':
        return this.renderImpactSurface();
      case 'settings':
        return this.renderSettingsSurface();
      case 'chat':
      default:
        return this.renderChatSurface();
    }
  }

  private renderChrome(): string {
    return this.renderSurfaceTabs();
  }

  private renderSurfaceTabs(): string {
    const tabs: Array<{ id: Surface; label: string }> = [
      { id: 'chat', label: 'Chat' },
      { id: 'inspector', label: 'Inspector' },
      { id: 'impact', label: 'Impact' },
      { id: 'settings', label: 'Settings' },
    ];

    return `
      <nav class="surface-tab-bar" aria-label="Surgical Context sections">
        ${tabs
          .map(tab => `
            <button
              class="surface-tab ${this.surface === tab.id ? 'active' : ''}"
              data-action="switchSurface"
              data-surface="${tab.id}"
              aria-current="${this.surface === tab.id ? 'page' : 'false'}"
            >
              ${tab.label}
            </button>
          `)
          .join('')}
      </nav>
    `;
  }

  private renderChatSurface(): string {
    if (!this.state) return '';

    return `
      <section class="surface surface-chat" aria-label="Surgical Context chat">
        ${this.renderChrome()}
        <div class="conversation-viewport" id="conversation"></div>
        <div class="accordion-stack">
          ${this.renderAccordions()}
        </div>
        ${renderComposerDock()}
        ${renderStatusChips({
          isDirty: this.state.workspace.isDirty,
          graphFirst: true,
          docLinked: true,
        })}
      </section>
    `;
  }

  private renderImpactSurface(): string {
    const symbol = this.currentImpactSymbol || this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || 'No symbol selected';
    const selectedPromptText = this.selectedPromptText();
    const subtitle = selectedPromptText || 'Related code and files for the selected prompt.';

    if (this.impactLoading) {
      return `
        <section class="surface surface-impact" aria-label="Impact analysis">
          ${this.renderChrome()}
          <div class="surface-title">Impact Analysis</div>
          <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
          <div class="loading-state">Loading impact analysis...</div>
        </section>
      `;
    }

    if (this.impactError) {
      return `
        <section class="surface surface-impact" aria-label="Impact analysis">
          ${this.renderChrome()}
          <div class="surface-title">Impact Analysis</div>
          <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
          <div class="error-state">${escapeHtml(this.impactError)}</div>
          <button class="secondary-action" data-action="openChat">Back to Ask</button>
        </section>
      `;
    }

    if (!this.currentImpact) {
      return `
        <section class="surface surface-impact" aria-label="Impact analysis">
          ${this.renderChrome()}
          <div class="surface-title">Impact Analysis</div>
          <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
          <div class="empty-state">Select a symbol to see its impact.</div>
          <button class="primary-action" data-action="showImpact">Analyze Current Symbol</button>
        </section>
      `;
    }

    return `
      <section class="surface surface-impact" aria-label="Impact analysis">
        ${this.renderChrome()}
        <div class="surface-title">Impact Analysis</div>
        <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
        ${renderSymbolSummaryCard({
          symbol,
          filePath: this.currentImpact.file_path || 'unknown',
          uid: this.currentImpact.symbol_uid || symbol,
        })}
        ${renderActionButtonRow()}
        <div class="impact-groups">
          ${renderAffectsGroup(
            this.currentImpact.affected_symbols || [],
            this.currentImpactSource === 'prompt' ? 'Selected Prompt Context' : 'Affects',
            true
          )}
          ${renderFilesGroup(this.currentImpact.affected_files || [], false)}
        </div>
        <div class="impact-legend">
          <span><span class="legend-dot direct"></span> direct</span>
          <span><span class="legend-dot indirect"></span> indirect</span>
          <span><span class="legend-dot conditional"></span> conditional</span>
          <span><span class="legend-dot type"></span> via type</span>
        </div>
        <div class="surface-footer">
          <span>${this.currentImpactSource === 'prompt' ? 'From selected ask' : 'Graph built just now'}</span>
          <button class="icon-action" data-action="showImpact" title="Refresh impact">Refresh</button>
        </div>
      </section>
    `;
  }

  private renderInspectorSurface(): string {
    const context = this.currentPromptContext;

    if (!context) {
      return `
        <section class="surface surface-inspector" aria-label="Context inspector">
          ${this.renderChrome()}
          <div class="surface-title">Context Inspector</div>
          <div class="surface-subtitle">${escapeHtml(this.selectedPromptText() || 'Inspect the evidence behind the selected answer.')}</div>
          <div class="empty-state">
            No prompt context yet. Ask a question first, then come back here.
          </div>
          <button class="primary-action surface-inline-action" data-action="openChat">Open Chat</button>
        </section>
      `;
    }

    return `
      <section class="surface surface-inspector" aria-label="Context inspector">
        ${this.renderChrome()}
        <div class="inspector-header">
          <h2>Context Inspector</h2>
          <div class="surface-subtitle">${escapeHtml(this.selectedPromptText() || 'Selected prompt')}</div>
          <div class="inspector-tab-bar" role="tablist" aria-label="Context detail tabs">
            ${this.renderInspectorTabButton('primary', 'Primary')}
            ${this.renderInspectorTabButton('graph', 'Graph')}
            ${this.renderInspectorTabButton('docs', 'Docs')}
            ${this.renderInspectorTabButton('tokens', 'Tokens')}
            ${this.renderInspectorTabButton('json', 'JSON')}
          </div>
        </div>
        <div class="inspector-content">
          ${this.renderInspectorTabContent(context)}
        </div>
      </section>
    `;
  }

  private renderInspectorTabButton(tab: InspectorTab, label: string): string {
    return `
      <button
        class="tab-button ${this.inspectorTab === tab ? 'active' : ''}"
        data-action="switchInspectorTab"
        data-inspector-tab="${tab}"
        role="tab"
        aria-selected="${this.inspectorTab === tab}"
      >
        ${label}
      </button>
    `;
  }

  private renderInspectorTabContent(context: PromptContextPayload): string {
    switch (this.inspectorTab) {
      case 'graph':
        return renderGraphContextTab(context);
      case 'docs':
        return renderDocumentationTab(context);
      case 'tokens':
        return renderTokenBreakdownTab(context);
      case 'json':
        return renderPromptJsonTab(context);
      case 'primary':
      default:
        return renderPrimarySourceTab(context);
    }
  }

  private renderSettingsSurface(): string {
    return `
      <section class="surface surface-settings" aria-label="Surgical Context settings">
        ${this.renderChrome()}
        ${
          this.settings
            ? renderSettingsForm(this.settings)
            : '<div class="loading-state">Loading settings...</div>'
        }
      </section>
    `;
  }

  private renderAccordions(): string {
    if (!this.state) return '';
    const expanded = this.state.expandedAccordions;

    return `
      ${renderEnvironmentAccordion({
        workspace: this.state.workspace.activeFile || 'No active file',
        cloud: this.state.backend.cloudStatus,
        mode: 'Surgical',
        symbol: this.state.workspace.selectedSymbol || undefined,
      }, Boolean(expanded.environment))}
      ${renderContextSummaryAccordion(this.currentContextSummary || undefined, Boolean(expanded.contextSummary))}
      ${renderAdvancedInfoAccordion({
        intent: 'exploration',
        tiersUsed: this.currentContextSummary?.chips || ['code', 'docs'],
        isDirty: this.state.workspace.isDirty,
      }, Boolean(expanded.advancedInfo))}
    `;
  }

  private attachEventListeners(): void {
    document.querySelectorAll('[data-action]').forEach(element => {
      element.addEventListener('click', event => this.handleAction(event));
    });

    document.querySelectorAll('.accordion-header').forEach(header => {
      header.addEventListener('click', () => this.toggleAccordion(header as HTMLElement));
    });

    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    const sendBtn = document.getElementById('composer-send') as HTMLButtonElement | null;
    if (composer) {
      composer.addEventListener('input', () => {
        resizeComposerToFit(composer);
        this.persistState();
      });
      composer.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault();
          this.askAboutSymbol();
        }
      });
    }
    sendBtn?.addEventListener('click', () => this.askAboutSymbol());

    if (!this.keyboardListenerAttached) {
      document.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'l') {
          event.preventDefault();
          (document.getElementById('composer-input') as HTMLTextAreaElement | null)?.focus();
        }
      });
      this.keyboardListenerAttached = true;
    }
  }

  private handleAction(event: Event): void {
    const target = event.currentTarget as HTMLElement;
    const action = target.getAttribute('data-action');

    switch (action) {
      case 'switchSurface':
        this.switchSurface(target.getAttribute('data-surface') as Surface);
        break;
      case 'switchInspectorTab':
        this.switchInspectorTab(target.getAttribute('data-inspector-tab') as InspectorTab);
        break;
      case 'selectPrompt':
        this.selectPrompt(target.getAttribute('data-request-id'));
        break;
      case 'ask':
        (document.getElementById('composer-input') as HTMLTextAreaElement | null)?.focus();
        break;
      case 'openChat':
        this.switchSurface('chat');
        setTimeout(() => {
          (document.getElementById('composer-input') as HTMLTextAreaElement | null)?.focus();
        }, 0);
        break;
      case 'openInspector':
        this.switchSurface('inspector');
        break;
      case 'openSettings':
        this.switchSurface('settings');
        break;
      case 'showImpact':
        this.switchSurface('impact');
        if (target.classList.contains('icon-action')) {
          this.requestImpactForActiveSymbol();
        }
        break;
      case 'ask-followup':
        this.switchSurface('chat');
        this.prefillComposer(
          `What should I check before changing ${this.currentImpactSymbol || 'this symbol'}?`
        );
        break;
      case 'open-related-files':
        this.showToast('Related file opener is coming soon.', 'info');
        break;
      case 'create-refactor-plan':
        this.switchSurface('chat');
        this.prefillComposer(
          `Create a refactor plan for ${this.currentImpactSymbol || 'this symbol'}.`
        );
        break;
      case 'save':
        this.saveSettings();
        break;
      case 'reset':
        this.resetSettings();
        break;
      case 'testUrl':
        this.testSettingsUrl();
        break;
      case 'openKeybindings':
        this.postMessage({ type: 'settings.openKeybindings' });
        break;
      case 'search':
        this.showToast('Search is coming soon.', 'info');
        break;
      case 'noop':
        this.toggleImpactGroup(target);
        break;
      case 'feedback':
        this.submitFeedback(target);
        break;
      case 'copy':
        this.copyMessage(target);
        break;
    }
  }

  private switchSurface(surface: Surface | null): void {
    if (!surface) return;

    this.surface = surface;
    this.persistState();

    if (surface === 'impact') {
      this.render();
      const selectedSymbol = this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || undefined;
      if ((!this.currentImpact || (selectedSymbol && selectedSymbol !== this.currentImpactSymbol)) && !this.impactLoading) {
        this.requestImpactForActiveSymbol();
      }
      return;
    }

    if (surface === 'inspector') {
      this.render();
      if (!this.currentPromptContext) {
        this.postMessage({ type: 'action.openInspector' });
      }
      return;
    }

    if (surface === 'settings') {
      this.render();
      this.requestSettings();
      return;
    }

    this.render();
  }

  private switchInspectorTab(tab: InspectorTab | null): void {
    if (!tab) return;
    this.inspectorTab = tab;
    this.render();
  }

  private requestImpactForActiveSymbol(): void {
    if (this.impactLoading) return;

    const selectedSymbol = this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || undefined;
    this.postMessage({
      type: 'action.showImpact',
      symbol: selectedSymbol,
    });
  }

  private askAboutSymbol(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    if (!composer || !composer.value.trim() || !this.state) return;

    const prompt = composer.value.trim();
    this.pendingPrompt = prompt;
    composer.value = '';
    resizeComposerToFit(composer);
    this.persistState();

    this.postMessage({
      type: 'chat.ask',
      prompt,
      symbol: this.state.workspace.selectedSymbol || undefined,
    });
  }

  private requestSettings(): void {
    this.postMessage({ type: 'settings.loaded' });
  }

  private saveSettings(): void {
    if (!this.settings) return;

    const backendUrl = (document.getElementById('backendUrl') as HTMLInputElement | null)?.value || '';
    const workspaceId = (document.getElementById('workspaceId') as HTMLInputElement | null)?.value || '';
    const modelPreference = (document.getElementById('modelPreference') as HTMLSelectElement | null)?.value || 'auto';
    const authToken = (document.getElementById('authToken') as HTMLInputElement | null)?.value || '';
    const overlaySync = (document.getElementById('overlaySync') as HTMLInputElement | null)?.checked || false;
    const autoOpenInspector = (document.getElementById('autoOpenInspector') as HTMLInputElement | null)?.checked || false;

    if (backendUrl && !backendUrl.startsWith('http://') && !backendUrl.startsWith('https://')) {
      showFieldStatus('backendUrl', false, 'URL must start with http:// or https://');
      return;
    }

    this.postMessage({ type: 'settings.update', key: 'surgicalContext.backendUrl', value: backendUrl });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.workspaceId', value: workspaceId });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.modelPreference', value: modelPreference });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.authToken', value: authToken });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.overlaySync', value: overlaySync });
    this.postMessage({ type: 'settings.update', key: 'surgicalContext.chat.autoOpenInspector', value: autoOpenInspector });
  }

  private resetSettings(): void {
    const defaults: SettingsData = {
      backendUrl: 'http://localhost:8000',
      workspaceId: 'local/default@main',
      modelPreference: 'auto',
      authToken: '',
      overlaySync: true,
      autoOpenInspector: false,
    };

    const backendUrl = document.getElementById('backendUrl') as HTMLInputElement | null;
    const workspaceId = document.getElementById('workspaceId') as HTMLInputElement | null;
    const modelPreference = document.getElementById('modelPreference') as HTMLSelectElement | null;
    const authToken = document.getElementById('authToken') as HTMLInputElement | null;
    const overlaySync = document.getElementById('overlaySync') as HTMLInputElement | null;
    const autoOpenInspector = document.getElementById('autoOpenInspector') as HTMLInputElement | null;

    if (backendUrl) backendUrl.value = defaults.backendUrl;
    if (workspaceId) workspaceId.value = defaults.workspaceId;
    if (modelPreference) modelPreference.value = defaults.modelPreference;
    if (authToken) authToken.value = defaults.authToken;
    if (overlaySync) overlaySync.checked = defaults.overlaySync;
    if (autoOpenInspector) autoOpenInspector.checked = defaults.autoOpenInspector;

    showFeedback('Reset to default settings', 'info');
  }

  private testSettingsUrl(): void {
    const url = (document.getElementById('backendUrl') as HTMLInputElement | null)?.value || '';
    if (!url) {
      showFieldStatus('backendUrl', false, 'Please enter a URL');
      return;
    }

    this.postMessage({ type: 'settings.testUrl', url });
  }

  private onRequestStarted(requestId: string, symbol?: string): void {
    this.currentStreamingRequestId = requestId;
    this.selectedPromptRequestId = requestId;
    this.currentPromptContext = null;
    this.currentContextSummary = null;
    this.currentImpact = null;
    this.currentImpactSymbol = symbol || null;
    this.currentImpactSource = null;
    this.impactError = null;

    const prompt = this.pendingPrompt || 'Ask about current symbol';
    this.pendingPrompt = null;

    const userMessageId = `msg-${Date.now()}`;
    this.messages.set(userMessageId, {
      id: userMessageId,
      requestId,
      type: 'user',
      content: prompt,
      timestamp: Date.now(),
      symbol,
    });
    this.messages.set(requestId, {
      id: requestId,
      requestId,
      type: 'assistant',
      content: '',
      timestamp: Date.now(),
      symbol,
      status: 'streaming',
    });

    this.render();
    this.scrollToBottom();
  }

  private onStreamChunk(requestId: string, chunk: string): void {
    if (this.currentStreamingRequestId !== requestId) return;
    const message = this.messages.get(requestId);
    if (!message) return;

    message.content += chunk;
    message.status = 'streaming';
    this.updateConversationView();
    this.scrollToBottom();
  }

  private onRequestCompleted(requestId: string, answer: string, context: unknown): void {
    if (this.currentStreamingRequestId !== requestId) return;
    this.currentStreamingRequestId = null;

    const message = this.messages.get(requestId);
    if (message) {
      if (answer.trim()) {
        message.content = answer;
      }
      message.context = context as ChatMessage['context'];
      this.activatePromptContext(requestId, context as PromptContextPayload);
      message.status = 'done';
      this.updateConversationView();
    }
    this.refreshAccordions();
  }

  private onRequestFailed(requestId: string, error: string): void {
    this.currentStreamingRequestId = null;
    const message = this.messages.get(requestId);
    if (message) {
      message.status = 'error';
      message.error = error;
    } else {
      this.messages.set(requestId, {
        id: requestId,
        type: 'assistant',
        content: '',
        timestamp: Date.now(),
        status: 'error',
        error,
      });
    }
    this.updateConversationView();
  }

  private onRequestStopped(requestId: string): void {
    const message = this.messages.get(requestId);
    if (message) {
      message.status = 'done';
      this.updateConversationView();
    }
    this.currentStreamingRequestId = null;
  }

  private updateConversationView(): void {
    const viewport = document.getElementById('conversation');
    if (!viewport) return;

    viewport.innerHTML = Array.from(this.messages.values())
      .map(message => renderMessageCard(message, this.selectedPromptRequestId))
      .join('');

    viewport.querySelectorAll('[data-action]').forEach(element => {
      element.addEventListener('click', event => this.handleAction(event));
    });

    viewport.querySelectorAll('.message-card.selectable').forEach(element => {
      element.addEventListener('keydown', event => {
        const keyboardEvent = event as KeyboardEvent;
        if (keyboardEvent.key === 'Enter' || keyboardEvent.key === ' ') {
          keyboardEvent.preventDefault();
          this.selectPrompt((element as HTMLElement).getAttribute('data-request-id'));
        }
      });
    });
  }

  private selectPrompt(requestId: string | null): void {
    if (!requestId) return;

    this.selectedPromptRequestId = requestId;
    const context = this.contextForRequest(requestId);
    if (context) {
      this.activatePromptContext(requestId, context);
    } else {
      this.currentPromptContext = null;
      this.currentContextSummary = null;
      this.currentImpact = null;
      this.currentImpactSource = null;
      this.showToast('Prompt is still waiting for context.', 'info');
    }

    this.persistState();
    this.render();
  }

  private contextForRequest(requestId: string): PromptContextPayload | null {
    const message = this.messages.get(requestId);
    return message?.context || null;
  }

  private activatePromptContext(requestId: string, context: PromptContextPayload): void {
    this.selectedPromptRequestId = requestId;
    this.currentPromptContext = context;
    this.currentContextSummary = this.summaryFromContext(context);
    this.currentImpact = this.impactFromContext(context);
    this.currentImpactSymbol = context.primary_source.symbol;
    this.currentImpactSource = 'prompt';
    this.impactError = null;
  }

  private summaryFromContext(context: PromptContextPayload): ContextSummaryDto {
    const tierTokens = context.metadata.tier_tokens || {};
    const totalTokens = Object.values(tierTokens).reduce((sum, value) => {
      return sum + (typeof value === 'number' ? value : 0);
    }, 0);
    const askLevel = typeof context.budget?.ask_level === 'string'
      ? [`level:${context.budget.ask_level}`]
      : [];

    return {
      primaryLabel: `${context.primary_source.symbol} in ${context.primary_source.file_path}`,
      graphCount: context.graph_context.length,
      docsCount: context.documentation.length,
      tokenText: `${totalTokens} tokens`,
      chips: [...askLevel, ...(context.metadata.tiers_used || [])],
    };
  }

  private impactFromContext(context: PromptContextPayload): ImpactResponse {
    const affectedSymbols = context.graph_context.map(symbol => ({
      symbol: symbol.symbol,
      file_path: symbol.file_path,
      relation: symbol.relation,
      direction: symbol.direction,
      depth: symbol.depth,
      relevance_score: symbol.relevance_score,
      is_dirty: symbol.is_dirty,
    }));
    const affectedFiles = Array.from(new Set(
      [
        context.primary_source.file_path,
        ...context.graph_context.map(symbol => symbol.file_path),
        ...context.documentation.map(doc => doc.source_file),
      ].filter(Boolean)
    ));

    return {
      symbol: context.primary_source.symbol,
      symbol_uid: context.primary_source.symbol,
      file_path: context.primary_source.file_path,
      affected_symbols: affectedSymbols,
      affected_files: affectedFiles,
    };
  }

  private selectedPromptText(): string | null {
    if (!this.selectedPromptRequestId) return null;

    const prompt = Array.from(this.messages.values()).find(message => (
      message.type === 'user' && message.requestId === this.selectedPromptRequestId
    ));
    return prompt?.content || null;
  }

  private refreshWorkspaceBits(): void {
    if (!this.state) return;

    const statusRow = document.querySelector('.status-chip-row');
    if (statusRow) {
      statusRow.outerHTML = renderStatusChips({
        isDirty: this.state.workspace.isDirty,
        graphFirst: true,
        docLinked: true,
      });
    }
    this.refreshAccordions();
  }

  private refreshAccordions(): void {
    const stack = document.querySelector('.accordion-stack');
    if (stack) {
      stack.innerHTML = this.renderAccordions();
      document.querySelectorAll('.accordion-header').forEach(header => {
        header.addEventListener('click', () => this.toggleAccordion(header as HTMLElement));
      });
    }
  }

  private toggleAccordion(header: HTMLElement): void {
    const group = header.closest('[data-accordion]');
    const id = group?.getAttribute('data-accordion');
    const content = group?.querySelector('.accordion-content');
    if (!group || !content || !id) return;

    const expanded = header.getAttribute('aria-expanded') === 'true';
    header.setAttribute('aria-expanded', String(!expanded));
    content.toggleAttribute('hidden', expanded);
    content.classList.toggle('expanded', !expanded);

    if (this.state) {
      this.state.expandedAccordions[id] = !expanded;
      this.persistState();
    }
  }

  private toggleImpactGroup(header: HTMLElement): void {
    const group = header.closest('.impact-group');
    const content = group?.querySelector('.group-content');
    if (!group || !content) return;

    const expanded = header.getAttribute('aria-expanded') === 'true';
    header.setAttribute('aria-expanded', String(!expanded));
    group.classList.toggle('expanded', !expanded);
    content.toggleAttribute('hidden', expanded);
  }

  private submitFeedback(target: HTMLElement): void {
    const rating = target.getAttribute('data-rating') as 'up' | 'down' | null;
    const card = target.closest('.message-card');
    const messageId = card?.getAttribute('data-message-id');
    if (rating && messageId) {
      this.postMessage({ type: 'feedback.submit', messageId, rating });
      this.showToast('Thanks for the feedback.', 'info');
    }
  }

  private copyMessage(target: HTMLElement): void {
    const content = target.closest('.message-card')?.querySelector('.message-content')?.textContent;
    if (content) {
      navigator.clipboard.writeText(content).then(() => this.showToast('Copied.', 'info'));
    }
  }

  private prefillComposer(text: string): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    if (!composer) return;
    composer.value = text;
    resizeComposerToFit(composer);
    composer.focus();
    this.persistState();
  }

  private persistState(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    vscode.setState({
      composerDraft: composer?.value || '',
      expandedAccordions: this.state?.expandedAccordions || {},
      surface: this.surface,
    });
  }

  private restoreState(): void {
    const saved = vscode.getState();
    if (
      saved?.surface === 'chat' ||
      saved?.surface === 'inspector' ||
      saved?.surface === 'impact' ||
      saved?.surface === 'settings'
    ) {
      this.surface = saved.surface;
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

  private scrollToBottom(): void {
    const viewport = document.querySelector('.conversation-viewport');
    if (viewport) {
      setTimeout(() => {
        viewport.scrollTop = viewport.scrollHeight;
      }, 0);
    }
  }

  private showToast(message: string, level: 'info' | 'warning' | 'error' | 'success'): void {
    const toast = document.createElement('div');
    toast.className = `toast ${level}`;
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 250);
    }, 3000);
  }

  private postMessage(message: WebviewToHostMessage): void {
    vscode.postMessage(message);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new MainSurface());
} else {
  new MainSurface();
}
