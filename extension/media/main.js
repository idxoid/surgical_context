import {
  applySettingsDefaultsToDom,
  bindDataActions,
  bootWebview,
  clampImpactDepth,
  createAssistantChatMessage,
  createUserChatMessage,
  dispatchMainHostMessage,
  escapeHtml,
  handleMainSurfaceAction,
  hydrateFromPromptContext,
  listenForHostMessages,
  mountLayoutHtml,
  readSettingsFormFromDom,
  renderAdvancedInfoAccordion,
  renderComposerDock,
  renderContextSummaryAccordion,
  renderEnvironmentAccordion,
  renderImpactSurfaceShell,
  renderImpactWorkspace,
  renderInspectorSurfaceView,
  renderMainSurfaceTabBar,
  renderMessageCard,
  renderSettingsForm,
  renderStatusChips,
  renderSurfaceShell,
  replaceElementHtml,
  resizeComposerToFit,
  settingsFormDataFromSettings,
  showFeedback,
  showFieldStatus,
  toggleAriaExpandedSection,
  validateSettingsForm,
  vscode
} from "./chunk-44YOORCP.js";

// src/webview/main.ts
const MainSurface = class {
  surface = "chat";
  state = null;
  messages = /* @__PURE__ */ new Map();
  dialogHistory = [];
  currentStreamingRequestId = null;
  currentContextSummary = null;
  currentPromptContext = null;
  selectedPromptRequestId = null;
  inspectorTab = "primary";
  intentMatches = null;
  pendingPrompt = null;
  pendingAskAnchor = null;
  currentImpact = null;
  currentImpactSymbol = null;
  currentImpactFilePath = null;
  currentImpactSource = "none";
  currentImpactDepth = 3;
  impactError = null;
  impactLoading = false;
  historyCollapsed = true;
  settings = null;
  keyboardListenerAttached = false;
  constructor() {
    this.currentDialogId = `dialog-${Date.now()}`;
    this.initializeMessageListener();
    this.restoreState();
    this.renderLoadingShell();
    this.postMessage({ type: "surface.ready" });
  }
  initializeMessageListener() {
    listenForHostMessages((message) => {
      dispatchMainHostMessage(this.hostDelegate(), message);
    });
  }
  hostDelegate() {
    return this;
  }
  actionHost() {
    return this;
  }
  setSurface(surface) {
    this.surface = surface;
  }
  setContextSummary(summary) {
    this.currentContextSummary = summary;
  }
  postOpenDashboard() {
    this.postMessage({ type: "action.openDashboard" });
  }
  postOpenKeybindings() {
    this.postMessage({ type: "settings.openKeybindings" });
  }
  showSearchComingSoon() {
    this.showToast("Search is coming soon.", "info");
  }
  getActiveImpactSymbol() {
    return this.currentImpactSymbol;
  }
  showSurface(surface, beforeRender) {
    this.surface = surface;
    beforeRender?.();
    this.render();
  }
  onSurfaceInit(message) {
    this.state = message.state;
    if (message.state.lastContext && !this.currentPromptContext) {
      this.currentPromptContext = message.state.lastContext;
      this.applyHydratedContext(message.state.lastContext);
      this.selectedPromptRequestId = this.findRequestIdForContext(message.state.lastContext) || this.selectedPromptRequestId;
    }
    this.render();
  }
  onWorkspaceUpdated(message) {
    if (!this.state) return;
    this.state.workspace = {
      activeFile: message.activeFile,
      selectedSymbol: message.symbol,
      isDirty: message.isDirty
    };
    this.refreshWorkspaceBits();
  }
  onBackendUpdated(message) {
    if (!this.state) return;
    this.state.backend = {
      context_engineHealth: message.context_engineHealth,
      cloudStatus: message.cloudStatus
    };
    this.refreshWorkspaceBits();
  }
  onImpactLoading() {
    this.mutateImpactSurface(() => {
      this.impactLoading = true;
      this.impactError = null;
    });
  }
  onImpactLoaded(message) {
    this.mutateImpactSurface(() => {
      this.impactLoading = false;
      this.currentImpactSymbol = message.symbol;
      this.currentImpactFilePath = message.impact.file_path || null;
      this.currentImpact = message.impact;
      this.currentImpactDepth = clampImpactDepth(message.impact.max_depth || this.currentImpactDepth);
      this.currentImpactSource = "graph";
      this.impactError = null;
    });
  }
  onImpactLoadFailed(message) {
    this.mutateImpactSurface(() => {
      this.impactLoading = false;
      this.currentImpact = null;
      this.impactError = message.error;
    });
  }
  mutateImpactSurface(mutator) {
    this.surface = "impact";
    mutator();
    this.render();
  }
  onInspectorLoaded(message) {
    this.showSurface("inspector", () => {
      this.currentPromptContext = message.context;
      this.intentMatches = null;
      if (message.context) {
        this.applyHydratedContext(message.context);
      }
    });
  }
  onInspectorIntentLoaded(message) {
    this.intentMatches = message.intentMatches;
    if (this.surface === "inspector" && this.inspectorTab === "intent") {
      this.render();
    }
  }
  onSettingsLoaded(message) {
    this.settings = message.settings;
    if (this.surface === "settings") {
      this.render();
    }
  }
  render() {
    const root = document.getElementById("root");
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
  renderLoadingShell() {
    const root = document.getElementById("root");
    if (!root) return;
    mountLayoutHtml(
      root,
      renderSurfaceShell(
        "surface-chat",
        "Surgical Context loading",
        this.renderSurfaceTabs(),
        '<div class="loading-state">Loading Surgical Context...</div>'
      )
    );
    this.attachEventListeners();
  }
  renderCurrentSurface() {
    switch (this.surface) {
      case "inspector":
        return this.renderInspectorSurface();
      case "impact":
        return this.renderImpactSurface();
      case "settings":
        return this.renderSettingsSurface();
      case "chat":
      default:
        return this.renderChatSurface();
    }
  }
  renderChrome() {
    return this.renderSurfaceTabs();
  }
  renderSurfaceTabs() {
    return renderMainSurfaceTabBar(this.surface, this.renderChatSessionActions());
  }
  renderChatSurface() {
    if (!this.state) return "";
    return renderSurfaceShell(
      "surface-chat",
      "Surgical Context chat",
      this.renderChrome(),
      `
        <div class="conversation-viewport" id="conversation"></div>
        <div class="accordion-stack">
          ${this.renderAccordions()}
        </div>
        ${renderComposerDock(Boolean(this.currentStreamingRequestId))}
        ${renderStatusChips({
        isDirty: this.state.workspace.isDirty,
        graphFirst: true,
        docLinked: true
      })}
      `
    );
  }
  renderChatSessionActions() {
    const dialogs = this.dialogsForHistory();
    const rows = dialogs.length === 0 ? '<div class="chat-history-empty">No asks yet.</div>' : dialogs.map((dialog) => {
      const selected = this.currentDialogId === dialog.id;
      const label = dialog.title.length > 84 ? `${dialog.title.slice(0, 81)}...` : dialog.title;
      const askCount = dialog.messages.filter((message) => message.type === "user").length;
      return `
          <button
            class="chat-history-row ${selected ? "selected" : ""}"
            data-action="restoreDialog"
            data-dialog-id="${escapeHtml(dialog.id)}"
            title="${escapeHtml(dialog.title)}"
          >
            <span>${escapeHtml(label)}</span>
            <time>${askCount} ask${askCount === 1 ? "" : "s"}</time>
          </button>
        `;
    }).join("");
    return `
      <div class="chat-session-actions ${this.historyCollapsed ? "collapsed" : "expanded"}">
        <button
          class="chat-history-toggle"
          data-action="toggleHistory"
          aria-expanded="${!this.historyCollapsed}"
          title="History"
          aria-label="History"
        >
          <span aria-hidden="true">\u21BA</span>
        </button>
        <button
          class="chat-new-dialog"
          data-action="newDialog"
          title="New dialog"
          aria-label="New dialog"
        >
          <span aria-hidden="true">+</span>
        </button>
        <div class="chat-history-menu" ${this.historyCollapsed ? "hidden" : ""}>
          ${rows}
        </div>
      </div>
    `;
  }
  renderImpactSurface() {
    const symbol = this.currentImpactSymbol || this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || "No symbol selected";
    const subtitle = this.selectedPromptText() || "Related code and files for the selected prompt.";
    return renderImpactSurfaceShell(this.renderChrome(), subtitle, this.buildImpactSurfaceBody(symbol));
  }
  buildImpactSurfaceBody(symbol) {
    if (this.impactLoading) {
      return '<div class="loading-state">Loading impact analysis...</div>';
    }
    if (this.impactError) {
      return `
        <div class="error-state">${escapeHtml(this.impactError)}</div>
        <button class="secondary-action" data-action="openChat">Back to Ask</button>
      `;
    }
    if (!this.currentImpact) {
      return `
        <div class="empty-state">Select a symbol to see its impact.</div>
        <button class="primary-action" data-action="showImpact">Analyze Current Symbol</button>
      `;
    }
    return `
      ${renderImpactWorkspace(
      this.currentImpact,
      symbol,
      this.impactContextSubtitle(),
      { depth: this.currentImpactDepth }
    )}
      <div class="surface-footer">
        <span>${this.impactFooterSubtitle()}</span>
        <button class="icon-action" data-action="showImpact" title="Refresh impact">Refresh</button>
      </div>
    `;
  }
  renderInspectorSurface() {
    return renderInspectorSurfaceView(
      this.renderChrome(),
      this.currentPromptContext,
      this.inspectorTab,
      this.selectedPromptText() || (this.currentPromptContext ? "Selected prompt" : "Inspect the evidence behind the selected answer."),
      this.intentMatches
    );
  }
  renderSettingsSurface() {
    return renderSurfaceShell(
      "surface-settings",
      "Surgical Context settings",
      this.renderChrome(),
      this.settings ? renderSettingsForm(settingsFormDataFromSettings(this.settings)) : '<div class="loading-state">Loading settings...</div>'
    );
  }
  renderAccordions() {
    if (!this.state) return "";
    const expanded = this.state.expandedAccordions;
    return `
      ${renderEnvironmentAccordion({
      workspace: this.state.workspace.activeFile || "No active file",
      cloud: this.state.backend.cloudStatus,
      mode: "Surgical",
      symbol: this.state.workspace.selectedSymbol || void 0
    }, Boolean(expanded.environment))}
      ${renderContextSummaryAccordion(this.currentContextSummary || void 0, Boolean(expanded.contextSummary))}
      ${renderAdvancedInfoAccordion({
      intent: "exploration",
      tiersUsed: this.currentContextSummary?.chips || ["code", "docs"],
      isDirty: this.state.workspace.isDirty
    }, Boolean(expanded.advancedInfo))}
    `;
  }
  attachEventListeners() {
    bindDataActions(document, (event) => handleMainSurfaceAction(this.actionHost(), event));
    document.querySelectorAll(".accordion-header").forEach((header) => {
      header.addEventListener("click", () => this.toggleAccordion(header));
    });
    const composer = document.getElementById("composer-input");
    const sendBtn = document.getElementById("composer-send");
    if (composer) {
      composer.addEventListener("input", () => {
        resizeComposerToFit(composer);
        this.persistState();
      });
      composer.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          this.askAboutSymbol();
        }
      });
    }
    document.querySelectorAll("[data-impact-depth]").forEach((slider) => {
      slider.addEventListener("input", (event) => this.previewImpactDepth(event));
      slider.addEventListener("change", (event) => this.changeImpactDepth(event));
    });
    sendBtn?.addEventListener("click", () => this.askAboutSymbol());
    if (!this.keyboardListenerAttached) {
      document.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === "l") {
          event.preventDefault();
          this.focusComposer();
        }
      });
      this.keyboardListenerAttached = true;
    }
  }
  handleSurfaceDomAction(surface, action, target) {
    this.switchSurface(surface);
    if (action === "openChat") {
      this.focusComposerDeferred();
    } else if (action === "showImpact" && target.classList.contains("icon-action")) {
      this.requestImpactForActiveSymbol();
    }
  }
  focusComposer() {
    document.getElementById("composer-input")?.focus();
  }
  focusComposerDeferred() {
    setTimeout(() => this.focusComposer(), 0);
  }
  syncView(scrollToConversation = false) {
    this.persistState();
    this.render();
    if (scrollToConversation) {
      this.scrollToBottom();
    }
  }
  switchSurface(surface) {
    if (!surface) return;
    this.surface = surface;
    this.persistState();
    if (surface === "impact") {
      this.render();
      const selectedSymbol = this.impactTarget().symbol;
      const needsGraphImpact = !this.currentImpact || this.currentImpactSource !== "graph" || Boolean(selectedSymbol && selectedSymbol !== this.currentImpactSymbol);
      if (needsGraphImpact && !this.impactLoading) {
        this.requestImpactForActiveSymbol();
      }
      return;
    }
    if (surface === "inspector") {
      this.render();
      if (!this.currentPromptContext) {
        this.postMessage({ type: "action.openInspector" });
      }
      return;
    }
    if (surface === "settings") {
      this.render();
      this.requestSettings();
      return;
    }
    this.render();
  }
  switchInspectorTab(tab) {
    if (!tab) return;
    this.inspectorTab = tab;
    this.render();
  }
  requestImpactForActiveSymbol() {
    if (this.impactLoading) return;
    const target = this.impactTarget();
    this.postMessage({
      type: "action.showImpact",
      symbol: target.symbol,
      filePath: target.filePath,
      maxDepth: this.currentImpactDepth
    });
  }
  resetPromptDerivedState(impactSymbol = null, impactFilePath = null) {
    this.currentPromptContext = null;
    this.currentContextSummary = null;
    this.currentImpact = null;
    this.currentImpactSource = "none";
    this.currentImpactDepth = 3;
    this.currentImpactSymbol = impactSymbol;
    this.currentImpactFilePath = impactFilePath;
  }
  impactSymbolTarget(symbol, filePath) {
    return {
      symbol: symbol || void 0,
      filePath: filePath || void 0
    };
  }
  impactContextSubtitle() {
    return this.currentImpactSource === "prompt" ? "prompt context" : "live graph";
  }
  impactFooterSubtitle() {
    return this.currentImpactSource === "prompt" ? "From selected ask" : "Graph built just now";
  }
  impactTarget() {
    if (this.currentImpactSource === "graph" && this.currentImpactSymbol) {
      return this.impactSymbolTarget(this.currentImpactSymbol, this.currentImpactFilePath);
    }
    if (this.currentPromptContext) {
      return this.impactSymbolTarget(
        this.currentPromptContext.primary_source.symbol,
        this.currentPromptContext.primary_source.file_path
      );
    }
    if (this.selectedPromptRequestId && this.currentImpactSymbol) {
      return this.impactSymbolTarget(this.currentImpactSymbol, this.currentImpactFilePath);
    }
    return this.impactSymbolTarget(
      this.state?.workspace.selectedSymbol,
      this.state?.workspace.activeFile
    );
  }
  previewImpactDepth(event) {
    const slider = event.currentTarget;
    if (!slider) return;
    const output = slider.closest(".impact-depth-control")?.querySelector("output");
    const depth = clampImpactDepth(Number(slider.value));
    if (output) {
      output.textContent = `d${depth}`;
    }
  }
  changeImpactDepth(event) {
    const slider = event.currentTarget;
    if (!slider) return;
    const depth = clampImpactDepth(Number(slider.value));
    if (depth === this.currentImpactDepth && this.currentImpactSource === "graph") return;
    this.currentImpactDepth = depth;
    this.requestImpactForActiveSymbol();
  }
  openRelatedImpactFiles() {
    const filePaths = Array.from(new Set(this.currentImpact?.affected_files || [])).filter(Boolean).slice(0, 12);
    if (filePaths.length === 0) {
      this.showToast("No related files to open.", "info");
      return;
    }
    this.postMessage({
      type: "impact.openFiles",
      filePaths
    });
    this.showToast(`Opening ${filePaths.length} related file${filePaths.length === 1 ? "" : "s"}.`, "info");
  }
  askAboutSymbol() {
    const composer = document.getElementById("composer-input");
    if (!composer?.value.trim() || !this.state) return;
    if (this.currentStreamingRequestId) {
      this.showToast("Stop the current response before sending another ask.", "info");
      return;
    }
    const prompt = composer.value.trim();
    const anchor = this.pendingAskAnchor;
    const targetSymbol = anchor?.symbol || this.state.workspace.selectedSymbol || void 0;
    const targetFilePath = anchor?.filePath || this.state.workspace.activeFile || void 0;
    this.pendingPrompt = prompt;
    this.pendingAskAnchor = null;
    this.currentImpactSymbol = targetSymbol || null;
    this.currentImpactFilePath = targetFilePath || null;
    composer.value = "";
    resizeComposerToFit(composer);
    this.persistState();
    this.postMessage({
      type: "chat.ask",
      prompt,
      symbol: targetSymbol,
      filePath: targetFilePath,
      conversationId: this.currentDialogId
    });
  }
  requestSettings() {
    this.postMessage({ type: "settings.loaded" });
  }
  saveSettings() {
    if (!this.settings) return;
    const values = readSettingsFormFromDom();
    const validationError = validateSettingsForm(values);
    if (validationError) {
      showFieldStatus(validationError.fieldId, false, validationError.message);
      return;
    }
    this.postMessage({
      type: "settings.save",
      settings: values
    });
  }
  resetSettings() {
    applySettingsDefaultsToDom();
    showFeedback("Reset to default settings", "info");
  }
  testSettingsUrl() {
    const { backendUrl, authToken } = readSettingsFormFromDom();
    if (!backendUrl) {
      showFieldStatus("backendUrl", false, "Please enter a URL");
      return;
    }
    this.postMessage({ type: "settings.testUrl", url: backendUrl, authToken });
  }
  onRequestStarted(requestId, symbol) {
    this.currentStreamingRequestId = requestId;
    if (symbol && this.state) {
      this.state.workspace.selectedSymbol = symbol;
    }
    this.selectedPromptRequestId = requestId;
    this.resetPromptDerivedState(symbol || null, null);
    this.impactError = null;
    const prompt = this.pendingPrompt || "Ask about current symbol";
    this.pendingPrompt = null;
    const userMessage = createUserChatMessage(requestId, prompt, symbol);
    this.messages.set(userMessage.id, userMessage);
    this.messages.set(requestId, createAssistantChatMessage(requestId, symbol));
    this.syncView(true);
  }
  finalizeAssistantExchange(options) {
    this.persistState();
    if (options?.refreshAccordions) {
      this.refreshAccordions();
    }
    this.updateComposerStreamingState(false);
  }
  onStreamChunk(requestId, chunk) {
    if (this.currentStreamingRequestId !== requestId) return;
    const message = this.messages.get(requestId);
    if (!message) return;
    message.content += chunk;
    message.status = "streaming";
    this.updateConversationView();
    this.scrollToBottom();
  }
  onRequestCompleted(requestId, answer, context) {
    if (this.currentStreamingRequestId !== requestId) return;
    this.currentStreamingRequestId = null;
    const message = this.messages.get(requestId);
    if (message) {
      if (answer.trim()) {
        message.content = answer;
      }
      message.context = context;
      this.activatePromptContext(requestId, context);
      message.status = "done";
      this.updateConversationView();
    }
    this.finalizeAssistantExchange({ refreshAccordions: true });
  }
  onRequestFailed(requestId, error) {
    this.currentStreamingRequestId = null;
    const message = this.messages.get(requestId);
    if (message) {
      message.status = "error";
      message.error = error;
      this.updateConversationView();
    } else {
      this.messages.set(requestId, createAssistantChatMessage(requestId, void 0, error, "error"));
      this.updateConversationView();
    }
    this.finalizeAssistantExchange();
  }
  onRequestStopped(requestId) {
    const message = this.messages.get(requestId);
    if (message) {
      message.status = "done";
      this.updateConversationView();
    }
    this.currentStreamingRequestId = null;
    this.finalizeAssistantExchange();
  }
  stopStreaming() {
    if (!this.currentStreamingRequestId) return;
    const stopButton = document.getElementById("composer-stop");
    if (stopButton) {
      stopButton.disabled = true;
      stopButton.title = "Stopping response\u2026";
      stopButton.setAttribute("aria-label", "Stopping response");
    }
    this.postMessage({ type: "chat.stop", requestId: this.currentStreamingRequestId });
  }
  updateComposerStreamingState(isStreaming) {
    const sendButton = document.getElementById("composer-send");
    const stopButton = document.getElementById("composer-stop");
    if (sendButton) sendButton.hidden = isStreaming;
    if (stopButton) {
      stopButton.hidden = !isStreaming;
      stopButton.disabled = false;
      stopButton.title = "Stop response";
      stopButton.setAttribute("aria-label", "Stop response generation");
    }
  }
  updateConversationView() {
    const viewport = document.getElementById("conversation");
    if (!viewport) return;
    mountLayoutHtml(
      viewport,
      Array.from(this.messages.values()).map((message) => renderMessageCard(message, this.selectedPromptRequestId)).join("")
    );
    bindDataActions(viewport, (event) => handleMainSurfaceAction(this.actionHost(), event));
    viewport.querySelectorAll(".message-card.selectable").forEach((element) => {
      element.addEventListener("keydown", (event) => {
        const keyboardEvent = event;
        if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
          keyboardEvent.preventDefault();
          this.selectPrompt(element.dataset.requestId ?? null);
        }
      });
    });
  }
  selectPrompt(requestId) {
    if (!requestId) return;
    this.selectedPromptRequestId = requestId;
    const context = this.contextForRequest(requestId);
    if (context) {
      this.activatePromptContext(requestId, context);
    } else {
      const promptMessage = Array.from(this.messages.values()).find((message) => message.type === "user" && message.requestId === requestId);
      this.resetPromptDerivedState(promptMessage?.symbol || null, null);
      this.showToast("Prompt is still waiting for context.", "info");
    }
    this.historyCollapsed = true;
    this.syncView();
  }
  toggleHistory() {
    this.historyCollapsed = !this.historyCollapsed;
    this.render();
  }
  startNewDialog() {
    if (this.currentStreamingRequestId) {
      this.postMessage({ type: "chat.stop", requestId: this.currentStreamingRequestId });
    }
    const composer = document.getElementById("composer-input");
    if (composer) {
      composer.value = "";
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
    this.syncView();
  }
  restoreDialog(dialogId) {
    if (!dialogId) return;
    const dialog = this.dialogHistory.find((item) => item.id === dialogId);
    if (!dialog) return;
    this.persistState();
    this.pendingAskAnchor = null;
    this.currentDialogId = dialog.id;
    this.messages = new Map(dialog.messages.map((message) => [message.id, { ...message }]));
    this.selectedPromptRequestId = dialog.selectedPromptRequestId || this.latestContextRequestId();
    const context = this.selectedPromptRequestId ? this.contextForRequest(this.selectedPromptRequestId) : null;
    if (context && this.selectedPromptRequestId) {
      this.activatePromptContext(this.selectedPromptRequestId, context);
    } else {
      this.resetPromptDerivedState();
      this.impactError = null;
    }
    this.historyCollapsed = true;
    this.syncView(true);
  }
  trimDialogHistory(dialogs) {
    const sorted = dialogs.toSorted((left, right) => right.updatedAt - left.updatedAt);
    return sorted.slice(0, 30);
  }
  dialogsForHistory() {
    const current = this.currentDialogSnapshot();
    const dialogs = current ? [current, ...this.dialogHistory.filter((dialog) => dialog.id !== current.id)] : [...this.dialogHistory];
    return this.trimDialogHistory(dialogs.filter((dialog) => dialog.messages.length > 0));
  }
  currentDialogSnapshot() {
    const messages = Array.from(this.messages.values());
    if (messages.length === 0) return null;
    const firstPrompt = messages.find((message) => message.type === "user");
    const latestTimestamp = Math.max(...messages.map((message) => message.timestamp));
    const title = firstPrompt?.content?.trim() || "Untitled dialog";
    return {
      id: this.currentDialogId,
      title,
      updatedAt: latestTimestamp,
      messages: messages.map((message) => ({ ...message })),
      selectedPromptRequestId: this.selectedPromptRequestId
    };
  }
  saveCurrentDialogSnapshot() {
    const snapshot = this.currentDialogSnapshot();
    if (!snapshot) {
      this.dialogHistory = this.dialogHistory.filter((dialog) => dialog.id !== this.currentDialogId);
      return;
    }
    this.dialogHistory = this.trimDialogHistory([
      snapshot,
      ...this.dialogHistory.filter((dialog) => dialog.id !== snapshot.id)
    ]);
  }
  latestContextRequestId() {
    const messages = Array.from(this.messages.values()).filter((message) => Boolean(message.requestId && message.context)).sort((left, right) => right.timestamp - left.timestamp);
    return messages[0]?.requestId || null;
  }
  contextForRequest(requestId) {
    const message = this.messages.get(requestId);
    return message?.context || null;
  }
  activatePromptContext(requestId, context) {
    this.selectedPromptRequestId = requestId;
    this.currentPromptContext = context;
    this.applyHydratedContext(context);
    this.impactError = null;
    this.syncSelectedRequestToHost(requestId, context);
  }
  applyHydratedContext(context) {
    const hydrated = hydrateFromPromptContext(context);
    this.currentContextSummary = hydrated.summary;
    this.currentImpact = hydrated.impact;
    this.currentImpactSymbol = hydrated.symbol;
    this.currentImpactFilePath = hydrated.filePath;
    this.currentImpactDepth = hydrated.depth;
    this.currentImpactSource = "prompt";
  }
  syncSelectedRequestToHost(requestId, context) {
    const assistantMessage = this.messages.get(requestId);
    this.postMessage({
      type: "request.selected",
      requestId,
      symbol: context.primary_source.symbol,
      question: this.selectedPromptText() || void 0,
      answer: assistantMessage?.content || void 0,
      context
    });
  }
  findRequestIdForContext(context) {
    const traceId = context.metadata?.assembly?.trace_id;
    const entries = Array.from(this.messages.values()).filter((message) => message.context);
    if (traceId) {
      const exact = entries.find((message) => message.context?.metadata?.assembly?.trace_id === traceId);
      if (exact?.requestId) return exact.requestId;
    }
    const bySymbol = entries.filter((message) => message.context?.primary_source.symbol === context.primary_source.symbol).sort((left, right) => right.timestamp - left.timestamp);
    return bySymbol[0]?.requestId || null;
  }
  selectedPromptText() {
    if (!this.selectedPromptRequestId) return null;
    const prompt = Array.from(this.messages.values()).find((message) => message.type === "user" && message.requestId === this.selectedPromptRequestId);
    return prompt?.content || null;
  }
  refreshWorkspaceBits() {
    if (!this.state) return;
    const statusRow = document.querySelector(".status-chip-row");
    if (statusRow) {
      replaceElementHtml(statusRow, renderStatusChips({
        isDirty: this.state.workspace.isDirty,
        graphFirst: true,
        docLinked: true
      }));
    }
    this.refreshAccordions();
  }
  refreshAccordions() {
    const stack = document.querySelector(".accordion-stack");
    if (stack) {
      mountLayoutHtml(stack, this.renderAccordions());
      document.querySelectorAll(".accordion-header").forEach((header) => {
        header.addEventListener("click", () => this.toggleAccordion(header));
      });
    }
  }
  toggleAccordion(header) {
    const group = header.closest("[data-accordion]");
    const id = group?.dataset.accordion;
    const content = group?.querySelector(".accordion-content");
    if (!group || !content || !id) return;
    const expanded = toggleAriaExpandedSection(header, content, group);
    if (this.state) {
      this.state.expandedAccordions[id] = expanded;
      this.persistState();
    }
  }
  toggleImpactGroup(header) {
    const group = header.closest(".impact-group");
    const content = group?.querySelector(".group-content");
    if (!group || !content) return;
    toggleAriaExpandedSection(header, content, group, "expanded");
  }
  showMoreImpactRows(target) {
    const group = target.closest(".impact-group");
    const overflow = group?.querySelector(".impact-overflow");
    if (!overflow) return;
    overflow.removeAttribute("hidden");
    target.remove();
  }
  toggleImpactExplanation(target) {
    const item = target.closest(".impact-item");
    const explanation = item?.querySelector(".impact-explanation");
    if (!explanation) return;
    const expanded = target.getAttribute("aria-expanded") === "true";
    target.setAttribute("aria-expanded", String(!expanded));
    target.textContent = expanded ? "Explain" : "Hide";
    explanation.toggleAttribute("hidden", expanded);
  }
  openFileFromImpact(target) {
    const filePath = target.dataset.filePath;
    if (!filePath) return;
    const line = Number.parseInt(target.dataset.line || "1", 10);
    this.postMessage({
      type: "link.openFile",
      filePath,
      line: Number.isFinite(line) ? line : 1
    });
  }
  submitFeedback(target) {
    const rating = target.dataset.rating;
    const card = target.closest(".message-card");
    const messageId = card?.dataset.messageId;
    const feedbackToken = messageId ? this.messages.get(messageId)?.context?.metadata?.assembly?.feedback_token : void 0;
    if (rating && messageId && feedbackToken) {
      this.postMessage({ type: "feedback.submit", messageId, rating, feedbackToken });
      this.showToast("Thanks for the feedback.", "info");
    } else if (rating) {
      this.showToast("Feedback token is not available for this response yet.", "warning");
    }
  }
  copyMessage(target) {
    const content = target.closest(".message-card")?.querySelector(".message-content")?.textContent;
    if (content) {
      navigator.clipboard.writeText(content).then(() => this.showToast("Copied.", "info"));
    }
  }
  copyInspectorJson(target) {
    const content = target.closest(".json-viewer")?.querySelector("pre code")?.textContent;
    if (!content) {
      this.showToast("JSON is not available to copy.", "warning");
      return;
    }
    this.postMessage({ type: "clipboard.write", text: content });
  }
  prefillComposer(text) {
    const composer = document.getElementById("composer-input");
    if (!composer) return;
    composer.value = text;
    resizeComposerToFit(composer);
    composer.focus();
    this.persistState();
  }
  prefillImpactAsk(text) {
    const symbol = this.currentImpactSymbol || this.currentPromptContext?.primary_source.symbol;
    const filePath = this.currentImpact?.file_path || this.currentPromptContext?.primary_source.file_path;
    if (symbol) {
      this.pendingAskAnchor = { symbol, filePath: filePath || void 0 };
      this.currentImpactSymbol = symbol;
      this.currentImpactFilePath = filePath || null;
      if (this.state) {
        this.state.workspace.selectedSymbol = symbol;
        if (filePath) this.state.workspace.activeFile = filePath;
      }
    }
    this.switchSurface("chat");
    this.prefillComposer(text);
  }
  persistState() {
    const composer = document.getElementById("composer-input");
    this.saveCurrentDialogSnapshot();
    vscode.setState({
      composerDraft: composer?.value || "",
      expandedAccordions: this.state?.expandedAccordions || {},
      surface: this.surface,
      currentDialogId: this.currentDialogId,
      dialogHistory: this.dialogHistory
    });
  }
  restoreState() {
    const saved = vscode.getState();
    if (saved?.surface === "chat" || saved?.surface === "inspector" || saved?.surface === "impact" || saved?.surface === "settings") {
      this.surface = saved.surface;
    }
    if (Array.isArray(saved?.dialogHistory)) {
      this.dialogHistory = saved.dialogHistory.filter((dialog) => dialog?.id && Array.isArray(dialog.messages)).slice(0, 30);
    }
    if (typeof saved?.currentDialogId === "string") {
      this.currentDialogId = saved.currentDialogId;
    }
    const currentDialog = this.dialogHistory.find((dialog) => dialog.id === this.currentDialogId);
    if (currentDialog) {
      this.messages = new Map(
        currentDialog.messages.map((message) => [message.id, { ...message }])
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
  restoreComposerDraft() {
    const composer = document.getElementById("composer-input");
    const saved = vscode.getState();
    if (composer && saved?.composerDraft) {
      composer.value = saved.composerDraft;
      resizeComposerToFit(composer);
    }
  }
  scrollToBottom() {
    const viewport = document.querySelector(".conversation-viewport");
    if (viewport) {
      setTimeout(() => {
        viewport.scrollTop = viewport.scrollHeight;
      }, 0);
    }
  }
  showToast(message, level) {
    const toast = document.createElement("div");
    toast.className = `toast ${level}`;
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.classList.add("show"), 10);
    setTimeout(() => {
      toast.classList.remove("show");
      setTimeout(() => toast.remove(), 250);
    }, 3e3);
  }
  postMessage(message) {
    vscode.postMessage(message);
  }
};
bootWebview(() => new MainSurface());
//# sourceMappingURL=main.js.map
