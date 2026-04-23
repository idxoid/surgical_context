"use strict";
(() => {
  // src/webview/shared/impactLayout.ts
  function escapeHtml(text) {
    const map = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    };
    return text.replace(/[&<>"']/g, (m) => map[m]);
  }
  function renderSymbolSummaryCard(symbolInfo) {
    return `
    <div class="impact-symbol-card">
      <div class="impact-symbol-title">
        <span class="impact-info-icon" aria-hidden="true">i</span>
        <strong>${escapeHtml(symbolInfo.symbol)}</strong>
      </div>
      <div class="impact-symbol-meta">
        <span>Method</span>
        <span>${escapeHtml(symbolInfo.filePath)}</span>
        <code>${escapeHtml(symbolInfo.uid)}</code>
      </div>
    </div>
  `;
  }
  function renderAffectsGroup(affectedSymbols, title = "Affects", expanded = true) {
    if (affectedSymbols.length === 0) {
      return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml(title)}</div>
        <div class="group-content empty">
          No related symbols found.
        </div>
      </div>
    `;
    }
    const rows = affectedSymbols.map((sym) => {
      const filePath = sym.file_path || "unknown";
      const symbolName = sym.symbol || sym.name || "unknown";
      const score = sym.relevance_score;
      const isDirty = sym.is_dirty;
      const relation = sym.relation || sym.direction || "related";
      const depth = typeof sym.depth === "number" ? `d${sym.depth}` : "";
      return `
        <div class="impact-row" data-file-path="${escapeHtml(filePath)}">
          <span class="impact-chevron" aria-hidden="true">\u203A</span>
          <span class="impact-symbol">${escapeHtml(symbolName)}</span>
          <span class="impact-file">${escapeHtml(filePath)}</span>
          <span class="impact-tag direct">${escapeHtml(depth || relation)}</span>
          ${score ? `<span class="impact-tag indirect">${(score * 100).toFixed(0)}%</span>` : ""}
          ${isDirty ? '<span class="impact-tag conditional">dirty</span>' : ""}
        </div>
      `;
    }).join("");
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml(title)}</strong>
        <span>(${affectedSymbols.length})</span>
      </button>
      <div class="group-content" ${expanded ? "" : "hidden"}>
        ${rows}
      </div>
    </div>
  `;
  }
  function renderPlaceholderGroup(title, message, count, expanded = false) {
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml(title)}</strong>
        ${count !== void 0 ? `<span>(${count})</span>` : ""}
      </button>
      <div class="group-content placeholder" ${expanded ? "" : "hidden"}>
        <p>${escapeHtml(message)}</p>
        ${expanded ? `
              <div class="impact-row static">
                <span class="impact-chevron" aria-hidden="true">\u203A</span>
                <span class="impact-symbol">SymbolResolver.resolve()</span>
                <span class="impact-file">packages/core/src/symbolResolver.ts:87</span>
                <span class="impact-tag direct">direct</span>
              </div>
              <div class="impact-row static">
                <span class="impact-chevron" aria-hidden="true">\u203A</span>
                <span class="impact-symbol">Graph.getNeighbors()</span>
                <span class="impact-file">packages/core/src/graphBuilder.ts:142</span>
                <span class="impact-tag direct">direct</span>
              </div>
            ` : ""}
      </div>
    </div>
  `;
  }
  function renderActionButtonRow() {
    return `
    <div class="impact-actions">
      <button class="secondary-action" data-action="open-related-files">
        Open related files
      </button>
      <button class="secondary-action" data-action="ask-followup">
        Ask follow-up
      </button>
      <button class="secondary-action" data-action="create-refactor-plan">
        Create refactor plan
      </button>
    </div>
  `;
  }

  // src/webview/impact.ts
  var vscode = acquireVsCodeApi();
  var ImpactPanel = class {
    constructor() {
      this.currentSymbol = null;
      this.currentImpact = null;
      this.isLoading = false;
      this.initializeMessageListener();
      this.initializeUI();
    }
    initializeMessageListener() {
      window.addEventListener("message", (event) => {
        const message = event.data;
        switch (message.type) {
          case "impact.loading":
            this.onLoading();
            break;
          case "impact.loaded":
            this.currentSymbol = message.symbol || null;
            this.currentImpact = message.impact || null;
            this.render();
            break;
          case "impact.loadFailed":
            this.onError(message.error);
            break;
          case "workspace.updated":
            this.onWorkspaceUpdated(message.symbol);
            break;
        }
      });
    }
    initializeUI() {
      const askBtn = document.querySelector('[data-action="ask-impact"]');
      if (askBtn) {
        askBtn.addEventListener("click", () => {
          if (this.currentSymbol) {
            vscode.postMessage({
              type: "action.showImpact",
              symbol: this.currentSymbol
            });
          }
        });
      }
    }
    onLoading() {
      this.isLoading = true;
      this.render();
    }
    onError(error) {
      this.isLoading = false;
      const root = document.getElementById("root");
      if (root) {
        root.innerHTML = `
        <div class="impact-error">
          <p>Failed to load impact: ${escapeHtml(error)}</p>
        </div>
      `;
      }
    }
    onWorkspaceUpdated(symbol) {
      if (symbol && symbol !== this.currentSymbol) {
        this.currentSymbol = symbol;
        vscode.postMessage({
          type: "action.showImpact",
          symbol
        });
      }
    }
    render() {
      const root = document.getElementById("root");
      if (!root) return;
      if (this.isLoading) {
        root.innerHTML = `
        <div class="impact-loading">
          <p>Loading impact analysis...</p>
        </div>
      `;
        return;
      }
      if (!this.currentSymbol || !this.currentImpact) {
        root.innerHTML = `
        <div class="impact-empty">
          <p>Select a symbol to see its impact.</p>
        </div>
      `;
        return;
      }
      const summaryCard = renderSymbolSummaryCard({
        symbol: this.currentSymbol,
        filePath: this.currentImpact.file_path || "unknown",
        uid: this.currentImpact.symbol_uid || this.currentSymbol
      });
      const affectsGroup = renderAffectsGroup(this.currentImpact.affected_symbols || []);
      const callsGroup = renderPlaceholderGroup("Calls", "Calls are coming in Phase 6");
      const calledByGroup = renderPlaceholderGroup("Called By", "Called By information is coming in Phase 6");
      const dependsOnGroup = renderPlaceholderGroup("Depends On", "Dependency analysis is coming in Phase 6");
      const docsCoveringGroup = renderPlaceholderGroup("Docs Covering", "Documentation linking is coming in Phase 6");
      const actionButtons = renderActionButtonRow();
      root.innerHTML = `
      <div class="impact-container">
        ${summaryCard}
        <div class="impact-groups">
          ${affectsGroup}
          ${callsGroup}
          ${calledByGroup}
          ${dependsOnGroup}
          ${docsCoveringGroup}
        </div>
        ${actionButtons}
      </div>
    `;
      this.attachEventListeners();
    }
    attachEventListeners() {
      document.querySelectorAll("[data-file-path]").forEach((row) => {
        row.addEventListener("click", (e) => {
          const filePath = e.currentTarget.getAttribute("data-file-path");
          if (filePath) {
            vscode.postMessage({
              type: "link.openFile",
              filePath,
              line: 1
            });
          }
        });
      });
      const askFollowUpBtn = document.querySelector('[data-action="ask-followup"]');
      if (askFollowUpBtn && this.currentSymbol) {
        askFollowUpBtn.addEventListener("click", () => {
          vscode.postMessage({
            type: "action.openChat",
            prefillSymbol: this.currentSymbol
          });
        });
      }
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new ImpactPanel());
  } else {
    new ImpactPanel();
  }
})();
//# sourceMappingURL=impact.js.map
