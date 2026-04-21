// @ts-ignore vscode API injected at runtime
declare const vscode: any;

import {
  HostToWebviewMessage,
  AuditAction,
} from './shared/protocol';
import {
  renderMetricCardGrid,
  renderAuditEventsCard,
  renderDashboardHeader,
  renderRefreshButton,
  escapeHtml,
} from './shared/dashboardLayout';

interface DashboardState {
  health: 'up' | 'down' | 'degraded' | null;
  cloudStatus: 'connected' | 'fallback-local' | 'offline' | null;
  auditActions: AuditAction[];
  isLoading: boolean;
  error: string | null;
  lastUpdate: number | null;
}

class DashboardPanel {
  private state: DashboardState = {
    health: null,
    cloudStatus: null,
    auditActions: [],
    isLoading: false,
    error: null,
    lastUpdate: null,
  };

  constructor() {
    this.initializeMessageListener();
    this.initializeUI();
  }

  private initializeMessageListener(): void {
    window.addEventListener('message', (event: MessageEvent<HostToWebviewMessage>) => {
      const message = event.data;

      switch (message.type) {
        case 'dashboard.loading':
          this.state.isLoading = true;
          this.render();
          break;

        case 'dashboard.metricsLoaded':
          this.state.health = message.health;
          this.state.cloudStatus = message.cloudStatus;
          this.state.auditActions = message.auditActions;
          this.state.isLoading = false;
          this.state.error = null;
          this.state.lastUpdate = Date.now();
          this.render();
          break;

        case 'dashboard.metricsFailed':
          this.state.isLoading = false;
          this.state.error = message.error;
          this.render();
          break;
      }
    });
  }

  private initializeUI(): void {
    const refreshBtn = document.querySelector('[data-action="refresh"]') as HTMLButtonElement | null;
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        vscode.postMessage({ type: 'dashboard.refresh' });
      });
    }
  }

  private render(): void {
    const root = document.getElementById('root');
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
      health: this.state.health || 'degraded',
      cloudStatus: this.state.cloudStatus || 'offline',
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
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new DashboardPanel());
} else {
  new DashboardPanel();
}
