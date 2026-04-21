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
    <div class="impact-header">
      <div class="symbol-summary">
        <h2>${escapeHtml(symbolInfo.symbol)}</h2>
        <div class="summary-details">
          <span class="detail-item">
            <strong>File:</strong> ${escapeHtml(symbolInfo.filePath)}
          </span>
          <span class="detail-item">
            <strong>UID:</strong> <code>${escapeHtml(symbolInfo.uid)}</code>
          </span>
        </div>
      </div>
    </div>
  `;
  }
  function renderAffectsGroup(affectedSymbols) {
    if (affectedSymbols.length === 0) {
      return `
      <div class="impact-group">
        <div class="group-header">\u{1F4C4} Affects</div>
        <div class="group-content empty">
          No affected symbols found.
        </div>
      </div>
    `;
    }
    const rows = affectedSymbols.map((sym) => {
      const filePath = sym.file_path || "unknown";
      const symbolName = sym.symbol || "unknown";
      const score = sym.relevance_score;
      const isDirty = sym.is_dirty;
      return `
        <div class="impact-row" data-file-path="${escapeHtml(filePath)}">
          <div class="impact-row-main">
            <span class="symbol-name">${escapeHtml(symbolName)}</span>
            <span class="file-name">${escapeHtml(filePath)}</span>
          </div>
          <div class="impact-row-meta">
            ${score ? `<span class="score">${(score * 100).toFixed(0)}%</span>` : ""}
            ${isDirty ? '<span class="dirty-badge">\u{1F534} Unsaved</span>' : ""}
          </div>
        </div>
      `;
    }).join("");
    return `
    <div class="impact-group">
      <div class="group-header">\u{1F4C4} Affects (${affectedSymbols.length})</div>
      <div class="group-content">
        ${rows}
      </div>
    </div>
  `;
  }
  function renderPlaceholderGroup(title, message) {
    return `
    <div class="impact-group">
      <div class="group-header">${escapeHtml(title)}</div>
      <div class="group-content placeholder">
        <p>${escapeHtml(message)}</p>
      </div>
    </div>
  `;
  }
  function renderActionButtonRow() {
    return `
    <div class="impact-actions">
      <button class="action-button" data-action="ask-followup">
        \u{1F4AC} Ask Follow-up
      </button>
      <button class="action-button" data-action="ask-impact">
        \u{1F504} Refresh Impact
      </button>
    </div>
  `;
  }

  // src/webview/impact.ts
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
              filePath
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
