import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { getWebviewContent } from '../utils';
import {
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
    try {
      this.postMessage({ type: 'dashboard.loading' });

      const [health, cloudStatus, auditActionsResponse, metricsData] = await Promise.all([
        SidecarClient.health().then(ok => ({ ok })),
        SidecarClient.cloudStatus(),
        SidecarClient.auditActions(undefined, 10),
        SidecarClient.metrics().catch(() => null),
      ]);

      const auditActions = auditActionsResponse.actions.map(action => ({
        timestamp: action.timestamp,
        action_type: action.action,
        symbol: action.symbol || 'N/A',
        status: 'completed',
      }));

      this.postMessage({
        type: 'dashboard.metricsLoaded',
        health: health.ok ? 'up' : 'down',
        cloudStatus: cloudStatus.using_fallback ? 'fallback-local' : cloudStatus.using_aura ? 'connected' : 'offline',
        auditActions,
        metrics: metricsData,
      });
    } catch (error) {
      this.postMessage({
        type: 'dashboard.metricsFailed',
        error: error instanceof Error ? error.message : 'Unknown error',
      });
    }
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
