import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { getWebviewContent } from '../utils';
import {
  AuditAction,
  DashboardNotice,
  DashboardMetrics,
  HealthCheckItem,
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';

export class DashboardViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'surgicalContext.dashboard';

  private webviewView: vscode.WebviewView | undefined;
  private refreshInterval: NodeJS.Timeout | undefined;

  constructor(private extensionUri: vscode.Uri) {}

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.webviewView = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
    };

    webviewView.webview.html = getWebviewContent(
      webviewView.webview,
      this.extensionUri,
      'dashboard.js',
      'styles.css'
    );

    webviewView.webview.onDidReceiveMessage((message: WebviewToHostMessage) => {
      this.handleWebviewMessage(message);
    });

    webviewView.onDidChangeVisibility(() => {
      if (webviewView.visible) {
        this.loadMetrics();
        this.startPolling();
      } else {
        this.stopPolling();
      }
    });

    this.loadMetrics();
    this.startPolling();
  }

  private async loadMetrics(): Promise<void> {
    this.postMessage({ type: 'dashboard.loading' });

    const healthOk = await SidecarClient.health();
    const [cloudStatus, auditActionsResponse] = healthOk
      ? await Promise.all([
        SidecarClient.cloudStatus().catch(() => null),
        SidecarClient.auditActions(undefined, 10).catch(() => null),
      ])
      : [null, null];

    const auditActions: AuditAction[] = (auditActionsResponse?.actions || []).map(action => ({
      timestamp: action.timestamp,
      action_type: action.action,
      symbol: action.symbol
        || (typeof action.details?.symbol === 'string' ? action.details.symbol : undefined)
        || action.resource
        || 'N/A',
      status: action.status === 'error' ? 'failed' : 'success',
      details: action.details,
    }));

    this.postMessage({
      type: 'dashboard.metricsLoaded',
      health: healthOk ? 'up' : 'down',
      cloudStatus: cloudStatus?.using_fallback
        ? 'fallback-local'
        : cloudStatus?.using_aura
          ? 'connected'
          : cloudStatus
            ? 'local'
            : 'offline',
      auditActions,
      metrics: this.emptyDashboardMetrics(),
      healthChecks: this.buildHealthChecks(healthOk, cloudStatus),
      notices: this.buildDashboardNotices(healthOk),
      workspaceId: vscode.workspace
        .getConfiguration('surgicalContext')
        .get<string>('workspaceId', 'local/default@main'),
      warnings: [],
    });
  }

  private emptyDashboardMetrics(): DashboardMetrics {
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

  private buildHealthChecks(
    healthOk: boolean,
    cloudStatus: Awaited<ReturnType<typeof SidecarClient.cloudStatus>> | null
  ): HealthCheckItem[] {
    const config = vscode.workspace.getConfiguration('surgicalContext');
    const backendUrl = config.get<string>('backendUrl', 'http://localhost:8000');
    const workspaceId = config.get<string>('workspaceId', 'local/default@main');
    const modelPreference = config.get<string>('modelPreference', 'auto');
    const workspaceFolders = vscode.workspace.workspaceFolders || [];

    return [
      {
        id: 'sidecar',
        label: 'Sidecar',
        status: healthOk ? 'ok' : 'error',
        value: healthOk ? 'reachable' : 'offline',
        detail: backendUrl,
      },
      {
        id: 'graph',
        label: 'Graph provider',
        status: cloudStatus ? cloudStatus.using_fallback ? 'warning' : 'ok' : 'error',
        value: cloudStatus
          ? cloudStatus.using_fallback
            ? 'fallback-local'
            : cloudStatus.using_aura
              ? 'aura'
              : 'local'
          : 'offline',
        detail: cloudStatus
          ? 'Graph endpoint responded through /status/cloud.'
          : 'Could not read graph provider status.',
      },
      {
        id: 'vector',
        label: 'Vector provider',
        status: healthOk ? 'pending' : 'error',
        value: healthOk ? 'sidecar-loaded' : 'unknown',
        detail: healthOk
          ? 'Validated when dashboard metrics or retrieval calls respond.'
          : 'Sidecar is offline.',
      },
      {
        id: 'index',
        label: 'Index state',
        status: 'pending',
        value: 'unknown',
        detail: 'Open the full dashboard panel for queue details.',
      },
      {
        id: 'llm',
        label: 'LLM provider',
        status: healthOk ? 'pending' : 'error',
        value: modelPreference,
        detail: 'Model route is validated on the next ask.',
      },
      {
        id: 'workspace',
        label: 'Workspace',
        status: workspaceFolders.length > 0 && workspaceId ? 'ok' : 'warning',
        value: workspaceId || 'unset',
        detail: workspaceFolders.length > 0
          ? workspaceFolders.map(folder => folder.name).join(', ')
          : 'No VS Code workspace folder is open.',
      },
    ];
  }

  private buildDashboardNotices(healthOk: boolean): DashboardNotice[] {
    if (!healthOk) {
      return [
        {
          id: 'sidecar-offline',
          level: 'error',
          title: 'Sidecar is offline',
          message: 'Start the local sidecar and refresh to load graph, vector, index, and audit data.',
          action: 'refresh',
          actionLabel: 'Refresh',
        },
      ];
    }

    return [
      {
        id: 'dashboard-panel-required',
        level: 'info',
        title: 'Open the dashboard panel for live queue details',
        message: 'The sidebar dashboard keeps lightweight status only; the full panel reads metrics and index state.',
      },
    ];
  }

  private startPolling(): void {
    const config = vscode.workspace.getConfiguration('surgicalContext');
    const interval = config.get<number>('dashboard.autoRefreshSeconds', 30) * 1000;

    this.refreshInterval = setInterval(() => {
      if (this.webviewView?.visible) {
        this.loadMetrics();
      }
    }, interval);
  }

  private stopPolling(): void {
    if (this.refreshInterval) {
      clearInterval(this.refreshInterval);
      this.refreshInterval = undefined;
    }
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    switch (message.type) {
      case 'dashboard.refresh':
        this.loadMetrics();
        break;
      case 'dashboard.indexWorkspace':
        await vscode.commands.executeCommand('surgicalContext.indexProject');
        await this.loadMetrics();
        break;
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.webviewView?.webview.postMessage(message);
  }
}
