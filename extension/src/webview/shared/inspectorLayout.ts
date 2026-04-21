import { PromptContextPayload } from '../../sidecarClient';

export function escapeHtml(text: string): string {
  const map: { [key: string]: string } = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  };
  return text.replace(/[&<>"']/g, m => map[m]);
}

export function renderPrimarySourceTab(context: PromptContextPayload): string {
  const primary = context.primary_source;
  if (!primary) {
    return '<div class="tab-content-empty">No primary source available</div>';
  }

  const symbolName = primary.symbol || 'unknown';
  const filePath = primary.file_path || 'unknown file';
  const isDirty = primary.is_dirty ? '🔴 Unsaved' : '✓ Saved';
  const code = primary.code || '';

  return `
    <div class="primary-source-card">
      <div class="symbol-header">
        <h3>${escapeHtml(symbolName)}</h3>
        <span class="dirty-badge">${isDirty}</span>
      </div>
      <div class="file-path">
        <strong>File:</strong> ${escapeHtml(filePath)}
      </div>
      ${code ? `
        <div class="code-snippet">
          <pre><code>${escapeHtml(code)}</code></pre>
        </div>
      ` : ''}
    </div>
  `;
}

export function renderGraphContextTab(context: PromptContextPayload): string {
  const graphItems = context.graph_context || [];

  if (graphItems.length === 0) {
    return '<div class="tab-content-empty">No graph context available</div>';
  }

  const rows = graphItems
    .map(item => `
      <tr class="context-row" data-file-path="${escapeHtml(item.file_path)}">
        <td class="symbol-col">${escapeHtml(item.symbol)}</td>
        <td class="relation-col">${escapeHtml(item.relation || '')}</td>
        <td class="depth-col">${item.depth || 0}</td>
        <td class="score-col">${(item.relevance_score || 0).toFixed(2)}</td>
        <td class="dirty-col">${item.is_dirty ? '🔴' : '✓'}</td>
        <td class="file-col">${escapeHtml(item.file_path)}</td>
      </tr>
    `)
    .join('');

  return `
    <div class="graph-context-table">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Relation</th>
            <th>Depth</th>
            <th>Score</th>
            <th>Dirty</th>
            <th>File</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </div>
  `;
}

export function renderDocumentationTab(context: PromptContextPayload): string {
  const docs = context.documentation || [];

  if (docs.length === 0) {
    return '<div class="tab-content-empty">No documentation available</div>';
  }

  const rows = docs
    .map(doc => `
      <div class="doc-item">
        <div class="doc-header">
          <strong>Source:</strong> ${escapeHtml(doc.source_file)}
          <span class="score">${(doc.score || 0).toFixed(2)}</span>
        </div>
        <div class="doc-content">
          ${escapeHtml((doc.content || '').substring(0, 500))}${(doc.content || '').length > 500 ? '...' : ''}
        </div>
      </div>
    `)
    .join('');

  return `
    <div class="documentation-list">
      ${rows}
    </div>
  `;
}

export function renderPromptJsonTab(context: PromptContextPayload): string {
  const jsonStr = JSON.stringify(context, null, 2);

  return `
    <div class="json-viewer">
      <button class="copy-button" data-action="copy-json">Copy JSON</button>
      <pre><code>${escapeHtml(jsonStr)}</code></pre>
    </div>
  `;
}

export function renderTokenBreakdownTab(context: PromptContextPayload): string {
  const metadata = context.metadata || {};
  const tiersUsed = metadata.tiers_used || [];

  const tokensPrimary = metadata.tokens_primary || 0;
  const tokensGraph = metadata.tokens_graph || 0;
  const tokensDocs = metadata.tokens_docs || 0;
  const tokensTotal = tokensPrimary + tokensGraph + tokensDocs;

  const estimatedFull = tokensTotal * 3;

  const rows = [
    { tier: 'Primary Code', tokens: tokensPrimary },
    { tier: 'Graph Context', tokens: tokensGraph },
    { tier: 'Documentation', tokens: tokensDocs },
  ]
    .filter(r => r.tokens > 0)
    .map(r => `
      <tr>
        <td>${escapeHtml(r.tier)}</td>
        <td>${r.tokens}</td>
        <td>${((r.tokens / tokensTotal) * 100).toFixed(1)}%</td>
      </tr>
    `)
    .join('');

  return `
    <div class="token-breakdown">
      <div class="summary-cards">
        <div class="summary-card">
          <div class="label">Surgical Total</div>
          <div class="value">${tokensTotal}</div>
        </div>
        <div class="summary-card">
          <div class="label">Est. Full-Open</div>
          <div class="value">${estimatedFull}</div>
        </div>
        <div class="summary-card">
          <div class="label">Savings</div>
          <div class="value">${((1 - tokensTotal / estimatedFull) * 100).toFixed(0)}%</div>
        </div>
      </div>
      <table class="tier-table">
        <thead>
          <tr>
            <th>Tier</th>
            <th>Tokens</th>
            <th>% of Total</th>
          </tr>
        </thead>
        <tbody>
          ${rows}
        </tbody>
      </table>
    </div>
  `;
}
