import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { getWebviewContent } from '../utils';
import {
  AuditAction,
  DashboardMetrics,
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
    const [cloudStatus, auditActionsResponse] = await Promise.all([
      SidecarClient.cloudStatus().catch(() => null),
      SidecarClient.auditActions(undefined, 10).catch(() => null),
    ]);

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
      workspaceId: vscode.workspace
        .getConfiguration('surgicalContext')
        .get<string>('workspaceId', 'local/default@main'),
      warnings: healthOk ? [] : ['Sidecar health check failed. Showing degraded dashboard data.'],
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
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.webviewView?.webview.postMessage(message);
  }
}
