
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
  affectedCount?: number;
  fileCount?: number;
  maxDepth?: number;
  sourceLabel?: string;
}

export function renderSymbolSummaryCard(symbolInfo: SymbolInfo): string {
  return `
    <div class="impact-symbol-card">
      <div class="impact-symbol-title">
        <span class="impact-info-icon" aria-hidden="true">i</span>
        <strong>${escapeHtml(symbolInfo.symbol)}</strong>
      </div>
      <div class="impact-symbol-meta">
        <span>Method</span>
        <span>${escapeHtml(symbolInfo.filePath)}</span>
        <code>${escapeHtml(symbolInfo.uid)}</code>
      </div>
      <div class="impact-metrics" aria-label="Impact summary">
        ${renderMetric('Symbols', symbolInfo.affectedCount)}
        ${renderMetric('Files', symbolInfo.fileCount)}
        ${renderMetric('Depth', symbolInfo.maxDepth)}
        ${symbolInfo.sourceLabel ? `<span class="impact-source-chip">${escapeHtml(symbolInfo.sourceLabel)}</span>` : ''}
      </div>
    </div>
  `;
}

function renderMetric(label: string, value: number | undefined): string {
  return `
    <span class="impact-metric">
      <strong>${Number.isFinite(value) ? value : 0}</strong>
      <span>${escapeHtml(label)}</span>
    </span>
  `;
}

export function renderAffectsGroup(
  affectedSymbols: Array<Record<string, unknown>>,
  title = 'Affects',
  expanded = true
): string {
  if (affectedSymbols.length === 0) {
    return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml(title)}</div>
        <div class="group-content empty">
          No related symbols found.
        </div>
      </div>
    `;
  }

  const rows = affectedSymbols
    .map(sym => {
      const filePath = (sym.file_path as string) || 'unknown';
      const symbolName = (sym.symbol as string) || (sym.name as string) || 'unknown';
      const score = sym.relevance_score as number | undefined;
      const isDirty = sym.is_dirty as boolean | undefined;
      const relation = (sym.relation as string) || (sym.direction as string) || 'related';
      const depth = typeof sym.depth === 'number' ? `d${sym.depth}` : '';
      const line = lineFromSymbol(sym);
      const depthClass = typeof sym.depth === 'number' && sym.depth <= 1 ? 'direct' : 'indirect';

      return `
        <button
          type="button"
          class="impact-row"
          data-action="openFile"
          data-file-path="${escapeHtml(filePath)}"
          data-line="${line}"
          title="Open ${escapeHtml(symbolName)}"
        >
          <span class="impact-chevron" aria-hidden="true">›</span>
          <span class="impact-symbol">${escapeHtml(symbolName)}</span>
          <span class="impact-file">${escapeHtml(filePath)}</span>
          <span class="impact-tag ${depthClass}">${escapeHtml(depth || relation)}</span>
          ${score ? `<span class="impact-tag indirect">${(score * 100).toFixed(0)}%</span>` : ''}
          ${isDirty ? '<span class="impact-tag conditional">dirty</span>' : ''}
        </button>
      `;
    })
    .join('');

  return `
    <div class="impact-group ${expanded ? 'expanded' : ''}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">›</span>
        <strong>${escapeHtml(title)}</strong>
        <span>(${affectedSymbols.length})</span>
      </button>
      <div class="group-content" ${expanded ? '' : 'hidden'}>
        ${rows}
      </div>
    </div>
  `;
}

function lineFromSymbol(sym: Record<string, unknown>): number {
  const explicit = sym.line || sym.start_line || sym.lineno;
  if (typeof explicit === 'number' && Number.isFinite(explicit)) {
    return Math.max(1, explicit);
  }
  const range = sym.range;
  if (Array.isArray(range) && typeof range[0] === 'number') {
    return Math.max(1, range[0]);
  }
  return 1;
}

export function renderFilesGroup(filePaths: string[], expanded = false): string {
  const uniquePaths = Array.from(new Set(filePaths.filter(Boolean)));
  if (uniquePaths.length === 0) {
    return renderAffectsGroup([], 'Files', expanded);
  }

  const rows = uniquePaths
    .map(filePath => `
      <button
        type="button"
        class="impact-row impact-file-row"
        data-action="openFile"
        data-file-path="${escapeHtml(filePath)}"
        data-line="1"
        title="Open ${escapeHtml(filePath)}"
      >
        <span class="impact-chevron" aria-hidden="true">›</span>
        <span class="impact-symbol">File</span>
        <span class="impact-file">${escapeHtml(filePath)}</span>
        <span class="impact-tag indirect">related</span>
      </button>
    `)
    .join('');

  return `
    <div class="impact-group ${expanded ? 'expanded' : ''}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">›</span>
        <strong>Files</strong>
        <span>(${uniquePaths.length})</span>
      </button>
      <div class="group-content" ${expanded ? '' : 'hidden'}>
        ${rows}
      </div>
    </div>
  `;
}

export function renderActionButtonRow(): string {
  return `
    <div class="impact-actions">
      <button class="secondary-action" data-action="open-related-files">
        Open related files
      </button>
      <button class="secondary-action" data-action="ask-followup">
        Ask follow-up
      </button>
      <button class="secondary-action" data-action="create-refactor-plan">
        Create refactor plan
      </button>
    </div>
  `;
}
