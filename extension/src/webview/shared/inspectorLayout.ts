import { IntentMatch, PromptContextPayload } from './protocol';
import { escapeHtml } from './html';
import { renderInspectorSurfaceShell } from './surfaceChrome';

export { escapeHtml };

export type InspectorTab = 'primary' | 'intent' | 'graph' | 'docs' | 'tokens' | 'json' | 'api';

const INSPECTOR_TABS: Array<{ id: InspectorTab; label: string }> = [
  { id: 'primary', label: 'Primary' },
  { id: 'intent', label: 'Intent' },
  { id: 'graph', label: 'Graph' },
  { id: 'docs', label: 'Docs' },
  { id: 'tokens', label: 'Tokens' },
  { id: 'json', label: 'JSON' },
  { id: 'api', label: 'API' },
];

function renderTable(headers: string[], bodyRows: string, tableClass = ''): string {
  const classAttr = tableClass ? ` class="${tableClass}"` : '';
  const head = headers.map(header => `<th>${escapeHtml(header)}</th>`).join('');
  return `
    <table${classAttr}>
      <thead>
        <tr>${head}</tr>
      </thead>
      <tbody>
        ${bodyRows}
      </tbody>
    </table>
  `;
}

function renderJsonViewer(
  jsonStr: string,
  copyAction: string,
  infoHtml = '',
): string {
  return `
    <div class="json-viewer">
      ${infoHtml}
      <button class="copy-button" data-action="${copyAction}">Copy JSON</button>
      <pre><code>${escapeHtml(jsonStr)}</code></pre>
    </div>
  `;
}

export function renderIntentTab(matches: IntentMatch[] | null): string {
  if (matches === null) {
    return `<div class="inspector-tab-content"><p style="color:var(--vscode-descriptionForeground);">Classifying intent…</p></div>`;
  }
  if (matches.length === 0) {
    return `<div class="inspector-tab-content"><p style="color:var(--vscode-descriptionForeground);">No role matched above threshold for this question.</p></div>`;
  }
  const rows = matches
    .map(m => {
      const pct = Math.max(0, Math.min(100, Math.round(m.similarity * 100)));
      return `
        <div style="margin:0 0 12px;">
          <div style="display:flex;justify-content:space-between;align-items:baseline;">
            <span style="font-weight:600;">${escapeHtml(m.role)}</span>
            <span style="font-variant-numeric:tabular-nums;color:var(--vscode-descriptionForeground);">${m.similarity.toFixed(2)}</span>
          </div>
          <div style="height:6px;background:var(--vscode-editorWidget-border,#444);border-radius:3px;overflow:hidden;margin:3px 0 4px;">
            <div style="height:100%;width:${pct}%;background:var(--vscode-progressBar-background,#0a84ff);"></div>
          </div>
          <div style="font-size:12px;color:var(--vscode-descriptionForeground);">${escapeHtml(m.description)}</div>
        </div>
      `;
    })
    .join('');
  return `
    <div class="inspector-tab-content">
      <p style="color:var(--vscode-descriptionForeground);font-size:12px;margin:0 0 12px;">
        Role intent the retrieval classifier inferred from the question (embedding cosine vs role descriptions) — this drives which axes are searched.
      </p>
      ${rows}
    </div>
  `;
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
      ${renderTable(['Symbol', 'Relation', 'Depth', 'Score', 'Dirty', 'File'], rows)}
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
  return renderJsonViewer(JSON.stringify(context, null, 2), 'copy-json');
}

export function renderTokenBreakdownTab(context: PromptContextPayload): string {
  const metadata = context.metadata || {};

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
      ${renderTable(['Tier', 'Tokens', '% of Total'], rows, 'tier-table')}
    </div>
  `;
}

export function renderApiPayloadTab(context: PromptContextPayload): string {
  const primary = context.primary_source;
  const graphItems = context.graph_context || [];
  const docs = context.documentation || [];

  // Reconstruct the system prompt (as built by PromptCompiler)
  const systemPrompt = buildSystemPrompt(context);

  // Build the API request object
  const apiRequest = {
    model: 'claude-opus-4-7',
    max_tokens: 8096,
    system: systemPrompt,
    messages: [
      {
        role: 'user',
        content: '(User query would appear here)',
      },
    ],
  };

  // Also include metadata about the context assembly
  const metadata = {
    mode: context.mode,
    intent: context.intent,
    assembly_metadata: context.metadata?.assembly,
    tier_tokens: context.metadata?.tier_tokens,
    budget_info: context.budget,
  };

  const jsonStr = JSON.stringify(
    {
      api_request: apiRequest,
      context_metadata: metadata,
      assembly_summary: {
        primary_symbol: primary?.symbol,
        graph_context_count: graphItems.length,
        documentation_count: docs.length,
        total_tokens:
          (context.metadata?.tokens_primary || 0) +
          (context.metadata?.tokens_graph || 0) +
          (context.metadata?.tokens_docs || 0),
      },
    },
    null,
    2
  );

  return renderJsonViewer(
    jsonStr,
    'copy-api-json',
    `<div class="json-info">
        <p>This is the final JSON sent to the Claude API (system prompt + context).</p>
        <p>The <code>system</code> field contains the assembled surgical context.</p>
      </div>`,
  );
}

function buildSystemPrompt(context: PromptContextPayload): string {
  const primary = context.primary_source;
  const graphItems = context.graph_context || [];
  const docs = context.documentation || [];

  const blocks: string[] = [
    `--- TARGET SYMBOL: ${primary?.symbol || 'unknown'} ---`,
  ];

  if (primary?.code) {
    blocks.push(primary.code);
  }

  if (graphItems.length > 0) {
    blocks.push('\n--- DEPENDENCIES ---');
    for (const dep of graphItems) {
      blocks.push(`\n# From ${dep.symbol} [${dep.relation}]:`);
      if (dep.code) {
        blocks.push(dep.code);
      }
    }
  }

  if (docs.length > 0) {
    blocks.push('\n--- DOCUMENTATION ---');
    for (const doc of docs) {
      blocks.push(`[${doc.source_file}]\n${doc.content}`);
    }
  }

  return blocks.join('\n');
}

function renderInspectorTabButton(tab: InspectorTab, label: string, activeTab: InspectorTab): string {
  return `
    <button
      class="tab-button ${activeTab === tab ? 'active' : ''}"
      data-action="switchInspectorTab"
      data-inspector-tab="${tab}"
      role="tab"
      aria-selected="${activeTab === tab}"
    >
      ${label}
    </button>
  `;
}

export function renderInspectorTabContent(
  activeTab: InspectorTab,
  context: PromptContextPayload,
  intentMatches: IntentMatch[] | null,
): string {
  switch (activeTab) {
    case 'intent':
      return renderIntentTab(intentMatches);
    case 'graph':
      return renderGraphContextTab(context);
    case 'docs':
      return renderDocumentationTab(context);
    case 'tokens':
      return renderTokenBreakdownTab(context);
    case 'json':
      return renderPromptJsonTab(context);
    case 'api':
      return renderApiPayloadTab(context);
    case 'primary':
    default:
      return renderPrimarySourceTab(context);
  }
}

export function renderInspectorSurfaceView(
  chrome: string,
  context: PromptContextPayload | null,
  activeTab: InspectorTab,
  subtitle: string,
  intentMatches: IntentMatch[] | null,
): string {
  if (!context) {
    return renderInspectorSurfaceShell(
      chrome,
      `
        <div class="surface-title">Context Inspector</div>
        <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
        <div class="empty-state">
          No prompt context yet. Ask a question first, then come back here.
        </div>
        <button class="primary-action surface-inline-action" data-action="openChat">Open Chat</button>
      `,
    );
  }

  return renderInspectorSurfaceShell(
    chrome,
    `
      <div class="inspector-header">
        <h2>Context Inspector</h2>
        <div class="surface-subtitle">${escapeHtml(subtitle)}</div>
        <div class="inspector-tab-bar" role="tablist" aria-label="Context detail tabs">
          ${INSPECTOR_TABS.map(tab => renderInspectorTabButton(tab.id, tab.label, activeTab)).join('')}
        </div>
      </div>
      <div class="inspector-content">
        ${renderInspectorTabContent(activeTab, context, intentMatches)}
      </div>
    `,
  );
}
