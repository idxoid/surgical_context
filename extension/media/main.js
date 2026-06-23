"use strict";
(() => {
  // src/contextSummary.ts
  function buildContextSummary(context) {
    const tierTokens = context.metadata.tier_tokens || {};
    const totalTokens = Object.values(tierTokens).reduce((sum, value) => {
      return sum + (typeof value === "number" ? value : 0);
    }, 0);
    const askLevel = typeof context.budget?.ask_level === "string" ? context.budget.ask_level : "";
    const warningChips = fallbackWarningChips(context);
    return {
      primaryLabel: `${context.primary_source.symbol} in ${context.primary_source.file_path}`,
      graphCount: context.graph_context.length,
      docsCount: context.documentation.length,
      tokenText: `${totalTokens} tokens`,
      chips: [
        ...askLevel ? [`level:${askLevel}`] : [],
        ...warningChips,
        ...context.metadata.tiers_used || []
      ]
    };
  }
  function fallbackWarningChips(context) {
    const budget = context.budget || {};
    if (budget.fallback_reason !== "symbol_not_found" || typeof budget.ask_level !== "string") {
      return [];
    }
    return ["warning:symbol not found", `fallback:${budget.ask_level}`];
  }

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
    const isSelected = Boolean(message.requestId && selectedRequestId === message.requestId);
    const isSelectablePrompt = message.type === "user" && Boolean(message.requestId);
    const baseClass = `message-card ${message.type}${isSelected ? " selected" : ""}${isSelectablePrompt ? " selectable" : ""}`;
    const statusClass = message.status ? ` status-${message.status}` : "";
    const requestAttrs = message.requestId ? ` data-request-id="${escapeHtml(message.requestId)}"` : "";
    const selectionAttrs = isSelectablePrompt ? ` data-action="selectPrompt" role="button" tabindex="0" aria-pressed="${isSelected}"` : "";
    if (message.type === "user") {
      return `
      <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}${selectionAttrs} title="${escapeHtml(message.content)}">
        <div class="message-content">${escapeHtml(message.content)}</div>
        ${renderMessageFooter(message)}
      </article>
    `;
    }
    let content = `
    <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}>
      <div class="message-content">${escapeHtml(message.content)}</div>
  `;
    if (message.error) {
      content += `<div class="message-error">Error: ${escapeHtml(message.error)}</div>`;
    }
    content += renderMessageFooter(message);
    content += "</article>";
    return content;
  }
  function renderMessageFooter(message) {
    const time = formatMessageTime(message.timestamp);
    const route = formatModelRoute(message);
    const assistantFeedback = message.type === "assistant" && message.status === "done" ? `
        <button class="message-action-button" data-action="feedback" data-rating="up" title="Helpful" aria-label="Helpful">+</button>
        <button class="message-action-button" data-action="feedback" data-rating="down" title="Not helpful" aria-label="Not helpful">-</button>
      ` : "";
    return `
    <div class="message-footer">
      <time class="message-time" datetime="${escapeHtml(time.iso)}" title="${escapeHtml(time.title)}">${escapeHtml(time.label)}</time>
      ${route ? `<span class="message-route ${route.fallback ? "fallback" : ""}" title="${escapeHtml(route.title)}">${escapeHtml(route.label)}</span>` : ""}
      <div class="message-actions">
        ${assistantFeedback}
        <button class="message-action-button" data-action="copy" title="Copy message" aria-label="Copy message">
          <svg class="message-action-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <rect x="5" y="3" width="8" height="10" rx="1.5"></rect>
            <path d="M3 6.5V12a2 2 0 0 0 2 2h5.5"></path>
          </svg>
        </button>
      </div>
    </div>
  `;
  }
  function formatModelRoute(message) {
    if (message.type !== "assistant") {
      return null;
    }
    const route = message.context?.metadata?.assembly?.model_route;
    if (!route) {
      return null;
    }
    const provider = routeText(route.provider) || "unknown";
    const model = routeText(route.model);
    const preference = routeText(route.preference);
    const reason = routeText(route.reason);
    const degraded = Boolean(route.degraded);
    const fallback = degraded || reason.includes("fallback") || reason.includes("unavailable");
    const reasonText = routeReasonLabel(reason);
    const labelParts = [provider, model].filter(Boolean);
    const label = `${labelParts.join(" / ") || provider}${fallback ? " \xB7 fallback" : ""}`;
    const titleParts = [
      `Answered by ${labelParts.join(" / ") || provider}`,
      preference ? `Preference: ${preference}` : "",
      reasonText,
      degraded ? "Response was degraded." : ""
    ].filter(Boolean);
    return {
      label,
      title: titleParts.join(" | "),
      fallback
    };
  }
  function routeText(value) {
    return typeof value === "string" ? value.trim() : "";
  }
  function routeReasonLabel(reason) {
    switch (reason) {
      case "claude_unavailable_fallback":
        return "Auto wanted Claude, but Anthropic credentials/client were unavailable; Ollama answered.";
      case "claude_error_fallback":
        return "Claude failed during the request; Ollama answered.";
      case "router_selected_claude":
        return "Router selected Claude.";
      case "router_selected_ollama":
        return "Router selected Ollama.";
      case "llm_unreachable_context_only":
        return "LLM was unreachable; context-only degraded response.";
      default:
        return reason ? `Route reason: ${reason}` : "";
    }
  }
  function formatMessageTime(timestamp) {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return { label: "", title: "", iso: "" };
    }
    return {
      label: date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      title: date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }),
      iso: date.toISOString()
    };
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
      ${summary.chips.map(renderContextChip).join("")}
    </div>
  `;
    return renderAccordion("contextSummary", "Context Summary", content, expanded);
  }
  function renderContextChip(chip) {
    const className = chip.startsWith("warning:") ? "chip warning" : "chip";
    const label = chip.startsWith("warning:") ? chip.slice("warning:".length) : chip;
    return `<span class="${className}">${escapeHtml(label)}</span>`;
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

  // src/webview/shared/impactLayout.ts
  function escapeHtml2(text) {
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
        <strong>${escapeHtml2(symbolInfo.symbol)}</strong>
      </div>
      <div class="impact-symbol-meta">
        <span>Method</span>
        <span>${escapeHtml2(symbolInfo.filePath)}</span>
        <code>${escapeHtml2(symbolInfo.uid)}</code>
      </div>
      <div class="impact-metrics" aria-label="Impact summary">
        ${renderMetric("Symbols", symbolInfo.affectedCount)}
        ${renderMetric("Files", symbolInfo.fileCount)}
        ${renderMetric("Depth", symbolInfo.maxDepth)}
        ${symbolInfo.sourceLabel ? `<span class="impact-source-chip">${escapeHtml2(symbolInfo.sourceLabel)}</span>` : ""}
      </div>
    </div>
  `;
  }
  function renderMetric(label, value) {
    return `
    <span class="impact-metric">
      <strong>${Number.isFinite(value) ? value : 0}</strong>
      <span>${escapeHtml2(label)}</span>
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
    <span class="impact-severity-chip ${escapeHtml2(tone)}">
      <strong>${count}</strong>
      <span>${escapeHtml2(label)}</span>
    </span>
  `;
  }
  function renderFocusGraph(symbol, items) {
    const focusItems = items.filter((item) => !item.synthetic).slice(0, 6);
    if (focusItems.length === 0) {
      return `
      <div class="impact-focus-card">
        <div class="impact-focus-center">${escapeHtml2(symbol)}</div>
        <div class="impact-focus-empty">No high-utility neighbours returned.</div>
      </div>
    `;
    }
    return `
    <div class="impact-focus-card">
      <div class="impact-focus-center" title="${escapeHtml2(symbol)}">${escapeHtml2(symbol)}</div>
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
      data-file-path="${escapeHtml2(item.filePath)}"
      data-line="${item.line}"
      title="Open ${escapeHtml2(item.symbolName)}"
    >
      <span>${escapeHtml2(item.symbolName)}</span>
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
        <div class="group-header">${escapeHtml2(title)}</div>
        <div class="group-content empty">${escapeHtml2(emptyText)}</div>
      </div>
    `;
    }
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml2(title)}</strong>
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
      data-file-path="${escapeHtml2(item.filePath)}"
      data-line="${item.line}"
      title="${escapeHtml2(title)}"
      ${disabled}
    >
      <span class="impact-chevron" aria-hidden="true">\u203A</span>
      <span class="impact-symbol">${escapeHtml2(item.symbolName)}</span>
      <span class="impact-file">${escapeHtml2(item.filePath)}</span>
      <span class="impact-tag ${item.severity}">${escapeHtml2(item.severity)}</span>
      <span class="impact-tag indirect">${Math.round(item.utilityScore * 100)}%</span>
      <span class="impact-tag ${item.category === "event" || item.category === "config" ? "conditional" : "direct"}">${escapeHtml2(item.category)}</span>
    </button>
  `;
  }
  function renderAffectsGroup(affectedSymbols, title = "Affects", expanded = true) {
    if (affectedSymbols.length === 0) {
      return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml2(title)}</div>
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
          data-file-path="${escapeHtml2(filePath)}"
          data-line="${line}"
          title="Open ${escapeHtml2(symbolName)}"
        >
          <span class="impact-chevron" aria-hidden="true">\u203A</span>
          <span class="impact-symbol">${escapeHtml2(symbolName)}</span>
          <span class="impact-file">${escapeHtml2(filePath)}</span>
          <span class="impact-tag ${depthClass}">${escapeHtml2(depth || relation)}</span>
          ${score ? `<span class="impact-tag indirect">${(score * 100).toFixed(0)}%</span>` : ""}
          ${isDirty ? '<span class="impact-tag conditional">dirty</span>' : ""}
        </button>
      `;
    }).join("");
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml2(title)}</strong>
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
        data-file-path="${escapeHtml2(filePath)}"
        data-line="1"
        title="Open ${escapeHtml2(filePath)}"
      >
        <span class="impact-chevron" aria-hidden="true">\u203A</span>
        <span class="impact-symbol">File</span>
        <span class="impact-file">${escapeHtml2(filePath)}</span>
        <span class="impact-tag indirect">related</span>
      </button>
    `).join("");
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml2(title)}</strong>
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

  // src/webview/shared/inspectorLayout.ts
  function escapeHtml3(text) {
    const map = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    };
    return text.replace(/[&<>"']/g, (m) => map[m]);
  }
  function renderPrimarySourceTab(context) {
    const primary = context.primary_source;
    if (!primary) {
      return '<div class="tab-content-empty">No primary source available</div>';
    }
    const symbolName = primary.symbol || "unknown";
    const filePath = primary.file_path || "unknown file";
    const isDirty = primary.is_dirty ? "\u{1F534} Unsaved" : "\u2713 Saved";
    const code = primary.code || "";
    return `
    <div class="primary-source-card">
      <div class="symbol-header">
        <h3>${escapeHtml3(symbolName)}</h3>
        <span class="dirty-badge">${isDirty}</span>
      </div>
      <div class="file-path">
        <strong>File:</strong> ${escapeHtml3(filePath)}
      </div>
      ${code ? `
        <div class="code-snippet">
          <pre><code>${escapeHtml3(code)}</code></pre>
        </div>
      ` : ""}
    </div>
  `;
  }
  function renderGraphContextTab(context) {
    const graphItems = context.graph_context || [];
    if (graphItems.length === 0) {
      return '<div class="tab-content-empty">No graph context available</div>';
    }
    const rows = graphItems.map((item) => `
      <tr class="context-row" data-file-path="${escapeHtml3(item.file_path)}">
        <td class="symbol-col">${escapeHtml3(item.symbol)}</td>
        <td class="relation-col">${escapeHtml3(item.relation || "")}</td>
        <td class="depth-col">${item.depth || 0}</td>
        <td class="score-col">${(item.relevance_score || 0).toFixed(2)}</td>
        <td class="dirty-col">${item.is_dirty ? "\u{1F534}" : "\u2713"}</td>
        <td class="file-col">${escapeHtml3(item.file_path)}</td>
      </tr>
    `).join("");
    return `
    <div class="graph-context-table">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Relation</th>
            <th>Depth</th>
            <th>Score</th>
            <th>Dirty</th>
            <th>File</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </div>
  `;
  }
  function renderDocumentationTab(context) {
    const docs = context.documentation || [];
    if (docs.length === 0) {
      return '<div class="tab-content-empty">No documentation available</div>';
    }
    const rows = docs.map((doc) => `
      <div class="doc-item">
        <div class="doc-header">
          <strong>Source:</strong> ${escapeHtml3(doc.source_file)}
          <span class="score">${(doc.score || 0).toFixed(2)}</span>
        </div>
        <div class="doc-content">
          ${escapeHtml3((doc.content || "").substring(0, 500))}${(doc.content || "").length > 500 ? "..." : ""}
        </div>
      </div>
    `).join("");
    return `
    <div class="documentation-list">
      ${rows}
    </div>
  `;
  }
  function renderPromptJsonTab(context) {
    const jsonStr = JSON.stringify(context, null, 2);
    return `
    <div class="json-viewer">
      <button class="copy-button" data-action="copy-json">Copy JSON</button>
      <pre><code>${escapeHtml3(jsonStr)}</code></pre>
    </div>
  `;
  }
  function renderTokenBreakdownTab(context) {
    const metadata = context.metadata || {};
    const tiersUsed = metadata.tiers_used || [];
    const tokensPrimary = metadata.tokens_primary || 0;
    const tokensGraph = metadata.tokens_graph || 0;
    const tokensDocs = metadata.tokens_docs || 0;
    const tokensTotal = tokensPrimary + tokensGraph + tokensDocs;
    const estimatedFull = tokensTotal * 3;
    const rows = [
      { tier: "Primary Code", tokens: tokensPrimary },
      { tier: "Graph Context", tokens: tokensGraph },
      { tier: "Documentation", tokens: tokensDocs }
    ].filter((r) => r.tokens > 0).map((r) => `
      <tr>
        <td>${escapeHtml3(r.tier)}</td>
        <td>${r.tokens}</td>
        <td>${(r.tokens / tokensTotal * 100).toFixed(1)}%</td>
      </tr>
    `).join("");
    return `
    <div class="token-breakdown">
      <div class="summary-cards">
        <div class="summary-card">
          <div class="label">Surgical Total</div>
          <div class="value">${tokensTotal}</div>
        </div>
        <div class="summary-card">
          <div class="label">Est. Full-Open</div>
          <div class="value">${estimatedFull}</div>
        </div>
        <div class="summary-card">
          <div class="label">Savings</div>
          <div class="value">${((1 - tokensTotal / estimatedFull) * 100).toFixed(0)}%</div>
        </div>
      </div>
      <table class="tier-table">
        <thead>
          <tr>
            <th>Tier</th>
            <th>Tokens</th>
            <th>% of Total</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </div>
  `;
  }
  function renderApiPayloadTab(context) {
    const primary = context.primary_source;
    const graphItems = context.graph_context || [];
    const docs = context.documentation || [];
    const systemPrompt = buildSystemPrompt(context);
    const apiRequest = {
      model: "claude-opus-4-7",
      max_tokens: 8096,
      system: systemPrompt,
      messages: [
        {
          role: "user",
          content: "(User query would appear here)"
        }
      ]
    };
    const metadata = {
      mode: context.mode,
      intent: context.intent,
      assembly_metadata: context.metadata?.assembly,
      tier_tokens: context.metadata?.tier_tokens,
      budget_info: context.budget
    };
    const jsonStr = JSON.stringify(
      {
        api_request: apiRequest,
        context_metadata: metadata,
        assembly_summary: {
          primary_symbol: primary?.symbol,
          graph_context_count: graphItems.length,
          documentation_count: docs.length,
          total_tokens: (context.metadata?.tokens_primary || 0) + (context.metadata?.tokens_graph || 0) + (context.metadata?.tokens_docs || 0)
        }
      },
      null,
      2
    );
    return `
    <div class="json-viewer">
      <div class="json-info">
        <p>This is the final JSON sent to the Claude API (system prompt + context).</p>
        <p>The <code>system</code> field contains the assembled surgical context.</p>
      </div>
      <button class="copy-button" data-action="copy-api-json">Copy JSON</button>
      <pre><code>${escapeHtml3(jsonStr)}</code></pre>
    </div>
  `;
  }
  function buildSystemPrompt(context) {
    const primary = context.primary_source;
    const graphItems = context.graph_context || [];
    const docs = context.documentation || [];
    const blocks = [
      `--- TARGET SYMBOL: ${primary?.symbol || "unknown"} ---`
    ];
    if (primary?.code) {
      blocks.push(primary.code);
    }
    if (graphItems.length > 0) {
      blocks.push("\n--- DEPENDENCIES ---");
      for (const dep of graphItems) {
        blocks.push(`
# From ${dep.symbol} [${dep.relation}]:`);
        if (dep.code) {
          blocks.push(dep.code);
        }
      }
    }
    if (docs.length > 0) {
      blocks.push("\n--- DOCUMENTATION ---");
      for (const doc of docs) {
        blocks.push(`[${doc.source_file}]
${doc.content}`);
      }
    }
    return blocks.join("\n");
  }

  // src/webview/shared/settingsLayout.ts
  function escapeHtml4(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }
  function settingsFormDataFromSettings(data) {
    return {
      backendUrl: data.backendUrl,
      workspaceId: data.workspaceId,
      modelPreference: data.modelPreference,
      authToken: data.authToken,
      tokenBudget: data.tokenBudget,
      lancedbPath: data.lancedbPath,
      historyPath: data.historyPath,
      neo4jUri: data.neo4jUri,
      indexProfile: data.indexProfile,
      overlaySync: data.overlaySync,
      autoOpenInspector: data.autoOpenInspector,
      graphStatusLabel: data.graphStatus?.label,
      graphStatusDetail: data.graphStatus?.detail,
      graphStatusHealthy: data.graphStatus?.healthy
    };
  }
  function renderSettingsForm(data) {
    return `
    <div class="settings-form">
      <div class="settings-header">
        <h2>Surgical Context Settings</h2>
        <p class="settings-description">Configure your Surgical Context environment and preferences.</p>
      </div>

      <div class="settings-section">
        <h3>Connection</h3>

        <div class="setting-field">
          <label for="backendUrl">Sidecar URL</label>
          <div class="field-group">
            <input
              type="text"
              id="backendUrl"
              class="setting-input"
              value="${escapeHtml4(data.backendUrl)}"
              placeholder="http://localhost:8000"
              aria-label="Sidecar backend URL"
              aria-describedby="backendUrl-hint"
            />
            <button class="field-action-btn" data-action="testUrl" aria-label="Test connection">
              Test
            </button>
          </div>
          <p class="field-hint" id="backendUrl-hint">Base URL where the Surgical Context sidecar is running</p>
          <div class="field-status" id="backendUrl-status"></div>
        </div>

        <div class="setting-field">
          <label for="authToken">Auth Token (Optional)</label>
          <input
            type="password"
            id="authToken"
            class="setting-input"
            value="${escapeHtml4(data.authToken)}"
            placeholder="Leave blank if no authentication required"
            aria-label="Authentication token for sidecar"
            aria-describedby="authToken-hint"
          />
          <p class="field-hint" id="authToken-hint">Token for authenticating with the sidecar if required</p>
        </div>
      </div>

      <div class="settings-section">
        <h3>Workspace</h3>

        <div class="setting-field">
          <label for="workspaceId">Workspace ID</label>
          <input
            type="text"
            id="workspaceId"
            class="setting-input"
            value="${escapeHtml4(data.workspaceId)}"
            placeholder="derived from workspace and Git branch"
            aria-label="Workspace scope identifier"
            aria-describedby="workspaceId-hint"
          />
          <p class="field-hint" id="workspaceId-hint">Optional override. Leave blank to derive from the open workspace and Git branch.</p>
        </div>

        <div class="setting-field">
          <label for="modelPreference">Model Preference</label>
          <select
            id="modelPreference"
            class="setting-input"
            aria-label="LLM model to use"
            aria-describedby="modelPreference-hint"
          >
            <option value="auto" ${data.modelPreference === "auto" ? "selected" : ""}>Auto</option>
            <option value="claude" ${data.modelPreference === "claude" ? "selected" : ""}>Claude</option>
            <option value="ollama" ${data.modelPreference === "ollama" ? "selected" : ""}>Ollama</option>
          </select>
          <p class="field-hint" id="modelPreference-hint">Preferred sidecar model route for local asks</p>
        </div>

        <div class="setting-field">
          <label for="tokenBudget">Token Budget</label>
          <input
            type="number"
            id="tokenBudget"
            class="setting-input"
            value="${escapeHtml4(String(data.tokenBudget))}"
            min="1000"
            max="32000"
            step="500"
            aria-label="Default token budget"
            aria-describedby="tokenBudget-hint"
          />
          <p class="field-hint" id="tokenBudget-hint">Default context budget used for ask and streaming ask requests</p>
          <div class="field-status" id="tokenBudget-status"></div>
        </div>
      </div>

      <div class="settings-section">
        <h3>Graph (Neo4j)</h3>

        <div class="setting-field">
          <label for="neo4jUri">Neo4j URI</label>
          <input
            type="text"
            id="neo4jUri"
            class="setting-input"
            value="${escapeHtml4(data.neo4jUri)}"
            placeholder="bolt://localhost:7687"
            aria-label="Neo4j Bolt URI"
            aria-describedby="neo4jUri-hint"
          />
          <p class="field-hint" id="neo4jUri-hint">Sidecar reads NEO4J_URI from the repo <code>.env</code>. Match this for documentation; start graph with <code>docker compose up -d neo4j</code>.</p>
        </div>

        <div class="setting-field">
          <label for="indexProfile">Index profile</label>
          <select
            id="indexProfile"
            class="setting-input"
            aria-label="Sidecar index profile"
            aria-describedby="indexProfile-hint"
          >
            <option value="axis_python_v1" ${data.indexProfile === "axis_python_v1" ? "selected" : ""}>axis_python_v1</option>
            <option value="legacy" ${data.indexProfile === "legacy" ? "selected" : ""}>legacy</option>
          </select>
          <p class="field-hint" id="indexProfile-hint">Set INDEX_PROFILE in sidecar <code>.env</code> to the same value, then restart sidecar and reindex.</p>
        </div>

        <div class="setting-field">
          <label>Graph provider status</label>
          <div class="field-status ${data.graphStatusHealthy ? "success" : "warning"}" style="display:block">
            ${escapeHtml4(data.graphStatusLabel || "Unknown")}
          </div>
          <p class="field-hint">${escapeHtml4(data.graphStatusDetail || "Open Settings to refresh status from /status/cloud.")}</p>
        </div>
      </div>

      <div class="settings-section">
        <h3>Local Storage</h3>

        <div class="setting-grid">
          <div class="setting-field">
            <label for="lancedbPath">LanceDB Path</label>
            <input
              type="text"
              id="lancedbPath"
              class="setting-input"
              value="${escapeHtml4(data.lancedbPath)}"
              placeholder="./data/lancedb"
              aria-label="LanceDB path"
              aria-describedby="lancedbPath-hint"
            />
            <p class="field-hint" id="lancedbPath-hint">Local vector index path used by the sidecar environment</p>
          </div>

          <div class="setting-field">
            <label for="historyPath">History DB Path</label>
            <input
              type="text"
              id="historyPath"
              class="setting-input"
              value="${escapeHtml4(data.historyPath)}"
              placeholder="./data/history/surgical_context.sqlite3"
              aria-label="SQLite history path"
              aria-describedby="historyPath-hint"
            />
            <p class="field-hint" id="historyPath-hint">Planned local SQLite history path for dialogs and snapshots</p>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <h3>Behavior</h3>

        <div class="setting-field checkbox-field">
          <label for="overlaySync">
            <input
              type="checkbox"
              id="overlaySync"
              class="setting-checkbox"
              ${data.overlaySync ? "checked" : ""}
              aria-describedby="overlaySync-hint"
            />
            <span>Send unsaved content to sidecar</span>
          </label>
          <p class="field-hint" id="overlaySync-hint">When enabled, unsaved editor changes are sent with asks so answers reflect in-memory code</p>
        </div>

        <div class="setting-field checkbox-field">
          <label for="autoOpenInspector">
            <input
              type="checkbox"
              id="autoOpenInspector"
              class="setting-checkbox"
              ${data.autoOpenInspector ? "checked" : ""}
              aria-describedby="autoOpenInspector-hint"
            />
            <span>Auto-open Context Inspector</span>
          </label>
          <p class="field-hint" id="autoOpenInspector-hint">Automatically open the Inspector tab after a completed ask</p>
        </div>
      </div>

      <div class="settings-section">
        <h3>Keyboard Shortcuts</h3>
        <p class="settings-description">VS Code keyboard shortcuts for Surgical Context commands:</p>
        <div class="shortcuts-list">
          <div class="shortcut-item">
            <code class="shortcut-key">Ctrl+Alt+A</code>
            <span class="shortcut-desc">Ask about current symbol</span>
          </div>
          <div class="shortcut-item">
            <code class="shortcut-key">Ctrl+Alt+I</code>
            <span class="shortcut-desc">Show impact</span>
          </div>
          <div class="shortcut-item">
            <code class="shortcut-key">Cmd+L</code>
            <span class="shortcut-desc">Focus chat composer</span>
          </div>
        </div>
        <button class="secondary-btn" data-action="openKeybindings" aria-label="Open VS Code keyboard shortcuts settings">
          Customize Shortcuts
        </button>
      </div>

      <div class="settings-actions">
        <button class="primary-btn" data-action="save" aria-label="Save all settings">
          Save Settings
        </button>
        <button class="secondary-btn" data-action="reset" aria-label="Reset to default values">
          Reset to Defaults
        </button>
      </div>

      <div class="settings-feedback" id="settings-feedback"></div>
    </div>
  `;
  }
  function showFieldStatus(fieldId, success, message) {
    const status = document.getElementById(`${fieldId}-status`);
    if (!status) return;
    status.className = `field-status ${success ? "success" : "error"}`;
    status.textContent = message;
    status.style.display = "block";
    if (success) {
      setTimeout(() => {
        status.style.display = "none";
      }, 3e3);
    }
  }
  function showFeedback(message, level) {
    const feedback = document.getElementById("settings-feedback");
    if (!feedback) return;
    feedback.className = `settings-feedback settings-feedback-${level}`;
    feedback.textContent = message;
    feedback.style.display = "block";
    if (level === "success") {
      setTimeout(() => {
        feedback.style.display = "none";
      }, 3e3);
    }
  }

  // src/webview/main.ts
  var vscode = acquireVsCodeApi();
  var MainSurface = class {
    constructor() {
      this.surface = "chat";
      this.state = null;
      this.messages = /* @__PURE__ */ new Map();
      this.dialogHistory = [];
      this.currentDialogId = `dialog-${Date.now()}`;
      this.currentStreamingRequestId = null;
      this.currentContextSummary = null;
      this.currentPromptContext = null;
      this.selectedPromptRequestId = null;
      this.inspectorTab = "primary";
      this.pendingPrompt = null;
      this.currentImpact = null;
      this.currentImpactSymbol = null;
      this.currentImpactSource = null;
      this.currentImpactDepth = 3;
      this.impactError = null;
      this.impactLoading = false;
      this.historyCollapsed = true;
      this.settings = null;
      this.keyboardListenerAttached = false;
      this.initializeMessageListener();
      this.restoreState();
      this.renderLoadingShell();
      this.postMessage({ type: "surface.ready" });
    }
    initializeMessageListener() {
      window.addEventListener("message", (event) => {
        const message = event.data;
        switch (message.type) {
          case "surface.init":
            this.state = message.state;
            if (message.state.lastContext && !this.currentPromptContext) {
              this.currentPromptContext = message.state.lastContext;
              this.currentContextSummary = this.summaryFromContext(message.state.lastContext);
              this.currentImpact = this.impactFromContext(message.state.lastContext);
              this.currentImpactSymbol = message.state.lastContext.primary_source.symbol;
              this.currentImpactSource = "prompt";
              this.selectedPromptRequestId = this.findRequestIdForContext(message.state.lastContext) || this.selectedPromptRequestId;
            }
            this.render();
            break;
          case "surface.showChat":
            this.surface = "chat";
            this.render();
            break;
          case "surface.showInspector":
            this.surface = "inspector";
            this.render();
            break;
          case "surface.showImpact":
            this.surface = "impact";
            this.render();
            break;
          case "surface.showSettings":
            this.surface = "settings";
            this.render();
            this.requestSettings();
            break;
          case "chat.requestStarted":
            this.surface = "chat";
            this.onRequestStarted(message.requestId, message.symbol);
            break;
          case "chat.streamChunk":
            this.onStreamChunk(message.requestId, message.chunk);
            break;
          case "chat.requestCompleted":
            this.onRequestCompleted(message.requestId, message.answer, message.context);
            break;
          case "chat.requestFailed":
            this.onRequestFailed(message.requestId, message.error);
            break;
          case "chat.requestStopped":
            this.onRequestStopped(message.requestId);
            break;
          case "chat.contextSummary":
            this.currentContextSummary = message.summary;
            this.refreshAccordions();
            break;
          case "workspace.updated":
            if (this.state) {
              this.state.workspace = {
                activeFile: message.activeFile,
                selectedSymbol: message.symbol,
                isDirty: message.isDirty
              };
              this.refreshWorkspaceBits();
            }
            break;
          case "backend.updated":
            if (this.state) {
              this.state.backend = {
                sidecarHealth: message.sidecarHealth,
                cloudStatus: message.cloudStatus
              };
              this.refreshWorkspaceBits();
            }
            break;
          case "impact.loading":
            this.surface = "impact";
            this.impactLoading = true;
            this.impactError = null;
            this.render();
            break;
          case "impact.loaded":
            this.surface = "impact";
            this.impactLoading = false;
            this.currentImpactSymbol = message.symbol;
            this.currentImpact = message.impact;
            this.currentImpactDepth = this.clampImpactDepth(message.impact.max_depth || this.currentImpactDepth);
            this.currentImpactSource = "graph";
            this.impactError = null;
            this.render();
            break;
          case "impact.loadFailed":
            this.surface = "impact";
            this.impactLoading = false;
            this.currentImpact = null;
            this.impactError = message.error;
            this.render();
            break;
          case "inspector.loaded":
            this.surface = "inspector";
            this.currentPromptContext = message.context;
            if (message.context) {
              this.currentContextSummary = this.summaryFromContext(message.context);
              this.currentImpact = this.impactFromContext(message.context);
              this.currentImpactSymbol = message.context.primary_source.symbol;
              this.currentImpactSource = "prompt";
            }
            this.render();
            break;
          case "settings.loaded":
            this.settings = message.settings;
            if (this.surface === "settings") {
              this.render();
            }
            break;
          case "settings.saved":
            showFeedback(message.message, "success");
            break;
          case "settings.saveFailed":
            showFeedback(message.error, "error");
            break;
          case "settings.testUrlComplete":
            showFieldStatus("backendUrl", message.success, message.message);
            break;
          case "toast.show":
            this.showToast(message.message, message.level);
            break;
        }
      });
    }
    render() {
      const root = document.getElementById("root");
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
    renderLoadingShell() {
      const root = document.getElementById("root");
      if (!root) return;
      root.innerHTML = `
      <section class="surface surface-chat" aria-label="Surgical Context loading">
        ${this.renderSurfaceTabs()}
        <div class="loading-state">Loading Surgical Context...</div>
      </section>
    `;
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
      const tabs = [
        { id: "chat", label: "Chat", icon: "\u25CC" },
        { id: "inspector", label: "Inspector", icon: "\u25CE" },
        { id: "impact", label: "Impact", icon: "\u2301" }
      ];
      return `
      <nav class="surface-tab-bar" aria-label="Surgical Context sections">
        <div class="surface-tab-group">
          ${tabs.map((tab) => `
            <button
              class="surface-tab ${this.surface === tab.id ? "active" : ""}"
              data-action="switchSurface"
              data-surface="${tab.id}"
              aria-current="${this.surface === tab.id ? "page" : "false"}"
              title="${tab.label}"
              aria-label="${tab.label}"
            >
              <span aria-hidden="true">${tab.icon}</span>
            </button>
          `).join("")}
          <button
            class="surface-tab"
            data-action="openDashboard"
            title="Dashboard"
            aria-label="Dashboard"
          >
            <span aria-hidden="true">\u25A6</span>
          </button>
        </div>
        <div class="surface-tab-actions">
          ${this.surface === "chat" ? this.renderChatSessionActions() : ""}
          <button
            class="surface-tab ${this.surface === "settings" ? "active" : ""}"
            data-action="switchSurface"
            data-surface="settings"
            aria-current="${this.surface === "settings" ? "page" : "false"}"
            title="Settings"
            aria-label="Settings"
          >
            <span aria-hidden="true">\u2699</span>
          </button>
        </div>
      </nav>
    `;
    }
    renderChatSurface() {
      if (!this.state) return "";
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
        docLinked: true
      })}
      </section>
    `;
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
      const selectedPromptText = this.selectedPromptText();
      const subtitle = selectedPromptText || "Related code and files for the selected prompt.";
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
        ${renderImpactWorkspace(
        this.currentImpact,
        symbol,
        this.currentImpactSource === "prompt" ? "prompt context" : "live graph",
        { depth: this.currentImpactDepth }
      )}
        <div class="surface-footer">
          <span>${this.currentImpactSource === "prompt" ? "From selected ask" : "Graph built just now"}</span>
          <button class="icon-action" data-action="showImpact" title="Refresh impact">Refresh</button>
        </div>
      </section>
    `;
    }
    renderInspectorSurface() {
      const context = this.currentPromptContext;
      if (!context) {
        return `
        <section class="surface surface-inspector" aria-label="Context inspector">
          ${this.renderChrome()}
          <div class="surface-title">Context Inspector</div>
          <div class="surface-subtitle">${escapeHtml(this.selectedPromptText() || "Inspect the evidence behind the selected answer.")}</div>
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
          <div class="surface-subtitle">${escapeHtml(this.selectedPromptText() || "Selected prompt")}</div>
          <div class="inspector-tab-bar" role="tablist" aria-label="Context detail tabs">
            ${this.renderInspectorTabButton("primary", "Primary")}
            ${this.renderInspectorTabButton("graph", "Graph")}
            ${this.renderInspectorTabButton("docs", "Docs")}
            ${this.renderInspectorTabButton("tokens", "Tokens")}
            ${this.renderInspectorTabButton("json", "JSON")}
            ${this.renderInspectorTabButton("api", "API")}
          </div>
        </div>
        <div class="inspector-content">
          ${this.renderInspectorTabContent(context)}
        </div>
      </section>
    `;
    }
    renderInspectorTabButton(tab, label) {
      return `
      <button
        class="tab-button ${this.inspectorTab === tab ? "active" : ""}"
        data-action="switchInspectorTab"
        data-inspector-tab="${tab}"
        role="tab"
        aria-selected="${this.inspectorTab === tab}"
      >
        ${label}
      </button>
    `;
    }
    renderInspectorTabContent(context) {
      switch (this.inspectorTab) {
        case "graph":
          return renderGraphContextTab(context);
        case "docs":
          return renderDocumentationTab(context);
        case "tokens":
          return renderTokenBreakdownTab(context);
        case "json":
          return renderPromptJsonTab(context);
        case "api":
          return renderApiPayloadTab(context);
        case "primary":
        default:
          return renderPrimarySourceTab(context);
      }
    }
    renderSettingsSurface() {
      return `
      <section class="surface surface-settings" aria-label="Surgical Context settings">
        ${this.renderChrome()}
        ${this.settings ? renderSettingsForm(settingsFormDataFromSettings(this.settings)) : '<div class="loading-state">Loading settings...</div>'}
      </section>
    `;
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
      document.querySelectorAll("[data-action]").forEach((element) => {
        element.addEventListener("click", (event) => this.handleAction(event));
      });
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
            document.getElementById("composer-input")?.focus();
          }
        });
        this.keyboardListenerAttached = true;
      }
    }
    handleAction(event) {
      const target = event.currentTarget;
      const action = target.getAttribute("data-action");
      if (action === "copy" || action === "feedback") {
        event.preventDefault();
        event.stopPropagation();
      }
      switch (action) {
        case "switchSurface":
          this.switchSurface(target.getAttribute("data-surface"));
          break;
        case "switchInspectorTab":
          this.switchInspectorTab(target.getAttribute("data-inspector-tab"));
          break;
        case "selectPrompt":
          this.selectPrompt(target.getAttribute("data-request-id"));
          break;
        case "toggleHistory":
          this.toggleHistory();
          break;
        case "newDialog":
          this.startNewDialog();
          break;
        case "restoreDialog":
          this.restoreDialog(target.getAttribute("data-dialog-id"));
          break;
        case "openDashboard":
          this.postMessage({ type: "action.openDashboard" });
          break;
        case "ask":
          document.getElementById("composer-input")?.focus();
          break;
        case "openChat":
          this.switchSurface("chat");
          setTimeout(() => {
            document.getElementById("composer-input")?.focus();
          }, 0);
          break;
        case "openInspector":
          this.switchSurface("inspector");
          break;
        case "openSettings":
          this.switchSurface("settings");
          break;
        case "showImpact":
          this.switchSurface("impact");
          if (target.classList.contains("icon-action")) {
            this.requestImpactForActiveSymbol();
          }
          break;
        case "ask-followup":
          this.switchSurface("chat");
          this.prefillComposer(
            `What should I check before changing ${this.currentImpactSymbol || "this symbol"}?`
          );
          break;
        case "open-related-files":
          this.openRelatedImpactFiles();
          break;
        case "openFile":
          this.openFileFromImpact(target);
          break;
        case "showMoreImpact":
          this.showMoreImpactRows(target);
          break;
        case "create-refactor-plan":
          this.switchSurface("chat");
          this.prefillComposer(
            `Create a refactor plan for ${this.currentImpactSymbol || "this symbol"}.`
          );
          break;
        case "save":
          this.saveSettings();
          break;
        case "reset":
          this.resetSettings();
          break;
        case "testUrl":
          this.testSettingsUrl();
          break;
        case "openKeybindings":
          this.postMessage({ type: "settings.openKeybindings" });
          break;
        case "search":
          this.showToast("Search is coming soon.", "info");
          break;
        case "noop":
          this.toggleImpactGroup(target);
          break;
        case "feedback":
          this.submitFeedback(target);
          break;
        case "copy":
          this.copyMessage(target);
          break;
      }
    }
    switchSurface(surface) {
      if (!surface) return;
      this.surface = surface;
      this.persistState();
      if (surface === "impact") {
        this.render();
        const selectedSymbol = this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || void 0;
        if ((!this.currentImpact || selectedSymbol && selectedSymbol !== this.currentImpactSymbol) && !this.impactLoading) {
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
      const selectedSymbol = this.currentPromptContext?.primary_source.symbol || this.state?.workspace.selectedSymbol || void 0;
      this.postMessage({
        type: "action.showImpact",
        symbol: selectedSymbol,
        filePath: this.state?.workspace.activeFile || void 0,
        maxDepth: this.currentImpactDepth
      });
    }
    previewImpactDepth(event) {
      const slider = event.currentTarget;
      if (!slider) return;
      const output = slider.closest(".impact-depth-control")?.querySelector("output");
      const depth = this.clampImpactDepth(Number(slider.value));
      if (output) {
        output.textContent = `d${depth}`;
      }
    }
    changeImpactDepth(event) {
      const slider = event.currentTarget;
      if (!slider) return;
      const depth = this.clampImpactDepth(Number(slider.value));
      if (depth === this.currentImpactDepth && this.currentImpactSource === "graph") return;
      this.currentImpactDepth = depth;
      this.requestImpactForActiveSymbol();
    }
    clampImpactDepth(depth) {
      if (!Number.isFinite(depth)) return 3;
      return Math.max(1, Math.min(4, Math.round(depth)));
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
      if (!composer || !composer.value.trim() || !this.state) return;
      const prompt = composer.value.trim();
      this.pendingPrompt = prompt;
      composer.value = "";
      resizeComposerToFit(composer);
      this.persistState();
      this.postMessage({
        type: "chat.ask",
        prompt,
        symbol: this.state.workspace.selectedSymbol || void 0,
        conversationId: this.currentDialogId
      });
    }
    requestSettings() {
      this.postMessage({ type: "settings.loaded" });
    }
    saveSettings() {
      if (!this.settings) return;
      const backendUrl = document.getElementById("backendUrl")?.value || "";
      const workspaceId = document.getElementById("workspaceId")?.value || "";
      const modelPreference = document.getElementById("modelPreference")?.value || "auto";
      const authToken = document.getElementById("authToken")?.value || "";
      const tokenBudget = Number(document.getElementById("tokenBudget")?.value || "6000");
      const lancedbPath = document.getElementById("lancedbPath")?.value || "";
      const historyPath = document.getElementById("historyPath")?.value || "";
      const neo4jUri = document.getElementById("neo4jUri")?.value || "";
      const indexProfile = document.getElementById("indexProfile")?.value || "axis_python_v1";
      const overlaySync = document.getElementById("overlaySync")?.checked || false;
      const autoOpenInspector = document.getElementById("autoOpenInspector")?.checked || false;
      if (backendUrl && !backendUrl.startsWith("http://") && !backendUrl.startsWith("https://")) {
        showFieldStatus("backendUrl", false, "URL must start with http:// or https://");
        return;
      }
      if (!Number.isFinite(tokenBudget) || tokenBudget < 1e3 || tokenBudget > 32e3) {
        showFieldStatus("tokenBudget", false, "Use a value from 1000 to 32000");
        return;
      }
      this.postMessage({
        type: "settings.save",
        settings: {
          backendUrl,
          workspaceId,
          modelPreference,
          authToken,
          tokenBudget,
          lancedbPath,
          historyPath,
          neo4jUri,
          indexProfile,
          overlaySync,
          autoOpenInspector
        }
      });
    }
    resetSettings() {
      const defaults = {
        backendUrl: "http://localhost:8000",
        workspaceId: "",
        modelPreference: "auto",
        authToken: "",
        tokenBudget: 6e3,
        lancedbPath: "./data/lancedb",
        historyPath: "./data/history/surgical_context.sqlite3",
        neo4jUri: "bolt://localhost:7687",
        indexProfile: "axis_python_v1",
        overlaySync: true,
        autoOpenInspector: false
      };
      const backendUrl = document.getElementById("backendUrl");
      const workspaceId = document.getElementById("workspaceId");
      const modelPreference = document.getElementById("modelPreference");
      const authToken = document.getElementById("authToken");
      const tokenBudget = document.getElementById("tokenBudget");
      const lancedbPath = document.getElementById("lancedbPath");
      const historyPath = document.getElementById("historyPath");
      const neo4jUri = document.getElementById("neo4jUri");
      const indexProfile = document.getElementById("indexProfile");
      const overlaySync = document.getElementById("overlaySync");
      const autoOpenInspector = document.getElementById("autoOpenInspector");
      if (backendUrl) backendUrl.value = defaults.backendUrl;
      if (workspaceId) workspaceId.value = defaults.workspaceId;
      if (modelPreference) modelPreference.value = defaults.modelPreference;
      if (authToken) authToken.value = defaults.authToken;
      if (tokenBudget) tokenBudget.value = String(defaults.tokenBudget);
      if (lancedbPath) lancedbPath.value = defaults.lancedbPath;
      if (historyPath) historyPath.value = defaults.historyPath;
      if (neo4jUri) neo4jUri.value = defaults.neo4jUri;
      if (indexProfile) indexProfile.value = defaults.indexProfile;
      if (overlaySync) overlaySync.checked = defaults.overlaySync;
      if (autoOpenInspector) autoOpenInspector.checked = defaults.autoOpenInspector;
      showFeedback("Reset to default settings", "info");
    }
    testSettingsUrl() {
      const url = document.getElementById("backendUrl")?.value || "";
      if (!url) {
        showFieldStatus("backendUrl", false, "Please enter a URL");
        return;
      }
      const authToken = document.getElementById("authToken")?.value || "";
      this.postMessage({ type: "settings.testUrl", url, authToken });
    }
    onRequestStarted(requestId, symbol) {
      this.currentStreamingRequestId = requestId;
      this.selectedPromptRequestId = requestId;
      this.currentPromptContext = null;
      this.currentContextSummary = null;
      this.currentImpact = null;
      this.currentImpactSymbol = symbol || null;
      this.currentImpactSource = null;
      this.currentImpactDepth = 3;
      this.impactError = null;
      const prompt = this.pendingPrompt || "Ask about current symbol";
      this.pendingPrompt = null;
      const userMessageId = `msg-${Date.now()}`;
      this.messages.set(userMessageId, {
        id: userMessageId,
        requestId,
        type: "user",
        content: prompt,
        timestamp: Date.now(),
        symbol
      });
      this.messages.set(requestId, {
        id: requestId,
        requestId,
        type: "assistant",
        content: "",
        timestamp: Date.now(),
        symbol,
        status: "streaming"
      });
      this.persistState();
      this.render();
      this.scrollToBottom();
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
      this.persistState();
      this.refreshAccordions();
    }
    onRequestFailed(requestId, error) {
      this.currentStreamingRequestId = null;
      const message = this.messages.get(requestId);
      if (message) {
        message.status = "error";
        message.error = error;
      } else {
        this.messages.set(requestId, {
          id: requestId,
          type: "assistant",
          content: "",
          timestamp: Date.now(),
          status: "error",
          error
        });
      }
      this.persistState();
      this.updateConversationView();
    }
    onRequestStopped(requestId) {
      const message = this.messages.get(requestId);
      if (message) {
        message.status = "done";
        this.updateConversationView();
      }
      this.currentStreamingRequestId = null;
      this.persistState();
    }
    updateConversationView() {
      const viewport = document.getElementById("conversation");
      if (!viewport) return;
      viewport.innerHTML = Array.from(this.messages.values()).map((message) => renderMessageCard(message, this.selectedPromptRequestId)).join("");
      viewport.querySelectorAll("[data-action]").forEach((element) => {
        element.addEventListener("click", (event) => this.handleAction(event));
      });
      viewport.querySelectorAll(".message-card.selectable").forEach((element) => {
        element.addEventListener("keydown", (event) => {
          const keyboardEvent = event;
          if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
            keyboardEvent.preventDefault();
            this.selectPrompt(element.getAttribute("data-request-id"));
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
        this.currentPromptContext = null;
        this.currentContextSummary = null;
        this.currentImpact = null;
        this.currentImpactSource = null;
        this.currentImpactDepth = 3;
        this.showToast("Prompt is still waiting for context.", "info");
      }
      this.historyCollapsed = true;
      this.persistState();
      this.render();
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
      this.currentContextSummary = null;
      this.currentPromptContext = null;
      this.selectedPromptRequestId = null;
      this.pendingPrompt = null;
      this.currentImpact = null;
      this.currentImpactSymbol = null;
      this.currentImpactSource = null;
      this.currentImpactDepth = 3;
      this.impactError = null;
      this.impactLoading = false;
      this.historyCollapsed = true;
      this.persistState();
      this.render();
    }
    restoreDialog(dialogId) {
      if (!dialogId) return;
      const dialog = this.dialogHistory.find((item) => item.id === dialogId);
      if (!dialog) return;
      this.persistState();
      this.currentDialogId = dialog.id;
      this.messages = new Map(dialog.messages.map((message) => [message.id, { ...message }]));
      this.selectedPromptRequestId = dialog.selectedPromptRequestId || this.latestContextRequestId();
      const context = this.selectedPromptRequestId ? this.contextForRequest(this.selectedPromptRequestId) : null;
      if (context && this.selectedPromptRequestId) {
        this.activatePromptContext(this.selectedPromptRequestId, context);
      } else {
        this.currentPromptContext = null;
        this.currentContextSummary = null;
        this.currentImpact = null;
        this.currentImpactSymbol = null;
        this.currentImpactSource = null;
        this.currentImpactDepth = 3;
        this.impactError = null;
      }
      this.historyCollapsed = true;
      this.persistState();
      this.render();
      this.scrollToBottom();
    }
    dialogsForHistory() {
      const current = this.currentDialogSnapshot();
      const dialogs = current ? [current, ...this.dialogHistory.filter((dialog) => dialog.id !== current.id)] : [...this.dialogHistory];
      return dialogs.filter((dialog) => dialog.messages.length > 0).sort((left, right) => right.updatedAt - left.updatedAt).slice(0, 30);
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
      this.dialogHistory = [
        snapshot,
        ...this.dialogHistory.filter((dialog) => dialog.id !== snapshot.id)
      ].sort((left, right) => right.updatedAt - left.updatedAt).slice(0, 30);
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
      this.currentContextSummary = this.summaryFromContext(context);
      this.currentImpact = this.impactFromContext(context);
      this.currentImpactSymbol = context.primary_source.symbol;
      this.currentImpactSource = "prompt";
      this.currentImpactDepth = this.clampImpactDepth(this.currentImpact.max_depth || this.currentImpactDepth);
      this.impactError = null;
      this.syncSelectedRequestToHost(requestId, context);
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
    summaryFromContext(context) {
      return buildContextSummary(context);
    }
    impactFromContext(context) {
      const affectedSymbols = context.graph_context.map((symbol) => ({
        symbol: symbol.symbol,
        file_path: symbol.file_path,
        relation: symbol.relation,
        direction: symbol.direction,
        role: symbol.role,
        kind: symbol.kind,
        edge_type: symbol.edge_type,
        depth: symbol.depth,
        utility_score: symbol.utility_score,
        relevance_score: symbol.relevance_score,
        is_dirty: symbol.is_dirty
      }));
      const affectedFiles = Array.from(new Set(
        [
          context.primary_source.file_path,
          ...context.graph_context.map((symbol) => symbol.file_path),
          ...context.documentation.map((doc) => doc.source_file)
        ].filter(Boolean)
      ));
      return {
        symbol: context.primary_source.symbol,
        symbol_uid: context.primary_source.symbol,
        file_path: context.primary_source.file_path,
        affected_symbols: affectedSymbols,
        affected_files: affectedFiles,
        affected_count: affectedSymbols.length,
        affected_file_count: affectedFiles.length,
        max_depth: affectedSymbols.reduce((max, symbol) => typeof symbol.depth === "number" ? Math.max(max, symbol.depth) : max, 0)
      };
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
        statusRow.outerHTML = renderStatusChips({
          isDirty: this.state.workspace.isDirty,
          graphFirst: true,
          docLinked: true
        });
      }
      this.refreshAccordions();
    }
    refreshAccordions() {
      const stack = document.querySelector(".accordion-stack");
      if (stack) {
        stack.innerHTML = this.renderAccordions();
        document.querySelectorAll(".accordion-header").forEach((header) => {
          header.addEventListener("click", () => this.toggleAccordion(header));
        });
      }
    }
    toggleAccordion(header) {
      const group = header.closest("[data-accordion]");
      const id = group?.getAttribute("data-accordion");
      const content = group?.querySelector(".accordion-content");
      if (!group || !content || !id) return;
      const expanded = header.getAttribute("aria-expanded") === "true";
      header.setAttribute("aria-expanded", String(!expanded));
      content.toggleAttribute("hidden", expanded);
      content.classList.toggle("expanded", !expanded);
      if (this.state) {
        this.state.expandedAccordions[id] = !expanded;
        this.persistState();
      }
    }
    toggleImpactGroup(header) {
      const group = header.closest(".impact-group");
      const content = group?.querySelector(".group-content");
      if (!group || !content) return;
      const expanded = header.getAttribute("aria-expanded") === "true";
      header.setAttribute("aria-expanded", String(!expanded));
      group.classList.toggle("expanded", !expanded);
      content.toggleAttribute("hidden", expanded);
    }
    showMoreImpactRows(target) {
      const group = target.closest(".impact-group");
      const overflow = group?.querySelector(".impact-overflow");
      if (!overflow) return;
      overflow.removeAttribute("hidden");
      target.remove();
    }
    openFileFromImpact(target) {
      const filePath = target.getAttribute("data-file-path");
      if (!filePath) return;
      const line = Number.parseInt(target.getAttribute("data-line") || "1", 10);
      this.postMessage({
        type: "link.openFile",
        filePath,
        line: Number.isFinite(line) ? line : 1
      });
    }
    submitFeedback(target) {
      const rating = target.getAttribute("data-rating");
      const card = target.closest(".message-card");
      const messageId = card?.getAttribute("data-message-id");
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
    prefillComposer(text) {
      const composer = document.getElementById("composer-input");
      if (!composer) return;
      composer.value = text;
      resizeComposerToFit(composer);
      composer.focus();
      this.persistState();
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
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new MainSurface());
  } else {
    new MainSurface();
  }
})();
//# sourceMappingURL=main.js.map
