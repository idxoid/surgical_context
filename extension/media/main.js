"use strict";
(() => {
  // src/webview/shared/domActions.ts
  function bindDataActions(root, handler) {
    root.querySelectorAll("[data-action]").forEach((element) => {
      element.addEventListener("click", handler);
    });
  }

  // src/webview/shared/domRender.ts
  function sanitizeParsedDocument(doc) {
    doc.querySelectorAll("script, iframe, object, embed").forEach((node) => node.remove());
    doc.querySelectorAll("*").forEach((node) => {
      for (const attr of Array.from(node.attributes)) {
        const name = attr.name.toLowerCase();
        const value = attr.value.trim().toLowerCase();
        if (name.startsWith("on")) {
          node.removeAttribute(attr.name);
          continue;
        }
        if ((name === "href" || name === "src") && value.startsWith("javascript:")) {
          node.removeAttribute(attr.name);
        }
      }
    });
  }
  function fragmentFromHtml(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    sanitizeParsedDocument(doc);
    const fragment = document.createDocumentFragment();
    fragment.append(...Array.from(doc.body.childNodes));
    return fragment;
  }
  function mountLayoutHtml(element, html) {
    element.replaceChildren(...Array.from(fragmentFromHtml(html).childNodes));
  }
  function replaceElementHtml(element, html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    sanitizeParsedDocument(doc);
    const replacement = doc.body.firstElementChild;
    if (replacement) {
      element.replaceWith(replacement);
    }
  }

  // src/webview/shared/webviewRuntime.ts
  var vscode = acquireVsCodeApi();
  function isTrustedHostWebviewMessage(event) {
    const origin = event.origin;
    if (origin === window.location.origin) {
      return true;
    }
    if (origin === "") {
      return true;
    }
    return origin.startsWith("vscode-webview://");
  }
  function bootWebview(init) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  // src/webview/shared/html.ts
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

  // src/webview/shared/layout.ts
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
  function renderComposerDock(isStreaming = false) {
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
      <button id="composer-send" class="composer-send-btn" title="Send (Enter)" aria-label="Send message" ${isStreaming ? "hidden" : ""}>
        <span class="composer-send-icon" aria-hidden="true">\u27A4</span>
      </button>
      <button
        id="composer-stop"
        class="composer-stop-btn"
        data-action="stopStreaming"
        title="Stop response"
        aria-label="Stop response generation"
        ${isStreaming ? "" : "hidden"}
      >
        <span class="composer-stop-icon" aria-hidden="true"></span>
      </button>
      <div id="composer-help" class="sr-only">
        Press Enter to send. Press Shift+Enter for a new line. Press Cmd+L to focus composer. While a response is streaming, use Stop to cancel it.
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
      ${renderImpactZone("Direct Impact", model.direct, "No direct callers or first-hop consumers returned.", true, symbol)}
      ${renderImpactZone("Architectural Reach", model.reach, "No hook, event, config, data, or API reach returned.", true, symbol)}
      ${renderImpactZone("Hidden Risks", model.risks, "No cross-repo or coverage risks returned.", model.risks.length > 0, symbol)}
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
  function clampImpactDepth(depth, minDepth = 1, maxDepth = 4) {
    if (!Number.isFinite(depth)) return 3;
    return Math.max(minDepth, Math.min(maxDepth, Math.round(depth)));
  }
  function clampDepth(depth, options) {
    return clampImpactDepth(depth, options.minDepth ?? 1, options.maxDepth ?? 4);
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
    const explicitSeverity = stringField(sym, "severity");
    const explicitZone = stringField(sym, "zone");
    const severity = isSeverity(explicitSeverity) ? explicitSeverity : classifySeverity(category, depth, filePath);
    const zone = isImpactZone(explicitZone) ? explicitZone : classifyZone(category, depth, filePath);
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
  function isSeverity(value) {
    return value === "high" || value === "medium" || value === "low";
  }
  function isImpactZone(value) {
    return value === "direct" || value === "reach" || value === "risk";
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
  function renderImpactZone(title, items, emptyText, expanded, targetSymbol) {
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
        ${visible.map((item) => renderImpactItemRow(item, targetSymbol)).join("")}
        ${overflow.length ? renderOverflowRows(overflow, targetSymbol) : ""}
      </div>
    </div>
  `;
  }
  function renderOverflowRows(items, targetSymbol) {
    return `
    <div class="impact-overflow" hidden>
      ${items.map((item) => renderImpactItemRow(item, targetSymbol)).join("")}
    </div>
    <button class="impact-show-more" data-action="showMoreImpact">
      Show ${items.length} more
    </button>
  `;
  }
  function renderImpactItemRow(item, targetSymbol) {
    const disabled = item.synthetic ? "disabled" : "";
    const title = item.synthetic ? item.symbolName : `Open ${item.symbolName}`;
    const explanation = explainImpactItem(item, targetSymbol);
    return `
    <div class="impact-item ${item.synthetic ? "impact-risk-item" : ""}">
      <div class="impact-item-line">
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
        <button
          type="button"
          class="impact-explain-button"
          data-action="explainImpact"
          aria-expanded="false"
          title="Explain how this item is connected to ${escapeHtml(targetSymbol)}"
        >Explain</button>
      </div>
      ${renderImpactExplanation(explanation)}
    </div>
  `;
  }
  function explainImpactItem(item, targetSymbol) {
    const kind = stringField(item.source, "kind") || arrayField(item.source, "satisfying_kinds")[0] || item.relation || item.category;
    const edge = stringField(item.source, "edge_type", "relation") || item.relation;
    const role = stringField(item.source, "role");
    const provenance = arrayField(item.source, "provenance");
    const depth = item.depth ?? 1;
    const degraded = item.source.degraded === true;
    let summary;
    let path;
    switch (kind) {
      case "coverage_gap":
        summary = `No test symbols or test files were returned with the impact surface for ${targetSymbol}.`;
        path = `${targetSymbol} \u2192 no returned test coverage`;
        break;
      case "reverse_calls":
      case "overlay_caller":
        summary = depth <= 1 ? `${item.symbolName} calls or directly consumes ${targetSymbol}.` : `${item.symbolName} reaches ${targetSymbol} through ${depth} reverse call hops.`;
        path = `${item.symbolName} \u2014${edge || "CALLS_*"}${depth > 1 ? ` \xD7 ${depth}` : ""}\u2192 ${targetSymbol}`;
        break;
      case "forward_calls":
        summary = `${targetSymbol} calls or dispatches into ${item.symbolName}, so behavior can propagate forward.`;
        path = `${targetSymbol} \u2014${edge || "CALLS_*"}${depth > 1 ? ` \xD7 ${depth}` : ""}\u2192 ${item.symbolName}`;
        break;
      case "impacted_tests":
        summary = `${item.symbolName} exercises ${targetSymbol} or its downstream call spine.`;
        path = `${item.symbolName} \u2014test call path, ${depth} hop${depth === 1 ? "" : "s"}\u2192 ${targetSymbol}`;
        break;
      case "structural_inheritor":
        summary = `${item.symbolName} inherits an API or structural contract connected to ${targetSymbol}.`;
        path = `${item.symbolName} \u2014${edge || "INHERITED_API"}${depth > 1 ? ` \xD7 ${depth}` : ""}\u2192 ${targetSymbol}`;
        break;
      case "structural_api_carrier":
        summary = `${targetSymbol} carries or exposes the API surface ${item.symbolName}.`;
        path = `${targetSymbol} \u2014${edge || "HAS_API"}${depth > 1 ? ` \xD7 ${depth}` : ""}\u2192 ${item.symbolName}`;
        break;
      case "forward_affects":
        summary = `${item.symbolName} is in the precomputed downstream impact closure of ${targetSymbol}.`;
        path = `${targetSymbol} \u2014${edge || "AFFECTS"}${depth > 1 ? ` \xD7 ${depth}` : ""}\u2192 ${item.symbolName}`;
        break;
      default:
        summary = `${item.symbolName} was reached from ${targetSymbol} by the impact graph walk.`;
        path = `${targetSymbol} \u2014${edge || item.relation}, ${depth} hop${depth === 1 ? "" : "s"}\u2192 ${item.symbolName}`;
        break;
    }
    const risk = explainRisk(item);
    const evidence = [
      edge ? `edge ${edge}` : "",
      kind ? `walk ${kind}` : "",
      role ? `role ${role}` : "",
      `depth ${depth}`,
      `priority ${Math.round(item.utilityScore * 100)}%`,
      degraded ? "unsaved editor overlay" : "impact response",
      ...provenance.map((value) => `provenance ${value}`)
    ].filter(Boolean);
    return {
      summary,
      path,
      risk,
      evidence,
      caveat: item.synthetic ? "This warning is inferred from missing returned evidence; it does not prove that coverage is absent." : degraded ? "This connection comes from unsaved buffers and is name-based, so the impact surface is partial." : depth > 1 ? "The response identifies the traversal and hop count, but does not include every intermediate symbol." : void 0
    };
  }
  function explainRisk(item) {
    if (item.synthetic) {
      return "A change may ship without a directly identified regression test.";
    }
    if (item.category === "test") {
      return "The test may fail or need updated expectations when the target contract changes.";
    }
    if (item.category === "cross_repo") {
      return "The dependency crosses a service, package, or repository boundary where coordinated changes are easier to miss.";
    }
    if (isDocFile(item.filePath)) {
      return "Documentation can become stale even when the code continues to compile.";
    }
    if (item.category === "api" || item.category === "data") {
      return "This is a contract boundary; signature or schema changes can affect consumers that are not obvious at the call site.";
    }
    if (item.category === "event" || item.category === "config") {
      return "This connection is indirect or conditional, so it may only surface for particular runtime paths or settings.";
    }
    return item.depth !== void 0 && item.depth > 1 ? "The dependency is indirect; failures can surface away from the edited method." : "This is a direct consumer and may break when the target behavior or signature changes.";
  }
  function renderImpactExplanation(explanation) {
    return `
    <div class="impact-explanation" hidden>
      <p class="impact-explanation-summary">${escapeHtml(explanation.summary)}</p>
      <div class="impact-explanation-path">
        <span>Connection</span>
        <code>${escapeHtml(explanation.path)}</code>
      </div>
      <div class="impact-explanation-risk">
        <span>Why it matters</span>
        <p>${escapeHtml(explanation.risk)}</p>
      </div>
      <div class="impact-explanation-evidence" aria-label="Connection evidence">
        ${explanation.evidence.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}
      </div>
      ${explanation.caveat ? `<p class="impact-explanation-caveat">${escapeHtml(explanation.caveat)}</p>` : ""}
    </div>
  `;
  }
  function renderCollapsibleImpactGroup(title, count, rows, expanded, emptyMessage) {
    if (count === 0) {
      return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml(title)}</div>
        <div class="group-content empty">
          ${escapeHtml(emptyMessage)}
        </div>
      </div>
    `;
    }
    return `
    <div class="impact-group ${expanded ? "expanded" : ""}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">\u203A</span>
        <strong>${escapeHtml(title)}</strong>
        <span>(${count})</span>
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
      return renderCollapsibleImpactGroup(title, 0, "", expanded, "No related symbols found.");
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
    return renderCollapsibleImpactGroup(title, uniquePaths.length, rows, expanded, "");
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

  // src/webview/shared/impactTransforms.ts
  function impactResponseFromPromptContext(context) {
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
  function hydrateFromPromptContext(context) {
    const impact = impactResponseFromPromptContext(context);
    return {
      summary: buildContextSummary(context),
      impact,
      symbol: context.primary_source.symbol,
      filePath: context.primary_source.file_path,
      depth: clampImpactDepth(impact.max_depth || 3)
    };
  }

  // src/webview/shared/surfaceChrome.ts
  function renderImpactSurfaceShell(chrome, subtitle, body) {
    return `
    <section class="surface surface-impact" aria-label="Impact analysis">
      ${chrome}
      <div class="surface-title">Impact Analysis</div>
      <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
      ${body}
    </section>
  `;
  }

  // src/webview/shared/inspectorLayout.ts
  function renderTable(headers, bodyRows, tableClass = "") {
    const classAttr = tableClass ? ` class="${tableClass}"` : "";
    const head = headers.map((header) => `<th>${escapeHtml(header)}</th>`).join("");
    return `
    <table${classAttr}>
      <thead>
        <tr>${head}</tr>
      </thead>
      <tbody>
        ${bodyRows}
      </tbody>
    </table>
  `;
  }
  function renderJsonViewer(jsonStr, copyAction, infoHtml = "") {
    return `
    <div class="json-viewer">
      ${infoHtml}
      <button class="copy-button" data-action="${copyAction}">Copy JSON</button>
      <pre><code>${escapeHtml(jsonStr)}</code></pre>
    </div>
  `;
  }
  function renderIntentTab(matches) {
    if (matches === null) {
      return `<div class="inspector-tab-content"><p style="color:var(--vscode-descriptionForeground);">Classifying intent\u2026</p></div>`;
    }
    if (matches.length === 0) {
      return `<div class="inspector-tab-content"><p style="color:var(--vscode-descriptionForeground);">No role matched above threshold for this question.</p></div>`;
    }
    const rows = matches.map((m) => {
      const pct = Math.max(0, Math.min(100, Math.round(m.similarity * 100)));
      return `
        <div style="margin:0 0 12px;">
          <div style="display:flex;justify-content:space-between;align-items:baseline;">
            <span style="font-weight:600;">${escapeHtml(m.role)}</span>
            <span style="font-variant-numeric:tabular-nums;color:var(--vscode-descriptionForeground);">${m.similarity.toFixed(2)}</span>
          </div>
          <div style="height:6px;background:var(--vscode-editorWidget-border,#444);border-radius:3px;overflow:hidden;margin:3px 0 4px;">
            <div style="height:100%;width:${pct}%;background:var(--vscode-progressBar-background,#0a84ff);"></div>
          </div>
          <div style="font-size:12px;color:var(--vscode-descriptionForeground);">${escapeHtml(m.description)}</div>
        </div>
      `;
    }).join("");
    return `
    <div class="inspector-tab-content">
      <p style="color:var(--vscode-descriptionForeground);font-size:12px;margin:0 0 12px;">
        Role intent the retrieval classifier inferred from the question (embedding cosine vs role descriptions) \u2014 this drives which axes are searched.
      </p>
      ${rows}
    </div>
  `;
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
        <h3>${escapeHtml(symbolName)}</h3>
        <span class="dirty-badge">${isDirty}</span>
      </div>
      <div class="file-path">
        <strong>File:</strong> ${escapeHtml(filePath)}
      </div>
      ${code ? `
        <div class="code-snippet">
          <pre><code>${escapeHtml(code)}</code></pre>
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
      <tr class="context-row" data-file-path="${escapeHtml(item.file_path)}">
        <td class="symbol-col">${escapeHtml(item.symbol)}</td>
        <td class="relation-col">${escapeHtml(item.relation || "")}</td>
        <td class="depth-col">${item.depth || 0}</td>
        <td class="score-col">${(item.relevance_score || 0).toFixed(2)}</td>
        <td class="dirty-col">${item.is_dirty ? "\u{1F534}" : "\u2713"}</td>
        <td class="file-col">${escapeHtml(item.file_path)}</td>
      </tr>
    `).join("");
    return `
    <div class="graph-context-table">
      ${renderTable(["Symbol", "Relation", "Depth", "Score", "Dirty", "File"], rows)}
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
          <strong>Source:</strong> ${escapeHtml(doc.source_file)}
          <span class="score">${(doc.score || 0).toFixed(2)}</span>
        </div>
        <div class="doc-content">
          ${escapeHtml((doc.content || "").substring(0, 500))}${(doc.content || "").length > 500 ? "..." : ""}
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
    return renderJsonViewer(JSON.stringify(context, null, 2), "copy-json");
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
        <td>${escapeHtml(r.tier)}</td>
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
      ${renderTable(["Tier", "Tokens", "% of Total"], rows, "tier-table")}
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
    return renderJsonViewer(
      jsonStr,
      "copy-api-json",
      `<div class="json-info">
        <p>This is the final JSON sent to the Claude API (system prompt + context).</p>
        <p>The <code>system</code> field contains the assembled surgical context.</p>
      </div>`
    );
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

  // src/webview/shared/settingsDefaults.ts
  var DEFAULT_SETTINGS = {
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

  // src/webview/shared/settingsLayout.ts
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
  function renderTextInput(spec) {
    const type = spec.type || "text";
    const inputAttrs = [
      `type="${type}"`,
      `id="${spec.id}"`,
      'class="setting-input"',
      `value="${escapeHtml(spec.value)}"`,
      spec.placeholder ? `placeholder="${escapeHtml(spec.placeholder)}"` : "",
      `aria-label="${escapeHtml(spec.label)}"`,
      `aria-describedby="${spec.id}-hint"`,
      spec.min !== void 0 ? `min="${spec.min}"` : "",
      spec.max !== void 0 ? `max="${spec.max}"` : "",
      spec.step !== void 0 ? `step="${spec.step}"` : ""
    ].filter(Boolean).join(" ");
    const inputHtml = spec.testAction ? `<div class="field-group"><input ${inputAttrs} /><button class="field-action-btn" data-action="${spec.testAction.action}" aria-label="Test connection">${spec.testAction.label}</button></div>` : `<input ${inputAttrs} />`;
    const statusHtml = spec.showStatus ? `<div class="field-status" id="${spec.id}-status"></div>` : "";
    return `
    <div class="setting-field">
      <label for="${spec.id}">${escapeHtml(spec.label)}</label>
      ${inputHtml}
      <p class="field-hint" id="${spec.id}-hint">${spec.hint}</p>
      ${statusHtml}
    </div>
  `;
  }
  function renderSelect(spec) {
    const options = spec.options.map((option) => `<option value="${escapeHtml(option.value)}" ${spec.value === option.value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("");
    return `
    <div class="setting-field">
      <label for="${spec.id}">${escapeHtml(spec.label)}</label>
      <select id="${spec.id}" class="setting-input" aria-label="${escapeHtml(spec.label)}" aria-describedby="${spec.id}-hint">
        ${options}
      </select>
      <p class="field-hint" id="${spec.id}-hint">${spec.hint}</p>
    </div>
  `;
  }
  function renderCheckbox(spec) {
    return `
    <div class="setting-field checkbox-field">
      <label for="${spec.id}">
        <input type="checkbox" id="${spec.id}" class="setting-checkbox" ${spec.checked ? "checked" : ""} aria-describedby="${spec.id}-hint" />
        <span>${escapeHtml(spec.caption)}</span>
      </label>
      <p class="field-hint" id="${spec.id}-hint">${spec.hint}</p>
    </div>
  `;
  }
  function renderGraphStatus(data) {
    const statusClass = data.graphStatusHealthy ? "success" : "warning";
    return `
    <div class="setting-field">
      <label>Graph provider status</label>
      <div class="field-status ${statusClass}" style="display:block">
        ${escapeHtml(data.graphStatusLabel || "Unknown")}
      </div>
      <p class="field-hint">${escapeHtml(data.graphStatusDetail || "Open Settings to refresh status from /status/cloud.")}</p>
    </div>
  `;
  }
  function renderSettingsForm(data) {
    const connectionFields = [
      renderTextInput({
        id: "backendUrl",
        label: "Sidecar URL",
        value: data.backendUrl,
        placeholder: "http://localhost:8000",
        hint: "Base URL where the Surgical Context context_engine is running",
        testAction: { action: "testUrl", label: "Test" },
        showStatus: true
      }),
      renderTextInput({
        id: "authToken",
        label: "Auth Token (Optional)",
        value: data.authToken,
        type: "password",
        placeholder: "Leave blank if no authentication required",
        hint: "Token for authenticating with the context_engine if required"
      })
    ].join("");
    const workspaceFields = [
      renderTextInput({
        id: "workspaceId",
        label: "Workspace ID",
        value: data.workspaceId,
        placeholder: "derived from workspace and Git branch",
        hint: "Optional override. Leave blank to derive from the open workspace and Git branch."
      }),
      renderSelect({
        id: "modelPreference",
        label: "Model Preference",
        value: data.modelPreference,
        hint: "Preferred context_engine model route for local asks",
        options: [
          { value: "auto", label: "Auto" },
          { value: "claude", label: "Claude" },
          { value: "ollama", label: "Ollama" }
        ]
      }),
      renderTextInput({
        id: "tokenBudget",
        label: "Token Budget",
        value: String(data.tokenBudget),
        type: "number",
        min: 1e3,
        max: 32e3,
        step: 500,
        hint: "Default context budget used for ask and streaming ask requests",
        showStatus: true
      })
    ].join("");
    const graphFields = [
      renderTextInput({
        id: "neo4jUri",
        label: "Neo4j URI",
        value: data.neo4jUri,
        placeholder: "bolt://localhost:7687",
        hint: "Sidecar reads NEO4J_URI from the repo <code>.env</code>. Match this for documentation; start graph with <code>docker compose up -d neo4j</code>."
      }),
      renderSelect({
        id: "indexProfile",
        label: "Index profile",
        value: data.indexProfile,
        hint: "Set INDEX_PROFILE in context_engine <code>.env</code> to the same value, then restart context_engine and reindex.",
        options: [
          { value: "axis_python_v1", label: "axis_python_v1" },
          { value: "legacy", label: "legacy" }
        ]
      }),
      renderGraphStatus(data)
    ].join("");
    const storageFields = `
    <div class="setting-grid">
      ${renderTextInput({
      id: "lancedbPath",
      label: "LanceDB Path",
      value: data.lancedbPath,
      placeholder: "./data/lancedb",
      hint: "Local vector index path used by the context_engine environment"
    })}
      ${renderTextInput({
      id: "historyPath",
      label: "History DB Path",
      value: data.historyPath,
      placeholder: "./data/history/surgical_context.sqlite3",
      hint: "Planned local SQLite history path for dialogs and snapshots"
    })}
    </div>
  `;
    const behaviorFields = [
      renderCheckbox({
        id: "overlaySync",
        caption: "Send unsaved content to context_engine",
        checked: data.overlaySync,
        hint: "When enabled, unsaved editor changes are sent with asks so answers reflect in-memory code"
      }),
      renderCheckbox({
        id: "autoOpenInspector",
        caption: "Auto-open Context Inspector",
        checked: data.autoOpenInspector,
        hint: "Automatically open the Inspector tab after a completed ask"
      })
    ].join("");
    return `
    <div class="settings-form">
      <div class="settings-header">
        <h2>Surgical Context Settings</h2>
        <p class="settings-description">Configure your Surgical Context environment and preferences.</p>
      </div>

      <div class="settings-section">
        <h3>Connection</h3>
        ${connectionFields}
      </div>

      <div class="settings-section">
        <h3>Workspace</h3>
        ${workspaceFields}
      </div>

      <div class="settings-section">
        <h3>Graph (Neo4j)</h3>
        ${graphFields}
      </div>

      <div class="settings-section">
        <h3>Local Storage</h3>
        ${storageFields}
      </div>

      <div class="settings-section">
        <h3>Behavior</h3>
        ${behaviorFields}
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
  function inputValue(id) {
    return document.getElementById(id)?.value || "";
  }
  function selectValue(id, fallback) {
    return document.getElementById(id)?.value || fallback;
  }
  function checkboxValue(id) {
    return document.getElementById(id)?.checked || false;
  }
  function readSettingsFormFromDom() {
    return {
      backendUrl: inputValue("backendUrl"),
      workspaceId: inputValue("workspaceId"),
      modelPreference: selectValue("modelPreference", DEFAULT_SETTINGS.modelPreference),
      authToken: inputValue("authToken"),
      tokenBudget: Number(inputValue("tokenBudget") || String(DEFAULT_SETTINGS.tokenBudget)),
      lancedbPath: inputValue("lancedbPath"),
      historyPath: inputValue("historyPath"),
      neo4jUri: inputValue("neo4jUri"),
      indexProfile: selectValue("indexProfile", DEFAULT_SETTINGS.indexProfile),
      overlaySync: checkboxValue("overlaySync"),
      autoOpenInspector: checkboxValue("autoOpenInspector")
    };
  }
  function applySettingsDefaultsToDom(defaults = DEFAULT_SETTINGS) {
    const setInput = (id, value) => {
      const element = document.getElementById(id);
      if (element) element.value = value;
    };
    const setSelect = (id, value) => {
      const element = document.getElementById(id);
      if (element) element.value = value;
    };
    const setCheckbox = (id, checked) => {
      const element = document.getElementById(id);
      if (element) element.checked = checked;
    };
    setInput("backendUrl", defaults.backendUrl);
    setInput("workspaceId", defaults.workspaceId);
    setSelect("modelPreference", defaults.modelPreference);
    setInput("authToken", defaults.authToken);
    setInput("tokenBudget", String(defaults.tokenBudget));
    setInput("lancedbPath", defaults.lancedbPath);
    setInput("historyPath", defaults.historyPath);
    setInput("neo4jUri", defaults.neo4jUri);
    setSelect("indexProfile", defaults.indexProfile);
    setCheckbox("overlaySync", defaults.overlaySync);
    setCheckbox("autoOpenInspector", defaults.autoOpenInspector);
  }
  function validateSettingsForm(values) {
    if (values.backendUrl && !values.backendUrl.startsWith("http://") && !values.backendUrl.startsWith("https://")) {
      return { fieldId: "backendUrl", message: "URL must start with http:// or https://" };
    }
    if (!Number.isFinite(values.tokenBudget) || values.tokenBudget < 1e3 || values.tokenBudget > 32e3) {
      return { fieldId: "tokenBudget", message: "Use a value from 1000 to 32000" };
    }
    return null;
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
      this.intentMatches = null;
      this.pendingPrompt = null;
      this.pendingAskAnchor = null;
      this.currentImpact = null;
      this.currentImpactSymbol = null;
      this.currentImpactFilePath = null;
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
        if (!isTrustedHostWebviewMessage(event)) {
          return;
        }
        const message = event.data;
        switch (message.type) {
          case "surface.init":
            this.state = message.state;
            if (message.state.lastContext && !this.currentPromptContext) {
              this.currentPromptContext = message.state.lastContext;
              this.applyHydratedContext(message.state.lastContext);
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
                context_engineHealth: message.context_engineHealth,
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
            this.currentImpactFilePath = message.impact.file_path || null;
            this.currentImpact = message.impact;
            this.currentImpactDepth = clampImpactDepth(message.impact.max_depth || this.currentImpactDepth);
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
            this.intentMatches = null;
            if (message.context) {
              this.applyHydratedContext(message.context);
            }
            this.render();
            break;
          case "inspector.intentLoaded":
            this.intentMatches = message.intentMatches;
            if (this.surface === "inspector" && this.inspectorTab === "intent") {
              this.render();
            }
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
      mountLayoutHtml(root, this.renderCurrentSurface());
      this.attachEventListeners();
      this.restoreComposerDraft();
      this.updateConversationView();
    }
    renderLoadingShell() {
      const root = document.getElementById("root");
      if (!root) return;
      mountLayoutHtml(root, `
      <section class="surface surface-chat" aria-label="Surgical Context loading">
        ${this.renderSurfaceTabs()}
        <div class="loading-state">Loading Surgical Context...</div>
      </section>
    `);
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
        ${renderComposerDock(Boolean(this.currentStreamingRequestId))}
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
      const chrome = this.renderChrome();
      if (this.impactLoading) {
        return renderImpactSurfaceShell(chrome, subtitle, '<div class="loading-state">Loading impact analysis...</div>');
      }
      if (this.impactError) {
        return renderImpactSurfaceShell(
          chrome,
          subtitle,
          `<div class="error-state">${escapeHtml(this.impactError)}</div>
          <button class="secondary-action" data-action="openChat">Back to Ask</button>`
        );
      }
      if (!this.currentImpact) {
        return renderImpactSurfaceShell(
          chrome,
          subtitle,
          `<div class="empty-state">Select a symbol to see its impact.</div>
          <button class="primary-action" data-action="showImpact">Analyze Current Symbol</button>`
        );
      }
      return renderImpactSurfaceShell(
        chrome,
        subtitle,
        `${renderImpactWorkspace(
          this.currentImpact,
          symbol,
          this.currentImpactSource === "prompt" ? "prompt context" : "live graph",
          { depth: this.currentImpactDepth }
        )}
        <div class="surface-footer">
          <span>${this.currentImpactSource === "prompt" ? "From selected ask" : "Graph built just now"}</span>
          <button class="icon-action" data-action="showImpact" title="Refresh impact">Refresh</button>
        </div>`
      );
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
            ${this.renderInspectorTabButton("intent", "Intent")}
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
        case "intent":
          return renderIntentTab(this.intentMatches);
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
      bindDataActions(document, (event) => this.handleAction(event));
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
      if (action === "copy" || action === "copy-json" || action === "copy-api-json" || action === "feedback") {
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
          this.prefillImpactAsk(
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
        case "explainImpact":
          this.toggleImpactExplanation(target);
          break;
        case "create-refactor-plan":
          this.prefillImpactAsk(
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
        case "copy-json":
        case "copy-api-json":
          this.copyInspectorJson(target);
          break;
        case "stopStreaming":
          this.stopStreaming();
          break;
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
      this.currentImpactSource = null;
      this.currentImpactDepth = 3;
      this.currentImpactSymbol = impactSymbol;
      this.currentImpactFilePath = impactFilePath;
    }
    impactTarget() {
      if (this.currentImpactSource === "graph" && this.currentImpactSymbol) {
        return {
          symbol: this.currentImpactSymbol,
          filePath: this.currentImpactFilePath || void 0
        };
      }
      if (this.currentPromptContext) {
        return {
          symbol: this.currentPromptContext.primary_source.symbol,
          filePath: this.currentPromptContext.primary_source.file_path
        };
      }
      if (this.selectedPromptRequestId && this.currentImpactSymbol) {
        return {
          symbol: this.currentImpactSymbol,
          filePath: this.currentImpactFilePath || void 0
        };
      }
      return {
        symbol: this.state?.workspace.selectedSymbol || void 0,
        filePath: this.state?.workspace.activeFile || void 0
      };
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
      if (!composer || !composer.value.trim() || !this.state) return;
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
      this.updateComposerStreamingState(false);
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
      this.updateComposerStreamingState(false);
    }
    onRequestStopped(requestId) {
      const message = this.messages.get(requestId);
      if (message) {
        message.status = "done";
        this.updateConversationView();
      }
      this.currentStreamingRequestId = null;
      this.persistState();
      this.updateComposerStreamingState(false);
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
      bindDataActions(viewport, (event) => this.handleAction(event));
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
        const promptMessage = Array.from(this.messages.values()).find((message) => message.type === "user" && message.requestId === requestId);
        this.resetPromptDerivedState(promptMessage?.symbol || null, null);
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
})();
//# sourceMappingURL=main.js.map
