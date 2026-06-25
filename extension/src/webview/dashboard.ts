import {
  HostToWebviewMessage,
} from './shared/protocol';
import { emptyDashboardMetrics } from './shared/dashboardDefaults';
import {
  renderDashboardView,
  DashboardViewState,
} from './shared/dashboardLayout';
import { bootWebview, vscode } from './shared/webviewRuntime';

class DashboardPanel {
  private state: DashboardViewState & { error: string | null } = {
    health: null,
    cloudStatus: null,
    auditActions: [],
    metrics: emptyDashboardMetrics(),
    healthChecks: [],
    notices: [],
    workspaceId: '',
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
          this.state.healthChecks = message.healthChecks;
          this.state.notices = message.notices;
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
          this.state.warnings = [];
          this.state.notices = [{
            id: 'dashboard-load-failed',
            level: 'error',
            title: 'Dashboard data failed to load',
            message: message.error,
            action: 'refresh',
            actionLabel: 'Retry',
          }];
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

    const indexBtn = document.querySelector('[data-action="indexWorkspace"]') as HTMLButtonElement | null;
    if (indexBtn) {
      indexBtn.addEventListener('click', () => {
        vscode.postMessage({ type: 'dashboard.indexWorkspace' });
      });
    }
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    root.innerHTML = renderDashboardView(this.state);
    this.initializeUI();
  }
}

bootWebview(() => new DashboardPanel());
