import type { ImpactResponse } from './protocol';

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

type Severity = 'high' | 'medium' | 'low';
type ImpactZone = 'direct' | 'reach' | 'risk';

interface ImpactItem {
  source: Record<string, unknown>;
  symbolName: string;
  filePath: string;
  relation: string;
  category: string;
  zone: ImpactZone;
  severity: Severity;
  utilityScore: number;
  depth?: number;
  line: number;
  synthetic?: boolean;
}

interface ImpactModel {
  items: ImpactItem[];
  direct: ImpactItem[];
  reach: ImpactItem[];
  risks: ImpactItem[];
  summary: {
    endpoints: number;
    hooks: number;
    tests: number;
    high: number;
    medium: number;
    low: number;
    files: number;
  };
}

interface ImpactWorkspaceOptions {
  depth?: number;
  minDepth?: number;
  maxDepth?: number;
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

export function renderImpactWorkspace(
  impact: ImpactResponse,
  symbol: string,
  sourceLabel = 'live graph',
  options: ImpactWorkspaceOptions = {}
): string {
  const model = buildImpactModel(impact);
  const depth = clampDepth(options.depth ?? impact.max_depth ?? 3, options);
  return `
    ${renderSymbolSummaryCard({
      symbol,
      filePath: impact.file_path || 'unknown',
      uid: impact.symbol_uid || symbol,
      affectedCount: impact.affected_count || impact.affected_symbols?.length || 0,
      fileCount: impact.affected_file_count || impact.affected_files?.length || 0,
      maxDepth: impact.max_depth || 0,
      sourceLabel,
    })}
    ${renderImpactDepthControl(depth, options)}
    ${renderImpactSummary(model)}
    ${renderFocusGraph(symbol, model.items)}
    ${renderActionButtonRow()}
    <div class="impact-groups">
      ${renderImpactZone('Direct Impact', model.direct, 'No direct callers or first-hop consumers returned.', true)}
      ${renderImpactZone('Architectural Reach', model.reach, 'No hook, event, config, data, or API reach returned.', true)}
      ${renderImpactZone('Hidden Risks', model.risks, 'No cross-repo or coverage risks returned.', model.risks.length > 0)}
      ${renderFilesGroup(impact.affected_files || [], false, 'Dependencies')}
    </div>
    <div class="impact-legend">
      <span><span class="legend-dot high"></span> high</span>
      <span><span class="legend-dot medium"></span> medium</span>
      <span><span class="legend-dot low"></span> low</span>
      <span><span class="legend-dot type"></span> focus walk</span>
    </div>
  `;
}

function renderImpactDepthControl(depth: number, options: ImpactWorkspaceOptions): string {
  const minDepth = options.minDepth ?? 1;
  const maxDepth = options.maxDepth ?? 4;
  return `
    <div class="impact-depth-control">
      <label for="impact-depth-slider">Depth</label>
      <input
        id="impact-depth-slider"
        type="range"
        min="${minDepth}"
        max="${maxDepth}"
        step="1"
        value="${depth}"
        data-impact-depth
        aria-label="Impact depth"
      />
      <output for="impact-depth-slider">d${depth}</output>
    </div>
  `;
}

function clampDepth(depth: number, options: ImpactWorkspaceOptions): number {
  const minDepth = options.minDepth ?? 1;
  const maxDepth = options.maxDepth ?? 4;
  if (!Number.isFinite(depth)) return 3;
  return Math.max(minDepth, Math.min(maxDepth, Math.round(depth)));
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

function buildImpactModel(impact: ImpactResponse): ImpactModel {
  const symbols = impact.affected_symbols || [];
  const items = symbols.map(toImpactItem);
  const affectedFiles = Array.from(new Set((impact.affected_files || []).filter(Boolean)));
  const sourceFile = impact.file_path || '';
  const hasTests = [...affectedFiles, ...items.map(item => item.filePath), sourceFile].some(isTestFile);

  if (!hasTests && (items.length > 0 || affectedFiles.length > 0)) {
    items.push({
      source: {},
      symbolName: 'No returned test coverage',
      filePath: sourceFile || 'workspace',
      relation: 'coverage_gap',
      category: 'coverage',
      zone: 'risk',
      severity: 'high',
      utilityScore: 0.93,
      line: 1,
      synthetic: true,
    });
  }

  items.sort((a, b) => b.utilityScore - a.utilityScore);

  const direct = items.filter(item => item.zone === 'direct');
  const reach = items.filter(item => item.zone === 'reach');
  const risks = items.filter(item => item.zone === 'risk');

  return {
    items,
    direct,
    reach,
    risks,
    summary: {
      endpoints: items.filter(item => item.category === 'api').length,
      hooks: items.filter(item => item.category === 'event').length,
      tests: items.filter(item => item.category === 'test').length,
      high: items.filter(item => item.severity === 'high').length,
      medium: items.filter(item => item.severity === 'medium').length,
      low: items.filter(item => item.severity === 'low').length,
      files: affectedFiles.length,
    },
  };
}

function toImpactItem(sym: Record<string, unknown>): ImpactItem {
  const filePath = stringField(sym, 'file_path', 'path', 'source_file') || 'unknown';
  const symbolName = stringField(sym, 'symbol', 'name', 'title') || 'unknown';
  const relation = stringField(sym, 'relation', 'direction', 'edge_type', 'kind', 'role') || 'affected';
  const depth = numberField(sym, 'depth', 'distance', 'hops');
  const rawScore = numberField(sym, 'utility_score', 'relevance_score', 'score');
  const category = classifyCategory(sym, filePath, relation);
  const severity = classifySeverity(category, depth, filePath);
  const zone = classifyZone(category, depth, filePath);
  return {
    source: sym,
    symbolName,
    filePath,
    relation,
    category,
    zone,
    severity,
    utilityScore: rawScore ?? fallbackUtility(severity, category, depth),
    depth,
    line: lineFromSymbol(sym),
  };
}

function classifyCategory(
  sym: Record<string, unknown>,
  filePath: string,
  relation: string
): string {
  const text = [
    relation,
    stringField(sym, 'role', 'edge_role', 'edge_kind', 'kind', 'type'),
    arrayField(sym, 'provenance').join(' '),
    filePath,
  ].join(' ').toLowerCase();

  if (/\b(test|spec|fixture)\b|(^|[/.])(tests?|specs?)([/.]|$)/.test(text)) return 'test';
  if (/\b(hook|hook_exec|event|event_pub|listener|subscriber|signal)\b/.test(text)) return 'event';
  if (/\b(config|setting|settings|env|option|feature_flag)\b/.test(text)) return 'config';
  if (/\b(model|schema|serializer|pydantic|sqlalchemy|orm|migration)\b/.test(text)) return 'data';
  if (/\b(api|endpoint|route|router|controller|view)\b/.test(text)) return 'api';
  if (/\b(repo|workspace|service|package|contract)\b/.test(text)) return 'cross_repo';
  return 'caller';
}

function classifySeverity(category: string, depth: number | undefined, filePath: string): Severity {
  if (category === 'test' || isDocFile(filePath)) return 'low';
  if (category === 'event' || category === 'config') return 'medium';
  if (category === 'api' || category === 'data' || category === 'cross_repo') return 'high';
  return depth === undefined || depth <= 1 ? 'high' : 'medium';
}

function classifyZone(category: string, depth: number | undefined, filePath: string): ImpactZone {
  if (category === 'test' || category === 'cross_repo' || isDocFile(filePath)) return 'risk';
  if (category === 'event' || category === 'config' || category === 'data' || category === 'api') {
    return 'reach';
  }
  return depth === undefined || depth <= 1 ? 'direct' : 'reach';
}

function fallbackUtility(severity: Severity, category: string, depth: number | undefined): number {
  const base = severity === 'high' ? 0.88 : severity === 'medium' ? 0.66 : 0.42;
  const categoryBoost = category === 'api' || category === 'data' ? 0.08 : category === 'event' ? 0.05 : 0;
  const depthPenalty = typeof depth === 'number' ? Math.min(depth, 4) * 0.04 : 0;
  return Math.max(0.15, Math.min(0.99, base + categoryBoost - depthPenalty));
}

function renderImpactSummary(model: ImpactModel): string {
  return `
    <div class="impact-risk-summary" aria-label="Impact summary">
      <div class="impact-risk-title">
        <strong>Change touches ${model.summary.endpoints} endpoints, ${model.summary.hooks} hooks, ${model.summary.tests} tests</strong>
        <span>${model.summary.high} high / ${model.summary.medium} medium / ${model.summary.low} low</span>
      </div>
      <div class="impact-severity-strip">
        ${renderSeverityChip('High', model.summary.high, 'high')}
        ${renderSeverityChip('Medium', model.summary.medium, 'medium')}
        ${renderSeverityChip('Low', model.summary.low, 'low')}
        ${renderSeverityChip('Files', model.summary.files, 'neutral')}
      </div>
    </div>
  `;
}

function renderSeverityChip(label: string, count: number, tone: string): string {
  return `
    <span class="impact-severity-chip ${escapeHtml(tone)}">
      <strong>${count}</strong>
      <span>${escapeHtml(label)}</span>
    </span>
  `;
}

function renderFocusGraph(symbol: string, items: ImpactItem[]): string {
  const focusItems = items.filter(item => !item.synthetic).slice(0, 6);
  if (focusItems.length === 0) {
    return `
      <div class="impact-focus-card">
        <div class="impact-focus-center">${escapeHtml(symbol)}</div>
        <div class="impact-focus-empty">No high-utility neighbours returned.</div>
      </div>
    `;
  }

  return `
    <div class="impact-focus-card">
      <div class="impact-focus-center" title="${escapeHtml(symbol)}">${escapeHtml(symbol)}</div>
      <div class="impact-focus-grid">
        ${focusItems.map(renderFocusNode).join('')}
      </div>
    </div>
  `;
}

function renderFocusNode(item: ImpactItem): string {
  return `
    <button
      type="button"
      class="impact-focus-node ${item.severity}"
      data-action="openFile"
      data-file-path="${escapeHtml(item.filePath)}"
      data-line="${item.line}"
      title="Open ${escapeHtml(item.symbolName)}"
    >
      <span>${escapeHtml(item.symbolName)}</span>
      <small>${Math.round(item.utilityScore * 100)}%</small>
    </button>
  `;
}

function renderImpactZone(
  title: string,
  items: ImpactItem[],
  emptyText: string,
  expanded: boolean
): string {
  const visible = items.slice(0, 6);
  const overflow = items.slice(6);
  if (items.length === 0) {
    return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml(title)}</div>
        <div class="group-content empty">${escapeHtml(emptyText)}</div>
      </div>
    `;
  }

  return `
    <div class="impact-group ${expanded ? 'expanded' : ''}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">›</span>
        <strong>${escapeHtml(title)}</strong>
        <span>(${items.length})</span>
      </button>
      <div class="group-content" ${expanded ? '' : 'hidden'}>
        ${visible.map(renderImpactItemRow).join('')}
        ${overflow.length ? renderOverflowRows(overflow) : ''}
      </div>
    </div>
  `;
}

function renderOverflowRows(items: ImpactItem[]): string {
  return `
    <div class="impact-overflow" hidden>
      ${items.map(renderImpactItemRow).join('')}
    </div>
    <button class="impact-show-more" data-action="showMoreImpact">
      Show ${items.length} more
    </button>
  `;
}

function renderImpactItemRow(item: ImpactItem): string {
  const disabled = item.synthetic ? 'disabled' : '';
  const title = item.synthetic ? item.symbolName : `Open ${item.symbolName}`;
  return `
    <button
      type="button"
      class="impact-row ${item.synthetic ? 'impact-risk-row' : ''}"
      data-action="${item.synthetic ? 'noop' : 'openFile'}"
      data-file-path="${escapeHtml(item.filePath)}"
      data-line="${item.line}"
      title="${escapeHtml(title)}"
      ${disabled}
    >
      <span class="impact-chevron" aria-hidden="true">›</span>
      <span class="impact-symbol">${escapeHtml(item.symbolName)}</span>
      <span class="impact-file">${escapeHtml(item.filePath)}</span>
      <span class="impact-tag ${item.severity}">${escapeHtml(item.severity)}</span>
      <span class="impact-tag indirect">${Math.round(item.utilityScore * 100)}%</span>
      <span class="impact-tag ${item.category === 'event' || item.category === 'config' ? 'conditional' : 'direct'}">${escapeHtml(item.category)}</span>
    </button>
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

function stringField(sym: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = sym[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function numberField(sym: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const value = sym[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return undefined;
}

function arrayField(sym: Record<string, unknown>, key: string): string[] {
  const value = sym[key];
  if (!Array.isArray(value)) return [];
  return value.map(item => String(item)).filter(Boolean);
}

function isTestFile(filePath: string): boolean {
  return /(^|[/.])(tests?|specs?|__tests__)([/.]|$)|(\.|_)(test|spec)\.[jt]sx?$|test_.*\.py$|_test\.py$/.test(filePath.toLowerCase());
}

function isDocFile(filePath: string): boolean {
  return /\.(md|mdx|rst|txt)$/i.test(filePath);
}

export function renderFilesGroup(filePaths: string[], expanded = false, title = 'Files'): string {
  const uniquePaths = Array.from(new Set(filePaths.filter(Boolean)));
  if (uniquePaths.length === 0) {
    return renderAffectsGroup([], title, expanded);
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
        <strong>${escapeHtml(title)}</strong>
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
