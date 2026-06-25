import {
  HostToWebviewMessage,
} from './shared/protocol';
import { bindClickAction } from './shared/domActions';
import { emptyDashboardMetrics } from './shared/dashboardDefaults';
import {
  renderDashboardView,
  DashboardViewState,
} from './shared/dashboardLayout';
import { mountLayoutHtml } from './shared/domRender';
import { bootWebview, listenForHostMessages, vscode } from './shared/webviewRuntime';

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
    this.bindActions(document);
  }

  private initializeMessageListener(): void {
    listenForHostMessages<HostToWebviewMessage>((message) => {
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

  private bindActions(root: ParentNode): void {
    bindClickAction(root, 'refresh', () => {
      vscode.postMessage({ type: 'dashboard.refresh' });
    });
    bindClickAction(root, 'indexWorkspace', () => {
      vscode.postMessage({ type: 'dashboard.indexWorkspace' });
    });
  }

  private render(): void {
    const root = document.getElementById('root');
    if (!root) return;

    mountLayoutHtml(root, renderDashboardView(this.state));
    this.bindActions(root);
  }
}

bootWebview(() => new DashboardPanel());
