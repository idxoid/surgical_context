/**
 * Typed message protocol between webview and extension host.
 * Webview → Host: user actions (ask, accordion toggle, etc.)
 * Host → Webview: state updates and streaming responses
 */

// ============ Webview → Extension Host Messages ============

export type WebviewToHostMessage =
  | { type: 'surface.ready' }
  | { type: 'chat.ask'; prompt: string; symbol?: string }
  | { type: 'chat.stop'; requestId: string }
  | { type: 'chat.retry'; messageId: string }
  | { type: 'composer.changed'; text: string; heightPx: number }
  | { type: 'accordion.toggled'; id: string; expanded: boolean }
  | { type: 'feedback.submit'; messageId: string; rating: 'up' | 'down' }
  | { type: 'action.openInspector' }
  | { type: 'action.openSettings' }
  | { type: 'action.showImpact'; symbol?: string }
  | { type: 'action.openChat'; prefillSymbol?: string }
  | { type: 'action.openDashboard' }
  | { type: 'link.openFile'; filePath: string; line?: number }
  | { type: 'dashboard.refresh' }
  | { type: 'dashboard.indexWorkspace' }
  | { type: 'settings.loaded' }
  | { type: 'settings.save'; settings: SettingsData }
  | { type: 'settings.update'; key: string; value: unknown }
  | { type: 'settings.testUrl'; url: string; authToken?: string }
  | { type: 'settings.openKeybindings' };

// ============ Extension Host → Webview Messages ============

export type HostToWebviewMessage =
  | { type: 'surface.init'; state: ChatSurfaceState }
  | { type: 'surface.showChat' }
  | { type: 'surface.showInspector' }
  | { type: 'surface.showSettings' }
  | { type: 'chat.requestStarted'; requestId: string; symbol?: string }
  | { type: 'chat.streamChunk'; requestId: string; chunk: string }
  | { type: 'chat.requestCompleted'; requestId: string; answer: string; context: PromptContextPayload }
  | { type: 'chat.requestFailed'; requestId: string; error: string }
  | { type: 'chat.requestStopped'; requestId: string }
  | { type: 'chat.contextSummary'; summary: ContextSummaryDto }
  | { type: 'workspace.updated'; activeFile: string | null; symbol: string | null; isDirty: boolean }
  | { type: 'backend.updated'; sidecarHealth: 'up' | 'down' | 'degraded'; cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline' }
  | { type: 'toast.show'; level: 'info' | 'warning' | 'error'; message: string }
  | { type: 'inspector.loaded'; context: PromptContextPayload | null }
  | { type: 'impact.loading' }
  | { type: 'impact.loaded'; symbol: string; impact: ImpactResponse }
  | { type: 'impact.loadFailed'; error: string }
  | { type: 'dashboard.loading' }
  | {
      type: 'dashboard.metricsLoaded';
      health: 'up' | 'down' | 'degraded';
      cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline';
      auditActions: AuditAction[];
      metrics: DashboardMetrics;
      healthChecks: HealthCheckItem[];
      notices: DashboardNotice[];
      workspaceId: string;
      warnings: string[];
    }
  | { type: 'dashboard.metricsFailed'; error: string }
  | { type: 'settings.loaded'; settings: SettingsData }
  | { type: 'settings.saved'; message: string }
  | { type: 'settings.saveFailed'; error: string }
  | { type: 'settings.testUrlComplete'; success: boolean; message: string };

// ============ Data Transfer Objects ============

export interface ChatSurfaceState {
  expandedAccordions: Record<string, boolean>;
  composerDraft: string;
  lastContext?: PromptContextPayload | null;
  workspace: {
    activeFile: string | null;
    selectedSymbol: string | null;
    isDirty: boolean;
  };
  backend: {
    sidecarHealth: 'up' | 'down' | 'degraded';
    cloudStatus: 'connected' | 'fallback-local' | 'local' | 'offline';
  };
}


export interface ContextSummaryDto {
  primaryLabel: string;
  graphCount: number;
  docsCount: number;
  tokenText: string;
  chips: string[];
}

export interface ChatMessage {
  id: string;
  requestId?: string;
  type: 'user' | 'assistant';
  content: string;
  timestamp: number;
  symbol?: string;
  context?: PromptContextPayload;
  status?: 'streaming' | 'done' | 'error';
  error?: string;
}

// Context types matching sidecar API
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

export interface ImpactResponse {
  symbol: string;
  symbol_uid: string;
  file_path: string;
  affected_symbols: Array<Record<string, unknown>>;
  affected_files: string[];
}

export interface AuditAction {
  timestamp: string;
  action_type: string;
  symbol: string;
  status: string;
  details?: Record<string, unknown>;
}

export interface DashboardMetrics {
  indexedFiles: number | null;
  indexedSymbols: number | null;
  docChunks: number | null;
  avgLatencyMs: number | null;
  tokenSavingsPercent: number | null;
  fallbackRatePercent: number | null;
  contextQualityPercent: number | null;
  symbolsWithDocs: number | null;
  storageGb: number | null;
  requestsTotal: number | null;
  tokensTotal: number | null;
  costUsdTotal: number | null;
  queuePending: number | null;
  queueProcessing: number | null;
  queueProcessed: number | null;
  queueFailedBatches: number | null;
  lastIndexJobStatus: string | null;
}

export interface HealthCheckItem {
  id: string;
  label: string;
  status: 'ok' | 'warning' | 'error' | 'pending';
  value: string;
  detail: string;
}

export interface DashboardNotice {
  id: string;
  level: 'info' | 'warning' | 'error';
  title: string;
  message: string;
  action?: 'refresh' | 'indexWorkspace';
  actionLabel?: string;
}

export interface SettingsData {
  backendUrl: string;
  workspaceId: string;
  modelPreference: string;
  authToken: string;
  tokenBudget: number;
  lancedbPath: string;
  historyPath: string;
  overlaySync: boolean;
  autoOpenInspector: boolean;
}
