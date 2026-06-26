// src/webview/shared/domActions.ts
function bindClickAction(root, action, handler) {
  const button = root.querySelector(`[data-action="${action}"]`);
  if (button) {
    button.addEventListener("click", handler);
  }
}
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
function toggleAriaExpandedSection(header, content, group, expandedClass) {
  const expanded = header.getAttribute("aria-expanded") === "true";
  header.setAttribute("aria-expanded", String(!expanded));
  content.toggleAttribute("hidden", expanded);
  content.classList.toggle("expanded", !expanded);
  if (expandedClass) {
    group.classList.toggle(expandedClass, !expanded);
  }
  return !expanded;
}
function replaceElementHtml(element, html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  sanitizeParsedDocument(doc);
  const replacement = doc.body.firstElementChild;
  if (replacement) {
    element.replaceWith(replacement);
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
  const userAttrs = message.type === "user" ? `${selectionAttrs} title="${escapeHtml(message.content)}"` : "";
  const errorBlock = message.type !== "user" && message.error ? `<div class="message-error">Error: ${escapeHtml(message.error)}</div>` : "";
  return `
    <article class="${baseClass}${statusClass}" data-message-id="${escapeHtml(message.id)}"${requestAttrs}${userAttrs}>
      <div class="message-content">${escapeHtml(message.content)}</div>
      ${errorBlock}
      ${renderMessageFooter(message)}
    </article>
  `;
}
function renderMessageFooter(message) {
  const time = formatMessageTime(message.timestamp);
  const route = formatModelRoute(message);
  const assistantFeedback = message.type === "assistant" && message.status === "done" ? `
        <button class="message-action-button" data-action="feedback" data-rating="up" title="Helpful" aria-label="Helpful">+</button>
        <button class="message-action-button" data-action="feedback" data-rating="down" title="Not helpful" aria-label="Not helpful">-</button>
      ` : "";
  let routeMarkup = "";
  if (route) {
    const routeClass = route.fallback ? "fallback" : "";
    routeMarkup = `<span class="message-route ${routeClass}" title="${escapeHtml(route.title)}">${escapeHtml(route.label)}</span>`;
  }
  return `
    <div class="message-footer">
      <time class="message-time" datetime="${escapeHtml(time.iso)}" title="${escapeHtml(time.title)}">${escapeHtml(time.label)}</time>
      ${routeMarkup}
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
  return presentModelRoute(route);
}
function presentModelRoute(route) {
  const provider = routeText(route.provider) || "unknown";
  const model = routeText(route.model);
  const preference = routeText(route.preference);
  const reason = routeText(route.reason);
  const degraded = Boolean(route.degraded);
  const fallback = degraded || reason.includes("fallback") || reason.includes("unavailable");
  const reasonText = routeReasonLabel(reason);
  const routeName = [provider, model].filter(Boolean).join(" / ") || provider;
  const label = `${routeName}${fallback ? " \xB7 fallback" : ""}`;
  const title = [
    `Answered by ${routeName}`,
    preference ? `Preference: ${preference}` : "",
    reasonText,
    degraded ? "Response was degraded." : ""
  ].filter(Boolean).join(" | ");
  return { label, title, fallback };
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
function renderAccordionRow(label, value) {
  return `
    <div class="accordion-row">
      <div class="accordion-label">${escapeHtml(label)}</div>
      <div class="accordion-value">${typeof value === "number" ? value : escapeHtml(value)}</div>
    </div>
  `;
}
function renderPlaceholderAccordion(id, title, expanded) {
  return renderAccordion(id, title, "Run an ask to populate this section.", expanded);
}
function renderEnvironmentAccordion(state, expanded = false) {
  const rows = [
    renderAccordionRow("Workspace", state.workspace),
    renderAccordionRow("Cloud", state.cloud),
    renderAccordionRow("Mode", state.mode),
    ...state.symbol ? [renderAccordionRow("Symbol", state.symbol)] : []
  ];
  return renderAccordion("environment", "Environment", rows.join(""), expanded);
}
function renderContextSummaryAccordion(summary, expanded = false) {
  if (!summary) {
    return renderPlaceholderAccordion("contextSummary", "Context Summary", expanded);
  }
  const content = [
    renderAccordionRow("Primary", summary.primaryLabel),
    renderAccordionRow("Graph Symbols", summary.graphCount),
    renderAccordionRow("Doc Chunks", summary.docsCount),
    renderAccordionRow("Tokens", summary.tokenText),
    `<div class="accordion-chips">${summary.chips.map(renderContextChip).join("")}</div>`
  ].join("");
  return renderAccordion("contextSummary", "Context Summary", content, expanded);
}
function renderContextChip(chip) {
  const className = chip.startsWith("warning:") ? "chip warning" : "chip";
  const label = chip.startsWith("warning:") ? chip.slice("warning:".length) : chip;
  return `<span class="${className}">${escapeHtml(label)}</span>`;
}
function renderAdvancedInfoAccordion(info, expanded = false) {
  if (!info) {
    return renderPlaceholderAccordion("advancedInfo", "Advanced Info", expanded);
  }
  const content = [
    renderAccordionRow("Intent", info.intent),
    renderAccordionRow("Tiers Used", info.tiersUsed.map(escapeHtml).join(", ")),
    renderAccordionRow("Has Unsaved Changes", info.isDirty ? "Yes" : "No")
  ].join("");
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
function createUserChatMessage(requestId, content, symbol) {
  const id = `msg-${Date.now()}`;
  return {
    id,
    requestId,
    type: "user",
    content,
    timestamp: Date.now(),
    symbol
  };
}
function createAssistantChatMessage(requestId, symbol, error, status = "streaming") {
  return {
    id: requestId,
    requestId,
    type: "assistant",
    content: "",
    timestamp: Date.now(),
    symbol,
    status,
    error
  };
}
function resizeComposerToFit(textarea, maxHeightPx = 220) {
  textarea.style.height = "auto";
  const scrollHeight = textarea.scrollHeight;
  const newHeight = Math.min(scrollHeight, maxHeightPx);
  textarea.style.height = `${newHeight}px`;
  textarea.style.overflow = scrollHeight > maxHeightPx ? "auto" : "hidden";
}

// src/webview/shared/impactLayout.ts
const REACH_CATEGORIES = /* @__PURE__ */ new Set(["event", "config", "data", "api"]);
const HIGH_SEVERITY_CATEGORIES = /* @__PURE__ */ new Set(["api", "data", "cross_repo"]);
const SEVERITY_BASE_SCORE = {
  high: 0.88,
  medium: 0.66,
  low: 0.42
};
const CATEGORY_UTILITY_BOOST = {
  api: 0.08,
  data: 0.08,
  event: 0.05
};
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
  if (HIGH_SEVERITY_CATEGORIES.has(category)) return "high";
  return depth === void 0 || depth <= 1 ? "high" : "medium";
}
function classifyZone(category, depth, filePath) {
  if (category === "test" || category === "cross_repo" || isDocFile(filePath)) return "risk";
  if (REACH_CATEGORIES.has(category)) return "reach";
  return depth === void 0 || depth <= 1 ? "direct" : "reach";
}
function fallbackUtility(severity, category, depth) {
  const base = SEVERITY_BASE_SCORE[severity];
  const categoryBoost = CATEGORY_UTILITY_BOOST[category] ?? 0;
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
const DEFAULT_CALLS_EDGE_LABEL = "CALLS_*";
function impactHopSuffix(depth) {
  return depth > 1 ? ` \xD7 ${depth}` : "";
}
function impactHopsLabel(depth) {
  return `${depth} hop${depth === 1 ? "" : "s"}`;
}
function explainCoverageGap(ctx) {
  return {
    summary: `No test symbols or test files were returned with the impact surface for ${ctx.targetSymbol}.`,
    path: `${ctx.targetSymbol} \u2192 no returned test coverage`
  };
}
function explainReverseCalls(ctx) {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: depth <= 1 ? `${item.symbolName} calls or directly consumes ${targetSymbol}.` : `${item.symbolName} reaches ${targetSymbol} through ${depth} reverse call hops.`,
    path: `${item.symbolName} \u2014${edge || DEFAULT_CALLS_EDGE_LABEL}${impactHopSuffix(depth)}\u2192 ${targetSymbol}`
  };
}
function explainForwardCalls(ctx) {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${targetSymbol} calls or dispatches into ${item.symbolName}, so behavior can propagate forward.`,
    path: `${targetSymbol} \u2014${edge || DEFAULT_CALLS_EDGE_LABEL}${impactHopSuffix(depth)}\u2192 ${item.symbolName}`
  };
}
function explainImpactedTests(ctx) {
  const { item, targetSymbol, depth } = ctx;
  return {
    summary: `${item.symbolName} exercises ${targetSymbol} or its downstream call spine.`,
    path: `${item.symbolName} \u2014test call path, ${impactHopsLabel(depth)}\u2192 ${targetSymbol}`
  };
}
function explainStructuralInheritor(ctx) {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${item.symbolName} inherits an API or structural contract connected to ${targetSymbol}.`,
    path: `${item.symbolName} \u2014${edge || "INHERITED_API"}${impactHopSuffix(depth)}\u2192 ${targetSymbol}`
  };
}
function explainStructuralApiCarrier(ctx) {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${targetSymbol} carries or exposes the API surface ${item.symbolName}.`,
    path: `${targetSymbol} \u2014${edge || "HAS_API"}${impactHopSuffix(depth)}\u2192 ${item.symbolName}`
  };
}
function explainForwardAffects(ctx) {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${item.symbolName} is in the precomputed downstream impact closure of ${targetSymbol}.`,
    path: `${targetSymbol} \u2014${edge || "AFFECTS"}${impactHopSuffix(depth)}\u2192 ${item.symbolName}`
  };
}
function explainDefaultImpact(ctx) {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${item.symbolName} was reached from ${targetSymbol} by the impact graph walk.`,
    path: `${targetSymbol} \u2014${edge || item.relation}, ${impactHopsLabel(depth)}\u2192 ${item.symbolName}`
  };
}
const IMPACT_KIND_EXPLAINERS = {
  coverage_gap: explainCoverageGap,
  reverse_calls: explainReverseCalls,
  overlay_caller: explainReverseCalls,
  forward_calls: explainForwardCalls,
  impacted_tests: explainImpactedTests,
  structural_inheritor: explainStructuralInheritor,
  structural_api_carrier: explainStructuralApiCarrier,
  forward_affects: explainForwardAffects
};
function buildImpactEvidence(item, options) {
  const { edge, kind, role, depth, degraded, provenance } = options;
  return [
    edge ? `edge ${edge}` : "",
    kind ? `walk ${kind}` : "",
    role ? `role ${role}` : "",
    `depth ${depth}`,
    `priority ${Math.round(item.utilityScore * 100)}%`,
    degraded ? "unsaved editor overlay" : "impact response",
    ...provenance.map((value) => `provenance ${value}`)
  ].filter(Boolean);
}
function explainImpactCaveat(item, options) {
  const { depth, degraded } = options;
  if (item.synthetic) {
    return "This warning is inferred from missing returned evidence; it does not prove that coverage is absent.";
  }
  if (degraded) {
    return "This connection comes from unsaved buffers and is name-based, so the impact surface is partial.";
  }
  if (depth > 1) {
    return "The response identifies the traversal and hop count, but does not include every intermediate symbol.";
  }
  return void 0;
}
function explainImpactItem(item, targetSymbol) {
  const kind = stringField(item.source, "kind") || arrayField(item.source, "satisfying_kinds")[0] || item.relation || item.category;
  const edge = stringField(item.source, "edge_type", "relation") || item.relation;
  const role = stringField(item.source, "role");
  const provenance = arrayField(item.source, "provenance");
  const depth = item.depth ?? 1;
  const degraded = item.source.degraded === true;
  const ctx = { item, targetSymbol, edge, depth };
  const explainer = IMPACT_KIND_EXPLAINERS[kind] ?? explainDefaultImpact;
  const { summary, path } = explainer(ctx);
  return {
    summary,
    path,
    risk: explainRisk(item),
    evidence: buildImpactEvidence(item, {
      edge,
      kind,
      role,
      depth,
      degraded,
      provenance
    }),
    caveat: explainImpactCaveat(item, { depth, degraded })
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
  return value.map(String).filter(Boolean);
}
function isTestFile(filePath) {
  const path = filePath.toLowerCase();
  const testDirectorySegment = /(^|[/.])(tests?|specs?|__tests__)([/.]|$)/;
  const jsTsTestSuffix = /[._](test|spec)\.[jt]sx?$/;
  return testDirectorySegment.test(path) || jsTsTestSuffix.test(path) || path.endsWith(".py") && path.includes("test_") || path.endsWith("_test.py");
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
const SURFACE_FROM_HOST_MESSAGE = {
  "surface.showChat": "chat",
  "surface.showInspector": "inspector",
  "surface.showImpact": "impact",
  "surface.showSettings": "settings"
};
const SURFACE_FROM_DOM_ACTION = {
  openChat: "chat",
  openInspector: "inspector",
  openSettings: "settings",
  showImpact: "impact"
};
const MAIN_SURFACE_TABS = [
  { id: "chat", label: "Chat", icon: "\u25CC" },
  { id: "inspector", label: "Inspector", icon: "\u25CE" },
  { id: "impact", label: "Impact", icon: "\u2301" }
];
function renderSurfaceNavTab(options) {
  const surfaceAttr = options.surface ? ` data-surface="${options.surface}"` : "";
  return `
    <button
      class="surface-tab ${options.active ? "active" : ""}"
      data-action="${options.action}"${surfaceAttr}
      aria-current="${options.active ? "page" : "false"}"
      title="${options.label}"
      aria-label="${options.label}"
    >
      <span aria-hidden="true">${options.icon}</span>
    </button>
  `;
}
function renderMainSurfaceTabBar(activeSurface, chatSessionActionsHtml) {
  return `
    <nav class="surface-tab-bar" aria-label="Surgical Context sections">
      <div class="surface-tab-group">
        ${MAIN_SURFACE_TABS.map((tab) => renderSurfaceNavTab({
    label: tab.label,
    icon: tab.icon,
    active: activeSurface === tab.id,
    action: "switchSurface",
    surface: tab.id
  })).join("")}
        ${renderSurfaceNavTab({
    label: "Dashboard",
    icon: "\u25A6",
    action: "openDashboard"
  })}
      </div>
      <div class="surface-tab-actions">
        ${activeSurface === "chat" ? chatSessionActionsHtml : ""}
        ${renderSurfaceNavTab({
    label: "Settings",
    icon: "\u2699",
    active: activeSurface === "settings",
    action: "switchSurface",
    surface: "settings"
  })}
      </div>
    </nav>
  `;
}
function renderSurfaceShell(surfaceClass, ariaLabel, chrome, body) {
  return `
    <section class="surface ${surfaceClass}" aria-label="${escapeHtml(ariaLabel)}">
      ${chrome}
      ${body}
    </section>
  `;
}
function renderImpactSurfaceShell(chrome, subtitle, body) {
  return renderSurfaceShell(
    "surface-impact",
    "Impact analysis",
    chrome,
    `
      <div class="surface-title">Impact Analysis</div>
      <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
      ${body}
    `
  );
}
function renderInspectorSurfaceShell(chrome, body) {
  return renderSurfaceShell("surface-inspector", "Context inspector", chrome, body);
}

// src/webview/shared/inspectorLayout.ts
const INSPECTOR_TABS = [
  { id: "primary", label: "Primary" },
  { id: "intent", label: "Intent" },
  { id: "graph", label: "Graph" },
  { id: "docs", label: "Docs" },
  { id: "tokens", label: "Tokens" },
  { id: "json", label: "JSON" },
  { id: "api", label: "API" }
];
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
function lineFromContextSymbol(item) {
  const explicit = item.line ?? item.start_line ?? item.lineno;
  if (typeof explicit === "number" && Number.isFinite(explicit)) {
    return Math.max(1, explicit);
  }
  if (Array.isArray(item.range) && typeof item.range[0] === "number") {
    return Math.max(1, item.range[0]);
  }
  return 1;
}
function isOpenableFilePath(filePath) {
  return Boolean(filePath && filePath !== "unknown" && !filePath.startsWith("<"));
}
function renderOpenFileButton(label, filePath, line, className, title) {
  if (!isOpenableFilePath(filePath)) {
    return `<span class="${className}">${escapeHtml(label)}</span>`;
  }
  return `
    <button
      type="button"
      class="${className} inspector-open-file"
      data-action="openFile"
      data-file-path="${escapeHtml(filePath)}"
      data-line="${line}"
      title="${escapeHtml(title)}"
    >
      ${escapeHtml(label)}
    </button>
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
  const rows = graphItems.map((item) => {
    const filePath = item.file_path || "unknown";
    const symbol = item.symbol || "unknown";
    const line = lineFromContextSymbol(item);
    return `
      <tr class="context-row">
        <td class="symbol-col">
          ${renderOpenFileButton(symbol, filePath, line, "graph-symbol-link", `Open ${symbol}`)}
        </td>
        <td class="relation-col">${escapeHtml(item.relation || "")}</td>
        <td class="depth-col">${item.depth || 0}</td>
        <td class="score-col">${(item.relevance_score || 0).toFixed(2)}</td>
        <td class="dirty-col">${item.is_dirty ? "\u{1F534}" : "\u2713"}</td>
        <td class="file-col">
          ${renderOpenFileButton(filePath, filePath, line, "graph-file-link", `Open ${filePath}`)}
        </td>
      </tr>
    `;
  }).join("");
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
function renderInspectorTabButton(tab, label, activeTab) {
  return `
    <button
      class="tab-button ${activeTab === tab ? "active" : ""}"
      data-action="switchInspectorTab"
      data-inspector-tab="${tab}"
      role="tab"
      aria-selected="${activeTab === tab}"
    >
      ${label}
    </button>
  `;
}
function renderInspectorTabContent(activeTab, context, intentMatches) {
  switch (activeTab) {
    case "intent":
      return renderIntentTab(intentMatches);
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
function renderInspectorSurfaceView(chrome, context, activeTab, subtitle, intentMatches) {
  if (!context) {
    return renderInspectorSurfaceShell(
      chrome,
      `
        <div class="surface-title">Context Inspector</div>
        <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
        <div class="empty-state">
          No prompt context yet. Ask a question first, then come back here.
        </div>
        <button class="primary-action surface-inline-action" data-action="openChat">Open Chat</button>
      `
    );
  }
  return renderInspectorSurfaceShell(
    chrome,
    `
      <div class="inspector-header">
        <h2>Context Inspector</h2>
        <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
        <div class="inspector-tab-bar" role="tablist" aria-label="Context detail tabs">
          ${INSPECTOR_TABS.map((tab) => renderInspectorTabButton(tab.id, tab.label, activeTab)).join("")}
        </div>
      </div>
      <div class="inspector-content">
        ${renderInspectorTabContent(activeTab, context, intentMatches)}
      </div>
    `
  );
}

// src/webview/shared/settingsDefaults.ts
const DEFAULT_SETTINGS = {
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
const SETTINGS_FORM_FIELD_KEYS = [
  "backendUrl",
  "workspaceId",
  "modelPreference",
  "authToken",
  "tokenBudget",
  "lancedbPath",
  "historyPath",
  "neo4jUri",
  "indexProfile",
  "overlaySync",
  "autoOpenInspector"
];

// src/webview/shared/settingsLayout.ts
function settingsFormDataFromSettings(data) {
  const values = Object.fromEntries(
    SETTINGS_FORM_FIELD_KEYS.map((key) => [key, data[key]])
  );
  return {
    ...values,
    graphStatusLabel: data.graphStatus?.label,
    graphStatusDetail: data.graphStatus?.detail,
    graphStatusHealthy: data.graphStatus?.healthy
  };
}
function renderSettingField(id, label, hint, controlHtml, statusHtml = "") {
  return `
    <div class="setting-field">
      <label for="${id}">${escapeHtml(label)}</label>
      ${controlHtml}
      <p class="field-hint" id="${id}-hint">${hint}</p>
      ${statusHtml}
    </div>
  `;
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
    spec.min === void 0 ? "" : `min="${spec.min}"`,
    spec.max === void 0 ? "" : `max="${spec.max}"`,
    spec.step === void 0 ? "" : `step="${spec.step}"`
  ].filter(Boolean).join(" ");
  const inputHtml = spec.testAction ? `<div class="field-group"><input ${inputAttrs} /><button class="field-action-btn" data-action="${spec.testAction.action}" aria-label="Test connection">${spec.testAction.label}</button></div>` : `<input ${inputAttrs} />`;
  const statusHtml = spec.showStatus ? `<div class="field-status" id="${spec.id}-status"></div>` : "";
  return renderSettingField(spec.id, spec.label, spec.hint, inputHtml, statusHtml);
}
function renderSelect(spec) {
  const options = spec.options.map((option) => `<option value="${escapeHtml(option.value)}" ${spec.value === option.value ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("");
  const selectHtml = `<select id="${spec.id}" class="setting-input" aria-label="${escapeHtml(spec.label)}" aria-describedby="${spec.id}-hint">
        ${options}
      </select>`;
  return renderSettingField(spec.id, spec.label, spec.hint, selectHtml);
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
const SETTINGS_CHECKBOX_FIELDS = /* @__PURE__ */ new Set(["overlaySync", "autoOpenInspector"]);
const SETTINGS_SELECT_FIELDS = /* @__PURE__ */ new Set(["modelPreference", "indexProfile"]);
function readSettingsFormField(key) {
  if (SETTINGS_CHECKBOX_FIELDS.has(key)) {
    return checkboxValue(key);
  }
  if (SETTINGS_SELECT_FIELDS.has(key)) {
    return selectValue(key, String(DEFAULT_SETTINGS[key]));
  }
  if (key === "tokenBudget") {
    return Number(inputValue(key) || String(DEFAULT_SETTINGS.tokenBudget));
  }
  return inputValue(key);
}
function applySettingsFormField(key, value) {
  const setInput = (id, nextValue) => {
    const element = document.getElementById(id);
    if (element) element.value = nextValue;
  };
  const setSelect = (id, nextValue) => {
    const element = document.getElementById(id);
    if (element) element.value = nextValue;
  };
  const setCheckbox = (id, checked) => {
    const element = document.getElementById(id);
    if (element) element.checked = checked;
  };
  if (SETTINGS_CHECKBOX_FIELDS.has(key)) {
    setCheckbox(key, Boolean(value));
    return;
  }
  if (SETTINGS_SELECT_FIELDS.has(key)) {
    setSelect(key, String(value));
    return;
  }
  setInput(key, String(value));
}
function readSettingsFormFromDom() {
  return Object.fromEntries(
    SETTINGS_FORM_FIELD_KEYS.map((key) => [key, readSettingsFormField(key)])
  );
}
function applySettingsDefaultsToDom(defaults = DEFAULT_SETTINGS) {
  for (const key of SETTINGS_FORM_FIELD_KEYS) {
    applySettingsFormField(key, defaults[key]);
  }
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
function showTransientDomMessage(elementId, className, message, autoHide) {
  const element = document.getElementById(elementId);
  if (!element) return;
  element.className = className;
  element.textContent = message;
  element.style.display = "block";
  if (autoHide) {
    setTimeout(() => {
      element.style.display = "none";
    }, 3e3);
  }
}
function showFieldStatus(fieldId, success, message) {
  showTransientDomMessage(
    `${fieldId}-status`,
    `field-status ${success ? "success" : "error"}`,
    message,
    success
  );
}
function showFeedback(message, level) {
  showTransientDomMessage(
    "settings-feedback",
    `settings-feedback settings-feedback-${level}`,
    message,
    level === "success"
  );
}

// src/webview/shared/webviewRuntime.ts
const vscode = acquireVsCodeApi();
const VSCODE_WEBVIEW_ORIGIN_PREFIX = "vscode-webview://";
function listenForHostMessages(handler) {
  const webviewOrigin = globalThis.location.origin;
  function receiveHostMessage(event) {
    if (event.origin !== webviewOrigin && event.origin !== "" && !event.origin.startsWith(VSCODE_WEBVIEW_ORIGIN_PREFIX)) {
      return;
    }
    handler(event.data);
  }
  globalThis.addEventListener("message", receiveHostMessage);
}
function bootWebview(init) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}

// src/webview/shared/mainSurfaceHost.ts
function hostHandler(handler) {
  return handler;
}
function callDelegate(method) {
  return hostHandler((delegate) => {
    delegate[method]();
  });
}
function forwardMessage(method) {
  return hostHandler((delegate, message) => {
    delegate[method](message);
  });
}
const MAIN_SURFACE_HOST_HANDLERS = {
  "surface.init": forwardMessage("onSurfaceInit"),
  "chat.requestStarted": hostHandler((d, m) => {
    d.setSurface("chat");
    d.onRequestStarted(m.requestId, m.symbol);
  }),
  "chat.streamChunk": hostHandler((d, m) => d.onStreamChunk(m.requestId, m.chunk)),
  "chat.requestCompleted": hostHandler((d, m) => d.onRequestCompleted(m.requestId, m.answer, m.context)),
  "chat.requestFailed": hostHandler((d, m) => d.onRequestFailed(m.requestId, m.error)),
  "chat.requestStopped": hostHandler((d, m) => d.onRequestStopped(m.requestId)),
  "chat.contextSummary": hostHandler((d, m) => {
    d.setContextSummary(m.summary);
    d.refreshAccordions();
  }),
  "workspace.updated": forwardMessage("onWorkspaceUpdated"),
  "backend.updated": forwardMessage("onBackendUpdated"),
  "impact.loading": callDelegate("onImpactLoading"),
  "impact.loaded": forwardMessage("onImpactLoaded"),
  "impact.loadFailed": forwardMessage("onImpactLoadFailed"),
  "inspector.loaded": forwardMessage("onInspectorLoaded"),
  "inspector.intentLoaded": forwardMessage("onInspectorIntentLoaded"),
  "settings.loaded": forwardMessage("onSettingsLoaded"),
  "settings.saved": hostHandler((_d, m) => showFeedback(m.message, "success")),
  "settings.saveFailed": hostHandler((_d, m) => showFeedback(m.error, "error")),
  "settings.testUrlComplete": hostHandler((_d, m) => showFieldStatus("backendUrl", m.success, m.message)),
  "toast.show": hostHandler((d, m) => d.showToast(m.message, m.level))
};
function dispatchMainHostMessage(delegate, message) {
  const hostSurface = SURFACE_FROM_HOST_MESSAGE[message.type];
  if (hostSurface) {
    delegate.showSurface(
      hostSurface,
      hostSurface === "settings" ? () => delegate.requestSettings() : void 0
    );
    return;
  }
  const handler = MAIN_SURFACE_HOST_HANDLERS[message.type];
  if (handler) {
    handler(delegate, message);
  }
}

// src/webview/shared/mainSurfaceActions.ts
const COPY_ACTIONS = /* @__PURE__ */ new Set(["copy", "copy-json", "copy-api-json", "feedback"]);
const IMPACT_CHANGE_CHECK_PROMPT = (symbol) => `What should I check before changing ${symbol || "this symbol"}?`;
const IMPACT_REFACTOR_PLAN_PROMPT = (symbol) => `Create a refactor plan for ${symbol || "this symbol"}.`;
function invokeVoidAction(method) {
  return (host) => {
    host[method]();
  };
}
function invokeTargetAction(method) {
  return (host, target) => {
    host[method](target);
  };
}
const MAIN_SURFACE_DOM_ACTION_HANDLERS = {
  switchSurface: (h, t) => h.switchSurface(t.dataset.surface),
  switchInspectorTab: (h, t) => h.switchInspectorTab(t.dataset.inspectorTab),
  selectPrompt: (h, t) => h.selectPrompt(t.dataset.requestId ?? null),
  toggleHistory: invokeVoidAction("toggleHistory"),
  newDialog: invokeVoidAction("startNewDialog"),
  restoreDialog: (h, t) => h.restoreDialog(t.dataset.dialogId ?? null),
  openDashboard: invokeVoidAction("postOpenDashboard"),
  ask: invokeVoidAction("focusComposer"),
  "ask-followup": (h) => h.prefillImpactAsk(IMPACT_CHANGE_CHECK_PROMPT(h.getActiveImpactSymbol())),
  "open-related-files": invokeVoidAction("openRelatedImpactFiles"),
  openFile: invokeTargetAction("openFileFromImpact"),
  showMoreImpact: invokeTargetAction("showMoreImpactRows"),
  explainImpact: invokeTargetAction("toggleImpactExplanation"),
  "create-refactor-plan": (h) => h.prefillImpactAsk(IMPACT_REFACTOR_PLAN_PROMPT(h.getActiveImpactSymbol())),
  save: invokeVoidAction("saveSettings"),
  reset: invokeVoidAction("resetSettings"),
  testUrl: invokeVoidAction("testSettingsUrl"),
  openKeybindings: invokeVoidAction("postOpenKeybindings"),
  search: invokeVoidAction("showSearchComingSoon"),
  noop: invokeTargetAction("toggleImpactGroup"),
  feedback: invokeTargetAction("submitFeedback"),
  copy: invokeTargetAction("copyMessage"),
  "copy-json": invokeTargetAction("copyInspectorJson"),
  "copy-api-json": invokeTargetAction("copyInspectorJson"),
  stopStreaming: invokeVoidAction("stopStreaming")
};
function handleMainSurfaceAction(host, event) {
  const target = event.currentTarget;
  const action = target.dataset.action;
  if (!action) return;
  if (COPY_ACTIONS.has(action)) {
    event.preventDefault();
    event.stopPropagation();
  }
  const surfaceAction = SURFACE_FROM_DOM_ACTION[action];
  if (surfaceAction) {
    host.handleSurfaceDomAction(surfaceAction, action, target);
    return;
  }
  const handler = MAIN_SURFACE_DOM_ACTION_HANDLERS[action];
  if (handler) {
    handler(host, target);
  }
}

export {
  bindClickAction,
  bindDataActions,
  mountLayoutHtml,
  toggleAriaExpandedSection,
  replaceElementHtml,
  escapeHtml,
  renderMessageCard,
  renderEnvironmentAccordion,
  renderContextSummaryAccordion,
  renderAdvancedInfoAccordion,
  renderStatusChips,
  renderComposerDock,
  createUserChatMessage,
  createAssistantChatMessage,
  resizeComposerToFit,
  renderImpactWorkspace,
  clampImpactDepth,
  hydrateFromPromptContext,
  renderMainSurfaceTabBar,
  renderSurfaceShell,
  renderImpactSurfaceShell,
  renderInspectorSurfaceView,
  settingsFormDataFromSettings,
  renderSettingsForm,
  readSettingsFormFromDom,
  applySettingsDefaultsToDom,
  validateSettingsForm,
  showFieldStatus,
  showFeedback,
  vscode,
  listenForHostMessages,
  bootWebview,
  dispatchMainHostMessage,
  handleMainSurfaceAction
};
//# sourceMappingURL=chunk-GRHNO7TC.js.map
