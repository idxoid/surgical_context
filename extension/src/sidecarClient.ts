const BASE = 'http://localhost:8000';

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Sidecar ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

export interface OverlayResponse {
  file_path: string;
  symbols: string[];
}

export interface ContextSymbol {
  symbol: string;
  file_path: string;
  relation?: string;
  direction?: string;
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
      resolver_version?: string;
      stage_timings_ms?: Record<string, number>;
      token_counts?: Record<string, number>;
      model_route?: Record<string, unknown>;
      estimated_cost_usd?: number;
      cost_basis?: string;
    };
  };
  primary_source: ContextSymbol;
  graph_context: ContextSymbol[];
  documentation: ContextDoc[];
  budget?: Record<string, unknown>;
}

export interface AskResponse {
  symbol: string;
  answer: string;
  context: PromptContextPayload;
  trace_id?: string;
  model_route?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  workspace_id?: string;
}

export interface SearchResponse {
  results: Array<{ symbol: string; score: number; snippet: string }>;
}

export const SidecarClient = {

  async health(): Promise<boolean> {
    try {
      const res = await fetch(`${BASE}/health`);
      return res.ok;
    } catch {
      return false;
    }
  },

  overlay(file_path: string, content: string): Promise<OverlayResponse> {
    return post('/overlay', { file_path, content });
  },

  async deleteOverlay(file_path: string): Promise<void> {
    try {
      await fetch(`${BASE}/overlay?file_path=${encodeURIComponent(file_path)}`, { method: 'DELETE' });
    } catch {
      // silently ignore
    }
  },

  ask(symbol: string, question: string): Promise<AskResponse> {
    return post('/ask', { symbol, question });
  },

  index(project_path: string): Promise<{ status: string }> {
    return post('/index', { project_path });
  },

  indexDocs(docs_path: string): Promise<{ status: string }> {
    return post('/index/docs', { docs_path });
  },

  search(query: string, limit = 10): Promise<SearchResponse> {
    return post('/search', { query, limit });
  },
};
