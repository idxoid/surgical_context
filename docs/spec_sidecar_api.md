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
| `MODEL_PREFERENCE` | `auto` | AI routing preference: `auto`, `claude`, or `ollama` |
| `AUTH_REQUIRED` | `false` | When true, protected endpoints require `Authorization: Bearer <token>` |
| `DEFAULT_WORKSPACE_ID` | `local/surgical_context@main` | Development fallback when `X-Workspace` is absent |
| `HISTORY_MODE` | `local` | Local history mode: `local`, `ephemeral`, or `disabled` |
| `HISTORY_DB_PATH` | `./data/history/surgical_context.sqlite3` | SQLite path for local history mode |
| `HISTORY_RETENTION_DAYS` | unset | Optional non-negative retention window for local history |
| `SIDECAR_REQUEST_LATENCY_SLO_MS` | `200` | Request latency SLO target used by metrics and structured logs |
| `SIDECAR_OTEL_ENABLED` | `false` | When true and OpenTelemetry is installed/configured, request stages emit spans |

### Workspace Identity

The sidecar resolves workspace scope from the optional `X-Workspace` header. The
canonical format is:

```text
{tenant}/{repo}@{ref}
```

Example:

```text
X-Workspace: local/surgical_context@main
```

If the header is absent, the sidecar uses `DEFAULT_WORKSPACE_ID`. The VS Code
extension leaves `surgicalContext.workspaceId` blank by default and derives a
workspace id from the first open VS Code workspace folder plus the active Git
branch, e.g. `local/surgical_context@context-engine-refocus`. If a user enters an
explicit `surgicalContext.workspaceId`, the extension sends that value instead.
The legacy extension default `local/default@main` is treated as unset and is not
sent as a header.

---

## Endpoints

### GET /health
Liveness check.

**Response:** `{"status": "ok"}`

---

### GET /metrics
Prometheus text metrics for request latency, indexing, feedback, and retrieval counters.

---

### POST /index
Index a code directory into Neo4j + LanceDB.

**Request:**
```json
{ "project_path": "/absolute/path/to/project", "queue": true }
```

**Response:**
```json
{ "status": "queued", "path": "/absolute/path/to/project", "queued": 42, "coalesced": 0 }
```

**Errors:** `400` if path does not exist.

**Behavior:** Queues discovered source files by default. With `queue=false`, runs `run_indexing()` immediately: symbol extraction → call linking → symbol embeddings → pending DocAnchor resolution.

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

### POST /index/file
Incrementally index one saved source file.

**Request:**
```json
{ "file_path": "/absolute/path/to/project/app.py", "queue": true }
```

**Response:**
```json
{
  "status": "queued",
  "file_path": "/absolute/path/to/project/app.py",
  "job_id": 0,
  "workspace_id": "local/repo@main",
  "queue_depth": 1
}
```

**Errors:** `400` if the file does not exist; `500` with `job_id` and `job_status` if the graph/vector update fails.

**Behavior:** Queues the file by default. With `queue=false`, hashes the file, creates a durable indexing job record, deletes previous symbols for the file, re-indexes symbols/calls/embeddings, resolves pending DocAnchors, then marks the job `succeeded`. Failures are captured for retry/dead-letter handling.

---

### POST /index/files
Incrementally index a bounded batch of saved source files.

**Request:**
```json
{ "file_paths": ["/absolute/path/to/project/app.py"], "queue": true }
```

**Response:** includes per-file results plus queue depth. When `queue=true`, files are queued and debounced; when `false`, the endpoint runs the batch immediately.

---

### GET /index/queue
Return the bounded indexing queue snapshot.

**Response:** queue status with pending count and recent job metadata.

---

### GET /index/manifest
Return the current workspace index manifest from Neo4j when available.

**Headers:** `X-Workspace` selects the workspace.

**Response:** manifest schema version, manifest id, and git/index metadata. Returns `404` when the workspace has no stored manifest yet.

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
  },
  "user": "alice",
  "cloud": true,
  "workspace_id": "acme/repo@main",
  "trace_id": "trace_...",
  "feedback_token": "fbk_..."
}
```

**Errors:** `404` if symbol not found in graph.

**Behavior:**
1. Resolve the user from `Authorization: Bearer <token>` or `X-User-Id`. When `AUTH_REQUIRED=true`, missing or invalid bearer tokens return `401`.
2. Resolve workspace from `X-Workspace` (`tenant/repo@ref`) or `DEFAULT_WORKSPACE_ID` when the header is absent.
3. `ContextArbitrator.get_context_for_symbol(symbol, question, token_budget)` runs intent classification, workspace-scoped graph expansion, deduplication, code resolution, and doc retrieval.
4. `AIEngine.chat()` routes to the configured local/cloud model based on model preference, context size, and intent.
5. Audit logging records successful and failed query actions.
6. A privacy-scoped retrieval snapshot is written with an opaque `feedback_token`. The snapshot stores selected candidate metadata and hashes, not raw prompts, code bodies, answers, or free-text comments.
7. If the selected model is unreachable, `/ask` returns HTTP 200 with a degraded context-only answer, `model_route.degraded=true`, and the full assembled `context`.

---

### POST /ask/stream
Streaming version of `/ask` using server-sent events.

**Request:** same as `/ask`.

**Event types:**
- `chunk` — one generated model chunk.
- `context` — final JSON Prompt Contract plus `feedback_token`.
- `error` — JSON error payload.
- `done` — terminal event.

**Behavior:** Uses the same arbitration and model-routing path as `/ask`, but frames every SSE payload through JSON-safe `format_sse()`.

---

### POST /feedback
Record retrieval feedback against a token issued by `/ask` or `/ask/stream`.

**Request:**
```json
{
  "feedback_token": "fbk_...",
  "kind": "explicit_reject",
  "details": {
    "missing_symbols": ["RequestTimeout.apply"],
    "comment": "I was looking for timeout logic"
  }
}
```

**Response:**
```json
{
  "status": "recorded",
  "feedback_token": "fbk_...",
  "kind": "explicit_reject",
  "outcome": "reject",
  "workspace_id": "acme/repo@main",
  "trace_id": "trace_..."
}
```

**Errors:** `404` for unknown tokens, `403` for workspace/user scope mismatch, `400` for unsupported feedback kinds.

**Privacy:** Feedback is append-only and workspace-scoped. Structural details such as `missing_symbols`, `wrong_symbols`, and `correct_intent` are stored. Free-text `comment` content is not stored before a redaction pipeline exists; only `comment_present` and `comment_length` are retained.

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

### POST /search/unified
Unified search over symbols, graph neighbors, and docs.

**Request:**
```json
{
  "query": "ranking recovery",
  "symbol": "UnifiedRanker",
  "include_graph": true,
  "limit": 10
}
```

**Response:** ranked mixed results plus optional retrieval trace and index manifest ids when graph context is included.

---

### POST /history/ask
Persist a sanitized local ask/request snapshot.

**Request:** conversation id (optional), request id, symbol, prompt/answer summaries or hashes, trace id, feedback token, and optional ask/inspector/impact snapshots.

**Response:**
```json
{
  "status": "recorded",
  "conversation_id": "conv_...",
  "user_message_id": "msg_...",
  "assistant_message_id": "msg_...",
  "selected_request_id": "req_..."
}
```

**Behavior:** Uses `SQLiteHistoryProvider` in `local` mode, a temporary SQLite database in `ephemeral` mode, or returns a no-op response in `disabled` mode. Snapshots are sanitized before persistence.

---

### GET /history/conversations
List local history conversations for the current workspace and user.

**Query param:** `limit` default `30`.

---

### GET /history/conversations/{conversation_id}
Return a sanitized conversation bundle with messages and snapshots. Workspace/user mismatches return `403`.

---

### GET /history/conversations/{conversation_id}/requests/{request_id}
Return ask, inspector, and impact snapshots for one selected request. Workspace/user mismatches return `403`.

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

### GET /impact
Return downstream symbols and files affected by changing a symbol.

**Query param:** `symbol=process_payment`

**Response:**
```json
{
  "symbol": "process_payment",
  "symbol_uid": "...",
  "file_path": "/repo/payments.py",
  "affected_symbols": [],
  "affected_files": [],
  "affected_count": 0,
  "affected_file_count": 0,
  "max_depth": 4
}
```

---

### POST /auth/token
Generate a signed bearer token for a user id.

**Query param:** `user_id=alice`

**Response:**
```json
{ "token": "...", "user_id": "alice", "expires_in_hours": 24 }
```

---

### GET /auth/users
List active users tracked by the auth helper.

**Response:**
```json
{ "users": [] }
```

---

### GET /status/cloud
Report Aura/fallback health from a request-scoped DB session.

**Response:**
```json
{
  "cloud_enabled": true,
  "using_aura": true,
  "using_fallback": false,
  "health": {}
}
```

---

### GET /audit/actions
Return recent audit log entries.

**Query params:** `user_id` optional, `limit` default `100`.

**Response:**
```json
{ "actions": [], "total": 0 }
```

---

## Singletons

`overlay` (`InMemoryOverlay`), `vector_db` (`LanceDBClient`), `ai_engine` (`AIEngine`), `user_auth`, `audit_log`, `feedback_store`, and `history_provider` are process-level singletons.

Neo4j access goes through `db_session(...)`, which creates a request-scoped client and closes it after the endpoint finishes. This avoids mutating shared request identity on the global database object.

Protected endpoints accept local `X-User-Id` identity by default for development. Set `AUTH_REQUIRED=true` to require signed bearer tokens from `/auth/token`.

Graph endpoints accept `X-Workspace: tenant/repo@ref`. Neo4j `File`, `CONTAINS`, call, and `AFFECTS` operations are scoped by that workspace id.

Structured request logs and Prometheus metrics track request latency against `SIDECAR_REQUEST_LATENCY_SLO_MS` and emit SLO check/violation counters. When `SIDECAR_OTEL_ENABLED=true` and OpenTelemetry is available, each request stage also emits an OpenTelemetry span with trace/workspace/stage attributes.

---

## Planned Extensions

- Add production auth policy: persistent users, secret rotation, token revocation, and role-based authorization.
- Expand `GET /metrics` with local release SLO checks and dashboard-ready health fields.
- Keep prompt-contract observability consistent across extension surfaces, especially `pruned[]`, ranker weights, intent distribution, and ambiguous-intent signal.
- Extend history/storage policy only after local metadata-first retention behavior is validated.
