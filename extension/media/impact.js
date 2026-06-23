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
  function renderImpactWorkspace(impact, symbol, sourceLabel = "live graph", options = {}) {
    const model = buildImpactModel(impact);
    const depth = clampDepth(options.depth ?? impact.max_depth ?? 3, options);
    return `
    ${renderSymbolSummaryCard({
      symbol,
      filePath: impact.file_path || "unknown",
      uid: impact.symbol_uid || symbol,
      affectedCount: impact.affected_count || impact.affected_symbols?.length || 0,
      fileCount: impact.affected_file_count || impact.affected_files?.length || 0,
      maxDepth: impact.max_depth || 0,
      sourceLabel
    })}
    ${renderImpactDepthControl(depth, options)}
    ${renderImpactSummary(model)}
    ${renderFocusGraph(symbol, model.items)}
    ${renderActionButtonRow()}
    <div class="impact-groups">
      ${renderImpactZone("Direct Impact", model.direct, "No direct callers or first-hop consumers returned.", true)}
      ${renderImpactZone("Architectural Reach", model.reach, "No hook, event, config, data, or API reach returned.", true)}
      ${renderImpactZone("Hidden Risks", model.risks, "No cross-repo or coverage risks returned.", model.risks.length > 0)}
      ${renderFilesGroup(impact.affected_files || [], false, "Dependencies")}
    </div>
    <div class="impact-legend">
      <span><span class="legend-dot high"></span> high</span>
      <span><span class="legend-dot medium"></span> medium</span>
      <span><span class="legend-dot low"></span> low</span>
      <span><span class="legend-dot type"></span> focus walk</span>
    </div>
  `;
  }
  function renderImpactDepthControl(depth, options) {
    const minDepth = options.minDepth ?? 1;
    const maxDepth = options.maxDepth ?? 4;
    return `
    <div class="impact-depth-control">
      <label for="impact-depth-slider">Depth</label>
      <input
        id="impact-depth-slider"
        type="range"
        min="${minDepth}"
        max="${maxDepth}"
        step="1"
        value="${depth}"
        data-impact-depth
        aria-label="Impact depth"
      />
      <output for="impact-depth-slider">d${depth}</output>
    </div>
  `;
  }
  function clampDepth(depth, options) {
    const minDepth = options.minDepth ?? 1;
    const maxDepth = options.maxDepth ?? 4;
    if (!Number.isFinite(depth)) return 3;
    return Math.max(minDepth, Math.min(maxDepth, Math.round(depth)));
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
  function buildImpactModel(impact) {
    const symbols = impact.affected_symbols || [];
    const items = symbols.map(toImpactItem);
    const affectedFiles = Array.from(new Set((impact.affected_files || []).filter(Boolean)));
    const sourceFile = impact.file_path || "";
    const hasTests = [...affectedFiles, ...items.map((item) => item.filePath), sourceFile].some(isTestFile);
    if (!hasTests && (items.length > 0 || affectedFiles.length > 0)) {
      items.push({
        source: {},
        symbolName: "No returned test coverage",
        filePath: sourceFile || "workspace",
        relation: "coverage_gap",
        category: "coverage",
        zone: "risk",
        severity: "high",
        utilityScore: 0.93,
        line: 1,
        synthetic: true
      });
    }
    items.sort((a, b) => b.utilityScore - a.utilityScore);
    const direct = items.filter((item) => item.zone === "direct");
    const reach = items.filter((item) => item.zone === "reach");
    const risks = items.filter((item) => item.zone === "risk");
    return {
      items,
      direct,
      reach,
      risks,
      summary: {
        endpoints: items.filter((item) => item.category === "api").length,
        hooks: items.filter((item) => item.category === "event").length,
        tests: items.filter((item) => item.category === "test").length,
        high: items.filter((item) => item.severity === "high").length,
        medium: items.filter((item) => item.severity === "medium").length,
        low: items.filter((item) => item.severity === "low").length,
        files: affectedFiles.length
      }
    };
  }
  function toImpactItem(sym) {
    const filePath = stringField(sym, "file_path", "path", "source_file") || "unknown";
    const symbolName = stringField(sym, "symbol", "name", "title") || "unknown";
    const relation = stringField(sym, "relation", "direction", "edge_type", "kind", "role") || "affected";
    const depth = numberField(sym, "depth", "distance", "hops");
    const rawScore = numberField(sym, "utility_score", "relevance_score", "score");
    const category = classifyCategory(sym, filePath, relation);
    const severity = classifySeverity(category, depth, filePath);
    const zone = classifyZone(category, depth, filePath);
    return {
      source: sym,
      symbolName,
      filePath,
      relation,
      category,
      zone,
      severity,
      utilityScore: rawScore ?? fallbackUtility(severity, category, depth),
      depth,
      line: lineFromSymbol(sym)
    };
  }
  function classifyCategory(sym, filePath, relation) {
    const text = [
      relation,
      stringField(sym, "role", "edge_role", "edge_kind", "kind", "type"),
      arrayField(sym, "provenance").join(" "),
      filePath
    ].join(" ").toLowerCase();
    if (/\b(test|spec|fixture)\b|(^|[/.])(tests?|specs?)([/.]|$)/.test(text)) return "test";
    if (/\b(hook|hook_exec|event|event_pub|listener|subscriber|signal)\b/.test(text)) return "event";
    if (/\b(config|setting|settings|env|option|feature_flag)\b/.test(text)) return "config";
    if (/\b(model|schema|serializer|pydantic|sqlalchemy|orm|migration)\b/.test(text)) return "data";
    if (/\b(api|endpoint|route|router|controller|view)\b/.test(text)) return "api";
    if (/\b(repo|workspace|service|package|contract)\b/.test(text)) return "cross_repo";
    return "caller";
  }
  function classifySeverity(category, depth, filePath) {
    if (category === "test" || isDocFile(filePath)) return "low";
    if (category === "event" || category === "config") return "medium";
    if (category === "api" || category === "data" || category === "cross_repo") return "high";
    return depth === void 0 || depth <= 1 ? "high" : "medium";
  }
  function classifyZone(category, depth, filePath) {
    if (category === "test" || category === "cross_repo" || isDocFile(filePath)) return "risk";
    if (category === "event" || category === "config" || category === "data" || category === "api") {
      return "reach";
    }
    return depth === void 0 || depth <= 1 ? "direct" : "reach";
  }
  function fallbackUtility(severity, category, depth) {
    const base = severity === "high" ? 0.88 : severity === "medium" ? 0.66 : 0.42;
    const categoryBoost = category === "api" || category === "data" ? 0.08 : category === "event" ? 0.05 : 0;
    const depthPenalty = typeof depth === "number" ? Math.min(depth, 4) * 0.04 : 0;
    return Math.max(0.15, Math.min(0.99, base + categoryBoost - depthPenalty));
  }
  function renderImpactSummary(model) {
    return `
    <div class="impact-risk-summary" aria-label="Impact summary">
      <div class="impact-risk-title">
        <strong>Change touches ${model.summary.endpoints} endpoints, ${model.summary.hooks} hooks, ${model.summary.tests} tests</strong>
        <span>${model.summary.high} high / ${model.summary.medium} medium / ${model.summary.low} low</span>
      </div>
      <div class="impact-severity-strip">
        ${renderSeverityChip("High", model.summary.high, "high")}
        ${renderSeverityChip("Medium", model.summary.medium, "medium")}
        ${renderSeverityChip("Low", model.summary.low, "low")}
        ${renderSeverityChip("Files", model.summary.files, "neutral")}
      </div>
    </div>
  `;
  }
  function renderSeverityChip(label, count, tone) {
    return `
    <span class="impact-severity-chip ${escapeHtml(tone)}">
      <strong>${count}</strong>
      <span>${escapeHtml(label)}</span>
    </span>
  `;
  }
  function renderFocusGraph(symbol, items) {
    const focusItems = items.filter((item) => !item.synthetic).slice(0, 6);
    if (focusItems.length === 0) {
      return `
      <div class="impact-focus-card">
        <div class="impact-focus-center">${escapeHtml(symbol)}</div>
        <div class="impact-focus-empty">No high-utility neighbours returned.</div>
      </div>
    `;
    }
    return `
    <div class="impact-focus-card">
      <div class="impact-focus-center" title="${escapeHtml(symbol)}">${escapeHtml(symbol)}</div>
      <div class="impact-focus-grid">
        ${focusItems.map(renderFocusNode).join("")}
      </div>
    </div>
  `;
  }
  function renderFocusNode(item) {
    return `
    <button
      type="button"
      class="impact-focus-node ${item.severity}"
      data-action="openFile"
      data-file-path="${escapeHtml(item.filePath)}"
      data-line="${item.line}"
      title="Open ${escapeHtml(item.symbolName)}"
    >
      <span>${escapeHtml(item.symbolName)}</span>
      <small>${Math.round(item.utilityScore * 100)}%</small>
    </button>
  `;
  }
  function renderImpactZone(title, items, emptyText, expanded) {
    const visible = items.slice(0, 6);
    const overflow = items.slice(6);
    if (items.length === 0) {
      return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml(title)}</div>
        <div class="group-content empty">${escapeHtml(emptyText)}</div>
      </div>
    `;
    }
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml(title)}</strong>
        <span>(${items.length})</span>
      </button>
      <div class="group-content" ${expanded ? "" : "hidden"}>
        ${visible.map(renderImpactItemRow).join("")}
        ${overflow.length ? renderOverflowRows(overflow) : ""}
      </div>
    </div>
  `;
  }
  function renderOverflowRows(items) {
    return `
    <div class="impact-overflow" hidden>
      ${items.map(renderImpactItemRow).join("")}
    </div>
    <button class="impact-show-more" data-action="showMoreImpact">
      Show ${items.length} more
    </button>
  `;
  }
  function renderImpactItemRow(item) {
    const disabled = item.synthetic ? "disabled" : "";
    const title = item.synthetic ? item.symbolName : `Open ${item.symbolName}`;
    return `
    <button
      type="button"
      class="impact-row ${item.synthetic ? "impact-risk-row" : ""}"
      data-action="${item.synthetic ? "noop" : "openFile"}"
      data-file-path="${escapeHtml(item.filePath)}"
      data-line="${item.line}"
      title="${escapeHtml(title)}"
      ${disabled}
    >
      <span class="impact-chevron" aria-hidden="true">\u203A</span>
      <span class="impact-symbol">${escapeHtml(item.symbolName)}</span>
      <span class="impact-file">${escapeHtml(item.filePath)}</span>
      <span class="impact-tag ${item.severity}">${escapeHtml(item.severity)}</span>
      <span class="impact-tag indirect">${Math.round(item.utilityScore * 100)}%</span>
      <span class="impact-tag ${item.category === "event" || item.category === "config" ? "conditional" : "direct"}">${escapeHtml(item.category)}</span>
    </button>
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
  function stringField(sym, ...keys) {
    for (const key of keys) {
      const value = sym[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
    return "";
  }
  function numberField(sym, ...keys) {
    for (const key of keys) {
      const value = sym[key];
      if (typeof value === "number" && Number.isFinite(value)) return value;
    }
    return void 0;
  }
  function arrayField(sym, key) {
    const value = sym[key];
    if (!Array.isArray(value)) return [];
    return value.map((item) => String(item)).filter(Boolean);
  }
  function isTestFile(filePath) {
    return /(^|[/.])(tests?|specs?|__tests__)([/.]|$)|(\.|_)(test|spec)\.[jt]sx?$|test_.*\.py$|_test\.py$/.test(filePath.toLowerCase());
  }
  function isDocFile(filePath) {
    return /\.(md|mdx|rst|txt)$/i.test(filePath);
  }
  function renderFilesGroup(filePaths, expanded = false, title = "Files") {
    const uniquePaths = Array.from(new Set(filePaths.filter(Boolean)));
    if (uniquePaths.length === 0) {
      return renderAffectsGroup([], title, expanded);
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
        <strong>${escapeHtml(title)}</strong>
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
      this.currentDepth = 3;
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
            this.currentDepth = this.clampDepth(message.impact?.max_depth || this.currentDepth);
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
              symbol: this.currentSymbol,
              maxDepth: this.currentDepth
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
          symbol,
          maxDepth: this.currentDepth
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
      root.innerHTML = `
      <div class="impact-container">
        ${renderImpactWorkspace(this.currentImpact, this.currentSymbol, "live graph", {
        depth: this.currentDepth
      })}
      </div>
    `;
      this.attachEventListeners();
    }
    attachEventListeners() {
      document.querySelectorAll("[data-file-path]").forEach((row) => {
        row.addEventListener("click", (e) => {
          const target = e.currentTarget;
          const filePath = target.getAttribute("data-file-path");
          const line = Number.parseInt(target.getAttribute("data-line") || "1", 10);
          if (filePath) {
            vscode.postMessage({
              type: "link.openFile",
              filePath,
              line: Number.isFinite(line) ? line : 1
            });
          }
        });
      });
      document.querySelectorAll(".impact-group-header").forEach((header) => {
        header.addEventListener("click", (e) => {
          const target = e.currentTarget;
          const group = target.closest(".impact-group");
          const content = group?.querySelector(".group-content");
          if (!group || !content) return;
          const expanded = target.getAttribute("aria-expanded") === "true";
          target.setAttribute("aria-expanded", String(!expanded));
          group.classList.toggle("expanded", !expanded);
          content.toggleAttribute("hidden", expanded);
        });
      });
      document.querySelectorAll('[data-action="showMoreImpact"]').forEach((button) => {
        button.addEventListener("click", (e) => {
          const target = e.currentTarget;
          const group = target.closest(".impact-group");
          const overflow = group?.querySelector(".impact-overflow");
          if (!overflow) return;
          overflow.removeAttribute("hidden");
          target.remove();
        });
      });
      document.querySelectorAll("[data-impact-depth]").forEach((slider) => {
        slider.addEventListener("input", (e) => {
          const target = e.currentTarget;
          const depth = this.clampDepth(Number(target.value));
          const output = target.closest(".impact-depth-control")?.querySelector("output");
          if (output) output.textContent = `d${depth}`;
        });
        slider.addEventListener("change", (e) => {
          const target = e.currentTarget;
          this.currentDepth = this.clampDepth(Number(target.value));
          if (this.currentSymbol) {
            vscode.postMessage({
              type: "action.showImpact",
              symbol: this.currentSymbol,
              maxDepth: this.currentDepth
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
    clampDepth(depth) {
      if (!Number.isFinite(depth)) return 3;
      return Math.max(1, Math.min(4, Math.round(depth)));
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new ImpactPanel());
  } else {
    new ImpactPanel();
  }
})();
//# sourceMappingURL=impact.js.map
