import {
  HistoryAskRecordRequest,
  PromptContextPayload,
  hashHistoryText,
  safeAnswerSummary,
  safePromptSummary,
} from './sidecarClient';

export interface AskHistoryInput {
  conversationId?: string;
  requestId: string;
  prompt: string;
  answer: string;
  symbol?: string;
  activeFile?: string;
  traceId?: string;
  context?: PromptContextPayload | null;
}

export function buildAskHistoryRecord(input: AskHistoryInput): HistoryAskRecordRequest {
  const assembly = input.context?.metadata?.assembly;
  const traceId = input.traceId || assembly?.trace_id || '';
  const feedbackToken = typeof assembly?.feedback_token === 'string'
    ? assembly.feedback_token
    : '';
  const symbol = input.context?.primary_source?.symbol || input.symbol || '';

  return {
    conversation_id: input.conversationId || null,
    request_id: input.requestId,
    prompt_summary: safePromptSummary(symbol, input.activeFile),
    prompt_hash: hashHistoryText(input.prompt),
    answer_summary: safeAnswerSummary(input.answer),
    answer_hash: hashHistoryText(input.answer),
    symbol,
    trace_id: traceId,
    feedback_token: feedbackToken,
    ask_snapshot: {
      request_id: input.requestId,
      trace_id: traceId,
      feedback_token: feedbackToken,
      symbol,
      intent: input.context?.intent || '',
      mode: input.context?.mode || '',
      context: input.context || null,
      model_route: assembly?.model_route || {},
      token_counts: assembly?.token_counts || {},
      estimated_cost_usd: assembly?.estimated_cost_usd ?? null,
    },
    inspector_snapshot: input.context
      ? {
          request_id: input.requestId,
          trace_id: traceId,
          feedback_token: feedbackToken,
          symbol,
          context: input.context,
        }
      : {},
    impact_snapshot: input.context
      ? {
          request_id: input.requestId,
          trace_id: traceId,
          feedback_token: feedbackToken,
          symbol,
          primary_source: input.context.primary_source,
          affected_symbols: input.context.graph_context,
          affected_files: affectedFilesFromContext(input.context),
        }
      : {},
    metadata: {
      source: 'vscode',
      has_context: Boolean(input.context),
    },
  };
}

function affectedFilesFromContext(context: PromptContextPayload): string[] {
  return Array.from(
    new Set(
      [
        context.primary_source?.file_path,
        ...context.graph_context.map(symbol => symbol.file_path),
        ...context.documentation.map(doc => doc.source_file),
      ].filter((filePath): filePath is string => Boolean(filePath))
    )
  );
}
