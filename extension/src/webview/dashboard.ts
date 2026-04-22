declare function acquireVsCodeApi(): any;
const vscode = acquireVsCodeApi();

import {
  HostToWebviewMessage,
  AuditAction,
  DashboardMetrics,
} from './shared/protocol';
import {
  renderMetricCardGrid,
  renderAuditEventsCard,
  renderDashboardHeader,
  renderRefreshButton,
  renderDashboardWarnings,
  renderIndexingJobsCard,
  renderTokenSavingsCard,
} from './shared/dashboardLayout';

interface DashboardState {
  health: 'up' | 'down' | 'degraded' | null;
  cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline' | null;
  auditActions: AuditAction[];
  metrics: DashboardMetrics;
  workspaceId: string;
  warnings: string[];
  isLoading: boolean;
  error: string | null;
  lastUpdate: number | null;
}

class DashboardPanel {
  private state: DashboardState = {
    health: null,
    cloudStatus: null,
    auditActions: [],
    metrics: emptyDashboardMetrics(),
    workspaceId: 'local/default@main',
    warnings: [],
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
          this.state.metrics = message.metrics;
          this.state.workspaceId = message.workspaceId;
          this.state.warnings = message.warnings;
          this.state.isLoading = false;
          this.state.error = null;
          this.state.lastUpdate = Date.now();
          this.render();
          break;

        case 'dashboard.metricsFailed':
          this.state.isLoading = false;
          this.state.error = message.error;
          this.state.warnings = [message.error];
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

    if (this.state.isLoading && !this.state.lastUpdate) {
      root.innerHTML = `
        <div class="dashboard-loading">
          <p>Loading dashboard metrics...</p>
        </div>
      `;
      return;
    }

    const header = renderDashboardHeader(this.state.workspaceId, this.state.lastUpdate);
    const refreshBtn = renderRefreshButton(this.state.isLoading);
    const warnings = renderDashboardWarnings(this.state.warnings);
    const metricCards = renderMetricCardGrid({
      health: this.state.health || 'degraded',
      cloudStatus: this.state.cloudStatus || 'offline',
      metrics: this.state.metrics,
    });
    const tokenSavingsCard = renderTokenSavingsCard(this.state.metrics);
    const indexingJobsCard = renderIndexingJobsCard(this.state.metrics);
    const auditCard = renderAuditEventsCard(this.state.auditActions);

    root.innerHTML = `
      ${header}
      <div class="dashboard-content">
        <div class="dashboard-toolbar">
          ${warnings}
          ${refreshBtn}
        </div>
        <div class="dashboard-grid">
          ${metricCards}
          <div class="dashboard-main-panels">
            ${tokenSavingsCard}
            ${indexingJobsCard}
          </div>
          ${auditCard}
        </div>
      </div>
    `;

    this.initializeUI();
  }
}

function emptyDashboardMetrics(): DashboardMetrics {
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
    lastIndexJobStatus: null,
  };
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => new DashboardPanel());
} else {
  new DashboardPanel();
}
