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

export interface AskResponse {
  symbol: string;
  answer: string;
  context: {
    primary_source: string;
    graph_context: string;
    documentation: string;
  };
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
