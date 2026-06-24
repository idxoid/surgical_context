"use strict";
(() => {
  // src/webview/shared/inspectorLayout.ts
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
    const jsonStr = JSON.stringify(context, null, 2);
    return `
    <div class="json-viewer">
      <button class="copy-button" data-action="copy-json">Copy JSON</button>
      <pre><code>${escapeHtml(jsonStr)}</code></pre>
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
      <pre><code>${escapeHtml(jsonStr)}</code></pre>
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

  // src/webview/inspector.ts
  var vscode = acquireVsCodeApi();
  var InspectorPanel = class {
    constructor() {
      this.context = null;
      this.tabState = { activeTab: "primary" };
      console.log("InspectorPanel constructor called");
      this.initializeMessageListener();
      this.restoreTabState();
    }
    initializeMessageListener() {
      window.addEventListener("message", (event) => {
        const message = event.data;
        console.log("InspectorPanel received message:", message.type);
        switch (message.type) {
          case "inspector.loaded":
            console.log("inspector.loaded message received, context:", message.context);
            this.context = message.context || null;
            this.symbol = message.symbol;
            this.question = message.question;
            this.render();
            break;
          case "inspector.notAvailable":
            console.log("inspector.notAvailable message received:", message.message);
            this.context = null;
            this.symbol = void 0;
            this.question = void 0;
            this.renderNotAvailable(message.message);
            break;
        }
      });
    }
    render() {
      const root = document.getElementById("root");
      if (!root) return;
      console.log("InspectorPanel.render() called, context:", this.context, "tabState:", this.tabState);
      if (!this.context) {
        root.innerHTML = `
        <div class="inspector-empty">
          <p>No context available. Ask about a symbol to populate the inspector.</p>
        </div>
      `;
        return;
      }
      const tabButtons = `
      <div class="inspector-tab-bar">
        <button class="tab-button ${this.tabState.activeTab === "primary" ? "active" : ""}" data-tab="primary">
          Primary Source
        </button>
        <button class="tab-button ${this.tabState.activeTab === "graph" ? "active" : ""}" data-tab="graph">
          Graph Context
        </button>
        <button class="tab-button ${this.tabState.activeTab === "docs" ? "active" : ""}" data-tab="docs">
          Documentation
        </button>
        <button class="tab-button ${this.tabState.activeTab === "json" ? "active" : ""}" data-tab="json">
          Prompt JSON
        </button>
        <button class="tab-button ${this.tabState.activeTab === "api" ? "active" : ""}" data-tab="api">
          API Payload
        </button>
        <button class="tab-button ${this.tabState.activeTab === "tokens" ? "active" : ""}" data-tab="tokens">
          Token Breakdown
        </button>
      </div>
    `;
      console.log("tabButtons HTML generated, about to render tabContent for:", this.tabState.activeTab);
      let tabContent = "";
      switch (this.tabState.activeTab) {
        case "primary":
          tabContent = renderPrimarySourceTab(this.context);
          break;
        case "graph":
          tabContent = renderGraphContextTab(this.context);
          break;
        case "docs":
          tabContent = renderDocumentationTab(this.context);
          break;
        case "json":
          tabContent = renderPromptJsonTab(this.context);
          break;
        case "api":
          tabContent = renderApiPayloadTab(this.context);
          break;
        case "tokens":
          tabContent = renderTokenBreakdownTab(this.context);
          break;
      }
      const headerTitle = this.symbol ? `Context Inspector \u2014 ${this.symbol}` : "Context Inspector";
      const questionHtml = this.question ? `<p class="inspector-question"><em>Question: ${escapeHtml(this.question)}</em></p>` : "";
      root.innerHTML = `
      <div class="inspector-header">
        <h2>${escapeHtml(headerTitle)}</h2>
        ${questionHtml}
      </div>
      ${tabButtons}
      <div class="inspector-content">
        ${tabContent}
      </div>
    `;
      this.attachTabListeners();
    }
    renderNotAvailable(message) {
      const root = document.getElementById("root");
      if (!root) return;
      root.innerHTML = `
      <div class="inspector-empty">
        <div style="padding: 20px; text-align: center;">
          <p style="margin: 0; color: var(--vscode-foreground);">${escapeHtml(message)}</p>
          <p style="margin: 10px 0 0 0; font-size: 12px; color: var(--vscode-descriptionForeground);">
            Click <strong>Ask</strong> about a symbol to get started.
          </p>
        </div>
      </div>
    `;
    }
    attachTabListeners() {
      document.querySelectorAll(".tab-button").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          const tab = e.currentTarget.getAttribute("data-tab");
          if (tab) {
            this.tabState.activeTab = tab;
            this.persistTabState();
            this.render();
          }
        });
      });
      document.querySelectorAll("[data-file-path]").forEach((row) => {
        row.addEventListener("click", (e) => {
          const filePath = e.currentTarget.getAttribute("data-file-path");
          const lineStr = e.currentTarget.getAttribute("data-line");
          if (filePath) {
            vscode.postMessage({
              type: "link.openFile",
              filePath,
              line: lineStr ? parseInt(lineStr, 10) : void 0
            });
          }
        });
      });
      const copyBtn = document.querySelector('[data-action="copy-json"]');
      if (copyBtn) {
        copyBtn.addEventListener("click", () => {
          this.copyJsonFromViewer(copyBtn);
        });
      }
      const copyApiBtn = document.querySelector('[data-action="copy-api-json"]');
      if (copyApiBtn) {
        copyApiBtn.addEventListener("click", () => {
          this.copyJsonFromViewer(copyApiBtn);
        });
      }
    }
    copyJsonFromViewer(button) {
      const content = button.closest(".json-viewer")?.querySelector("pre code")?.textContent;
      if (!content) return;
      vscode.postMessage({ type: "clipboard.write", text: content });
    }
    persistTabState() {
      vscode.setState(this.tabState);
    }
    restoreTabState() {
      const saved = vscode.getState();
      const validTabs = ["primary", "graph", "docs", "json", "api", "tokens"];
      if (saved?.activeTab && validTabs.includes(saved.activeTab)) {
        this.tabState.activeTab = saved.activeTab;
      }
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new InspectorPanel());
  } else {
    new InspectorPanel();
  }
})();
//# sourceMappingURL=inspector.js.map
