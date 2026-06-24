"use strict";
(() => {
  // src/webview/shared/dashboardLayout.ts
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
  function renderDashboardHeader(workspaceId, lastUpdate) {
    const lastUpdateText = lastUpdate ? `${secondsAgo(lastUpdate)} ago` : "never";
    return `
    <div class="dashboard-header">
      <div>
        <h1>Surgical Context Dashboard</h1>
        <p>Operational overview of your indexing context_engine and context system.</p>
      </div>
      <div class="dashboard-meta">
        <div>Workspace: <span>${escapeHtml(workspaceId)}</span></div>
        <div>Last updated: <span>${escapeHtml(lastUpdateText)}</span></div>
      </div>
    </div>
  `;
  }
  function renderRefreshButton(isLoading) {
    return `
    <button class="refresh-button ${isLoading ? "loading" : ""}" data-action="refresh" ${isLoading ? "disabled" : ""}>
      ${isLoading ? "Refreshing..." : "Refresh"}
    </button>
  `;
  }
  function renderIndexWorkspaceButton(isLoading) {
    return `
    <button
      class="primary-action"
      data-action="indexWorkspace"
      aria-label="Reindex the current workspace"
      ${isLoading ? "disabled" : ""}
    >
      Reindex workspace
    </button>
  `;
  }
  function renderDashboardWarnings(warnings) {
    if (warnings.length === 0) return "";
    return `
    <div class="dashboard-warning" role="status">
      ${warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join("")}
    </div>
  `;
  }
  function renderDashboardNotices(notices) {
    if (notices.length === 0) return "";
    return `
    <div class="dashboard-notices" role="status">
      ${notices.map((notice) => `
        <div class="dashboard-notice ${escapeHtml(notice.level)}">
          <div>
            <div class="dashboard-notice-title">${escapeHtml(notice.title)}</div>
            <div class="dashboard-notice-message">${escapeHtml(notice.message)}</div>
          </div>
          ${notice.action ? `
            <button class="notice-action" data-action="${escapeHtml(notice.action)}">
              ${escapeHtml(notice.actionLabel || "Open")}
            </button>
          ` : ""}
        </div>
      `).join("")}
    </div>
  `;
  }
  function renderMetricCardGrid(props) {
    const metrics = props.metrics;
    const healthStatus = props.health === "up" ? "success" : "danger";
    const cloudStatus = props.cloudStatus === "connected" || props.cloudStatus === "local" || props.cloudStatus === "fallback-local" ? "success" : "danger";
    const queueStatus = metrics.queueFailedBatches && metrics.queueFailedBatches > 0 ? "danger" : metrics.queuePending && metrics.queuePending > 0 ? "warning" : "success";
    return `
    <div class="metric-card-grid">
      ${renderMetricCard("Sidecar health", healthLabel(props.health), props.health === "up" ? "Ready for requests" : "Check backend URL", "pulse", healthStatus)}
      ${renderMetricCard("Graph provider", cloudLabel(props.cloudStatus), cloudNote(props.cloudStatus), "cloud", cloudStatus)}
      ${renderMetricCard("Indexed files", formatNumber(metrics.indexedFiles), metricSourceNote(metrics.indexedFiles, "Graph catalog"), "file")}
      ${renderMetricCard("Indexed symbols", formatNumber(metrics.indexedSymbols), metricSourceNote(metrics.indexedSymbols, "Graph catalog"), "code")}
      ${renderMetricCard("Doc chunks", formatNumber(metrics.docChunks), metricSourceNote(metrics.docChunks, "Docs index"), "doc")}
      ${renderMetricCard("Last indexing job", metrics.lastIndexJobStatus || "idle", queueSummary(metrics), "play", queueStatus)}
      ${renderMetricCard("Avg latency (ask)", formatMs(metrics.avgLatencyMs), metrics.requestsTotal ? `${formatNumber(metrics.requestsTotal)} requests observed` : "Waiting for ask traffic", "clock")}
      ${renderMetricCard("Token savings", formatPercent(metrics.tokenSavingsPercent), metrics.tokensTotal ? "Savings baseline not emitted yet" : "Waiting for ask traffic", "trend")}
      ${renderMetricCard("Fallback rate", formatPercent(metrics.fallbackRatePercent), metrics.fallbackRatePercent === null ? "Waiting for ask traffic" : "Resolved ask context modes", "sync")}
      ${renderMetricCard("Context quality", formatPercent(metrics.contextQualityPercent), metrics.contextQualityPercent === null ? "Feedback signal pending" : "Accepted retrieval feedback", "target")}
      ${renderMetricCard("Symbols with docs", formatNumber(metrics.symbolsWithDocs), metricSourceNote(metrics.symbolsWithDocs, "Documentation links"), "book")}
      ${renderMetricCard("Storage (context_engine)", formatStorage(metrics.storageGb), metrics.storageGb === null ? "Storage metric unavailable" : "Local LanceDB store", "db")}
    </div>
  `;
  }
  function renderTokenSavingsCard(metrics) {
    const savings = metrics.tokenSavingsPercent;
    const value = formatPercent(savings);
    const bars = savings === null ? [38, 42, 44, 40, 46, 43, 45, 41, 39, 44, 47, 45] : [56, 62, 67, 64, 70, 73, Math.max(8, savings), 68, 71, 66, 74, 76];
    return `
    <div class="dashboard-card token-savings-card">
      <div class="card-header">
        <span>Token savings vs naive context</span>
        <span class="card-header-meta">${savings === null ? "pending" : "live"}</span>
      </div>
      <div class="token-savings-body">
        <div>
          <div class="token-savings-value">${value}</div>
          <div class="metric-note">
            ${metrics.tokensTotal ? `${formatNumber(metrics.tokensTotal)} tokens processed` : "Prompt telemetry has not produced token savings yet."}
          </div>
        </div>
        <div class="savings-chart" aria-label="Token savings trend">
          ${bars.map((height, index) => `
            <span
              class="${savings === null ? "pending" : ""}"
              style="height: ${height}%"
              title="Sample ${index + 1}"
            ></span>
          `).join("")}
        </div>
      </div>
    </div>
  `;
  }
  function renderIndexingJobsCard(metrics) {
    const queueUnavailable = metrics.queuePending === null && metrics.queueProcessing === null && metrics.queueProcessed === null && metrics.queueFailedBatches === null;
    if (queueUnavailable) {
      return renderIndexingStateCard(
        "Index queue unavailable",
        "The dashboard cannot read indexing state right now.",
        "unknown"
      );
    }
    if (metrics.lastIndexJobStatus === "not indexed") {
      return renderIndexingStateCard(
        "No indexing jobs yet",
        "Run Index Workspace to populate graph and vector context for this workspace.",
        "empty"
      );
    }
    const rows = [
      {
        time: "now",
        type: "Queue",
        scope: "workspace",
        status: metrics.lastIndexJobStatus || "idle",
        duration: metrics.queueProcessing && metrics.queueProcessing > 0 ? "active" : "0s"
      },
      {
        time: "total",
        type: "Processed",
        scope: "files",
        status: metrics.queueFailedBatches && metrics.queueFailedBatches > 0 ? "attention" : "success",
        duration: formatNumber(metrics.queueProcessed)
      },
      {
        time: "pending",
        type: "Backlog",
        scope: "queue",
        status: metrics.queuePending && metrics.queuePending > 0 ? "queued" : "clear",
        duration: formatNumber(metrics.queuePending)
      }
    ];
    return `
    <div class="dashboard-card indexing-card">
      <div class="card-header">
        <span>Recent indexing jobs</span>
        <span class="card-header-meta">queue</span>
      </div>
      <div class="dashboard-table" role="table" aria-label="Recent indexing jobs">
        <div class="dashboard-table-row header" role="row">
          <span>Time</span>
          <span>Type</span>
          <span>Scope</span>
          <span>Status</span>
          <span>Duration</span>
        </div>
        ${rows.map((row) => `
          <div class="dashboard-table-row" role="row">
            <span>${escapeHtml(row.time)}</span>
            <span>${escapeHtml(row.type)}</span>
            <span>${escapeHtml(row.scope)}</span>
            <span class="status ${escapeHtml(row.status.toLowerCase())}">${escapeHtml(row.status)}</span>
            <span>${escapeHtml(row.duration)}</span>
          </div>
        `).join("")}
      </div>
    </div>
  `;
  }
  function renderIndexingStateCard(title, message, status) {
    return `
    <div class="dashboard-card indexing-card">
      <div class="card-header">
        <span>Recent indexing jobs</span>
        <span class="card-header-meta">${escapeHtml(status)}</span>
      </div>
      <div class="dashboard-empty-state">
        <div class="dashboard-empty-title">${escapeHtml(title)}</div>
        <div class="dashboard-empty-message">${escapeHtml(message)}</div>
      </div>
    </div>
  `;
  }
  function renderHealthChecklistCard(items) {
    const rows = items.length === 0 ? `
      <div class="health-check-row empty">
        <span>No health checks available</span>
      </div>
    ` : items.map((item) => `
      <div class="health-check-row ${escapeHtml(item.status)}">
        <div class="health-check-status" aria-hidden="true">${escapeHtml(statusSymbol(item.status))}</div>
        <div class="health-check-main">
          <div class="health-check-label">${escapeHtml(item.label)}</div>
          <div class="health-check-detail">${escapeHtml(item.detail)}</div>
        </div>
        <div class="health-check-value">${escapeHtml(item.value)}</div>
      </div>
    `).join("");
    return `
    <div class="dashboard-card health-check-card">
      <div class="card-header">
        <span>Health checklist</span>
        <span class="card-header-meta">local</span>
      </div>
      <div class="health-check-list">
        ${rows}
      </div>
    </div>
  `;
  }
  function renderAuditEventsCard(auditActions) {
    const rows = auditActions.length === 0 ? `
      <div class="dashboard-table-row empty" role="row">
        <span>No recent audit events</span>
      </div>
    ` : auditActions.map((action) => {
      const timestamp = formatTimestamp(action.timestamp);
      const actionType = action.action_type || "unknown";
      const symbol = action.symbol || "N/A";
      const status = action.status || "success";
      const detail = action.details ? summarizeDetails(action.details) : symbol;
      return `
          <div class="dashboard-table-row audit" role="row">
            <span>${escapeHtml(timestamp)}</span>
            <span>${escapeHtml(actionType)}</span>
            <span>${escapeHtml(detail)}</span>
            <span class="status ${escapeHtml(status.toLowerCase())}">${escapeHtml(status)}</span>
          </div>
        `;
    }).join("");
    return `
    <div class="dashboard-card audit-card">
      <div class="card-header">
        <span>Recent audit events</span>
        <span class="card-header-meta">latest</span>
      </div>
      <div class="dashboard-table audit-table" role="table" aria-label="Recent audit events">
        <div class="dashboard-table-row header" role="row">
          <span>Time</span>
          <span>Event</span>
          <span>Details</span>
          <span>Status</span>
        </div>
        ${rows}
      </div>
    </div>
  `;
  }
  function renderMetricCard(label, value, note, icon, status = "neutral") {
    return `
    <div class="metric-card ${status}">
      <div class="metric-icon" aria-hidden="true">${escapeHtml(iconSymbol(icon))}</div>
      <div class="metric-info">
        <div class="metric-label">${escapeHtml(label)}</div>
        <div class="metric-value">${escapeHtml(value)}</div>
        <div class="metric-note">${escapeHtml(note)}</div>
      </div>
    </div>
  `;
  }
  function iconSymbol(name) {
    const icons = {
      pulse: "\u25C7",
      cloud: "\u2601",
      file: "\u25A1",
      code: "</>",
      doc: "\u25A4",
      play: "\u25B7",
      clock: "\u25CB",
      trend: "\u2301",
      sync: "\u21BB",
      target: "\u25CE",
      book: "\u25B1",
      db: "\u25A5"
    };
    return icons[name] || "\u25A1";
  }
  function statusSymbol(status) {
    if (status === "ok") return "\u2713";
    if (status === "warning") return "!";
    if (status === "error") return "\xD7";
    return "\u25CB";
  }
  function healthLabel(health) {
    if (health === "up") return "healthy";
    if (health === "degraded") return "degraded";
    return "down";
  }
  function cloudLabel(status) {
    if (status === "connected") return "aura";
    if (status === "local" || status === "fallback-local") return "local";
    return "offline";
  }
  function cloudNote(status) {
    if (status === "connected") return "Neo4j Aura connected";
    if (status === "local" || status === "fallback-local") return "Local Neo4j active";
    return "Graph provider offline";
  }
  function queueSummary(metrics) {
    const pending = metrics.queuePending ?? 0;
    const processing = metrics.queueProcessing ?? 0;
    const failed = metrics.queueFailedBatches ?? 0;
    if (failed > 0) return `${failed} failed batch${failed === 1 ? "" : "es"}`;
    if (processing > 0) return `${processing} processing`;
    if (pending > 0) return `${pending} pending`;
    return "Queue clear";
  }
  function formatNumber(value) {
    return value === null ? "-" : new Intl.NumberFormat().format(Math.round(value));
  }
  function formatPercent(value) {
    return value === null ? "-" : `${Math.round(value)}%`;
  }
  function formatMs(value) {
    return value === null ? "-" : `${Math.round(value)} ms`;
  }
  function formatStorage(valueGb) {
    if (valueGb === null) return "-";
    if (valueGb < 0.1) return `${Math.round(valueGb * 1e3)} MB`;
    return `${valueGb.toFixed(1)} GB`;
  }
  function metricSourceNote(value, source) {
    return value === null ? `${source} metric unavailable` : source;
  }
  function secondsAgo(timestamp) {
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1e3));
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.round(seconds / 60);
    return `${minutes}m`;
  }
  function formatTimestamp(timestamp) {
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return timestamp;
    return `${secondsAgo(date.getTime())} ago`;
  }
  function summarizeDetails(details) {
    if (typeof details.symbol === "string") return details.symbol;
    if (typeof details.file_path === "string") return details.file_path;
    if (typeof details.project_path === "string") return details.project_path;
    if (typeof details.error === "string") return summarizeError(details.error);
    const entries = Object.entries(details).slice(0, 2);
    if (entries.length === 0) return "N/A";
    return entries.map(([key, value]) => `${key}: ${String(value)}`).join(" \u2022 ");
  }
  function summarizeError(error) {
    const missingSymbol = error.match(/Symbol '([^']+)' not found in graph/i);
    if (missingSymbol) {
      return `Symbol not indexed: ${missingSymbol[1]}`;
    }
    return error.replace(/^Error:\s*/i, "");
  }

  // src/webview/dashboard.ts
  var vscode = acquireVsCodeApi();
  var DashboardPanel = class {
    constructor() {
      this.state = {
        health: null,
        cloudStatus: null,
        auditActions: [],
        metrics: emptyDashboardMetrics(),
        healthChecks: [],
        notices: [],
        workspaceId: "",
        warnings: [],
        isLoading: false,
        error: null,
        lastUpdate: null
      };
      this.initializeMessageListener();
      this.initializeUI();
    }
    initializeMessageListener() {
      window.addEventListener("message", (event) => {
        const message = event.data;
        switch (message.type) {
          case "dashboard.loading":
            this.state.isLoading = true;
            this.render();
            break;
          case "dashboard.metricsLoaded":
            this.state.health = message.health;
            this.state.cloudStatus = message.cloudStatus;
            this.state.auditActions = message.auditActions;
            this.state.metrics = message.metrics;
            this.state.healthChecks = message.healthChecks;
            this.state.notices = message.notices;
            this.state.workspaceId = message.workspaceId;
            this.state.warnings = message.warnings;
            this.state.isLoading = false;
            this.state.error = null;
            this.state.lastUpdate = Date.now();
            this.render();
            break;
          case "dashboard.metricsFailed":
            this.state.isLoading = false;
            this.state.error = message.error;
            this.state.warnings = [];
            this.state.notices = [{
              id: "dashboard-load-failed",
              level: "error",
              title: "Dashboard data failed to load",
              message: message.error,
              action: "refresh",
              actionLabel: "Retry"
            }];
            this.render();
            break;
        }
      });
    }
    initializeUI() {
      const refreshBtn = document.querySelector('[data-action="refresh"]');
      if (refreshBtn) {
        refreshBtn.addEventListener("click", () => {
          vscode.postMessage({ type: "dashboard.refresh" });
        });
      }
      const indexBtn = document.querySelector('[data-action="indexWorkspace"]');
      if (indexBtn) {
        indexBtn.addEventListener("click", () => {
          vscode.postMessage({ type: "dashboard.indexWorkspace" });
        });
      }
    }
    render() {
      const root = document.getElementById("root");
      if (!root) return;
      if (this.state.isLoading && !this.state.lastUpdate) {
        root.innerHTML = `
        <div class="dashboard-loading">
          <p>Loading dashboard metrics...</p>
        </div>
      `;
        return;
      }
      const header = renderDashboardHeader(this.state.workspaceId, this.state.lastUpdate);
      const indexWorkspaceBtn = renderIndexWorkspaceButton(this.state.isLoading);
      const refreshBtn = renderRefreshButton(this.state.isLoading);
      const warnings = renderDashboardWarnings(this.state.warnings);
      const notices = renderDashboardNotices(this.state.notices);
      const metricCards = renderMetricCardGrid({
        health: this.state.health || "degraded",
        cloudStatus: this.state.cloudStatus || "offline",
        metrics: this.state.metrics
      });
      const tokenSavingsCard = renderTokenSavingsCard(this.state.metrics);
      const indexingJobsCard = renderIndexingJobsCard(this.state.metrics);
      const healthChecklistCard = renderHealthChecklistCard(this.state.healthChecks);
      const auditCard = renderAuditEventsCard(this.state.auditActions);
      root.innerHTML = `
      ${header}
      <div class="dashboard-content">
        <div class="dashboard-toolbar">
          ${warnings}
          <div class="dashboard-actions">
            ${indexWorkspaceBtn}
            ${refreshBtn}
          </div>
        </div>
        ${notices}
        <div class="dashboard-grid">
          ${metricCards}
          <div class="dashboard-main-panels">
            ${tokenSavingsCard}
            ${indexingJobsCard}
          </div>
          ${healthChecklistCard}
          ${auditCard}
        </div>
      </div>
    `;
      this.initializeUI();
    }
  };
  function emptyDashboardMetrics() {
    return {
      indexedFiles: null,
      indexedSymbols: null,
      docChunks: null,
      avgLatencyMs: null,
      tokenSavingsPercent: null,
      fallbackRatePercent: null,
      contextQualityPercent: null,
      symbolsWithDocs: null,
      storageGb: null,
      requestsTotal: null,
      tokensTotal: null,
      costUsdTotal: null,
      queuePending: null,
      queueProcessing: null,
      queueProcessed: null,
      queueFailedBatches: null,
      lastIndexJobStatus: null
    };
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new DashboardPanel());
  } else {
    new DashboardPanel();
  }
})();
//# sourceMappingURL=dashboard.js.map
