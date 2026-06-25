import { bindClickAction } from './domActions';
import { emptyDashboardMetrics } from './dashboardDefaults';
import type { DashboardViewState } from './dashboardLayout';
import type { DashboardNotice, HostToWebviewMessage, WebviewToHostMessage } from './protocol';

export type DashboardPanelState = DashboardViewState & { error: string | null };

export type DashboardHostMessage = Extract<
  HostToWebviewMessage,
  { type: `dashboard.${string}` }
>;

export function createInitialDashboardState(): DashboardPanelState {
  return {
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
}

function dashboardLoadFailedNotice(error: string): DashboardNotice {
  return {
    id: 'dashboard-load-failed',
    level: 'error',
    title: 'Dashboard data failed to load',
    message: error,
    action: 'refresh',
    actionLabel: 'Retry',
  };
}

export function reduceDashboardState(
  state: DashboardPanelState,
  message: DashboardHostMessage,
): DashboardPanelState {
  switch (message.type) {
    case 'dashboard.loading':
      return { ...state, isLoading: true };
    case 'dashboard.metricsLoaded':
      return {
        ...state,
        health: message.health,
        cloudStatus: message.cloudStatus,
        auditActions: message.auditActions,
        metrics: message.metrics,
        healthChecks: message.healthChecks,
        notices: message.notices,
        workspaceId: message.workspaceId,
        warnings: message.warnings,
        isLoading: false,
        error: null,
        lastUpdate: Date.now(),
      };
    case 'dashboard.metricsFailed':
      return {
        ...state,
        isLoading: false,
        error: message.error,
        warnings: [],
        notices: [dashboardLoadFailedNotice(message.error)],
      };
  }
}

export function applyDashboardHostMessage(
  state: DashboardPanelState,
  message: HostToWebviewMessage,
): DashboardPanelState | null {
  if (!message.type.startsWith('dashboard.')) {
    return null;
  }
  return reduceDashboardState(state, message as DashboardHostMessage);
}

export function bindDashboardActions(
  root: ParentNode,
  postMessage: (message: WebviewToHostMessage) => void,
): void {
  bindClickAction(root, 'refresh', () => {
    postMessage({ type: 'dashboard.refresh' });
  });
  bindClickAction(root, 'indexWorkspace', () => {
    postMessage({ type: 'dashboard.indexWorkspace' });
  });
}
