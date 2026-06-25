import { buildContextSummary } from '../../contextSummary';
import {
  ContextSummaryDto,
  ImpactResponse,
  PromptContextPayload,
} from './protocol';
import { clampImpactDepth } from './impactLayout';

export function impactResponseFromPromptContext(
  context: PromptContextPayload,
): ImpactResponse {
  const affectedSymbols = context.graph_context.map(symbol => ({
    symbol: symbol.symbol,
    file_path: symbol.file_path,
    relation: symbol.relation,
    direction: symbol.direction,
    role: symbol.role,
    kind: symbol.kind,
    edge_type: symbol.edge_type,
    depth: symbol.depth,
    utility_score: symbol.utility_score,
    relevance_score: symbol.relevance_score,
    is_dirty: symbol.is_dirty,
  }));
  const affectedFiles = Array.from(new Set(
    [
      context.primary_source.file_path,
      ...context.graph_context.map(symbol => symbol.file_path),
      ...context.documentation.map(doc => doc.source_file),
    ].filter(Boolean)
  ));

  return {
    symbol: context.primary_source.symbol,
    symbol_uid: context.primary_source.symbol,
    file_path: context.primary_source.file_path,
    affected_symbols: affectedSymbols,
    affected_files: affectedFiles,
    affected_count: affectedSymbols.length,
    affected_file_count: affectedFiles.length,
    max_depth: affectedSymbols.reduce((max, symbol) => (
      typeof symbol.depth === 'number' ? Math.max(max, symbol.depth) : max
    ), 0),
  };
}

export function hydrateFromPromptContext(context: PromptContextPayload): {
  summary: ContextSummaryDto;
  impact: ImpactResponse;
  symbol: string;
  filePath: string;
  depth: number;
} {
  const impact = impactResponseFromPromptContext(context);
  return {
    summary: buildContextSummary(context),
    impact,
    symbol: context.primary_source.symbol,
    filePath: context.primary_source.file_path,
    depth: clampImpactDepth(impact.max_depth || 3),
  };
}
