
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

export interface SymbolInfo {
  symbol: string;
  filePath: string;
  uid: string;
}

export function renderSymbolSummaryCard(symbolInfo: SymbolInfo): string {
  return `
    <div class="impact-header">
      <div class="symbol-summary">
        <h2>${escapeHtml(symbolInfo.symbol)}</h2>
        <div class="summary-details">
          <span class="detail-item">
            <strong>File:</strong> ${escapeHtml(symbolInfo.filePath)}
          </span>
          <span class="detail-item">
            <strong>UID:</strong> <code>${escapeHtml(symbolInfo.uid)}</code>
          </span>
        </div>
      </div>
    </div>
  `;
}

export function renderAffectsGroup(affectedSymbols: Array<Record<string, unknown>>): string {
  if (affectedSymbols.length === 0) {
    return `
      <div class="impact-group">
        <div class="group-header">📄 Affects</div>
        <div class="group-content empty">
          No affected symbols found.
        </div>
      </div>
    `;
  }

  const rows = affectedSymbols
    .map(sym => {
      const filePath = (sym.file_path as string) || 'unknown';
      const symbolName = (sym.symbol as string) || 'unknown';
      const score = sym.relevance_score as number | undefined;
      const isDirty = sym.is_dirty as boolean | undefined;

      return `
        <div class="impact-row" data-file-path="${escapeHtml(filePath)}">
          <div class="impact-row-main">
            <span class="symbol-name">${escapeHtml(symbolName)}</span>
            <span class="file-name">${escapeHtml(filePath)}</span>
          </div>
          <div class="impact-row-meta">
            ${score ? `<span class="score">${(score * 100).toFixed(0)}%</span>` : ''}
            ${isDirty ? '<span class="dirty-badge">🔴 Unsaved</span>' : ''}
          </div>
        </div>
      `;
    })
    .join('');

  return `
    <div class="impact-group">
      <div class="group-header">📄 Affects (${affectedSymbols.length})</div>
      <div class="group-content">
        ${rows}
      </div>
    </div>
  `;
}

export function renderPlaceholderGroup(title: string, message: string): string {
  return `
    <div class="impact-group">
      <div class="group-header">${escapeHtml(title)}</div>
      <div class="group-content placeholder">
        <p>${escapeHtml(message)}</p>
      </div>
    </div>
  `;
}

export function renderActionButtonRow(): string {
  return `
    <div class="impact-actions">
      <button class="action-button" data-action="ask-followup">
        💬 Ask Follow-up
      </button>
      <button class="action-button" data-action="ask-impact">
        🔄 Refresh Impact
      </button>
    </div>
  `;
}
