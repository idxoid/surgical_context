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
  function renderDashboardHeader() {
    return `
    <div class="dashboard-header">
      <h1>Surgical Context Dashboard</h1>
      <p>System health and operational metrics</p>
    </div>
  `;
  }
  function renderRefreshButton(isLoading, lastUpdate) {
    const lastUpdateText = lastUpdate ? new Date(lastUpdate).toLocaleTimeString() : "Never";
    return `
    <div class="refresh-info">
      <span class="last-update">Last updated: ${escapeHtml(lastUpdateText)}</span>
      <button class="refresh-button ${isLoading ? "loading" : ""}" data-action="refresh" ${isLoading ? "disabled" : ""}>
        ${isLoading ? "\u27F3 Refreshing..." : "\u{1F504} Refresh"}
      </button>
    </div>
  `;
  }
  function renderMetricCardGrid(props) {
    const healthColor = props.health === "up" ? "#4CAF50" : props.health === "degraded" ? "#FF9800" : "#F44336";
    const cloudColor = props.cloudStatus === "connected" ? "#4CAF50" : props.cloudStatus === "fallback-local" ? "#FF9800" : "#F44336";
    return `
    <div class="metric-card-grid">
      <div class="metric-card health-card">
        <div class="metric-icon" style="color: ${healthColor}">\u2699\uFE0F</div>
        <div class="metric-info">
          <div class="metric-label">Sidecar Health</div>
          <div class="metric-value">${props.health}</div>
        </div>
      </div>

      <div class="metric-card cloud-card">
        <div class="metric-icon" style="color: ${cloudColor}">\u2601\uFE0F</div>
        <div class="metric-info">
          <div class="metric-label">Cloud Status</div>
          <div class="metric-value">${props.cloudStatus}</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">\u{1F4C1}</div>
        <div class="metric-info">
          <div class="metric-label">Indexed Files</div>
          <div class="metric-value">\u2014</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">\u2728</div>
        <div class="metric-info">
          <div class="metric-label">Indexed Symbols</div>
          <div class="metric-value">\u2014</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">\u23F1\uFE0F</div>
        <div class="metric-info">
          <div class="metric-label">Avg Latency</div>
          <div class="metric-value">\u2014</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">\u{1F4B0}</div>
        <div class="metric-info">
          <div class="metric-label">Token Savings</div>
          <div class="metric-value">\u2014</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>
    </div>
  `;
  }
  function renderAuditEventsCard(auditActions) {
    if (auditActions.length === 0) {
      return `
      <div class="dashboard-card audit-card">
        <div class="card-header">\u{1F4CB} Recent Activity</div>
        <div class="card-content empty">
          No recent events
        </div>
      </div>
    `;
    }
    const rows = auditActions.map((action) => {
      const timestamp = new Date(action.timestamp).toLocaleString();
      const actionType = action.action_type || "unknown";
      const symbol = action.symbol || "\u2014";
      const status = action.status || "\u2014";
      return `
        <div class="audit-row">
          <div class="audit-main">
            <span class="action-type">${escapeHtml(actionType)}</span>
            <span class="timestamp">${escapeHtml(timestamp)}</span>
          </div>
          <div class="audit-meta">
            <span class="symbol">${escapeHtml(symbol)}</span>
            <span class="status ${status.toLowerCase()}">${escapeHtml(status)}</span>
          </div>
        </div>
      `;
    }).join("");
    return `
    <div class="dashboard-card audit-card">
      <div class="card-header">\u{1F4CB} Recent Activity</div>
      <div class="card-content">
        ${rows}
      </div>
    </div>
  `;
  }

  // src/webview/dashboard.ts
  var DashboardPanel = class {
    constructor() {
      this.state = {
        health: null,
        cloudStatus: null,
        auditActions: [],
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
            this.state.isLoading = false;
            this.state.error = null;
            this.state.lastUpdate = Date.now();
            this.render();
            break;
          case "dashboard.metricsFailed":
            this.state.isLoading = false;
            this.state.error = message.error;
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
    }
    render() {
      const root = document.getElementById("root");
      if (!root) return;
      if (this.state.isLoading && !this.state.health) {
        root.innerHTML = `
        <div class="dashboard-loading">
          <p>Loading dashboard metrics...</p>
        </div>
      `;
        return;
      }
      if (this.state.error && !this.state.health) {
        root.innerHTML = `
        <div class="dashboard-error">
          <h3>Failed to load metrics</h3>
          <p>${escapeHtml(this.state.error)}</p>
          <button class="retry-button" data-action="refresh">Retry</button>
        </div>
      `;
        this.initializeUI();
        return;
      }
      const header = renderDashboardHeader();
      const refreshBtn = renderRefreshButton(this.state.isLoading, this.state.lastUpdate);
      const metricCards = renderMetricCardGrid({
        health: this.state.health || "degraded",
        cloudStatus: this.state.cloudStatus || "offline"
      });
      const auditCard = renderAuditEventsCard(this.state.auditActions);
      root.innerHTML = `
      ${header}
      <div class="dashboard-content">
        <div class="dashboard-toolbar">
          ${refreshBtn}
        </div>
        <div class="dashboard-grid">
          ${metricCards}
          ${auditCard}
        </div>
      </div>
    `;
      this.initializeUI();
    }
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => new DashboardPanel());
  } else {
    new DashboardPanel();
  }
})();
//# sourceMappingURL=dashboard.js.map
