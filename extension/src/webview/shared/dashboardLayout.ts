import { AuditAction, DashboardMetrics } from './protocol';

export function escapeHtml(text: string): string {
  const map: Record<string, string> = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  };
  return text.replace(/[&<>"']/g, m => map[m]);
}

export interface MetricCardGridProps {
  health: 'up' | 'down' | 'degraded';
  cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline';
  metrics: DashboardMetrics;
}

export function renderDashboardHeader(workspaceId: string, lastUpdate: number | null): string {
  const lastUpdateText = lastUpdate ? `${secondsAgo(lastUpdate)} ago` : 'never';

  return `
    <div class="dashboard-header">
      <div>
        <h1>Surgical Context Dashboard</h1>
        <p>Operational overview of your indexing sidecar and context system.</p>
      </div>
      <div class="dashboard-meta">
        <div>Workspace: <span>${escapeHtml(workspaceId)}</span></div>
        <div>Last updated: <span>${escapeHtml(lastUpdateText)}</span></div>
      </div>
    </div>
  `;
}

export function renderRefreshButton(isLoading: boolean): string {
  return `
    <button class="refresh-button ${isLoading ? 'loading' : ''}" data-action="refresh" ${isLoading ? 'disabled' : ''}>
      ${isLoading ? 'Refreshing...' : 'Refresh'}
    </button>
  `;
}

export function renderDashboardWarnings(warnings: string[]): string {
  if (warnings.length === 0) return '';

  return `
    <div class="dashboard-warning" role="status">
      ${warnings.map(warning => `<div>${escapeHtml(warning)}</div>`).join('')}
    </div>
  `;
}

export function renderMetricCardGrid(props: MetricCardGridProps): string {
  const metrics = props.metrics;
  const healthStatus = props.health === 'up' ? 'success' : 'danger';
  const cloudStatus = props.cloudStatus === 'connected' || props.cloudStatus === 'local'
    ? 'success'
    : props.cloudStatus === 'fallback-local'
      ? 'warning'
      : 'danger';
  const queueStatus = metrics.queueFailedBatches && metrics.queueFailedBatches > 0
    ? 'danger'
    : metrics.queuePending && metrics.queuePending > 0
      ? 'warning'
      : 'success';

  return `
    <div class="metric-card-grid">
      ${renderMetricCard('Sidecar health', healthLabel(props.health), props.health === 'up' ? 'Ready for requests' : 'Check backend URL', 'pulse', healthStatus)}
      ${renderMetricCard('Cloud status', cloudLabel(props.cloudStatus), cloudNote(props.cloudStatus), 'cloud', cloudStatus)}
      ${renderMetricCard('Indexed files', formatNumber(metrics.indexedFiles), 'Metric pending from index catalog', 'file')}
      ${renderMetricCard('Indexed symbols', formatNumber(metrics.indexedSymbols), 'Metric pending from graph catalog', 'code')}
      ${renderMetricCard('Doc chunks', formatNumber(metrics.docChunks), 'Metric pending from docs index', 'doc')}
      ${renderMetricCard('Last indexing job', metrics.lastIndexJobStatus || 'idle', queueSummary(metrics), 'play', queueStatus)}
      ${renderMetricCard('Avg latency (ask)', formatMs(metrics.avgLatencyMs), metrics.requestsTotal ? `${formatNumber(metrics.requestsTotal)} requests observed` : 'Waiting for ask traffic', 'clock')}
      ${renderMetricCard('Token savings', formatPercent(metrics.tokenSavingsPercent), metrics.tokensTotal ? `${formatNumber(metrics.tokensTotal)} tokens observed` : 'Needs prompt telemetry', 'trend')}
      ${renderMetricCard('Fallback rate', formatPercent(metrics.fallbackRatePercent), 'Metric pending from retrieval cache', 'sync')}
      ${renderMetricCard('Context quality', formatPercent(metrics.contextQualityPercent), 'Feedback signal pending', 'target')}
      ${renderMetricCard('Symbols with docs', formatNumber(metrics.symbolsWithDocs), 'Metric pending from docs links', 'book')}
      ${renderMetricCard('Storage (sidecar)', formatGb(metrics.storageGb), metrics.costUsdTotal ? `$${metrics.costUsdTotal.toFixed(4)} estimated cost` : 'Metric pending from storage layer', 'db')}
    </div>
  `;
}

export function renderTokenSavingsCard(metrics: DashboardMetrics): string {
  const savings = metrics.tokenSavingsPercent;
  const value = formatPercent(savings);
  const bars = savings === null
    ? [38, 42, 44, 40, 46, 43, 45, 41, 39, 44, 47, 45]
    : [56, 62, 67, 64, 70, 73, Math.max(8, savings), 68, 71, 66, 74, 76];

  return `
    <div class="dashboard-card token-savings-card">
      <div class="card-header">
        <span>Token savings vs naive context</span>
        <span class="card-header-meta">${savings === null ? 'pending' : 'live'}</span>
      </div>
      <div class="token-savings-body">
        <div>
          <div class="token-savings-value">${value}</div>
          <div class="metric-note">
            ${metrics.tokensTotal ? `${formatNumber(metrics.tokensTotal)} tokens processed` : 'Prompt telemetry has not produced token savings yet.'}
          </div>
        </div>
        <div class="savings-chart" aria-label="Token savings trend">
          ${bars.map((height, index) => `
            <span
              class="${savings === null ? 'pending' : ''}"
              style="height: ${height}%"
              title="Sample ${index + 1}"
            ></span>
          `).join('')}
        </div>
      </div>
    </div>
  `;
}

export function renderIndexingJobsCard(metrics: DashboardMetrics): string {
  const rows = [
    {
      time: 'now',
      type: 'Queue',
      scope: 'workspace',
      status: metrics.lastIndexJobStatus || 'idle',
      duration: metrics.queueProcessing && metrics.queueProcessing > 0 ? 'active' : '0s',
    },
    {
      time: 'total',
      type: 'Processed',
      scope: 'files',
      status: metrics.queueFailedBatches && metrics.queueFailedBatches > 0 ? 'attention' : 'success',
      duration: formatNumber(metrics.queueProcessed),
    },
    {
      time: 'pending',
      type: 'Backlog',
      scope: 'queue',
      status: metrics.queuePending && metrics.queuePending > 0 ? 'queued' : 'clear',
      duration: formatNumber(metrics.queuePending),
    },
  ];

  return `
    <div class="dashboard-card indexing-card">
      <div class="card-header">
        <span>Recent indexing jobs</span>
        <span class="card-header-meta">queue</span>
      </div>
      <div class="dashboard-table" role="table" aria-label="Recent indexing jobs">
        <div class="dashboard-table-row header" role="row">
          <span>Time</span>
          <span>Type</span>
          <span>Scope</span>
          <span>Status</span>
          <span>Duration</span>
        </div>
        ${rows.map(row => `
          <div class="dashboard-table-row" role="row">
            <span>${escapeHtml(row.time)}</span>
            <span>${escapeHtml(row.type)}</span>
            <span>${escapeHtml(row.scope)}</span>
            <span class="status ${escapeHtml(row.status.toLowerCase())}">${escapeHtml(row.status)}</span>
            <span>${escapeHtml(row.duration)}</span>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

export function renderAuditEventsCard(auditActions: AuditAction[]): string {
  const rows = auditActions.length === 0
    ? `
      <div class="dashboard-table-row empty" role="row">
        <span>No recent audit events</span>
      </div>
    `
    : auditActions
      .map(action => {
        const timestamp = formatTimestamp(action.timestamp);
        const actionType = action.action_type || 'unknown';
        const symbol = action.symbol || 'N/A';
        const status = action.status || 'success';
        const detail = action.details ? summarizeDetails(action.details) : symbol;

        return `
          <div class="dashboard-table-row audit" role="row">
            <span>${escapeHtml(timestamp)}</span>
            <span>${escapeHtml(actionType)}</span>
            <span>${escapeHtml(detail)}</span>
            <span class="status ${escapeHtml(status.toLowerCase())}">${escapeHtml(status)}</span>
          </div>
        `;
      })
      .join('');

  return `
    <div class="dashboard-card audit-card">
      <div class="card-header">
        <span>Recent audit events</span>
        <span class="card-header-meta">latest</span>
      </div>
      <div class="dashboard-table audit-table" role="table" aria-label="Recent audit events">
        <div class="dashboard-table-row header" role="row">
          <span>Time</span>
          <span>Event</span>
          <span>Details</span>
          <span>Status</span>
        </div>
        ${rows}
      </div>
    </div>
  `;
}

function renderMetricCard(
  label: string,
  value: string,
  note: string,
  icon: string,
  status: 'success' | 'warning' | 'danger' | 'neutral' = 'neutral'
): string {
  return `
    <div class="metric-card ${status}">
      <div class="metric-icon" aria-hidden="true">${escapeHtml(iconSymbol(icon))}</div>
      <div class="metric-info">
        <div class="metric-label">${escapeHtml(label)}</div>
        <div class="metric-value">${escapeHtml(value)}</div>
        <div class="metric-note">${escapeHtml(note)}</div>
      </div>
    </div>
  `;
}

function iconSymbol(name: string): string {
  const icons: Record<string, string> = {
    pulse: '◇',
    cloud: '☁',
    file: '□',
    code: '</>',
    doc: '▤',
    play: '▷',
    clock: '○',
    trend: '⌁',
    sync: '↻',
    target: '◎',
    book: '▱',
    db: '▥',
  };
  return icons[name] || '□';
}

function healthLabel(health: 'up' | 'down' | 'degraded'): string {
  if (health === 'up') return 'healthy';
  if (health === 'degraded') return 'degraded';
  return 'down';
}

function cloudLabel(status: 'connected' | 'fallback-local' | 'local' | 'offline'): string {
  if (status === 'connected') return 'connected';
  if (status === 'fallback-local') return 'fallback';
  if (status === 'local') return 'local';
  return 'offline';
}

function cloudNote(status: 'connected' | 'fallback-local' | 'local' | 'offline'): string {
  if (status === 'connected') return 'Aura graph provider active';
  if (status === 'fallback-local') return 'Local fallback active';
  if (status === 'local') return 'Local graph provider active';
  return 'Cloud/local status unavailable';
}

function queueSummary(metrics: DashboardMetrics): string {
  const pending = metrics.queuePending ?? 0;
  const processing = metrics.queueProcessing ?? 0;
  const failed = metrics.queueFailedBatches ?? 0;
  if (failed > 0) return `${failed} failed batch${failed === 1 ? '' : 'es'}`;
  if (processing > 0) return `${processing} processing`;
  if (pending > 0) return `${pending} pending`;
  return 'Queue clear';
}

function formatNumber(value: number | null): string {
  return value === null ? '-' : new Intl.NumberFormat().format(Math.round(value));
}

function formatPercent(value: number | null): string {
  return value === null ? '-' : `${Math.round(value)}%`;
}

function formatMs(value: number | null): string {
  return value === null ? '-' : `${Math.round(value)} ms`;
}

function formatGb(value: number | null): string {
  return value === null ? '-' : `${value.toFixed(1)} GB`;
}

function secondsAgo(timestamp: number): string {
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  return `${minutes}m`;
}

function formatTimestamp(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return `${secondsAgo(date.getTime())} ago`;
}

function summarizeDetails(details: Record<string, unknown>): string {
  if (typeof details.symbol === 'string') return details.symbol;
  if (typeof details.file_path === 'string') return details.file_path;
  if (typeof details.project_path === 'string') return details.project_path;
  if (typeof details.error === 'string') return summarizeError(details.error);

  const entries = Object.entries(details).slice(0, 2);
  if (entries.length === 0) return 'N/A';
  return entries.map(([key, value]) => `${key}: ${String(value)}`).join(' • ');
}

function summarizeError(error: string): string {
  const missingSymbol = error.match(/Symbol '([^']+)' not found in graph/i);
  if (missingSymbol) {
    return `Symbol not indexed: ${missingSymbol[1]}`;
  }

  return error.replace(/^Error:\s*/i, '');
}
