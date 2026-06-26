/** Prompt context payload types shared by the extension host and webview protocol. */

export interface ContextSymbol {
  symbol: string;
  file_path: string;
  relation?: string;
  direction?: string;
  role?: string;
  kind?: string;
  edge_type?: string;
  utility_score?: number;
  depth?: number;
  relevance_score?: number;
  scores?: Record<string, number | null>;
  provenance?: string[];
  is_dirty?: boolean;
  code?: string;
}

export interface ContextDoc {
  chunk_id: string;
  source_file: string;
  content: string;
  score?: number | null;
  scores?: Record<string, number | null>;
  provenance?: string[];
}

export interface PromptContextPayload {
  mode: string;
  intent: string;
  metadata: {
    tiers_used?: string[];
    tier_tokens?: Record<string, number>;
    tokens_primary?: number;
    tokens_graph?: number;
    tokens_docs?: number;
    pruning_reasons?: string[];
    assembly?: {
      trace_id?: string;
      workspace_id?: string;
      context_pipeline_version?: string;
      stage_timings_ms?: Record<string, number>;
      token_counts?: Record<string, number>;
      model_route?: Record<string, unknown>;
      estimated_cost_usd?: number;
      cost_basis?: string;
      feedback_token?: string;
    };
  };
  primary_source: ContextSymbol;
  graph_context: ContextSymbol[];
  documentation: ContextDoc[];
  budget?: Record<string, unknown>;
}
