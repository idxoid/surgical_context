import { bindDataActions } from './shared/domActions';
import { mountLayoutHtml, replaceElementHtml } from './shared/domRender';
import { bootWebview, listenForHostMessages, vscode } from './shared/webviewRuntime';

import {
  ChatMessage,
  ChatSurfaceState,
  ContextSummaryDto,
  HostToWebviewMessage,
  ImpactResponse,
  IntentMatch,
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
  clampImpactDepth,
  renderImpactWorkspace,
} from './shared/impactLayout';
import { hydrateFromPromptContext } from './shared/impactTransforms';
import { renderImpactSurfaceShell } from './shared/surfaceChrome';
import {
  renderDocumentationTab,
  renderGraphContextTab,
  renderPrimarySourceTab,
  renderPromptJsonTab,
  renderApiPayloadTab,
  renderTokenBreakdownTab,
  renderIntentTab,
} from './shared/inspectorLayout';
import {
  applySettingsDefaultsToDom,
  readSettingsFormFromDom,
  renderSettingsForm,
  settingsFormDataFromSettings,
  showFeedback,
  showFieldStatus,
  validateSettingsForm,
} from './shared/settingsLayout';

type Surface = 'chat' | 'inspector' | 'impact' | 'settings';
type InspectorTab = 'primary' | 'intent' | 'graph' | 'docs' | 'tokens' | 'json' | 'api';
type ImpactSource = 'prompt' | 'graph' | 'none';

interface StoredDialog {
  id: string;
  title: string;
  updatedAt: number;
  messages: ChatMessage[];
  selectedPromptRequestId: string | null;
}

class MainSurface {
  private surface: Surface = 'chat';
  private state: ChatSurfaceState | null = null;
  private messages: Map<string, ChatMessage> = new Map();
  private dialogHistory: StoredDialog[] = [];
  private currentDialogId = `dialog-${Date.now()}`;
  private currentStreamingRequestId: string | null = null;
  private currentContextSummary: ContextSummaryDto | null = null;
  private currentPromptContext: PromptContextPayload | null = null;
  private selectedPromptRequestId: string | null = null;
  private inspectorTab: InspectorTab = 'primary';
  private intentMatches: IntentMatch[] | null = null;
  private pendingPrompt: string | null = null;
  private pendingAskAnchor: { symbol: string; filePath?: string } | null = null;
  private currentImpact: ImpactResponse | null = null;
  private currentImpactSymbol: string | null = null;
  private currentImpactFilePath: string | null = null;
  private currentImpactSource: ImpactSource = 'none';
  private currentImpactDepth = 3;
  private impactError: string | null = null;
  private impactLoading = false;
  private historyCollapsed = true;
  private settings: SettingsData | null = null;
  private keyboardListenerAttached = false;

  constructor() {
    this.initializeMessageListener();
    this.restoreState();
    this.renderLoadingShell();
    this.postMessage({ type: 'surface.ready' });
  }

  private initializeMessageListener(): void {
    listenForHostMessages<HostToWebviewMessage>((message) => {
      switch (message.type) {
        case 'surface.init':
          this.state = message.state;
          if (message.state.lastContext && !this.currentPromptContext) {
            this.currentPromptContext = message.state.lastContext;
            this.applyHydratedContext(message.state.lastContext);
            this.selectedPromptRequestId = this.findRequestIdForContext(message.state.lastContext) || this.selectedPromptRequestId;
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

        case 'surface.showImpact':
          this.surface = 'impact';
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
              context_engineHealth: message.context_engineHealth,
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
          this.currentImpactFilePath = message.impact.file_path || null;
          this.currentImpact = message.impact;
          this.currentImpactDepth = clampImpactDepth(message.impact.max_depth || this.currentImpactDepth);
          this.currentImpactSource = 'graph';
          this.impactError = null;
          this.render();
          break;

        case 'impact.loadFailed':
          this.surface = 'impact';
          this.impactLoading = false;
          this.currentImpact = null;
          this.impactError = message.error;
          this.render();
          break;

        case 'inspector.loaded':
          this.surface = 'inspector';
          this.currentPromptContext = message.context;
          this.intentMatches = null;
          if (message.context) {
            this.applyHydratedContext(message.context);
          }
          this.render();
          break;

        case 'inspector.intentLoaded':
          this.intentMatches = message.intentMatches;
          if (this.surface === 'inspector' && this.inspectorTab === 'intent') {
            this.render();
          }
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

    mountLayoutHtml(root, this.renderCurrentSurface());

    this.attachEventListeners();
    this.restoreComposerDraft();
    this.updateConversationView();
  }

  private renderLoadingShell(): void {
    const root = document.getElementById('root');
    if (!root) return;

    mountLayoutHtml(root, `
      <section class="surface surface-chat" aria-label="Surgical Context loading">
        ${this.renderSurfaceTabs()}
        <div class="loading-state">Loading Surgical Context...</div>
      </section>
    `);

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
    const tabs: Array<{ id: Surface; label: string; icon: string }> = [
      { id: 'chat', label: 'Chat', icon: '◌' },
      { id: 'inspector', label: 'Inspector', icon: '◎' },
      { id: 'impact', label: 'Impact', icon: '⌁' },
    ];

    return `
      <nav class="surface-tab-bar" aria-label="Surgical Context sections">
        <div class="surface-tab-group">
          ${tabs
            .map(tab => `
            <button
              class="surface-tab ${this.surface === tab.id ? 'active' : ''}"
              data-action="switchSurface"
              data-surface="${tab.id}"
              aria-current="${this.surface === tab.id ? 'page' : 'false'}"
              title="${tab.label}"
              aria-label="${tab.label}"
            >
              <span aria-hidden="true">${tab.icon}</span>
            </button>
          `)
            .join('')}
          <button
            class="surface-tab"
            data-action="openDashboard"
            title="Dashboard"
            aria-label="Dashboard"
          >
            <span aria-hidden="true">▦</span>
          </button>
        </div>
        <div class="surface-tab-actions">
          ${this.surface === 'chat' ? this.renderChatSessionActions() : ''}
          <button
            class="surface-tab ${this.surface === 'settings' ? 'active' : ''}"
            data-action="switchSurface"
            data-surface="settings"
            aria-current="${this.surface === 'settings' ? 'page' : 'false'}"
            title="Settings"
            aria-label="Settings"
          >
            <span aria-hidden="true">⚙</span>
          </button>
        </div>
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
        ${renderComposerDock(Boolean(this.currentStreamingRequestId))}
        ${renderStatusChips({
          isDirty: this.state.workspace.isDirty,
          graphFirst: true,
          docLinked: true,
        })}
      </section>
    `;
  }

  private renderChatSessionActions(): string {
    const dialogs = this.dialogsForHistory();

    const rows = dialogs.length === 0
      ? '<div class="chat-history-empty">No asks yet.</div>'
      : dialogs.map(dialog => {
        const selected = this.currentDialogId === dialog.id;
        const label = dialog.title.length > 84
          ? `${dialog.title.slice(0, 81)}...`
          : dialog.title;
        const askCount = dialog.messages.filter(message => message.type === 'user').length;

        return `
          <button
            class="chat-history-row ${selected ? 'selected' : ''}"
            data-action="restoreDialog"
            data-dialog-id="${escapeHtml(dialog.id)}"
            title="${escapeHtml(dialog.title)}"
          >
            <span>${escapeHtml(label)}</span>
            <time>${askCount} ask${askCount === 1 ? '' : 's'}</time>
          </button>
        `;
      }).join('');

    return `
      <div class="chat-session-actions ${this.historyCollapsed ? 'collapsed' : 'expanded'}">
        <button
          class="chat-history-toggle"
          data-action="toggleHistory"
          aria-expanded="${!this.historyCollapsed}"
          title="History"
          aria-label="History"
        >
          <span aria-hidden="true">↺</span>
        </button>
        <button
          class="chat-new-dialog"
          data-action="newDialog"
          title="New dialog"
          aria-label="New dialog"
        >
          <span aria-hidden="true">+</span>
        </button>
        <div class="chat-history-menu" ${this.historyCollapsed ? 'hidden' : ''}>
          ${rows}
        </div>
      </div>
    `;
  }

  private renderImpactSurface(): string {
    const symbol = this.currentImpactSymbol || this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || 'No symbol selected';
    const selectedPromptText = this.selectedPromptText();
    const subtitle = selectedPromptText || 'Related code and files for the selected prompt.';
    const chrome = this.renderChrome();

    if (this.impactLoading) {
      return renderImpactSurfaceShell(chrome, subtitle, '<div class="loading-state">Loading impact analysis...</div>');
    }

    if (this.impactError) {
      return renderImpactSurfaceShell(
        chrome,
        subtitle,
        `<div class="error-state">${escapeHtml(this.impactError)}</div>
          <button class="secondary-action" data-action="openChat">Back to Ask</button>`,
      );
    }

    if (!this.currentImpact) {
      return renderImpactSurfaceShell(
        chrome,
        subtitle,
        `<div class="empty-state">Select a symbol to see its impact.</div>
          <button class="primary-action" data-action="showImpact">Analyze Current Symbol</button>`,
      );
    }

    return renderImpactSurfaceShell(
      chrome,
      subtitle,
      `${renderImpactWorkspace(
        this.currentImpact,
        symbol,
        this.impactContextSubtitle(),
        { depth: this.currentImpactDepth },
      )}
        <div class="surface-footer">
          <span>${this.impactFooterSubtitle()}</span>
          <button class="icon-action" data-action="showImpact" title="Refresh impact">Refresh</button>
        </div>`,
    );
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
            ${this.renderInspectorTabButton('intent', 'Intent')}
            ${this.renderInspectorTabButton('graph', 'Graph')}
            ${this.renderInspectorTabButton('docs', 'Docs')}
            ${this.renderInspectorTabButton('tokens', 'Tokens')}
            ${this.renderInspectorTabButton('json', 'JSON')}
            ${this.renderInspectorTabButton('api', 'API')}
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
      case 'intent':
        return renderIntentTab(this.intentMatches);
      case 'graph':
        return renderGraphContextTab(context);
      case 'docs':
        return renderDocumentationTab(context);
      case 'tokens':
        return renderTokenBreakdownTab(context);
      case 'json':
        return renderPromptJsonTab(context);
      case 'api' :
         return renderApiPayloadTab(context);
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
            ? renderSettingsForm(settingsFormDataFromSettings(this.settings))
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
    bindDataActions(document, event => this.handleAction(event));

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

    document.querySelectorAll('[data-impact-depth]').forEach(slider => {
      slider.addEventListener('input', event => this.previewImpactDepth(event));
      slider.addEventListener('change', event => this.changeImpactDepth(event));
    });
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
    const action = target.dataset.action;

    if (
      action === 'copy' ||
      action === 'copy-json' ||
      action === 'copy-api-json' ||
      action === 'feedback'
    ) {
      event.preventDefault();
      event.stopPropagation();
    }

    switch (action) {
      case 'switchSurface':
        this.switchSurface(target.dataset.surface as Surface);
        break;
      case 'switchInspectorTab':
        this.switchInspectorTab(target.dataset.inspectorTab as InspectorTab);
        break;
      case 'selectPrompt':
        this.selectPrompt(target.dataset.requestId ?? null);
        break;
      case 'toggleHistory':
        this.toggleHistory();
        break;
      case 'newDialog':
        this.startNewDialog();
        break;
      case 'restoreDialog':
        this.restoreDialog(target.dataset.dialogId ?? null);
        break;
      case 'openDashboard':
        this.postMessage({ type: 'action.openDashboard' });
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
        this.prefillImpactAsk(
          `What should I check before changing ${this.currentImpactSymbol || 'this symbol'}?`
        );
        break;
      case 'open-related-files':
        this.openRelatedImpactFiles();
        break;
      case 'openFile':
        this.openFileFromImpact(target);
        break;
      case 'showMoreImpact':
        this.showMoreImpactRows(target);
        break;
      case 'explainImpact':
        this.toggleImpactExplanation(target);
        break;
      case 'create-refactor-plan':
        this.prefillImpactAsk(
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
      case 'copy-json':
      case 'copy-api-json':
        this.copyInspectorJson(target);
        break;
      case 'stopStreaming':
        this.stopStreaming();
        break;
    }
  }

  private switchSurface(surface: Surface | null): void {
    if (!surface) return;

    this.surface = surface;
    this.persistState();

    if (surface === 'impact') {
      this.render();
      const selectedSymbol = this.impactTarget().symbol;
      const needsGraphImpact = (
        !this.currentImpact
        || !this.isGraphImpactSource()
        || Boolean(selectedSymbol && selectedSymbol !== this.currentImpactSymbol)
      );
      if (needsGraphImpact && !this.impactLoading) {
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

    const target = this.impactTarget();
    this.postMessage({
      type: 'action.showImpact',
      symbol: target.symbol,
      filePath: target.filePath,
      maxDepth: this.currentImpactDepth,
    });
  }

  private resetPromptDerivedState(
    impactSymbol: string | null = null,
    impactFilePath: string | null = null,
  ): void {
    this.currentPromptContext = null;
    this.currentContextSummary = null;
    this.currentImpact = null;
    this.currentImpactSource = 'none';
    this.currentImpactDepth = 3;
    this.currentImpactSymbol = impactSymbol;
    this.currentImpactFilePath = impactFilePath;
  }

  private isPromptImpactSource(): boolean {
    switch (this.currentImpactSource) {
      case 'prompt':
        return true;
      default:
        return false;
    }
  }

  private isGraphImpactSource(): boolean {
    switch (this.currentImpactSource) {
      case 'graph':
        return true;
      default:
        return false;
    }
  }

  private impactContextSubtitle(): string {
    return this.isPromptImpactSource() ? 'prompt context' : 'live graph';
  }

  private impactFooterSubtitle(): string {
    return this.isPromptImpactSource() ? 'From selected ask' : 'Graph built just now';
  }

  private impactTarget(): { symbol?: string; filePath?: string } {
    // Once a live graph result is loaded, depth changes and refreshes must
    // stay anchored to that exact symbol. A selected prompt can still be
    // present in the inspector state; preferring it here silently retargeted
    // the second request, making a slider move appear to "fix" an empty
    // impact result with numbers belonging to a different symbol.
    if (this.isGraphImpactSource() && this.currentImpactSymbol) {
      return {
        symbol: this.currentImpactSymbol,
        filePath: this.currentImpactFilePath || undefined,
      };
    }
    if (this.currentPromptContext) {
      return {
        symbol: this.currentPromptContext.primary_source.symbol,
        filePath: this.currentPromptContext.primary_source.file_path,
      };
    }
    if (this.selectedPromptRequestId && this.currentImpactSymbol) {
      return {
        symbol: this.currentImpactSymbol,
        filePath: this.currentImpactFilePath || undefined,
      };
    }
    return {
      symbol: this.state?.workspace.selectedSymbol || undefined,
      filePath: this.state?.workspace.activeFile || undefined,
    };
  }

  private previewImpactDepth(event: Event): void {
    const slider = event.currentTarget as HTMLInputElement | null;
    if (!slider) return;
    const output = slider.closest('.impact-depth-control')?.querySelector('output');
    const depth = clampImpactDepth(Number(slider.value));
    if (output) {
      output.textContent = `d${depth}`;
    }
  }

  private changeImpactDepth(event: Event): void {
    const slider = event.currentTarget as HTMLInputElement | null;
    if (!slider) return;
    const depth = clampImpactDepth(Number(slider.value));
    if (depth === this.currentImpactDepth && this.isGraphImpactSource()) return;
    this.currentImpactDepth = depth;
    this.requestImpactForActiveSymbol();
  }


  private openRelatedImpactFiles(): void {
    const filePaths = Array.from(new Set(this.currentImpact?.affected_files || []))
      .filter(Boolean)
      .slice(0, 12);
    if (filePaths.length === 0) {
      this.showToast('No related files to open.', 'info');
      return;
    }
    this.postMessage({
      type: 'impact.openFiles',
      filePaths,
    });
    this.showToast(`Opening ${filePaths.length} related file${filePaths.length === 1 ? '' : 's'}.`, 'info');
  }

  private askAboutSymbol(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    if (!composer?.value.trim() || !this.state) return;
    if (this.currentStreamingRequestId) {
      this.showToast('Stop the current response before sending another ask.', 'info');
      return;
    }

    const prompt = composer.value.trim();
    const anchor = this.pendingAskAnchor;
    const targetSymbol = anchor?.symbol || this.state.workspace.selectedSymbol || undefined;
    const targetFilePath = anchor?.filePath || this.state.workspace.activeFile || undefined;
    this.pendingPrompt = prompt;
    this.pendingAskAnchor = null;
    this.currentImpactSymbol = targetSymbol || null;
    this.currentImpactFilePath = targetFilePath || null;
    composer.value = '';
    resizeComposerToFit(composer);
    this.persistState();

    this.postMessage({
      type: 'chat.ask',
      prompt,
      symbol: targetSymbol,
      filePath: targetFilePath,
      conversationId: this.currentDialogId,
    });
  }

  private requestSettings(): void {
    this.postMessage({ type: 'settings.loaded' });
  }

  private saveSettings(): void {
    if (!this.settings) return;

    const values = readSettingsFormFromDom();
    const validationError = validateSettingsForm(values);
    if (validationError) {
      showFieldStatus(validationError.fieldId, false, validationError.message);
      return;
    }

    this.postMessage({
      type: 'settings.save',
      settings: values,
    });
  }

  private resetSettings(): void {
    applySettingsDefaultsToDom();
    showFeedback('Reset to default settings', 'info');
  }

  private testSettingsUrl(): void {
    const { backendUrl, authToken } = readSettingsFormFromDom();
    if (!backendUrl) {
      showFieldStatus('backendUrl', false, 'Please enter a URL');
      return;
    }

    this.postMessage({ type: 'settings.testUrl', url: backendUrl, authToken });
  }

  private onRequestStarted(requestId: string, symbol?: string): void {
    this.currentStreamingRequestId = requestId;
    if (symbol && this.state) {
      this.state.workspace.selectedSymbol = symbol;
    }
    this.selectedPromptRequestId = requestId;
    this.resetPromptDerivedState(symbol || null, null);
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

    this.persistState();
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
    this.persistState();
    this.refreshAccordions();
    this.updateComposerStreamingState(false);
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
    this.persistState();
    this.updateConversationView();
    this.updateComposerStreamingState(false);
  }

  private onRequestStopped(requestId: string): void {
    const message = this.messages.get(requestId);
    if (message) {
      message.status = 'done';
      this.updateConversationView();
    }
    this.currentStreamingRequestId = null;
    this.persistState();
    this.updateComposerStreamingState(false);
  }

  private stopStreaming(): void {
    if (!this.currentStreamingRequestId) return;

    const stopButton = document.getElementById('composer-stop') as HTMLButtonElement | null;
    if (stopButton) {
      stopButton.disabled = true;
      stopButton.title = 'Stopping response…';
      stopButton.setAttribute('aria-label', 'Stopping response');
    }
    this.postMessage({ type: 'chat.stop', requestId: this.currentStreamingRequestId });
  }

  private updateComposerStreamingState(isStreaming: boolean): void {
    const sendButton = document.getElementById('composer-send') as HTMLButtonElement | null;
    const stopButton = document.getElementById('composer-stop') as HTMLButtonElement | null;
    if (sendButton) sendButton.hidden = isStreaming;
    if (stopButton) {
      stopButton.hidden = !isStreaming;
      stopButton.disabled = false;
      stopButton.title = 'Stop response';
      stopButton.setAttribute('aria-label', 'Stop response generation');
    }
  }

  private updateConversationView(): void {
    const viewport = document.getElementById('conversation');
    if (!viewport) return;

    mountLayoutHtml(
      viewport,
      Array.from(this.messages.values())
        .map(message => renderMessageCard(message, this.selectedPromptRequestId))
        .join(''),
    );

    bindDataActions(viewport, event => this.handleAction(event));

    viewport.querySelectorAll('.message-card.selectable').forEach(element => {
      element.addEventListener('keydown', event => {
        const keyboardEvent = event as KeyboardEvent;
        if (keyboardEvent.key === 'Enter' || keyboardEvent.key === ' ') {
          keyboardEvent.preventDefault();
          this.selectPrompt((element as HTMLElement).dataset.requestId ?? null);
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
      const promptMessage = Array.from(this.messages.values()).find(message => (
        message.type === 'user' && message.requestId === requestId
      ));
      this.resetPromptDerivedState(promptMessage?.symbol || null, null);
      this.showToast('Prompt is still waiting for context.', 'info');
    }

    this.historyCollapsed = true;
    this.persistState();
    this.render();
  }

  private toggleHistory(): void {
    this.historyCollapsed = !this.historyCollapsed;
    this.render();
  }

  private startNewDialog(): void {
    if (this.currentStreamingRequestId) {
      this.postMessage({ type: 'chat.stop', requestId: this.currentStreamingRequestId });
    }
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    if (composer) {
      composer.value = '';
    }
    this.persistState();
    this.currentDialogId = `dialog-${Date.now()}`;
    this.messages.clear();
    this.currentStreamingRequestId = null;
    this.resetPromptDerivedState();
    this.selectedPromptRequestId = null;
    this.pendingPrompt = null;
    this.pendingAskAnchor = null;
    this.impactError = null;
    this.impactLoading = false;
    this.historyCollapsed = true;
    this.persistState();
    this.render();
  }

  private restoreDialog(dialogId: string | null): void {
    if (!dialogId) return;
    const dialog = this.dialogHistory.find(item => item.id === dialogId);
    if (!dialog) return;

    this.persistState();
    this.pendingAskAnchor = null;
    this.currentDialogId = dialog.id;
    this.messages = new Map(dialog.messages.map(message => [message.id, { ...message }]));
    this.selectedPromptRequestId = dialog.selectedPromptRequestId || this.latestContextRequestId();

    const context = this.selectedPromptRequestId
      ? this.contextForRequest(this.selectedPromptRequestId)
      : null;
    if (context && this.selectedPromptRequestId) {
      this.activatePromptContext(this.selectedPromptRequestId, context);
    } else {
      this.resetPromptDerivedState();
      this.impactError = null;
    }

    this.historyCollapsed = true;
    this.persistState();
    this.render();
    this.scrollToBottom();
  }

  private dialogsForHistory(): StoredDialog[] {
    const current = this.currentDialogSnapshot();
    const dialogs = current
      ? [current, ...this.dialogHistory.filter(dialog => dialog.id !== current.id)]
      : [...this.dialogHistory];
    return dialogs
      .filter(dialog => dialog.messages.length > 0)
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .slice(0, 30);
  }

  private currentDialogSnapshot(): StoredDialog | null {
    const messages = Array.from(this.messages.values());
    if (messages.length === 0) return null;

    const firstPrompt = messages.find(message => message.type === 'user');
    const latestTimestamp = Math.max(...messages.map(message => message.timestamp));
    const title = firstPrompt?.content?.trim() || 'Untitled dialog';

    return {
      id: this.currentDialogId,
      title,
      updatedAt: latestTimestamp,
      messages: messages.map(message => ({ ...message })),
      selectedPromptRequestId: this.selectedPromptRequestId,
    };
  }

  private saveCurrentDialogSnapshot(): void {
    const snapshot = this.currentDialogSnapshot();
    if (!snapshot) {
      this.dialogHistory = this.dialogHistory.filter(dialog => dialog.id !== this.currentDialogId);
      return;
    }

    this.dialogHistory = [
      snapshot,
      ...this.dialogHistory.filter(dialog => dialog.id !== snapshot.id),
    ]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .slice(0, 30);
  }

  private latestContextRequestId(): string | null {
    const messages = Array.from(this.messages.values())
      .filter(message => Boolean(message.requestId && message.context))
      .sort((left, right) => right.timestamp - left.timestamp);
    return messages[0]?.requestId || null;
  }

  private contextForRequest(requestId: string): PromptContextPayload | null {
    const message = this.messages.get(requestId);
    return message?.context || null;
  }

  private activatePromptContext(requestId: string, context: PromptContextPayload): void {
    this.selectedPromptRequestId = requestId;
    this.currentPromptContext = context;
    this.applyHydratedContext(context);
    this.impactError = null;
    this.syncSelectedRequestToHost(requestId, context);
  }

  private applyHydratedContext(context: PromptContextPayload): void {
    const hydrated = hydrateFromPromptContext(context);
    this.currentContextSummary = hydrated.summary;
    this.currentImpact = hydrated.impact;
    this.currentImpactSymbol = hydrated.symbol;
    this.currentImpactFilePath = hydrated.filePath;
    this.currentImpactDepth = hydrated.depth;
    this.currentImpactSource = 'prompt';
  }

  private syncSelectedRequestToHost(requestId: string, context: PromptContextPayload): void {
    const assistantMessage = this.messages.get(requestId);
    this.postMessage({
      type: 'request.selected',
      requestId,
      symbol: context.primary_source.symbol,
      question: this.selectedPromptText() || undefined,
      answer: assistantMessage?.content || undefined,
      context,
    });
  }

  private findRequestIdForContext(context: PromptContextPayload): string | null {
    const traceId = context.metadata?.assembly?.trace_id;
    const entries = Array.from(this.messages.values()).filter(message => message.context);
    if (traceId) {
      const exact = entries.find(message => message.context?.metadata?.assembly?.trace_id === traceId);
      if (exact?.requestId) return exact.requestId;
    }
    const bySymbol = entries
      .filter(message => message.context?.primary_source.symbol === context.primary_source.symbol)
      .sort((left, right) => right.timestamp - left.timestamp);
    return bySymbol[0]?.requestId || null;
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
      replaceElementHtml(statusRow, renderStatusChips({
        isDirty: this.state.workspace.isDirty,
        graphFirst: true,
        docLinked: true,
      }));
    }
    this.refreshAccordions();
  }

  private refreshAccordions(): void {
    const stack = document.querySelector('.accordion-stack');
    if (stack) {
      mountLayoutHtml(stack as HTMLElement, this.renderAccordions());
      document.querySelectorAll('.accordion-header').forEach(header => {
        header.addEventListener('click', () => this.toggleAccordion(header as HTMLElement));
      });
    }
  }

  private toggleAccordion(header: HTMLElement): void {
    const group = header.closest('[data-accordion]');
    const id = (group as HTMLElement | null)?.dataset.accordion;
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

  private showMoreImpactRows(target: HTMLElement): void {
    const group = target.closest('.impact-group');
    const overflow = group?.querySelector('.impact-overflow');
    if (!overflow) return;

    overflow.removeAttribute('hidden');
    target.remove();
  }

  private toggleImpactExplanation(target: HTMLElement): void {
    const item = target.closest('.impact-item');
    const explanation = item?.querySelector('.impact-explanation') as HTMLElement | null;
    if (!explanation) return;

    const expanded = target.getAttribute('aria-expanded') === 'true';
    target.setAttribute('aria-expanded', String(!expanded));
    target.textContent = expanded ? 'Explain' : 'Hide';
    explanation.toggleAttribute('hidden', expanded);
  }

  private openFileFromImpact(target: HTMLElement): void {
    const filePath = target.dataset.filePath;
    if (!filePath) return;

    const line = Number.parseInt(target.dataset.line || '1', 10);
    this.postMessage({
      type: 'link.openFile',
      filePath,
      line: Number.isFinite(line) ? line : 1,
    });
  }

  private submitFeedback(target: HTMLElement): void {
    const rating = target.dataset.rating as 'up' | 'down' | undefined;
    const card = target.closest('.message-card') as HTMLElement | null;
    const messageId = card?.dataset.messageId;
    const feedbackToken = messageId
      ? this.messages.get(messageId)?.context?.metadata?.assembly?.feedback_token
      : undefined;
    if (rating && messageId && feedbackToken) {
      this.postMessage({ type: 'feedback.submit', messageId, rating, feedbackToken });
      this.showToast('Thanks for the feedback.', 'info');
    } else if (rating) {
      this.showToast('Feedback token is not available for this response yet.', 'warning');
    }
  }

  private copyMessage(target: HTMLElement): void {
    const content = target.closest('.message-card')?.querySelector('.message-content')?.textContent;
    if (content) {
      navigator.clipboard.writeText(content).then(() => this.showToast('Copied.', 'info'));
    }
  }

  private copyInspectorJson(target: HTMLElement): void {
    const content = target.closest('.json-viewer')?.querySelector('pre code')?.textContent;
    if (!content) {
      this.showToast('JSON is not available to copy.', 'warning');
      return;
    }

    this.postMessage({ type: 'clipboard.write', text: content });
  }

  private prefillComposer(text: string): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    if (!composer) return;
    composer.value = text;
    resizeComposerToFit(composer);
    composer.focus();
    this.persistState();
  }

  private prefillImpactAsk(text: string): void {
    const symbol = this.currentImpactSymbol || this.currentPromptContext?.primary_source.symbol;
    const filePath = this.currentImpact?.file_path || this.currentPromptContext?.primary_source.file_path;

    if (symbol) {
      this.pendingAskAnchor = { symbol, filePath: filePath || undefined };
      this.currentImpactSymbol = symbol;
      this.currentImpactFilePath = filePath || null;
      if (this.state) {
        this.state.workspace.selectedSymbol = symbol;
        if (filePath) this.state.workspace.activeFile = filePath;
      }
    }

    this.switchSurface('chat');
    this.prefillComposer(text);
  }

  private persistState(): void {
    const composer = document.getElementById('composer-input') as HTMLTextAreaElement | null;
    this.saveCurrentDialogSnapshot();
    vscode.setState({
      composerDraft: composer?.value || '',
      expandedAccordions: this.state?.expandedAccordions || {},
      surface: this.surface,
      currentDialogId: this.currentDialogId,
      dialogHistory: this.dialogHistory,
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
    if (Array.isArray(saved?.dialogHistory)) {
      this.dialogHistory = saved.dialogHistory
        .filter((dialog: StoredDialog) => dialog?.id && Array.isArray(dialog.messages))
        .slice(0, 30);
    }
    if (typeof saved?.currentDialogId === 'string') {
      this.currentDialogId = saved.currentDialogId;
    }
    const currentDialog = this.dialogHistory.find(dialog => dialog.id === this.currentDialogId);
    if (currentDialog) {
      this.messages = new Map(
        currentDialog.messages.map((message: ChatMessage) => [message.id, { ...message }])
      );
      this.selectedPromptRequestId = currentDialog.selectedPromptRequestId || this.latestContextRequestId();
      if (this.selectedPromptRequestId) {
        const context = this.contextForRequest(this.selectedPromptRequestId);
        if (context) {
          this.activatePromptContext(this.selectedPromptRequestId, context);
        }
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

bootWebview(() => new MainSurface());
