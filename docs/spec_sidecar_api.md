# Sidecar API — Spec

## Overview

FastAPI process running on localhost. VS Code communicates via HTTP/JSON. Fault-isolated from the editor: if the sidecar blocks on a Cypher query, the editor stays responsive.

Entry point: `sidecar/main.py`
Start: `uvicorn sidecar.main:app --port 8000`

---

## Configuration

All via environment variables with defaults:

| Variable | Default | Description |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection string |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `password` | Neo4j password |
| `OLLAMA_MODEL` | `llama3` | Ollama model for `/ask` |

---

## Endpoints

### GET /health
Liveness check.

**Response:** `{"status": "ok"}`

---

### POST /index
Index a code directory into Neo4j + LanceDB.

**Request:**
```json
{ "project_path": "/absolute/path/to/project" }
```

**Response:**
```json
{ "status": "indexed", "path": "/absolute/path/to/project" }
```

**Errors:** `400` if path does not exist.

**Behavior:** Calls `run_indexing()` from `indexer_main.py`. Runs all 4 phases: symbol extraction → call linking → symbol embeddings → pending DocAnchor resolution.

---

### POST /index/docs
Index a documentation directory into LanceDB + DocAnchor graph.

**Request:**
```json
{ "docs_path": "/absolute/path/to/docs" }
```

**Response:**
```json
{ "status": "indexed", "path": "/absolute/path/to/docs" }
```

**Errors:** `400` if path does not exist.

**Behavior:** Section-aware markdown chunking → LanceDB upsert → `link_docs_to_symbols()`.

---

### POST /ask
Assemble surgical context for a symbol and query the LLM.

**Request:**
```json
{
  "symbol": "SymbolExtractor",
  "question": "How does call extraction work?"
}
```

**Response:**
```json
{
  "symbol": "SymbolExtractor",
  "answer": "...",
  "context": {
    "primary_source": { "symbol": "...", "file_path": "...", "is_dirty": false, "code": "..." },
    "graph_context": [{ "symbol": "...", "file_path": "...", "relation": "CALLS", "is_dirty": false, "code": "..." }],
    "documentation": [{ "chunk_id": "...", "source_file": "...", "content": "..." }]
  }
}
```

**Errors:** `404` if symbol not found in graph.

**Behavior:**
1. `ContextArbitrator.get_context_for_symbol(symbol)` — BFS graph traversal, overlay-aware code assembly
2. `LanceDBClient.search(symbol + question, limit=3)` — top-3 doc chunks appended to context
3. `ollama.chat()` — stateless single turn, system prompt contains full context

---

### POST /search
Semantic search over indexed documentation.

**Request:**
```json
{ "query": "how does chunking work", "limit": 5 }
```

**Response:**
```json
{
  "results": [
    { "file_path": "docs/spec_parser.md", "chunk": "..." }
  ]
}
```

---

### POST /overlay
Push unsaved file content into the In-Memory Overlay.

**Request:**
```json
{ "file_path": "/abs/path/file.py", "content": "def foo(): ..." }
```

**Response:**
```json
{ "file_path": "/abs/path/file.py", "symbols": ["foo", "MyClass"] }
```

**Behavior:** Stores content in `InMemoryOverlay`, re-parses symbols via tree-sitter (no disk I/O), returns symbol names found in the dirty version.

---

### DELETE /overlay
Clear overlay for a file (called on save or editor close).

**Query param:** `file_path=/abs/path/file.py`

**Response:**
```json
{ "cleared": "/abs/path/file.py" }
```

---

## Singletons

`overlay` (`InMemoryOverlay`) and `vector_db` (`LanceDBClient`) are process-level singletons.  
`Neo4jClient` is created per request in `/ask` and closed in a `finally` block.

---

## Planned Extensions

- Stream LLM responses (SSE or chunked transfer) instead of blocking until full answer
- Anthropic API model routing (`sidecar/ai_engine.py` — implementation stubbed, deferred Phase 5)
- `/ask` depth parameter for BFS traversal (currently depth 1)
