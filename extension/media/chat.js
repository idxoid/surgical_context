"use strict";
(() => {
  // src/webview/shared/layout.ts
  function escapeHtml(text) {
    const map = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    };
    return text.replace(/[&<>"']/g, (char) => map[char]);
  }
  function renderMessageCard(message, selectedRequestId) {
    const timestamp = new Date(message.timestamp).toLocaleTimeString();
    const isSelected = Boolean(message.requestId && selectedRequestId === message.requestId);
    const isSelectablePrompt = message.type === "user" && Boolean(message.requestId);
    const baseClass = `message-card ${message.type}${isSelected ? " selected" : ""}${isSelectablePrompt ? " selectable" : ""}`;
    const statusClass = message.status ? ` status-${message.status}` : "";
    const requestAttrs = message.requestId ? ` data-request-id="${escapeHtml(message.requestId)}"` : "";
    const selectionAttrs = isSelectablePrompt ? ` data-action="selectPrompt" role="button" tabindex="0" aria-pressed="${isSelected}"` : "";
    if (message.type === "user") {
      return `
      <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}${selectionAttrs}>
        <div class="message-header">
          <span class="message-role">You</span>
          <span class="message-time">${timestamp}</span>
        </div>
        <div class="message-content">${escapeHtml(message.content)}</div>
      </article>
    `;
    }
    let content = `
    <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}>
      <div class="message-header">
        <span class="message-role">
          <span class="message-icon" aria-hidden="true">\u2726</span>
          Surgical Context
          ${message.status === "streaming" ? '<span class="live-dot"></span><span class="message-muted">Streaming answer...</span>' : ""}
        </span>
        <span class="message-time">${timestamp}</span>
      </div>
      <div class="message-content">${escapeHtml(message.content)}</div>
  `;
    if (message.error) {
      content += `<div class="message-error">Error: ${escapeHtml(message.error)}</div>`;
    }
    if (message.status === "done") {
      content += `
      <div class="message-actions">
        <button class="icon-button" data-action="feedback" data-rating="up" title="Helpful" aria-label="Helpful">+</button>
        <button class="icon-button" data-action="feedback" data-rating="down" title="Not helpful" aria-label="Not helpful">-</button>
        <button class="icon-button" data-action="copy" title="Copy response" aria-label="Copy response">Copy</button>
      </div>
    `;
    }
    content += "</article>";
    return content;
  }
  function renderAccordion(id, title, content, expanded = false) {
    return `
    <div class="accordion" data-accordion="${id}">
      <button id="${id}-header" class="accordion-header" aria-expanded="${expanded}" aria-controls="${id}-content" role="button">
        <span class="accordion-chevron" aria-hidden="true">\u203A</span>
        <span class="accordion-title">${escapeHtml(title)}</span>
      </button>
      <div id="${id}-content" class="accordion-content ${expanded ? "expanded" : ""}" ${expanded ? "" : "hidden"} role="region" aria-labelledby="${id}-header">
        ${content}
      </div>
    </div>
  `;
  }
  function renderEnvironmentAccordion(state, expanded = false) {
    const content = `
    <div class="accordion-row">
      <div class="accordion-label">Workspace</div>
      <div class="accordion-value">${escapeHtml(state.workspace)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Cloud</div>
      <div class="accordion-value">${escapeHtml(state.cloud)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Mode</div>
      <div class="accordion-value">${escapeHtml(state.mode)}</div>
    </div>
    ${state.symbol ? `<div class="accordion-row">
      <div class="accordion-label">Symbol</div>
      <div class="accordion-value">${escapeHtml(state.symbol)}</div>
    </div>` : ""}
  `;
    return renderAccordion("environment", "Environment", content, expanded);
  }
  function renderContextSummaryAccordion(summary, expanded = false) {
    if (!summary) {
      return renderAccordion("contextSummary", "Context Summary", "Run an ask to populate this section.", expanded);
    }
    const content = `
    <div class="accordion-row">
      <div class="accordion-label">Primary</div>
      <div class="accordion-value">${escapeHtml(summary.primaryLabel)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Graph Symbols</div>
      <div class="accordion-value">${summary.graphCount}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Doc Chunks</div>
      <div class="accordion-value">${summary.docsCount}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Tokens</div>
      <div class="accordion-value">${escapeHtml(summary.tokenText)}</div>
    </div>
    <div class="accordion-chips">
      ${summary.chips.map((chip) => `<span class="chip">${escapeHtml(chip)}</span>`).join("")}
    </div>
  `;
    return renderAccordion("contextSummary", "Context Summary", content, expanded);
  }
  function renderAdvancedInfoAccordion(info, expanded = false) {
    if (!info) {
      return renderAccordion("advancedInfo", "Advanced Info", "Run an ask to populate this section.", expanded);
    }
    const content = `
    <div class="accordion-row">
      <div class="accordion-label">Intent</div>
      <div class="accordion-value">${escapeHtml(info.intent)}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Tiers Used</div>
      <div class="accordion-value">${info.tiersUsed.map(escapeHtml).join(", ")}</div>
    </div>
    <div class="accordion-row">
      <div class="accordion-label">Has Unsaved Changes</div>
      <div class="accordion-value">${info.isDirty ? "Yes" : "No"}</div>
    </div>
  `;
    return renderAccordion("advancedInfo", "Advanced Info", content, expanded);
  }
  function renderStatusChips(state) {
    return `
    <div class="status-chip-row">
      <span class="status-chip dirty">${state.isDirty ? "dirty-aware" : "clean"}</span>
      ${state.graphFirst ? '<span class="status-chip graph">graph-first</span>' : ""}
      ${state.docLinked ? '<span class="status-chip docs">doc-linked</span>' : ""}
      <span class="status-spacer"></span>
      <button class="status-info" title="Context provenance and privacy state" aria-label="Context provenance and privacy state">i</button>
    </div>
  `;
  }
  function renderActionBar(active = "chat") {
    return `
    <div class="action-bar">
      <button class="action-btn ${active === "chat" ? "primary" : ""}" data-action="openChat" title="Ask about current symbol">
        <span aria-hidden="true">\u2726</span> Ask
      </button>
      <button class="action-btn ${active === "inspector" ? "primary" : ""}" data-action="openInspector" title="Inspect context">
        <span aria-hidden="true">\u25CB</span> Inspect Context
      </button>
      <button class="action-btn ${active === "impact" ? "primary" : ""}" data-action="showImpact" title="Show impact">
        <span aria-hidden="true">\u2318</span> Impact
      </button>
      <button class="action-btn" data-action="search" title="Search workspace">
        <span aria-hidden="true">\u2315</span> Search
      </button>
    </div>
  `;
  }
  function renderComposerDock() {
    return `
    <div class="composer-dock">
      <textarea
        id="composer-input"
        class="composer-textarea"
        placeholder="Ask about this symbol, its behavior, dependencies..."
        aria-label="Message composer"
        aria-describedby="composer-help"
        rows="1"
      ></textarea>
      <button id="composer-send" class="composer-send-btn" title="Send (Enter)" aria-label="Send message">
        <span class="composer-send-icon" aria-hidden="true">\u27A4</span>
      </button>
      <div id="composer-help" class="sr-only">
        Press Enter to send. Press Shift+Enter for a new line. Press Cmd+L to focus composer.
      </div>
    </div>
  `;
  }
  function resizeComposerToFit(textarea, maxHeightPx = 220) {
    textarea.style.height = "auto";
    const scrollHeight = textarea.scrollHeight;
    const newHeight = Math.min(scrollHeight, maxHeightPx);
    textarea.style.height = `${newHeight}px`;
    textarea.style.overflow = scrollHeight > maxHeightPx ? "auto" : "hidden";
  }

  // src/webview/chat.ts
  var vscode = acquireVsCodeApi();
  var ChatPanel = class {
    constructor() {
      this.state = null;
      this.messages = /* @__PURE__ */ new Map();
      this.currentStreamingRequestId = null;
      this.currentContextSummary = null;
      this.currentAbortController = null;
      this.initializeMessageListener();
      this.initializeUI();
      this.restoreState();
    }
    initializeMessageListener() {
      window.addEventListener("message", (event) => {
        const message = event.data;
        switch (message.type) {
          case "surface.init":
            this.state = message.state;
            this.render();
            break;
          case "chat.requestStarted":
            this.onRequestStarted(message.requestId, message.symbol);
            break;
          case "chat.streamChunk":
            this.onStreamChunk(message.requestId, message.chunk);
            break;
          case "chat.requestCompleted":
            this.onRequestCompleted(message.requestId, message.answer, message.context);
            this.currentContextSummary = null;
            break;
          case "chat.requestFailed":
            this.onRequestFailed(message.requestId, message.error);
            break;
          case "chat.requestStopped":
            this.onRequestStopped(message.requestId);
            break;
          case "chat.contextSummary":
            this.currentContextSummary = message.summary;
            break;
          case "workspace.updated":
            if (this.state) {
              this.state.workspace = {
                activeFile: message.activeFile,
                selectedSymbol: message.symbol,
                isDirty: message.isDirty
              };
              this.updateStatusChips();
            }
            break;
          case "backend.updated":
            if (this.state) {
              this.state.backend = {
                sidecarHealth: message.sidecarHealth,
                cloudStatus: message.cloudStatus
              };
              this.updateHeader();
            }
            break;
          case "toast.show":
            this.showToast(message.message, message.level);
            break;
        }
      });
    }
    initializeUI() {
      this.setupComposerListeners();
      this.setupAccordionListeners();
      this.setupActionBarListeners();
    }
    setupComposerListeners() {
      const composer = document.getElementById("composer-input");
      const sendBtn = document.getElementById("composer-send");
      if (!composer || !sendBtn) return;
      composer.addEventListener("input", () => {
        resizeComposerToFit(composer);
        this.persistState();
      });
      composer.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          this.askAboutSymbol();
        }
      });
      sendBtn.addEventListener("click", () => this.askAboutSymbol());
      document.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "l") {
          e.preventDefault();
          composer.focus();
        }
      });
    }
    setupAccordionListeners() {
      document.querySelectorAll(".accordion-header").forEach((header) => {
        header.addEventListener("click", () => {
          const accordionGroup = header.parentElement;
          if (!accordionGroup) return;
          const id = accordionGroup.getAttribute("data-accordion");
          const content = accordionGroup.querySelector(".accordion-content");
          const isExpanded = header.getAttribute("aria-expanded") === "true";
          if (content) {
            if (isExpanded) {
              header.setAttribute("aria-expanded", "false");
              content.setAttribute("hidden", "");
              content.classList.remove("expanded");
            } else {
              header.setAttribute("aria-expanded", "true");
              content.removeAttribute("hidden");
              content.classList.add("expanded");
            }
          }
          if (id) {
            this.postMessage({
              type: "accordion.toggled",
              id,
              expanded: !isExpanded
            });
            this.persistState();
          }
        });
      });
    }
    setupActionBarListeners() {
      document.querySelectorAll("[data-action]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          const action = e.currentTarget.getAttribute("data-action");
          switch (action) {
            case "openChat":
              document.getElementById("composer-input")?.focus();
              break;
            case "ask":
              this.askAboutSymbol();
              break;
            case "openInspector":
              this.postMessage({ type: "action.openInspector" });
              break;
            case "showImpact":
              this.postMessage({ type: "action.showImpact" });
              break;
            case "search":
              this.showToast("Search coming soon", "info");
              break;
            case "feedback":
              const rating = e.currentTarget.getAttribute("data-rating");
              const messageId = e.currentTarget.closest(".message-card")?.id;
              if (messageId) {
                this.postMessage({ type: "feedback.submit", messageId, rating });
                this.showToast("Thanks for your feedback!", "info");
              }
              break;
            case "copy":
              const card = e.currentTarget.closest(".message-card");
              const content = card?.querySelector(".message-content")?.textContent;
              if (content) {
                navigator.clipboard.writeText(content).then(() => {
                  this.showToast("Copied to clipboard", "info");
                });
              }
              break;
          }
        });
      });
    }
    askAboutSymbol() {
      const composer = document.getElementById("composer-input");
      if (!composer || !composer.value.trim() || !this.state) return;
      const prompt = composer.value.trim();
      const symbol = this.state.workspace.selectedSymbol || void 0;
      composer.value = "";
      resizeComposerToFit(composer);
      this.postMessage({
        type: "chat.ask",
        prompt,
        symbol
      });
    }
    onRequestStarted(requestId, symbol) {
      this.currentStreamingRequestId = requestId;
      const userMsg = {
        id: `msg-${Date.now()}`,
        type: "user",
        content: document.getElementById("composer-input")?.value || "",
        timestamp: Date.now()
      };
      this.messages.set(userMsg.id, userMsg);
      const assistantMsg = {
        id: requestId,
        type: "assistant",
        content: "",
        timestamp: Date.now(),
        status: "streaming"
      };
      this.messages.set(requestId, assistantMsg);
      this.updateConversationView();
      this.scrollToBottom();
    }
    onStreamChunk(requestId, chunk) {
      if (this.currentStreamingRequestId !== requestId) return;
      const msg = this.messages.get(requestId);
      if (msg) {
        msg.content += chunk;
        msg.status = "streaming";
        this.updateConversationView();
        this.scrollToBottom();
      }
    }
    onRequestCompleted(requestId, answer, context) {
      if (this.currentStreamingRequestId !== requestId) return;
      this.currentStreamingRequestId = null;
      const msg = this.messages.get(requestId);
      if (msg) {
        msg.content = answer;
        msg.context = context;
        msg.status = "done";
        this.updateConversationView();
      }
      if (this.state?.expandedAccordions["contextSummary"] === false) {
        const header = document.querySelector('[data-accordion="contextSummary"] .accordion-header');
        if (header) {
          header.click();
        }
      }
    }
    onRequestFailed(requestId, error) {
      if (this.currentStreamingRequestId !== requestId) return;
      this.currentStreamingRequestId = null;
      const msg = this.messages.get(requestId);
      if (msg) {
        msg.status = "error";
        msg.error = error;
        this.updateConversationView();
      }
    }
    onRequestStopped(requestId) {
      const msg = this.messages.get(requestId);
      if (msg) {
        msg.status = "done";
        this.updateConversationView();
      }
      this.currentStreamingRequestId = null;
    }
    render() {
      const root = document.getElementById("root");
      if (!root || !this.state) return;
      const environmentAccordion = renderEnvironmentAccordion({
        workspace: this.state.workspace.activeFile || "none",
        cloud: this.state.backend.cloudStatus,
        mode: "surgical",
        symbol: this.state.workspace.selectedSymbol || void 0
      });
      const contextSummaryAccordion = renderContextSummaryAccordion(this.currentContextSummary || void 0);
      const advancedInfoAccordion = renderAdvancedInfoAccordion({
        intent: "ask",
        tiersUsed: ["code", "docs"],
        isDirty: this.state.workspace.isDirty
      });
      const statusChips = renderStatusChips({
        isDirty: this.state.workspace.isDirty,
        graphFirst: true,
        docLinked: true
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
    updateConversationView() {
      const viewport = document.getElementById("conversation");
      if (!viewport) return;
      const html = Array.from(this.messages.values()).map((msg) => `<div id="${msg.id}">${renderMessageCard(msg)}</div>`).join("");
      viewport.innerHTML = html;
      this.setupActionBarListeners();
    }
    updateHeader() {
      const indicator = document.querySelector(".health-indicator");
      if (indicator && this.state) {
        indicator.className = `health-indicator ${this.state.backend.sidecarHealth}`;
      }
    }
    updateStatusChips() {
      if (!this.state) return;
      const chipRow = document.querySelector(".status-chip-row");
      if (chipRow) {
        chipRow.outerHTML = renderStatusChips({
          isDirty: this.state.workspace.isDirty,
          graphFirst: true,
          docLinked: true
        });
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
      console.log(`[${level}] ${message}`);
      const toast = document.createElement("div");
      toast.className = `toast toast-${level}`;
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
      toast.textContent = message;
      const container = document.body || document.documentElement;
      container.appendChild(toast);
      setTimeout(() => toast.classList.add("show"), 10);
      setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => container.removeChild(toast), 300);
      }, 4e3);
    }
    persistState() {
      const composer = document.getElementById("composer-input");
      const state = {
        composerDraft: composer?.value || "",
        expandedAccordions: this.state?.expandedAccordions || {}
      };
      vscode.setState(state);
    }
    restoreState() {
      const saved = vscode.getState();
      if (saved?.expandedAccordions) {
        if (!this.state) {
          this.state = {
            expandedAccordions: saved.expandedAccordions,
            composerDraft: saved.composerDraft || "",
            workspace: { activeFile: null, selectedSymbol: null, isDirty: false },
            backend: { sidecarHealth: "degraded", cloudStatus: "offline" }
          };
        } else {
          this.state.expandedAccordions = saved.expandedAccordions;
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
    postMessage(message) {
      vscode.postMessage(message);
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new ChatPanel());
  } else {
    new ChatPanel();
  }
})();
//# sourceMappingURL=chat.js.map
