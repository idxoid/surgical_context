import * as vscode from 'vscode';
import { createHash } from 'crypto';
import { SSECallbacks, parseSSEStream } from './utils';

function getBaseUrl(): string {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  return normalizeBaseUrl(config.get<string>('backendUrl', 'http://localhost:8000'));
}

function normalizeBaseUrl(url: string): string {
  return url.replace(/\/+$/, '');
}

function getAuthToken(): string {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  return config.get<string>('authToken', '').trim();
}

function authHeaderValue(token: string): string {
  return token.toLowerCase().startsWith('bearer ') ? token : `Bearer ${token}`;
}

function getTokenBudget(defaultValue = 40000): number {
  const config = vscode.workspace.getConfiguration('surgicalContext');
  const configured = config.get<number>('tokenBudget', defaultValue);
  return Number.isFinite(configured) ? Math.max(1000, Math.min(32000, configured)) : defaultValue;
}

function getHeaders(authToken = getAuthToken()): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const config = vscode.workspace.getConfiguration('surgicalContext');
  const workspaceId = config.get<string>('workspaceId', 'local/default@main');
  if (workspaceId) {
    headers['X-Workspace'] = workspaceId;
  }
  if (authToken) {
    headers.Authorization = authHeaderValue(authToken);
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

async function getText(path: string): Promise<string> {
  const res = await fetch(`${getBaseUrl()}${path}`, {
    method: 'GET',
    headers: getHeaders(),
  });
  if (!res.ok) throw new Error(`Sidecar ${path} → ${res.status}`);
  return res.text();
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
      feedback_token?: string;
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

export interface HistoryAskRecordRequest {
  conversation_id?: string | null;
  request_id: string;
  prompt_summary: string;
  prompt_hash: string;
  answer_summary: string;
  answer_hash: string;
  symbol?: string;
  trace_id?: string;
  feedback_token?: string;
  ask_snapshot?: Record<string, unknown>;
  inspector_snapshot?: Record<string, unknown>;
  impact_snapshot?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface HistoryAskRecordResponse {
  status: string;
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  selected_request_id: string;
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
  resource?: string;
  status?: string;
  symbol?: string;
  intent?: string;
  mode?: string;
  details?: Record<string, unknown>;
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

export interface IndexQueueResponse {
  status: string;
  queue: {
    pending: number;
    processing: number;
    max_pending: number;
    batch_size: number;
    debounce_ms: number;
    last_error: string;
    enqueued: number;
    coalesced: number;
    rejected: number;
    processed: number;
    failed_batches: number;
  };
}

export interface FeedbackEvent {
  message_id: string;
  feedback_token?: string;
  rating: 'up' | 'down';
  comment?: string;
  context?: Record<string, unknown>;
}

export function hashHistoryText(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

export function safePromptSummary(symbol?: string, filePath?: string): string {
  if (symbol) {
    return `Ask about symbol ${symbol}`;
  }

  const fileName = filePath?.split(/[\\/]/).pop();
  if (fileName) {
    return `Ask about file ${fileName}`;
  }

  return 'Workspace ask';
}

export function safeAnswerSummary(answer: string): string {
  const length = answer.trim().length;
  return length > 0 ? `Assistant response recorded (${length} chars)` : 'Assistant response recorded';
}

export const SidecarClient = {

  async health(baseUrl = getBaseUrl(), authToken = getAuthToken()): Promise<boolean> {
    try {
      const res = await fetch(`${normalizeBaseUrl(baseUrl)}/health`, {
        headers: getHeaders(authToken),
      });
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

  ask(
    symbol: string | undefined,
    question: string,
    tokenBudget = getTokenBudget(),
    filePath?: string
  ): Promise<AskResponse> {
    return post('/ask', {
      symbol: symbol || null,
      question,
      token_budget: tokenBudget,
      file_path: filePath || null,
    });
  },

  async askStream(
    symbol: string | undefined,
    question: string,
    callbacks: SSECallbacks,
    tokenBudget = getTokenBudget(),
    filePath?: string
  ): Promise<AbortController> {
    const controller = new AbortController();

    try {
      const res = await fetch(`${getBaseUrl()}/ask/stream`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({
          symbol: symbol || null,
          question,
          token_budget: tokenBudget,
          file_path: filePath || null,
        }),
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
    tokenBudget = getTokenBudget(2000)
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

  metrics(): Promise<string> {
    return getText('/metrics');
  },

  recordAskHistory(record: HistoryAskRecordRequest): Promise<HistoryAskRecordResponse> {
    return post('/history/ask', record);
  },

  indexQueueStatus(): Promise<IndexQueueResponse> {
    return get('/index/queue');
  },

  async submitFeedback(event: FeedbackEvent): Promise<void> {
    if (!event.feedback_token) {
      console.warn('Skipping feedback without feedback token:', event.message_id);
      return;
    }
    try {
      await post('/feedback', {
        feedback_token: event.feedback_token,
        kind: event.rating === 'up' ? 'explicit_accept' : 'explicit_reject',
        details: event.context || {},
        timestamp: new Date().toISOString(),
      });
    } catch (error) {
      console.error('Failed to submit feedback:', error);
      // Don't throw, feedback is non-critical
    }
  },
};
