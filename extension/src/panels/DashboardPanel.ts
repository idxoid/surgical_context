import * as vscode from 'vscode';
import {
  AuditActionsResponse,
  CloudStatusResponse,
  IndexQueueResponse,
  IndexStatsResponse,
  SidecarClient,
} from '../sidecarClient';
import { getWebviewContent } from '../utils';
import {
  AuditAction,
  DashboardNotice,
  DashboardMetrics,
  HealthCheckItem,
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';
import { resolveWorkspaceId } from '../workspaceIdentity';
import {
  graphProviderDetail,
  graphProviderHealthStatus,
  graphProviderValue,
  resolveCloudStatus,
} from '../graphProviderStatus';

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

    const healthOk = await SidecarClient.health();
    const [cloudStatus, auditActions, metricsText, indexQueue, indexStats] = healthOk
      ? await Promise.all([
        this.safeDashboardCall('Cloud status', () => SidecarClient.cloudStatus()),
        this.safeDashboardCall('Recent activity', () => SidecarClient.auditActions(undefined, 10)),
        this.safeDashboardCall('Prometheus metrics', () => SidecarClient.metrics()),
        this.safeDashboardCall('Index queue', () => SidecarClient.indexQueueStatus()),
        this.safeDashboardCall('Index catalog', () => SidecarClient.indexStats()),
      ])
      : [
        this.emptyDashboardCall<CloudStatusResponse>(),
        this.emptyDashboardCall<AuditActionsResponse>(),
        this.emptyDashboardCall<string>(),
        this.emptyDashboardCall<IndexQueueResponse>(),
        this.emptyDashboardCall<IndexStatsResponse>(),
      ];
    const metrics: DashboardMetrics = {
      ...this.emptyDashboardMetrics(),
      ...this.parsePrometheusMetrics(metricsText.value),
      ...this.metricsFromIndexQueue(indexQueue.value),
      ...this.metricsFromIndexStats(indexStats.value),
    };
    const hasIndexedCatalog = (metrics.indexedFiles || 0) > 0
      || (metrics.indexedSymbols || 0) > 0
      || (metrics.docChunks || 0) > 0;
    if (metrics.lastIndexJobStatus === 'not indexed' && hasIndexedCatalog) {
      metrics.lastIndexJobStatus = 'idle';
    }
    const notices = this.buildDashboardNotices({
      healthOk,
      metricsText,
      indexQueue,
      indexStats,
      metrics,
    });
    const warnings = this.buildDashboardWarnings({
      healthOk,
      cloudStatus,
      auditActions,
      metricsText,
      indexQueue,
      indexStats,
    });
    const workspaceId = await resolveWorkspaceId();

    this.postMessage({
      type: 'dashboard.metricsLoaded',
      health: healthOk ? 'up' : 'down',
      cloudStatus: this.resolveCloudStatus(cloudStatus.value),
      auditActions: this.mapAuditActions(auditActions.value),
      metrics,
      healthChecks: this.buildHealthChecks({
        healthOk,
        cloudStatus: cloudStatus.value,
        metricsText: metricsText.value,
        indexQueue: indexQueue.value,
        workspaceId,
      }),
      notices,
      workspaceId: workspaceId || 'sidecar default',
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

  private emptyDashboardCall<T>(): DashboardCallResult<T> {
    return { value: null };
  }

  private buildDashboardWarnings(input: {
    healthOk: boolean;
    cloudStatus: DashboardCallResult<CloudStatusResponse>;
    auditActions: DashboardCallResult<AuditActionsResponse>;
    metricsText: DashboardCallResult<string>;
    indexQueue: DashboardCallResult<IndexQueueResponse>;
    indexStats: DashboardCallResult<IndexStatsResponse>;
  }): string[] {
    if (!input.healthOk) {
      return [];
    }

    return Array.from(new Set([
      this.cleanWarning(input.cloudStatus.warning, 'Graph provider status is unavailable.'),
      this.cleanWarning(input.auditActions.warning, 'Recent activity is unavailable.'),
      this.cleanWarning(input.indexStats.warning, 'Index catalog metrics are unavailable.'),
    ].filter((warning): warning is string => Boolean(warning))));
  }

  private cleanWarning(warning: string | undefined, fallback: string): string | undefined {
    if (!warning) return undefined;
    return /fetch failed|failed to fetch|ECONNREFUSED|connection refused/i.test(warning)
      ? fallback
      : warning;
  }

  private buildDashboardNotices(input: {
    healthOk: boolean;
    metricsText: DashboardCallResult<string>;
    indexQueue: DashboardCallResult<IndexQueueResponse>;
    indexStats: DashboardCallResult<IndexStatsResponse>;
    metrics: DashboardMetrics;
  }): DashboardNotice[] {
    if (!input.healthOk) {
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

    const notices: DashboardNotice[] = [];

    if (!input.metricsText.value) {
      notices.push({
        id: 'metrics-unavailable',
        level: 'warning',
        title: 'Metrics are unavailable',
        message: 'Core health can still be shown, but token, latency, and cost cards will stay empty.',
        action: 'refresh',
        actionLabel: 'Retry',
      });
    }

    if (!input.indexQueue.value) {
      notices.push({
        id: 'index-unavailable',
        level: 'warning',
        title: 'Index state is unavailable',
        message: 'The dashboard cannot confirm whether graph and vector context are ready.',
        action: 'refresh',
        actionLabel: 'Retry',
      });
    } else if (
      input.indexStats.value
      && this.hasNoObservedIndexWork(input.indexQueue.value, input.metrics)
    ) {
      notices.push({
        id: 'index-empty',
        level: 'info',
        title: 'No indexing jobs yet',
        message: 'Index the workspace to populate graph and vector context before relying on ask or impact results.',
        action: 'indexWorkspace',
        actionLabel: 'Index workspace',
      });
    }

    if (!input.indexStats.value) {
      notices.push({
        id: 'index-stats-unavailable',
        level: 'warning',
        title: 'Index catalog metrics are unavailable',
        message: 'Restart or update the sidecar to load indexed file, symbol, documentation, and storage counts.',
        action: 'refresh',
        actionLabel: 'Retry',
      });
    }

    return notices;
  }

  private resolveCloudStatus(
    cloudStatus: CloudStatusResponse | null
  ): 'connected' | 'fallback-local' | 'local' | 'offline' {
    return resolveCloudStatus(cloudStatus);
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
          : this.hasQueueActivity(queue)
            ? 'idle'
            : 'not indexed';

    return {
      queuePending: queue.pending,
      queueProcessing: queue.processing,
      queueProcessed: queue.processed,
      queueFailedBatches: queue.failed_batches,
      lastIndexJobStatus,
    };
  }

  private metricsFromIndexStats(response: IndexStatsResponse | null): Partial<DashboardMetrics> {
    if (!response) return {};

    return {
      indexedFiles: response.indexed_files,
      indexedSymbols: response.indexed_symbols,
      docChunks: response.doc_chunks,
      symbolsWithDocs: response.symbols_with_docs,
      storageGb: response.storage_bytes / 1_000_000_000,
    };
  }

  private hasNoObservedIndexWork(
    response: IndexQueueResponse,
    metrics: DashboardMetrics
  ): boolean {
    const hasCatalogMetrics = (metrics.indexedFiles || 0) > 0
      || (metrics.indexedSymbols || 0) > 0
      || (metrics.docChunks || 0) > 0;
    return !hasCatalogMetrics && !this.hasQueueActivity(response.queue);
  }

  private hasQueueActivity(queue: IndexQueueResponse['queue']): boolean {
    return queue.pending > 0
      || queue.processing > 0
      || queue.enqueued > 0
      || queue.coalesced > 0
      || queue.rejected > 0
      || queue.processed > 0
      || queue.failed_batches > 0
      || Boolean(queue.last_error);
  }

  private parsePrometheusMetrics(metricsText: string | null): Partial<DashboardMetrics> {
    if (!metricsText) return {};

    let requestsTotal = 0;
    let tokensTotal = 0;
    let costUsdTotal = 0;
    let askLatencySum = 0;
    let askLatencyCount = 0;
    let contextModeTotal = 0;
    let fallbackTotal = 0;
    let feedbackTotal = 0;
    let feedbackAccepted = 0;

    for (const line of metricsText.split('\n')) {
      const parsed = this.parsePrometheusLine(line);
      if (!parsed) continue;

      const isAskEndpoint = parsed.labels.endpoint === '/ask'
        || parsed.labels.endpoint === '/ask/stream';

      if (parsed.name === 'sidecar_requests_total' && isAskEndpoint) {
        requestsTotal += parsed.value;
      } else if (parsed.name === 'sidecar_tokens_total' && isAskEndpoint) {
        tokensTotal += parsed.value;
      } else if (parsed.name === 'sidecar_estimated_cost_usd_total' && isAskEndpoint) {
        costUsdTotal += parsed.value;
      } else if (
        parsed.name === 'sidecar_request_latency_ms_sum'
        && isAskEndpoint
      ) {
        askLatencySum += parsed.value;
      } else if (
        parsed.name === 'sidecar_request_latency_ms_count'
        && isAskEndpoint
      ) {
        askLatencyCount += parsed.value;
      } else if (parsed.name === 'sidecar_ask_context_total') {
        contextModeTotal += parsed.value;
        if (['file', 'workspace', 'direct'].includes(parsed.labels.mode)) {
          fallbackTotal += parsed.value;
        }
      } else if (parsed.name === 'sidecar_feedback_events_total') {
        feedbackTotal += parsed.value;
        if (parsed.labels.outcome === 'accept') {
          feedbackAccepted += parsed.value;
        }
      }
    }

    return {
      avgLatencyMs: askLatencyCount > 0 ? askLatencySum / askLatencyCount : null,
      fallbackRatePercent: contextModeTotal > 0 ? (fallbackTotal / contextModeTotal) * 100 : null,
      contextQualityPercent: feedbackTotal > 0 ? (feedbackAccepted / feedbackTotal) * 100 : null,
      requestsTotal: requestsTotal || null,
      tokensTotal: tokensTotal || null,
      costUsdTotal: costUsdTotal || null,
    };
  }

  private buildHealthChecks(input: {
    healthOk: boolean;
    cloudStatus: CloudStatusResponse | null;
    metricsText: string | null;
    indexQueue: IndexQueueResponse | null;
    workspaceId: string | undefined;
  }): HealthCheckItem[] {
    const config = vscode.workspace.getConfiguration('surgicalContext');
    const backendUrl = config.get<string>('backendUrl', 'http://localhost:8000');
    const modelPreference = config.get<string>('modelPreference', 'auto');
    const workspaceFolders = vscode.workspace.workspaceFolders || [];
    const queue = input.indexQueue?.queue;
    const llmDegraded = this.metricValue(input.metricsText, 'sidecar_llm_degraded_total');

    return [
      {
        id: 'sidecar',
        label: 'Sidecar',
        status: input.healthOk ? 'ok' : 'error',
        value: input.healthOk ? 'reachable' : 'offline',
        detail: backendUrl,
      },
      {
        id: 'graph',
        label: 'Graph provider',
        status: graphProviderHealthStatus(input.cloudStatus),
        value: graphProviderValue(input.cloudStatus),
        detail: graphProviderDetail(input.cloudStatus),
      },
      {
        id: 'vector',
        label: 'Vector provider',
        status: input.metricsText ? 'ok' : input.healthOk ? 'warning' : 'error',
        value: input.metricsText ? 'sidecar-loaded' : 'unknown',
        detail: input.metricsText
          ? 'LanceDB client is loaded with the sidecar; retrieval metrics are reachable.'
          : 'Metrics endpoint unavailable; vector state cannot be inferred.',
      },
      {
        id: 'index',
        label: 'Index state',
        status: this.indexHealthStatus(input.indexQueue),
        value: queue
          ? queue.processing > 0
            ? 'processing'
            : queue.pending > 0
              ? 'queued'
              : 'idle'
          : 'unknown',
        detail: queue
          ? `${queue.pending} pending, ${queue.processing} processing, ${queue.failed_batches} failed batches`
          : 'Index queue endpoint unavailable.',
      },
      {
        id: 'llm',
        label: 'LLM provider',
        status: !input.healthOk ? 'error' : llmDegraded > 0 ? 'warning' : 'ok',
        value: modelPreference,
        detail: llmDegraded > 0
          ? `${llmDegraded} degraded LLM responses observed.`
          : 'Model route will be validated on the next ask.',
      },
      {
        id: 'workspace',
        label: 'Workspace',
        status: workspaceFolders.length > 0 && input.workspaceId ? 'ok' : 'warning',
        value: input.workspaceId || 'sidecar default',
        detail: workspaceFolders.length > 0
          ? workspaceFolders.map(folder => folder.name).join(', ')
          : 'No VS Code workspace folder is open.',
      },
    ];
  }

  private indexHealthStatus(response: IndexQueueResponse | null): HealthCheckItem['status'] {
    const queue = response?.queue;
    if (!queue) return 'warning';
    if (queue.failed_batches > 0 || queue.last_error) return 'error';
    if (queue.pending > 0 || queue.processing > 0) return 'warning';
    return 'ok';
  }

  private metricValue(metricsText: string | null, metricName: string): number {
    if (!metricsText) return 0;

    let total = 0;
    for (const line of metricsText.split('\n')) {
      const parsed = this.parsePrometheusLine(line);
      if (parsed?.name === metricName) {
        total += parsed.value;
      }
    }
    return total;
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
      case 'dashboard.indexWorkspace':
        await vscode.commands.executeCommand('surgicalContext.indexProject');
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
