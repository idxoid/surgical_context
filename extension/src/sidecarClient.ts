import * as vscode from 'vscode';
import { SSECallbacks, parseSSEStream } from './utils';

function getBaseUrl(): string {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  return config.get<string>('backendUrl', 'http://localhost:8000');
}

function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const config = vscode.workspace.getConfiguration('surgicalContext');
  const workspaceId = config.get<string>('workspaceId', 'local/default@main');
  if (workspaceId) {
    headers['X-Workspace'] = workspaceId;
  }
  return headers;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${getBaseUrl()}${path}`, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Sidecar ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${getBaseUrl()}${path}`, {
    method: 'GET',
    headers: getHeaders(),
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

export interface UnifiedSearchResponse {
  trace_id: string;
  workspace_id: string;
  results: Array<{
    type: 'doc' | 'symbol';
    title: string;
    file_path: string;
    content: string;
    score: number;
    scores: Record<string, number | null>;
    provenance: string[];
    metadata: Record<string, unknown>;
  }>;
  total: number;
}

export interface ImpactResponse {
  symbol: string;
  symbol_uid: string;
  file_path: string;
  affected_symbols: Array<Record<string, unknown>>;
  affected_files: string[];
  affected_count: number;
  affected_file_count: number;
  max_depth: number;
}

export interface CloudStatusResponse {
  cloud_enabled: boolean;
  using_aura: boolean;
  using_fallback: boolean;
  health: Record<string, unknown>;
}

export interface AuditAction {
  timestamp: string;
  user_id: string;
  action: string;
  symbol?: string;
  intent?: string;
  mode?: string;
}

export interface AuditActionsResponse {
  actions: AuditAction[];
  total: number;
}

export interface IndexFileResponse {
  status: string;
  file_path: string;
  job_id?: number;
  workspace_id: string;
  queue_depth?: number;
  reason?: string;
}

export interface IndexFilesResponse {
  status: string;
  workspace_id: string;
  results: Array<{
    accepted: boolean;
    status: string;
    file_path: string;
    queue_depth: number;
    reason?: string;
  }>;
  queued: number;
  coalesced: number;
  rejected: number;
  queue_depth: number;
}

export interface MetricsResponse {
  requests_total: number;
  requests_successful: number;
  requests_failed: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_p99_ms: number;
  tokens_used_total: number;
  cost_usd_total: number;
  cache_hit_rate: number;
}

export interface IndexQueueResponse {
  queue_depth: number;
  pending_jobs: number;
  processing: number;
  completed_total: number;
}

export interface FeedbackEvent {
  message_id: string;
  rating: 'up' | 'down';
  comment?: string;
  context?: Record<string, unknown>;
}

export const SidecarClient = {

  async health(): Promise<boolean> {
    try {
      const res = await fetch(`${getBaseUrl()}/health`);
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
      await fetch(`${getBaseUrl()}/overlay?file_path=${encodeURIComponent(file_path)}`, {
        method: 'DELETE',
        headers: getHeaders(),
      });
    } catch {
      // silently ignore
    }
  },

  ask(symbol: string, question: string, tokenBudget = 4000): Promise<AskResponse> {
    return post('/ask', { symbol, question, token_budget: tokenBudget });
  },

  async askStream(
    symbol: string,
    question: string,
    callbacks: SSECallbacks,
    tokenBudget = 4000
  ): Promise<AbortController> {
    const controller = new AbortController();

    try {
      const res = await fetch(`${getBaseUrl()}/ask/stream`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ symbol, question, token_budget: tokenBudget }),
        signal: controller.signal,
      });

      if (!res.ok) {
        throw new Error(`Sidecar /ask/stream → ${res.status}`);
      }

      await parseSSEStream(res, callbacks);
    } catch (error) {
      if (!(error instanceof Error && error.name === 'AbortError')) {
        callbacks.onError?.(error instanceof Error ? error.message : 'Unknown error');
      }
    }

    return controller;
  },

  impact(symbol: string): Promise<ImpactResponse> {
    return get(`/impact?symbol=${encodeURIComponent(symbol)}`);
  },

  cloudStatus(): Promise<CloudStatusResponse> {
    return get('/status/cloud');
  },

  auditActions(userId?: string, limit = 100): Promise<AuditActionsResponse> {
    const params = new URLSearchParams();
    if (userId) params.append('user_id', userId);
    params.append('limit', limit.toString());
    return get(`/audit/actions?${params.toString()}`);
  },

  unifiedSearch(
    query: string,
    symbol?: string,
    limit = 5,
    tokenBudget = 2000
  ): Promise<UnifiedSearchResponse> {
    return post('/search/unified', {
      query,
      symbol: symbol || null,
      limit,
      include_graph: true,
      token_budget: tokenBudget,
    });
  },

  indexFile(file_path: string): Promise<IndexFileResponse> {
    return post('/index/file', { file_path, queue: true });
  },

  indexFiles(file_paths: string[]): Promise<IndexFilesResponse> {
    return post('/index/files', { file_paths, queue: true });
  },

  index(project_path: string): Promise<{ status: string }> {
    return post('/index', { project_path, queue: true });
  },

  indexDocs(docs_path: string): Promise<{ status: string }> {
    return post('/index/docs', { docs_path });
  },

  search(query: string, limit = 10): Promise<SearchResponse> {
    return post('/search', { query, limit });
  },

  metrics(): Promise<MetricsResponse> {
    return get('/metrics');
  },

  indexQueueStatus(): Promise<IndexQueueResponse> {
    return get('/index/queue');
  },

  async submitFeedback(event: FeedbackEvent): Promise<void> {
    try {
      await post('/feedback', {
        message_id: event.message_id,
        rating: event.rating,
        comment: event.comment || '',
        context: event.context || {},
      });
    } catch (error) {
      console.error('Failed to submit feedback:', error);
      // Don't throw, feedback is non-critical
    }
  },
};
