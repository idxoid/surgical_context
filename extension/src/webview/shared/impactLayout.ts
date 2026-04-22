
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
    </div>
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

      return `
        <div class="impact-row" data-file-path="${escapeHtml(filePath)}">
          <span class="impact-chevron" aria-hidden="true">›</span>
          <span class="impact-symbol">${escapeHtml(symbolName)}</span>
          <span class="impact-file">${escapeHtml(filePath)}</span>
          <span class="impact-tag direct">${escapeHtml(depth || relation)}</span>
          ${score ? `<span class="impact-tag indirect">${(score * 100).toFixed(0)}%</span>` : ''}
          ${isDirty ? '<span class="impact-tag conditional">dirty</span>' : ''}
        </div>
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

export function renderFilesGroup(filePaths: string[], expanded = false): string {
  const uniquePaths = Array.from(new Set(filePaths.filter(Boolean)));
  if (uniquePaths.length === 0) {
    return renderAffectsGroup([], 'Files', expanded);
  }

  const rows = uniquePaths
    .map(filePath => `
      <div class="impact-row" data-file-path="${escapeHtml(filePath)}">
        <span class="impact-chevron" aria-hidden="true">›</span>
        <span class="impact-symbol">File</span>
        <span class="impact-file">${escapeHtml(filePath)}</span>
        <span class="impact-tag indirect">related</span>
      </div>
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

export function renderPlaceholderGroup(
  title: string,
  message: string,
  count?: number,
  expanded = false
): string {
  return `
    <div class="impact-group ${expanded ? 'expanded' : ''}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">›</span>
        <strong>${escapeHtml(title)}</strong>
        ${count !== undefined ? `<span>(${count})</span>` : ''}
      </button>
      <div class="group-content placeholder" ${expanded ? '' : 'hidden'}>
        <p>${escapeHtml(message)}</p>
        ${
          expanded
            ? `
              <div class="impact-row static">
                <span class="impact-chevron" aria-hidden="true">›</span>
                <span class="impact-symbol">SymbolResolver.resolve()</span>
                <span class="impact-file">packages/core/src/symbolResolver.ts:87</span>
                <span class="impact-tag direct">direct</span>
              </div>
              <div class="impact-row static">
                <span class="impact-chevron" aria-hidden="true">›</span>
                <span class="impact-symbol">Graph.getNeighbors()</span>
                <span class="impact-file">packages/core/src/graphBuilder.ts:142</span>
                <span class="impact-tag direct">direct</span>
              </div>
            `
            : ''
        }
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
