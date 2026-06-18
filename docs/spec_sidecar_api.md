# Sidecar API — Spec


## Overview

FastAPI process running on localhost. VS Code communicates via HTTP/JSON. Fault-isolated from the editor: if the sidecar blocks on a Cypher query, the editor stays responsive.

Entry point: `context_engine/main.py`
Start: `uvicorn context_engine.main:app --port 8000`

---

## Configuration

All via environment variables with defaults:

| Variable | Default | Description |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection string |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `password` | Neo4j password |
| `OLLAMA_MODEL` | `llama3` | Ollama model for `/ask` |
| `MODEL_PREFERENCE` | `ollama` | AI routing: `ollama` (local-first default), `auto`, or `claude` |
| `ALLOW_CLOUD_LLM` | `false` | Must be `true` before `auto`/`claude` may send assembled context to Anthropic, even if `ANTHROPIC_API_KEY` is set |
| `AUTH_REQUIRED` | `false` | When true, protected endpoints require `Authorization: Bearer <token>` |
| `DEFAULT_WORKSPACE_ID` | `local/surgical_context@main` | Development fallback when `X-Workspace` is absent |
| `HISTORY_MODE` | `local` | Local history mode: `local`, `ephemeral`, or `disabled` |
| `HISTORY_DB_PATH` | `./data/history/surgical_context.sqlite3` | SQLite path for local history mode |
| `HISTORY_RETENTION_DAYS` | unset | Optional non-negative retention window for local history |
| `SIDECAR_REQUEST_LATENCY_SLO_MS` | `200` | Request latency SLO target used by metrics and structured logs |
| `SIDECAR_OTEL_ENABLED` | `false` | When true and OpenTelemetry is installed/configured, request stages emit spans |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model ID when cloud routing is enabled. Override via env; do not use retired `claude-sonnet-4-20250514` (API retirement 2026-06-15). |

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

### Filesystem path sandboxing

Any endpoint that reads or indexes files on disk (`POST /index`, `/index/file`, `/index/files`, `/index/docs`, `/ask` with `file_path`, `/overlay`) resolves paths against the **registered workspace project root**:

- Root is stored in the index manifest (`project_path`) as soon as `POST /index` succeeds (including `queue=true`, before the batch worker finishes).
- Relative paths are resolved under that root; absolute paths must still lie inside it.
- Paths outside the root return **`403`** with a detail message.
- File/index operations before the workspace is indexed return **`400`** (“no registered project root; POST /index first”).
- `POST /index` registers the resolved `project_path` directory as the root for that workspace (queued file paths are validated under it).

**Graph-resolved reads:** `ContextArbitrator` / `CodeResolver` and `UnifiedRanker` module sizing use the same root for `file_path` values coming from Neo4j. Paths outside the root return empty code (no disk read). On manifest persist, outside-root `File` nodes are best-effort deleted from the graph.

This limits local callers when `AUTH_REQUIRED=false` from using the sidecar to read or index arbitrary readable files, including via stale graph nodes.

### Request validation bounds

Server-side Pydantic limits (invalid values → HTTP **422**):

| Field | Endpoints | Bounds | Default |
|---|---|---|---|
| `limit` | `/search`, `/search/unified` | 1–50 | `5` |
| `token_budget` | `/ask`, `/ask/stream`, `/search/unified` (graph leg) | 400–32 000 | `4000` / `2000` |

Implementation: `SEARCH_LIMIT_*` and `TOKEN_BUDGET_*` in `context_engine/main.py`. Tests: `tests/unit/test_api_bounds.py`.

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

**Errors:** `400` if path does not exist or is not a directory.

**Behavior:** Registers `project_path` as the workspace root for path sandboxing **before** returning (writes a minimal index manifest with `indexing_outcome: queued` to Neo4j and `.surgical_context/index_manifest.json`, even when `queue=true`). Queues discovered source files by default. With `queue=false`, runs `run_indexing()` immediately and replaces the manifest when the full fast pipeline completes.

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

**Errors:** `400` if path does not exist or workspace root is not registered; `403` if path is outside the workspace root.

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

**Errors:** `400` if the file does not exist or workspace root is not registered; `403` if path is outside the workspace root; `500` with `job_id` and `job_status` if the graph/vector update fails.

**Behavior:** Queues the file by default. With `queue=false`, hashes the file, creates a durable indexing job record, deletes previous symbols for the file, re-indexes symbols/calls/embeddings, resolves pending DocAnchors, then marks the job `succeeded`. Failures are captured for retry/dead-letter handling.

---

### POST /index/files
Incrementally index a bounded batch of saved source files.

**Request:**
```json
{ "file_paths": ["/absolute/path/to/project/app.py"], "queue": true }
```

**Response:** includes per-file results plus queue depth. When `queue=true`, files are queued and debounced; when `false`, the endpoint runs the batch immediately.

**Errors:** `400` / `403` same path sandboxing rules as `/index/file` (per path in the batch).

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
  "question": "How does call extraction work?",
  "token_budget": 4000,
  "file_path": "/absolute/path/to/project/module.py"
}
```

| Field | Required | Notes |
|---|---|---|
| `symbol` | No | When present, surgical context is assembled for this graph symbol first. |
| `question` | No | Defaults to `"What does this code do?"` |
| `token_budget` | No | Defaults to `4000`. Server bounds: **400–32 000** (inclusive); out-of-range → HTTP **422**. |
| `file_path` | No | Optional path used when symbol resolution fails (see fallback ladder). Resolved under the workspace `project_path`; relative paths are allowed. Outside root → `403`. |

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

**Errors:** `/ask` does **not** return `404` when a symbol is missing from the graph. Missing symbols trigger the fallback ladder below; the response is always HTTP `200` with a populated `context` (unless auth/workspace validation fails).

**Context resolution (fallback ladder):** `_resolve_ask_context` in `context_engine/main.py` tries, in order:

1. **Symbol** — when `symbol` is set, `ContextArbitrator.get_context_for_symbol(...)` runs intent classification, workspace-scoped graph expansion, code resolution, and doc retrieval. On success, `context.budget.ask_level` is `"symbol"`.
2. **File** — when symbol resolution returns an error string and `file_path` is set, assemble context from that file on disk (`ask_level`: `"file"`).
3. **Workspace** — vector search over indexed docs + symbols for the question (`ask_level`: `"workspace"`, `mode`: `"workspace"`). Skipped when both searches return nothing.
4. **Direct LLM** — minimal `PromptContext` with no graph or docs (`ask_level`: `"direct_llm"`, `mode`: `"direct"`). This step always succeeds.

When a later step is used after a failed symbol lookup, `context.budget` includes `missing_symbol`, `fallback_from`, `fallback_reason` (e.g. `symbol_not_found`), `fallback_ladder`, and a `warnings[]` entry explaining the downgrade. Clients should read these fields instead of treating a missing symbol as a hard error.

**Behavior:**
1. Resolve the user from `Authorization: Bearer <token>` or `X-User-Id`. When `AUTH_REQUIRED=true`, missing or invalid bearer tokens return `401`.
2. Resolve workspace from `X-Workspace` (`tenant/repo@ref`) or `DEFAULT_WORKSPACE_ID` when the header is absent.
3. Run the fallback ladder above to build `PromptContext`.
4. `AIEngine.chat()` routes to Ollama by default. Cloud routing (`auto`/`claude` → Anthropic) runs only when `ALLOW_CLOUD_LLM=true` and `ANTHROPIC_API_KEY` is set; otherwise assembled `system_prompt` never leaves the machine regardless of key presence.
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

**Behavior:** Uses the same context fallback ladder, model-routing, and L3 response-cache path as `/ask` (including `missing_symbol` / `fallback_*` metadata when symbol resolution fails). On a cache hit the full cached answer is emitted as a single `chunk` event followed by `context` and `done`. On a miss, chunks stream normally and the complete answer is written to L3 after the final chunk. Both endpoints are cache-symmetric.

**LLM degradation:** When the model is unreachable (`RuntimeError` from the provider), streaming mirrors non-streaming `/ask`: emit one `chunk` with the degraded context-only message, then `context` (full Prompt Contract + `feedback_token`, `model_route.degraded=true`) and `done`. Do not emit only `error` for this case — clients still receive assembled context for inspection.

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

| Field | Bounds |
|---|---|
| `limit` | **1–50** (default `5`); out-of-range → HTTP **422** |

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
  "limit": 10,
  "token_budget": 2000
}
```

| Field | Bounds |
|---|---|
| `limit` | **1–50** (inherited from `/search`) |
| `token_budget` | **400–32 000** (default `2000`); used when `include_graph` and `symbol` are set |

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

**Behavior:** Stores content in `InMemoryOverlay` keyed by `(workspace_id, user_id, file_path)` (resolved from `X-Workspace` and the authenticated user). Re-parses symbols via tree-sitter (no disk I/O), returns symbol names found in the dirty version. Two users in the same workspace do not share unsaved buffers.

**Errors:** `400` / `403` path sandboxing (same as `/index/file`).

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

**Implementation note:** symbol UID and file-path lookups go through `Neo4jClient.get_symbol_uid_by_name()` and `Neo4jClient.get_file_path_for_symbol()`. No raw Cypher is written in the route handler. `404` is returned when the symbol is not found in the workspace.

---

### POST /auth/token
Generate a signed bearer token.

**Query param:** `user_id=alice`

When `AUTH_REQUIRED=false` (local default), this endpoint is the explicit
local bootstrap/dev token issuer. When `AUTH_REQUIRED=true`, callers must
already present a valid `Authorization: Bearer <token>` header and may only
mint a replacement token for the authenticated user. Requests for another
`user_id` return `403`.

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
Return recent audit log entries for the authenticated/request user.

**Query params:** `user_id` optional self-filter, `limit` default `100`.

Omitting `user_id` returns only the requester's actions. Supplying another
user id returns `403`; the endpoint never returns all users' audit entries.

**Response:**
```json
{ "actions": [], "total": 0 }
```

---

## Singletons

`overlay` (`InMemoryOverlay`), `vector_db` (`LanceDBClient`), `ai_engine` (`AIEngine`), `user_auth`, `audit_log`, `feedback_store`, and `history_provider` are process-level singletons.

Neo4j access goes through `db_session(...)`, which creates a request-scoped client and closes it after the endpoint finishes. This avoids mutating shared request identity on the global database object.

Protected endpoints accept local `X-User-Id` identity by default for development.
Issue bootstrap tokens while `AUTH_REQUIRED=false`, then set
`AUTH_REQUIRED=true` to require signed bearer tokens. In protected mode,
`/auth/token` only refreshes the authenticated user's own token.

Graph endpoints accept `X-Workspace: tenant/repo@ref`. Neo4j `File`, `CONTAINS`, call, and `AFFECTS` operations are scoped by that workspace id.

Structured request logs and Prometheus metrics track request latency against `SIDECAR_REQUEST_LATENCY_SLO_MS` and emit SLO check/violation counters. When `SIDECAR_OTEL_ENABLED=true` and OpenTelemetry is available, each request stage also emits an OpenTelemetry span with trace/workspace/stage attributes.

---

## Planned Extensions

- Add production auth policy: persistent users, secret rotation, token revocation, and role-based authorization.
- Expand `GET /metrics` with local release SLO checks and dashboard-ready health fields.
- Keep prompt-contract observability consistent across extension surfaces, especially `pruned[]`, ranker weights, intent distribution, and ambiguous-intent signal.
- Extend history/storage policy only after local metadata-first retention behavior is validated.
