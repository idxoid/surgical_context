import { AuditAction } from './protocol';

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
  cloudStatus: 'connected' | 'fallback-local' | 'offline';
}

export function renderDashboardHeader(): string {
  return `
    <div class="dashboard-header">
      <h1>Surgical Context Dashboard</h1>
      <p>System health and operational metrics</p>
    </div>
  `;
}

export function renderRefreshButton(isLoading: boolean, lastUpdate: number | null): string {
  const lastUpdateText = lastUpdate
    ? new Date(lastUpdate).toLocaleTimeString()
    : 'Never';

  return `
    <div class="refresh-info">
      <span class="last-update">Last updated: ${escapeHtml(lastUpdateText)}</span>
      <button class="refresh-button ${isLoading ? 'loading' : ''}" data-action="refresh" ${isLoading ? 'disabled' : ''}>
        ${isLoading ? '⟳ Refreshing...' : '🔄 Refresh'}
      </button>
    </div>
  `;
}

export function renderMetricCardGrid(props: MetricCardGridProps): string {
  const healthColor = props.health === 'up' ? '#4CAF50' : props.health === 'degraded' ? '#FF9800' : '#F44336';
  const cloudColor = props.cloudStatus === 'connected' ? '#4CAF50' : props.cloudStatus === 'fallback-local' ? '#FF9800' : '#F44336';

  return `
    <div class="metric-card-grid">
      <div class="metric-card health-card">
        <div class="metric-icon" style="color: ${healthColor}">⚙️</div>
        <div class="metric-info">
          <div class="metric-label">Sidecar Health</div>
          <div class="metric-value">${props.health}</div>
        </div>
      </div>

      <div class="metric-card cloud-card">
        <div class="metric-icon" style="color: ${cloudColor}">☁️</div>
        <div class="metric-info">
          <div class="metric-label">Cloud Status</div>
          <div class="metric-value">${props.cloudStatus}</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">📁</div>
        <div class="metric-info">
          <div class="metric-label">Indexed Files</div>
          <div class="metric-value">—</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">✨</div>
        <div class="metric-info">
          <div class="metric-label">Indexed Symbols</div>
          <div class="metric-value">—</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">⏱️</div>
        <div class="metric-info">
          <div class="metric-label">Avg Latency</div>
          <div class="metric-value">—</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-icon">💰</div>
        <div class="metric-info">
          <div class="metric-label">Token Savings</div>
          <div class="metric-value">—</div>
          <div class="metric-note">Coming in Phase 6</div>
        </div>
      </div>
    </div>
  `;
}

export function renderAuditEventsCard(auditActions: AuditAction[]): string {
  if (auditActions.length === 0) {
    return `
      <div class="dashboard-card audit-card">
        <div class="card-header">📋 Recent Activity</div>
        <div class="card-content empty">
          No recent events
        </div>
      </div>
    `;
  }

  const rows = auditActions
    .map(action => {
      const timestamp = new Date(action.timestamp).toLocaleString();
      const actionType = action.action_type || 'unknown';
      const symbol = action.symbol || '—';
      const status = action.status || '—';

      return `
        <div class="audit-row">
          <div class="audit-main">
            <span class="action-type">${escapeHtml(actionType)}</span>
            <span class="timestamp">${escapeHtml(timestamp)}</span>
          </div>
          <div class="audit-meta">
            <span class="symbol">${escapeHtml(symbol)}</span>
            <span class="status ${status.toLowerCase()}">${escapeHtml(status)}</span>
          </div>
        </div>
      `;
    })
    .join('');

  return `
    <div class="dashboard-card audit-card">
      <div class="card-header">📋 Recent Activity</div>
      <div class="card-content">
        ${rows}
      </div>
    </div>
  `;
}
