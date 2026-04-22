import * as vscode from 'vscode';
import {
  AuditActionsResponse,
  CloudStatusResponse,
  IndexQueueResponse,
  SidecarClient,
} from '../sidecarClient';
import { getWebviewContent } from '../utils';
import {
  AuditAction,
  DashboardMetrics,
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';

type DashboardCallResult<T> = {
  value: T | null;
  warning?: string;
};

export class DashboardPanel {
  public static readonly viewType = 'surgicalContext.dashboard';
  private static instance: DashboardPanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly extensionUri: vscode.Uri;
  private disposables: vscode.Disposable[] = [];
  private refreshInterval: NodeJS.Timeout | undefined;

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri) {
    this.panel = panel;
    this.extensionUri = extensionUri;

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

    this.panel.onDidChangeViewState((e) => {
      if (e.webviewPanel.visible) {
        this.loadMetrics();
        this.startPolling();
      } else {
        this.stopPolling();
      }
    });

    this.loadMetrics();
    this.startPolling();
  }

  public static createOrReveal(extensionUri: vscode.Uri): void {
    const column = vscode.ViewColumn.One;

    if (DashboardPanel.instance) {
      DashboardPanel.instance.panel.reveal(column);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      DashboardPanel.viewType,
      'Surgical Context: Dashboard',
      column,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
      }
    );

    DashboardPanel.instance = new DashboardPanel(panel, extensionUri);
  }

  private async loadMetrics(): Promise<void> {
    this.postMessage({ type: 'dashboard.loading' });

    const [healthOk, cloudStatus, auditActions, metricsText, indexQueue] = await Promise.all([
      SidecarClient.health(),
      this.safeDashboardCall('Cloud status', () => SidecarClient.cloudStatus()),
      this.safeDashboardCall('Recent activity', () => SidecarClient.auditActions(undefined, 10)),
      this.safeDashboardCall('Prometheus metrics', () => SidecarClient.metrics()),
      this.safeDashboardCall('Index queue', () => SidecarClient.indexQueueStatus()),
    ]);

    const warnings = [
      healthOk ? undefined : 'Sidecar health check failed. Showing degraded dashboard data.',
      cloudStatus.warning,
      auditActions.warning,
      metricsText.warning,
      indexQueue.warning,
    ].filter((warning): warning is string => Boolean(warning));

    this.postMessage({
      type: 'dashboard.metricsLoaded',
      health: healthOk ? 'up' : 'down',
      cloudStatus: this.resolveCloudStatus(cloudStatus.value),
      auditActions: this.mapAuditActions(auditActions.value),
      metrics: {
        ...this.emptyDashboardMetrics(),
        ...this.parsePrometheusMetrics(metricsText.value),
        ...this.metricsFromIndexQueue(indexQueue.value),
      },
      workspaceId: vscode.workspace
        .getConfiguration('surgicalContext')
        .get<string>('workspaceId', 'local/default@main'),
      warnings,
    });
  }

  private async safeDashboardCall<T>(
    label: string,
    load: () => Promise<T>
  ): Promise<DashboardCallResult<T>> {
    try {
      return { value: await load() };
    } catch (error) {
      return {
        value: null,
        warning: `${label}: ${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  private resolveCloudStatus(
    cloudStatus: CloudStatusResponse | null
  ): 'connected' | 'fallback-local' | 'local' | 'offline' {
    if (!cloudStatus) return 'offline';
    if (cloudStatus.using_fallback) return 'fallback-local';
    if (cloudStatus.using_aura) return 'connected';
    return 'local';
  }

  private mapAuditActions(response: AuditActionsResponse | null): AuditAction[] {
    if (!response) return [];

    return response.actions.map(action => {
      const details = action.details || {};
      const symbol = action.symbol
        || (typeof details.symbol === 'string' ? details.symbol : undefined)
        || action.resource
        || 'N/A';

      return {
        timestamp: action.timestamp,
        action_type: action.action,
        symbol,
        status: action.status === 'error' ? 'failed' : 'success',
        details,
      };
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

  private metricsFromIndexQueue(response: IndexQueueResponse | null): Partial<DashboardMetrics> {
    if (!response) return {};

    const queue = response.queue;
    const lastIndexJobStatus = queue.processing > 0
      ? 'processing'
      : queue.pending > 0
        ? 'queued'
        : queue.last_error
          ? 'attention'
          : 'idle';

    return {
      queuePending: queue.pending,
      queueProcessing: queue.processing,
      queueProcessed: queue.processed,
      queueFailedBatches: queue.failed_batches,
      lastIndexJobStatus,
    };
  }

  private parsePrometheusMetrics(metricsText: string | null): Partial<DashboardMetrics> {
    if (!metricsText) return {};

    let requestsTotal = 0;
    let tokensTotal = 0;
    let costUsdTotal = 0;
    let askLatencySum = 0;
    let askLatencyCount = 0;

    for (const line of metricsText.split('\n')) {
      const parsed = this.parsePrometheusLine(line);
      if (!parsed) continue;

      if (parsed.name === 'sidecar_requests_total') {
        requestsTotal += parsed.value;
      } else if (parsed.name === 'sidecar_tokens_total') {
        tokensTotal += parsed.value;
      } else if (parsed.name === 'sidecar_estimated_cost_usd_total') {
        costUsdTotal += parsed.value;
      } else if (
        parsed.name === 'sidecar_request_latency_ms_sum'
        && parsed.labels.endpoint === '/ask'
      ) {
        askLatencySum += parsed.value;
      } else if (
        parsed.name === 'sidecar_request_latency_ms_count'
        && parsed.labels.endpoint === '/ask'
      ) {
        askLatencyCount += parsed.value;
      }
    }

    return {
      avgLatencyMs: askLatencyCount > 0 ? askLatencySum / askLatencyCount : null,
      requestsTotal: requestsTotal || null,
      tokensTotal: tokensTotal || null,
      costUsdTotal: costUsdTotal || null,
    };
  }

  private parsePrometheusLine(
    line: string
  ): { name: string; labels: Record<string, string>; value: number } | null {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) return null;

    const match = trimmed.match(/^([a-zA-Z_:][\w:]*)({([^}]*)})?\s+([-+0-9.eE]+)$/);
    if (!match) return null;

    const labels: Record<string, string> = {};
    const labelText = match[3] || '';
    const labelRegex = /(\w+)="([^"]*)"/g;
    let labelMatch: RegExpExecArray | null;
    while ((labelMatch = labelRegex.exec(labelText)) !== null) {
      labels[labelMatch[1]] = labelMatch[2];
    }

    const value = Number(match[4]);
    if (!Number.isFinite(value)) return null;

    return { name: match[1], labels, value };
  }

  private startPolling(): void {
    if (this.refreshInterval) return;

    const config = vscode.workspace.getConfiguration('surgicalContext');
    const interval = config.get<number>('dashboard.autoRefreshSeconds', 30) * 1000;

    this.refreshInterval = setInterval(() => {
      if (this.panel.visible) {
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
        await this.loadMetrics();
        break;
    }
  }

  private postMessage(message: HostToWebviewMessage): void {
    this.panel.webview.postMessage(message);
  }

  private dispose(): void {
    DashboardPanel.instance = undefined;
    this.stopPolling();

    while (this.disposables.length) {
      const x = this.disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }
}
