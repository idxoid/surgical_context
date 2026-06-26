import * as vscode from 'vscode';
import {
  AuditActionsResponse,
  CloudStatusResponse,
  IndexQueueResponse,
  IndexStatsResponse,
  SidecarClient,
} from '../context_engineClient';
import { getWebviewContent } from '../utils';
import {
  AuditAction,
  DashboardNotice,
  DashboardMetrics,
  HealthCheckItem,
  WebviewToHostMessage,
  HostToWebviewMessage,
} from '../webview/shared/protocol';
import { emptyDashboardMetrics } from '../webview/shared/dashboardDefaults';
import { resolveWorkspaceId } from '../workspaceIdentity';
import {
  graphProviderDetail,
  graphProviderHealthStatus,
  graphProviderValue,
  resolveCloudStatus,
} from '../graphProviderStatus';

function skipPrometheusLabelSeparators(labelText: string, start: number): number {
  let i = start;
  while (i < labelText.length && (labelText[i] === ' ' || labelText[i] === ',')) {
    i += 1;
  }
  return i;
}

function parseOnePrometheusLabel(
  labelText: string,
  start: number
): { key: string; value: string; next: number } | null {
  let i = skipPrometheusLabelSeparators(labelText, start);
  if (i >= labelText.length) {
    return null;
  }
  let eq = i;
  while (eq < labelText.length && labelText[eq] !== '=') {
    eq += 1;
  }
  if (eq + 1 >= labelText.length || labelText[eq + 1] !== '"') {
    return null;
  }
  const key = labelText.slice(i, eq);
  let close = eq + 2;
  while (close < labelText.length && labelText[close] !== '"') {
    close += 1;
  }
  if (close >= labelText.length) {
    return null;
  }
  return { key, value: labelText.slice(eq + 2, close), next: close + 1 };
}

function parsePrometheusLabels(labelText: string): Record<string, string> {
  const labels: Record<string, string> = {};
  let i = 0;
  while (i < labelText.length) {
    const parsed = parseOnePrometheusLabel(labelText, i);
    if (!parsed) {
      break;
    }
    labels[parsed.key] = parsed.value;
    i = parsed.next;
  }
  return labels;
}

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
        void this.loadMetrics();
        this.startPolling();
      } else {
        this.stopPolling();
      }
    });
  }

  private async initialize(): Promise<void> {
    await this.loadMetrics();
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
    void DashboardPanel.instance.initialize();
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
      ...emptyDashboardMetrics(),
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
      workspaceId: workspaceId || 'context_engine default',
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
          id: 'context_engine-offline',
          level: 'error',
          title: 'Sidecar is offline',
          message: 'Start the local context_engine and refresh to load graph, vector, index, and audit data.',
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
        message: 'Restart or update the context_engine to load indexed file, symbol, documentation, and storage counts.',
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

    const accum = this.emptyPrometheusMetricAccum();
    for (const line of metricsText.split('\n')) {
      const parsed = this.parsePrometheusLine(line);
      if (!parsed) continue;
      this.accumulatePrometheusMetric(parsed, accum);
    }
    return this.dashboardMetricsFromPrometheusAccum(accum);
  }

  private emptyPrometheusMetricAccum() {
    return {
      requestsTotal: 0,
      tokensTotal: 0,
      costUsdTotal: 0,
      askLatencySum: 0,
      askLatencyCount: 0,
      contextModeTotal: 0,
      fallbackTotal: 0,
      feedbackTotal: 0,
      feedbackAccepted: 0,
    };
  }

  private isAskEndpointMetric(labels: Record<string, string>): boolean {
    return labels.endpoint === '/ask' || labels.endpoint === '/ask/stream';
  }

  private accumulatePrometheusMetric(
    parsed: { name: string; labels: Record<string, string>; value: number },
    accum: ReturnType<DashboardPanel['emptyPrometheusMetricAccum']>,
  ): void {
    const isAskEndpoint = this.isAskEndpointMetric(parsed.labels);
    this.accumulateAskTrafficMetrics(parsed.name, parsed.value, isAskEndpoint, accum);
    this.accumulateAskLatencyMetrics(parsed.name, parsed.value, isAskEndpoint, accum);
    this.accumulateContextModeMetrics(parsed.name, parsed.value, parsed.labels, accum);
    this.accumulateFeedbackMetrics(parsed.name, parsed.value, parsed.labels, accum);
  }

  private accumulateAskTrafficMetrics(
    name: string,
    value: number,
    isAskEndpoint: boolean,
    accum: ReturnType<DashboardPanel['emptyPrometheusMetricAccum']>,
  ): void {
    if (!isAskEndpoint) return;
    if (name === 'context_engine_requests_total') {
      accum.requestsTotal += value;
      return;
    }
    if (name === 'context_engine_tokens_total') {
      accum.tokensTotal += value;
      return;
    }
    if (name === 'context_engine_estimated_cost_usd_total') {
      accum.costUsdTotal += value;
    }
  }

  private accumulateAskLatencyMetrics(
    name: string,
    value: number,
    isAskEndpoint: boolean,
    accum: ReturnType<DashboardPanel['emptyPrometheusMetricAccum']>,
  ): void {
    if (!isAskEndpoint) return;
    if (name === 'context_engine_request_latency_ms_sum') {
      accum.askLatencySum += value;
      return;
    }
    if (name === 'context_engine_request_latency_ms_count') {
      accum.askLatencyCount += value;
    }
  }

  private accumulateContextModeMetrics(
    name: string,
    value: number,
    labels: Record<string, string>,
    accum: ReturnType<DashboardPanel['emptyPrometheusMetricAccum']>,
  ): void {
    if (name !== 'context_engine_ask_context_total') return;
    accum.contextModeTotal += value;
    if (['file', 'workspace', 'direct'].includes(labels.mode)) {
      accum.fallbackTotal += value;
    }
  }

  private accumulateFeedbackMetrics(
    name: string,
    value: number,
    labels: Record<string, string>,
    accum: ReturnType<DashboardPanel['emptyPrometheusMetricAccum']>,
  ): void {
    if (name !== 'context_engine_feedback_events_total') return;
    accum.feedbackTotal += value;
    if (labels.outcome === 'accept') {
      accum.feedbackAccepted += value;
    }
  }

  private dashboardMetricsFromPrometheusAccum(
    accum: ReturnType<DashboardPanel['emptyPrometheusMetricAccum']>,
  ): Partial<DashboardMetrics> {
    return {
      avgLatencyMs: accum.askLatencyCount > 0 ? accum.askLatencySum / accum.askLatencyCount : null,
      fallbackRatePercent: accum.contextModeTotal > 0
        ? (accum.fallbackTotal / accum.contextModeTotal) * 100
        : null,
      contextQualityPercent: accum.feedbackTotal > 0
        ? (accum.feedbackAccepted / accum.feedbackTotal) * 100
        : null,
      requestsTotal: accum.requestsTotal || null,
      tokensTotal: accum.tokensTotal || null,
      costUsdTotal: accum.costUsdTotal || null,
    };
  }

  private indexQueueHealthValue(queue: IndexQueueResponse['queue']): string {
    if (!queue) return 'unknown';
    if (queue.processing > 0) return 'processing';
    if (queue.pending > 0) return 'queued';
    return 'idle';
  }

  private indexQueueHealthDetail(queue: IndexQueueResponse['queue']): string {
    if (!queue) return 'Index queue endpoint unavailable.';
    return `${queue.pending} pending, ${queue.processing} processing, ${queue.failed_batches} failed batches`;
  }

  private vectorHealthStatus(metricsText: string | null, healthOk: boolean): HealthCheckItem['status'] {
    if (metricsText) return 'ok';
    return healthOk ? 'warning' : 'error';
  }

  private vectorHealthDetail(metricsText: string | null): string {
    if (metricsText) {
      return 'LanceDB client is loaded with the context_engine; retrieval metrics are reachable.';
    }
    return 'Metrics endpoint unavailable; vector state cannot be inferred.';
  }

  private llmHealthStatus(healthOk: boolean, llmDegraded: number): HealthCheckItem['status'] {
    if (!healthOk) return 'error';
    if (llmDegraded > 0) return 'warning';
    return 'ok';
  }

  private llmHealthDetail(llmDegraded: number): string {
    if (llmDegraded > 0) {
      return `${llmDegraded} degraded LLM responses observed.`;
    }
    return 'Model route will be validated on the next ask.';
  }

  private workspaceHealthStatus(
    workspaceFolders: readonly vscode.WorkspaceFolder[],
    workspaceId: string | undefined,
  ): HealthCheckItem['status'] {
    return workspaceFolders.length > 0 && workspaceId ? 'ok' : 'warning';
  }

  private workspaceHealthDetail(workspaceFolders: readonly vscode.WorkspaceFolder[]): string {
    if (workspaceFolders.length > 0) {
      return workspaceFolders.map(folder => folder.name).join(', ');
    }
    return 'No VS Code workspace folder is open.';
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
    const llmDegraded = this.metricValue(input.metricsText, 'context_engine_llm_degraded_total');

    return [
      {
        id: 'context_engine',
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
        status: this.vectorHealthStatus(input.metricsText, input.healthOk),
        value: input.metricsText ? 'context_engine-loaded' : 'unknown',
        detail: this.vectorHealthDetail(input.metricsText),
      },
      {
        id: 'index',
        label: 'Index state',
        status: this.indexHealthStatus(input.indexQueue),
        value: this.indexQueueHealthValue(queue),
        detail: this.indexQueueHealthDetail(queue),
      },
      {
        id: 'llm',
        label: 'LLM provider',
        status: this.llmHealthStatus(input.healthOk, llmDegraded),
        value: modelPreference,
        detail: this.llmHealthDetail(llmDegraded),
      },
      {
        id: 'workspace',
        label: 'Workspace',
        status: this.workspaceHealthStatus(workspaceFolders, input.workspaceId),
        value: input.workspaceId || 'context_engine default',
        detail: this.workspaceHealthDetail(workspaceFolders),
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
    Object.assign(labels, parsePrometheusLabels(labelText));

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
