import type { ImpactResponse } from './protocol';
import { escapeHtml } from './html';

export { escapeHtml };

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

interface ImpactExplanation {
  summary: string;
  path: string;
  risk: string;
  evidence: string[];
  caveat?: string;
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
      ${renderImpactZone('Direct Impact', model.direct, 'No direct callers or first-hop consumers returned.', true, symbol)}
      ${renderImpactZone('Architectural Reach', model.reach, 'No hook, event, config, data, or API reach returned.', true, symbol)}
      ${renderImpactZone('Hidden Risks', model.risks, 'No cross-repo or coverage risks returned.', model.risks.length > 0, symbol)}
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

export function clampImpactDepth(depth: number, minDepth = 1, maxDepth = 4): number {
  if (!Number.isFinite(depth)) return 3;
  return Math.max(minDepth, Math.min(maxDepth, Math.round(depth)));
}

function clampDepth(depth: number, options: ImpactWorkspaceOptions): number {
  return clampImpactDepth(depth, options.minDepth ?? 1, options.maxDepth ?? 4);
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
  const explicitSeverity = stringField(sym, 'severity');
  const explicitZone = stringField(sym, 'zone');
  const severity = isSeverity(explicitSeverity)
    ? explicitSeverity
    : classifySeverity(category, depth, filePath);
  const zone = isImpactZone(explicitZone)
    ? explicitZone
    : classifyZone(category, depth, filePath);
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

function isSeverity(value: string): value is Severity {
  return value === 'high' || value === 'medium' || value === 'low';
}

function isImpactZone(value: string): value is ImpactZone {
  return value === 'direct' || value === 'reach' || value === 'risk';
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
  let base: number;
  if (severity === 'high') {
    base = 0.88;
  } else if (severity === 'medium') {
    base = 0.66;
  } else {
    base = 0.42;
  }

  let categoryBoost = 0;
  if (category === 'api' || category === 'data') {
    categoryBoost = 0.08;
  } else if (category === 'event') {
    categoryBoost = 0.05;
  }

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
  expanded: boolean,
  targetSymbol: string
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
        ${visible.map(item => renderImpactItemRow(item, targetSymbol)).join('')}
        ${overflow.length ? renderOverflowRows(overflow, targetSymbol) : ''}
      </div>
    </div>
  `;
}

function renderOverflowRows(items: ImpactItem[], targetSymbol: string): string {
  return `
    <div class="impact-overflow" hidden>
      ${items.map(item => renderImpactItemRow(item, targetSymbol)).join('')}
    </div>
    <button class="impact-show-more" data-action="showMoreImpact">
      Show ${items.length} more
    </button>
  `;
}

function renderImpactItemRow(item: ImpactItem, targetSymbol: string): string {
  const disabled = item.synthetic ? 'disabled' : '';
  const title = item.synthetic ? item.symbolName : `Open ${item.symbolName}`;
  const explanation = explainImpactItem(item, targetSymbol);
  return `
    <div class="impact-item ${item.synthetic ? 'impact-risk-item' : ''}">
      <div class="impact-item-line">
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
        <button
          type="button"
          class="impact-explain-button"
          data-action="explainImpact"
          aria-expanded="false"
          title="Explain how this item is connected to ${escapeHtml(targetSymbol)}"
        >Explain</button>
      </div>
      ${renderImpactExplanation(explanation)}
    </div>
  `;
}

const DEFAULT_CALLS_EDGE_LABEL = 'CALLS_*';

interface ImpactExplainContext {
  item: ImpactItem;
  targetSymbol: string;
  edge: string;
  depth: number;
}

function impactHopSuffix(depth: number): string {
  return depth > 1 ? ` × ${depth}` : '';
}

function impactHopsLabel(depth: number): string {
  return `${depth} hop${depth === 1 ? '' : 's'}`;
}

function explainCoverageGap(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  return {
    summary: `No test symbols or test files were returned with the impact surface for ${ctx.targetSymbol}.`,
    path: `${ctx.targetSymbol} → no returned test coverage`,
  };
}

function explainReverseCalls(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: depth <= 1
      ? `${item.symbolName} calls or directly consumes ${targetSymbol}.`
      : `${item.symbolName} reaches ${targetSymbol} through ${depth} reverse call hops.`,
    path: `${item.symbolName} —${edge || DEFAULT_CALLS_EDGE_LABEL}${impactHopSuffix(depth)}→ ${targetSymbol}`,
  };
}

function explainForwardCalls(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${targetSymbol} calls or dispatches into ${item.symbolName}, so behavior can propagate forward.`,
    path: `${targetSymbol} —${edge || DEFAULT_CALLS_EDGE_LABEL}${impactHopSuffix(depth)}→ ${item.symbolName}`,
  };
}

function explainImpactedTests(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, depth } = ctx;
  return {
    summary: `${item.symbolName} exercises ${targetSymbol} or its downstream call spine.`,
    path: `${item.symbolName} —test call path, ${impactHopsLabel(depth)}→ ${targetSymbol}`,
  };
}

function explainStructuralInheritor(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${item.symbolName} inherits an API or structural contract connected to ${targetSymbol}.`,
    path: `${item.symbolName} —${edge || 'INHERITED_API'}${impactHopSuffix(depth)}→ ${targetSymbol}`,
  };
}

function explainStructuralApiCarrier(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${targetSymbol} carries or exposes the API surface ${item.symbolName}.`,
    path: `${targetSymbol} —${edge || 'HAS_API'}${impactHopSuffix(depth)}→ ${item.symbolName}`,
  };
}

function explainForwardAffects(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${item.symbolName} is in the precomputed downstream impact closure of ${targetSymbol}.`,
    path: `${targetSymbol} —${edge || 'AFFECTS'}${impactHopSuffix(depth)}→ ${item.symbolName}`,
  };
}

function explainDefaultImpact(ctx: ImpactExplainContext): Pick<ImpactExplanation, 'summary' | 'path'> {
  const { item, targetSymbol, edge, depth } = ctx;
  return {
    summary: `${item.symbolName} was reached from ${targetSymbol} by the impact graph walk.`,
    path: `${targetSymbol} —${edge || item.relation}, ${impactHopsLabel(depth)}→ ${item.symbolName}`,
  };
}

const IMPACT_KIND_EXPLAINERS: Record<
  string,
  (ctx: ImpactExplainContext) => Pick<ImpactExplanation, 'summary' | 'path'>
> = {
  coverage_gap: explainCoverageGap,
  reverse_calls: explainReverseCalls,
  overlay_caller: explainReverseCalls,
  forward_calls: explainForwardCalls,
  impacted_tests: explainImpactedTests,
  structural_inheritor: explainStructuralInheritor,
  structural_api_carrier: explainStructuralApiCarrier,
  forward_affects: explainForwardAffects,
};

function buildImpactEvidence(
  item: ImpactItem,
  options: {
    edge: string;
    kind: string;
    role: string;
    depth: number;
    degraded: boolean;
    provenance: string[];
  },
): string[] {
  const { edge, kind, role, depth, degraded, provenance } = options;
  return [
    edge ? `edge ${edge}` : '',
    kind ? `walk ${kind}` : '',
    role ? `role ${role}` : '',
    `depth ${depth}`,
    `priority ${Math.round(item.utilityScore * 100)}%`,
    degraded ? 'unsaved editor overlay' : 'impact response',
    ...provenance.map(value => `provenance ${value}`),
  ].filter(Boolean);
}

function explainImpactCaveat(
  item: ImpactItem,
  options: { depth: number; degraded: boolean },
): string | undefined {
  const { depth, degraded } = options;
  if (item.synthetic) {
    return 'This warning is inferred from missing returned evidence; it does not prove that coverage is absent.';
  }
  if (degraded) {
    return 'This connection comes from unsaved buffers and is name-based, so the impact surface is partial.';
  }
  if (depth > 1) {
    return 'The response identifies the traversal and hop count, but does not include every intermediate symbol.';
  }
  return undefined;
}

function explainImpactItem(item: ImpactItem, targetSymbol: string): ImpactExplanation {
  const kind = stringField(item.source, 'kind') || arrayField(item.source, 'satisfying_kinds')[0] || item.relation || item.category;
  const edge = stringField(item.source, 'edge_type', 'relation') || item.relation;
  const role = stringField(item.source, 'role');
  const provenance = arrayField(item.source, 'provenance');
  const depth = item.depth ?? 1;
  const degraded = item.source.degraded === true;
  const ctx: ImpactExplainContext = { item, targetSymbol, edge, depth };
  const explainer = IMPACT_KIND_EXPLAINERS[kind] ?? explainDefaultImpact;
  const { summary, path } = explainer(ctx);

  return {
    summary,
    path,
    risk: explainRisk(item),
    evidence: buildImpactEvidence(item, {
      edge,
      kind,
      role,
      depth,
      degraded,
      provenance,
    }),
    caveat: explainImpactCaveat(item, { depth, degraded }),
  };
}

function explainRisk(item: ImpactItem): string {
  if (item.synthetic) {
    return 'A change may ship without a directly identified regression test.';
  }
  if (item.category === 'test') {
    return 'The test may fail or need updated expectations when the target contract changes.';
  }
  if (item.category === 'cross_repo') {
    return 'The dependency crosses a service, package, or repository boundary where coordinated changes are easier to miss.';
  }
  if (isDocFile(item.filePath)) {
    return 'Documentation can become stale even when the code continues to compile.';
  }
  if (item.category === 'api' || item.category === 'data') {
    return 'This is a contract boundary; signature or schema changes can affect consumers that are not obvious at the call site.';
  }
  if (item.category === 'event' || item.category === 'config') {
    return 'This connection is indirect or conditional, so it may only surface for particular runtime paths or settings.';
  }
  return item.depth !== undefined && item.depth > 1
    ? 'The dependency is indirect; failures can surface away from the edited method.'
    : 'This is a direct consumer and may break when the target behavior or signature changes.';
}

function renderImpactExplanation(explanation: ImpactExplanation): string {
  return `
    <div class="impact-explanation" hidden>
      <p class="impact-explanation-summary">${escapeHtml(explanation.summary)}</p>
      <div class="impact-explanation-path">
        <span>Connection</span>
        <code>${escapeHtml(explanation.path)}</code>
      </div>
      <div class="impact-explanation-risk">
        <span>Why it matters</span>
        <p>${escapeHtml(explanation.risk)}</p>
      </div>
      <div class="impact-explanation-evidence" aria-label="Connection evidence">
        ${explanation.evidence.map(value => `<span>${escapeHtml(value)}</span>`).join('')}
      </div>
      ${explanation.caveat ? `<p class="impact-explanation-caveat">${escapeHtml(explanation.caveat)}</p>` : ''}
    </div>
  `;
}

function renderCollapsibleImpactGroup(
  title: string,
  count: number,
  rows: string,
  expanded: boolean,
  emptyMessage: string,
): string {
  if (count === 0) {
    return `
      <div class="impact-group">
        <div class="group-header">${escapeHtml(title)}</div>
        <div class="group-content empty">
          ${escapeHtml(emptyMessage)}
        </div>
      </div>
    `;
  }

  return `
    <div class="impact-group ${expanded ? 'expanded' : ''}">
      <button class="impact-group-header" data-action="noop" aria-expanded="${expanded}">
        <span aria-hidden="true">›</span>
        <strong>${escapeHtml(title)}</strong>
        <span>(${count})</span>
      </button>
      <div class="group-content" ${expanded ? '' : 'hidden'}>
        ${rows}
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
    return renderCollapsibleImpactGroup(title, 0, '', expanded, 'No related symbols found.');
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

  return renderCollapsibleImpactGroup(title, affectedSymbols.length, rows, expanded, '');
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
  return value.map(String).filter(Boolean);
}

function isTestFile(filePath: string): boolean {
  const path = filePath.toLowerCase();
  const testDirectorySegment = /(^|[/.])(tests?|specs?|__tests__)([/.]|$)/;
  const jsTsTestSuffix = /[._](test|spec)\.[jt]sx?$/;
  return (
    testDirectorySegment.test(path)
    || jsTsTestSuffix.test(path)
    || (path.endsWith('.py') && path.includes('test_'))
    || path.endsWith('_test.py')
  );
}

function isDocFile(filePath: string): boolean {
  return /\.(md|mdx|rst|txt)$/i.test(filePath);
}

export function renderFilesGroup(filePaths: string[], expanded = false, title = 'Files'): string {
  const uniquePaths = Array.from(new Set(filePaths.filter(Boolean)));
  if (uniquePaths.length === 0) {
    return renderCollapsibleImpactGroup(title, 0, '', expanded, 'No related symbols found.');
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

  return renderCollapsibleImpactGroup(title, uniquePaths.length, rows, expanded, '');
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
