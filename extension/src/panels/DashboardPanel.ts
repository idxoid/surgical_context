import * as vscode from 'vscode';
import { SidecarClient } from '../sidecarClient';
import { getWebviewContent } from '../utils';
import {
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';

export class DashboardPanel {
  public static readonly viewType = 'surgicalContext.dashboard';
  private static instance: DashboardPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly extensionUri: vscode.Uri;
  private disposables: vscode.Disposable[] = [];
  private refreshInterval: NodeJS.Timeout | undefined;

  private constructor(extensionUri: vscode.Uri) {
    this.extensionUri = extensionUri;

    this.panel = vscode.window.createWebviewPanel(
      DashboardPanel.viewType,
      'Surgical Context: Dashboard',
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      }
    );

    this.panel.webview.html = getWebviewContent(
      this.panel.webview,
      extensionUri,
      'dashboard.js',
      'styles.css'
    );

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (message: WebviewToHostMessage) => this.handleWebviewMessage(message),
      null,
      this.disposables
    );

    this.loadMetrics();
    this.startPolling();
  }

  public static createOrReveal(extensionUri: vscode.Uri): void {
    if (DashboardPanel.instance) {
      DashboardPanel.instance.panel.reveal(vscode.ViewColumn.One);
      return;
    }

    DashboardPanel.instance = new DashboardPanel(extensionUri);
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
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : 'Unknown error';
      this.postMessage({
        type: 'dashboard.metricsFailed',
        error: errMsg,
      });
    }
  }

  private startPolling(): void {
    const config = vscode.workspace.getConfiguration('surgicalContext');
    const refreshSeconds = config.get<number>('dashboard.autoRefreshSeconds', 30);

    this.refreshInterval = setInterval(() => {
      if (!this.panel.active) return;
      this.loadMetrics();
    }, refreshSeconds * 1000);
  }

  private async handleWebviewMessage(message: WebviewToHostMessage): Promise<void> {
    switch (message.type) {
      case 'dashboard.refresh':
        await this.loadMetrics();
        break;
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.panel.webview.postMessage(message);
  }

  private dispose(): void {
    DashboardPanel.instance = undefined;
    if (this.refreshInterval) {
      clearInterval(this.refreshInterval);
    }
    this.disposables.forEach(d => d.dispose());
  }
}
