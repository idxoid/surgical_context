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
      <div class="impact-metrics" aria-label="Impact summary">
        ${renderMetric("Symbols", symbolInfo.affectedCount)}
        ${renderMetric("Files", symbolInfo.fileCount)}
        ${renderMetric("Depth", symbolInfo.maxDepth)}
        ${symbolInfo.sourceLabel ? `<span class="impact-source-chip">${escapeHtml(symbolInfo.sourceLabel)}</span>` : ""}
      </div>
    </div>
  `;
  }
  function renderMetric(label, value) {
    return `
    <span class="impact-metric">
      <strong>${Number.isFinite(value) ? value : 0}</strong>
      <span>${escapeHtml(label)}</span>
    </span>
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
      const line = lineFromSymbol(sym);
      const depthClass = typeof sym.depth === "number" && sym.depth <= 1 ? "direct" : "indirect";
      return `
        <button
          type="button"
          class="impact-row"
          data-action="openFile"
          data-file-path="${escapeHtml(filePath)}"
          data-line="${line}"
          title="Open ${escapeHtml(symbolName)}"
        >
          <span class="impact-chevron" aria-hidden="true">\u203A</span>
          <span class="impact-symbol">${escapeHtml(symbolName)}</span>
          <span class="impact-file">${escapeHtml(filePath)}</span>
          <span class="impact-tag ${depthClass}">${escapeHtml(depth || relation)}</span>
          ${score ? `<span class="impact-tag indirect">${(score * 100).toFixed(0)}%</span>` : ""}
          ${isDirty ? '<span class="impact-tag conditional">dirty</span>' : ""}
        </button>
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
  function lineFromSymbol(sym) {
    const explicit = sym.line || sym.start_line || sym.lineno;
    if (typeof explicit === "number" && Number.isFinite(explicit)) {
      return Math.max(1, explicit);
    }
    const range = sym.range;
    if (Array.isArray(range) && typeof range[0] === "number") {
      return Math.max(1, range[0]);
    }
    return 1;
  }
  function renderFilesGroup(filePaths, expanded = false) {
    const uniquePaths = Array.from(new Set(filePaths.filter(Boolean)));
    if (uniquePaths.length === 0) {
      return renderAffectsGroup([], "Files", expanded);
    }
    const rows = uniquePaths.map((filePath) => `
      <button
        type="button"
        class="impact-row impact-file-row"
        data-action="openFile"
        data-file-path="${escapeHtml(filePath)}"
        data-line="1"
        title="Open ${escapeHtml(filePath)}"
      >
        <span class="impact-chevron" aria-hidden="true">\u203A</span>
        <span class="impact-symbol">File</span>
        <span class="impact-file">${escapeHtml(filePath)}</span>
        <span class="impact-tag indirect">related</span>
      </button>
    `).join("");
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>Files</strong>
        <span>(${uniquePaths.length})</span>
      </button>
      <div class="group-content" ${expanded ? "" : "hidden"}>
        ${rows}
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
        uid: this.currentImpact.symbol_uid || this.currentSymbol,
        affectedCount: this.currentImpact.affected_count || this.currentImpact.affected_symbols?.length || 0,
        fileCount: this.currentImpact.affected_file_count || this.currentImpact.affected_files?.length || 0,
        maxDepth: this.currentImpact.max_depth || 0,
        sourceLabel: "live graph"
      });
      const affectsGroup = renderAffectsGroup(this.currentImpact.affected_symbols || []);
      const filesGroup = renderFilesGroup(this.currentImpact.affected_files || [], false);
      const actionButtons = renderActionButtonRow();
      root.innerHTML = `
      <div class="impact-container">
        ${summaryCard}
        ${actionButtons}
        <div class="impact-groups">
          ${affectsGroup}
          ${filesGroup}
        </div>
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
      const openFilesBtn = document.querySelector('[data-action="open-related-files"]');
      if (openFilesBtn && this.currentImpact?.affected_files?.length) {
        openFilesBtn.addEventListener("click", () => {
          vscode.postMessage({
            type: "impact.openFiles",
            filePaths: this.currentImpact?.affected_files || []
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
