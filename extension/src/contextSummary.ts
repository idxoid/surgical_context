import type {
  ContextSummaryDto,
  PromptContextPayload,
} from './webview/shared/protocol';

export function buildContextSummary(context: PromptContextPayload): ContextSummaryDto {
  const tierTokens = context.metadata.tier_tokens || {};
  const totalTokens = Object.values(tierTokens).reduce((sum, value) => {
    return sum + (typeof value === 'number' ? value : 0);
  }, 0);
  const askLevel = typeof context.budget?.ask_level === 'string'
    ? context.budget.ask_level
    : '';
  const warningChips = fallbackWarningChips(context);

  return {
    primaryLabel: `${context.primary_source.symbol} in ${context.primary_source.file_path}`,
    graphCount: context.graph_context.length,
    docsCount: context.documentation.length,
    tokenText: `${totalTokens} tokens`,
    chips: [
      ...(askLevel ? [`level:${askLevel}`] : []),
      ...warningChips,
      ...(context.metadata.tiers_used || []),
    ],
  };
}

function fallbackWarningChips(context: PromptContextPayload): string[] {
  const budget = context.budget || {};
  if (budget.fallback_reason !== 'symbol_not_found' || typeof budget.ask_level !== 'string') {
    return [];
  }

  return ['warning:symbol not found', `fallback:${budget.ask_level}`];
}
