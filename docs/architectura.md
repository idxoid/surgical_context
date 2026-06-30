# Surgical Context — Architecture

> **Status:** The active release target is the Local Developer Product: a Python context engine with local graph/vector/history defaults and optional VS Code UI. Code indexing, typed call edges, stable UID v2, scoped call resolution, workspace-scoped graph queries, AFFECTS, doc enrichment, **axis retrieval** (`context_engine/axis/`) as the default `/ask` path, model routing, metrics, feedback telemetry, durable index jobs, bounded indexing, and the extension surface are present. The legacy ranking cascade (`ContextArbitrator`, `UnifiedRanker`) was removed in 2026-06. Main open gaps: richer axis-to-prompt observability/doc propagation, extension polish, broader real-repo benchmark validation, impact-analysis precision, and provider boundaries around local defaults. See [road_map.md](road_map.md).
>
> **Future layer:** tenant-level API contract graph. Each project indexes and publishes its own safe service/API facts; the tenant graph links those facts across projects and systems without scanning neighboring repositories. This is Team/Enterprise horizon work, not a dependency for the local single-tenant release. See [spec_tenant_api_graph.md](spec_tenant_api_graph.md).

## Section 1: Executive Summary & Goals

### 1.1. Project Overview
Surgical Context is a local-first context engine that enhances LLM grounding and reduces token waste through structural and semantic retrieval. The VS Code extension is one client of the context_engine API, alongside scripts and the QA harness.

Instead of "carpet-bombing" the model with all open files, the system feeds only the specific code snippets and documentation fragments that are mathematically relevant to the user's current task.

### 1.2. The Problem
1. **Context Noise** — irrelevant code confuses the model and causes hallucinations.
2. **Token Inefficiency** — superfluous data inflates cost and hits rate limits.
3. **Knowledge Silos** — AI misses connections between code and docs unless both are open.
4. **Service Boundary Drift** — in microservice systems, APIs, schemas, generated clients, and event contracts change across project boundaries faster than humans can track manually.

### 1.3. Success Metrics
- **Precision:** reduce transmitted code by 60–80% with equal or better answer quality.
- **Cost:** lower average token cost 3–5× via surgical selection + model routing.
- **Latency:** context assembly (Graph + Vector + FS) under 200ms.
- **Portability:** local defaults work out of the box, while provider boundaries leave room for customer-owned storage later.

### 1.4. Design Principles
- **Ownership over Hype:** robust data infrastructure, not an API wrapper.
- **Security by Design:** source code never enters graph storage; vector/history persistence follows explicit storage policy and defaults local. Filesystem access from the context_engine is limited to paths under the workspace **project root** registered at index time (see §2.3.1).
- **Transparency:** user always sees what context was collected and what it cost.

### 1.5. Product Layers

| Layer | Purpose | Required for Local v0.1 |
|---|---|---|
| **Local Developer Product** | Single-tenant local VS Code tool with context_engine, Neo4j Docker, LanceDB, SQLite history, ask/inspect/impact, and local docs indexing. | Yes |
| **Team Layer** | Shared customer-owned storage, admin/user roles, connectable doc sources, and tenant API contract links between project-published manifests. | No |
| **Enterprise / Platform Layer** | Alternate database connectors, audit/retention stores, LLM proxy gateway transport, dedicated deployments, service split, and performance hot-path rewrites after profiling. | No |

---

## Section 2: System Architecture

### 2.1. Components

| Component | Stack | Role |
|---|---|---|
| **Extension Host** | TypeScript / VS Code API | Proxies webview messages to an externally started context_engine, derives workspace identity, and manages file watchers/overlays. |
| **Webviews** | TypeScript + DOM | Render Chat Panel, Context Inspector, Impact Explorer, Dashboard. No React dependency is currently used. |
| **Sidecar Process** | Python + FastAPI | Orchestrator: indexing, graph queries, prompt assembly, LLM calls. |
| **Storage Layer** | Neo4j + LanceDB + SQLite + FS | Concrete local defaults. History has a provider interface; retrieval protocols/fakes exist, while full GraphProvider/VectorProvider wrappers remain staged. |
| **Tenant API Contract Graph** | GraphProvider metadata layer (future Team layer) | Links project-published service/API manifests across a tenant without cross-project source scanning. |

**Default provider implementations:**

| Provider Family | Default | Alternatives |
|---|---|---|
| `GraphProvider` | Neo4j local Docker / customer Neo4j | NebulaGraph, Memgraph, dedicated managed graph service |
| `VectorProvider` | LanceDB local | Qdrant, Weaviate, pgvector, customer-managed vector services |
| `HistoryProvider` | SQLite local | encrypted SQLite, Postgres, enterprise audit store, memory-only, disabled |

**Current context_engine module boundaries:**

| Path | Responsibility |
|---|---|
| `context_engine/main.py` | Process bootstrap, stderr filtering, compatibility exports used by direct-call tests, and the `app` object. |
| `context_engine/api/app.py` | FastAPI factory, lifespan, per-app route dependencies, router registration. |
| `context_engine/api/routes/` | HTTP transport grouped by ask, indexing, search, history, overlay, auth, feedback, impact, and health. |
| `context_engine/api/schemas/` | Pydantic request/response contracts and public bounds. |
| `context_engine/api/state.py` | Process-level service construction and ownership. |
| `context_engine/ask/` | Context-provider fallback ladder and LLM/stream orchestration. |
| `context_engine/indexer/service.py` | Synchronous, queued, and git-delta indexing orchestration. |
| `context_engine/axis/` | Active intent, seed, graph-walk, ranking, and context-bundle pipeline. |

HTTP requests resolve route dependencies from `request.app.state`; the fallback
binding in the route modules exists only for tests that call handlers directly.

### 2.2. Inter-Process Communication
VS Code ↔ Sidecar via local FastAPI (HTTP/JSON). Ensures editor stays responsive even if a heavy Cypher query blocks the context_engine. Enables future replacement of Python binary with Rust without frontend changes.

### 2.3. Current Sidecar Endpoints

| Method | Path | Status |
|---|---|---|
| GET | `/health` | ✅ |
| POST | `/index` | ✅ |
| POST | `/index/docs` | ✅ |
| POST | `/index/file` | ✅ |
| POST | `/index/files` | ✅ |
| POST | `/index/git-delta` | ✅ |
| GET | `/index/git-delta/status` | ✅ |
| GET | `/index/queue` | ✅ |
| GET | `/index/manifest` | ✅ |
| POST | `/ask` | ✅ |
| POST | `/ask/axis` | ✅ (context-only diagnostic) |
| POST | `/ask/stream` | ✅ |
| POST | `/search` | ✅ |
| POST | `/search/unified` | ✅ |
| POST | `/overlay` | ✅ |
| DELETE | `/overlay` | ✅ |
| POST | `/feedback` | ✅ |
| POST | `/history/ask` | ✅ |
| GET | `/history/conversations` | ✅ |
| GET | `/history/conversations/{conversation_id}` | ✅ |
| GET | `/history/conversations/{conversation_id}/requests/{request_id}` | ✅ |
| GET | `/impact` | ✅ |
| POST | `/auth/token` | ✅ |
| GET | `/auth/users` | ✅ |
| GET | `/status/cloud` | ✅ |
| GET | `/audit/actions` | ✅ |
| GET | `/metrics` | ✅ |

### 2.3.1. Filesystem path sandboxing ✅

Local development often runs with `AUTH_REQUIRED=false`. Without path checks, any process on the machine could ask the context_engine to read or index arbitrary readable files.

**Rules** (implemented in `context_engine/workspace_paths.py` and `context_engine/api/workspace_security.py`, enforced by the route layer):

| Step | Behavior |
|---|---|
| Register root | `POST /index` resolves `project_path` and writes it to the index manifest immediately (including `queue=true`, via `register_workspace_project_root` before enqueue). |
| Resolve paths | `/ask` (`file_path`), `/index/file`, `/index/files`, `/index/docs`, and `/overlay` normalize relative paths under that root; absolute paths must still lie inside it (`Path.resolve()` + `relative_to`). |
| Reject | No manifest yet → HTTP `400`. Path escapes root → HTTP `403`. |

`workspace_paths` plus `sandbox_path()` enforce the same root check on caller-supplied paths and IDE anchors. Stale outside-root graph nodes are pruned at index time; overlay reads stay under the registered root. Details: [spec_sidecar_api.md](spec_sidecar_api.md#filesystem-path-sandboxing).

### 2.3.2. API request bounds ✅

Public request models enforce bounded resource use (local DoS and cloud cost protection):

| Parameter | Typical use | Server range |
|---|---|---|
| `limit` | `/search`, `/search/unified` | 1–50 |
| `token_budget` | `/ask`, unified search graph leg | 400–32 000 |

Out-of-range values return HTTP 422 before vector search or context assembly runs.

---

### 2.4. Observability (current)

The system's value proposition rests on three measurable claims: **<200ms context assembly**, **60–80% token reduction**, and **3–5× cost savings**. The axis benchmark (`QA/axis_benchmark.py`) measures **`file_recall`**, seed/pool recall, token cost, and assembly latency. Runtime metrics, trace IDs, and prompt-contract metadata exist; remaining work is extension surfacing and local release SLO checks.

**Axis retrieval ✅ implemented** (`run_axis_retrieval` in `context_engine/axis/pipeline.py`)
- **Intent:** structural intent roles via embedding classifier (`classify_intent`)
- **Seeds:** role + vector retrieval with file-tier weights (`file_tier_signal.md`)
- **Pool:** graph walks (`walk_neighbours`) — structural, inheritance, impact/trace, cross-role
- **Context:** per-candidate expansion + LanceDB code fetch + overlay dirty buffers
- **Prompt:** `axis_bundles_to_prompt_context` → `PromptContext.to_system_prompt()`

**DocAnchor confidence/type scoring 🚧 indexing complete, active consumption partial**
- Markdown `COVERS` edges persist `anchor_type`, `confidence`, `primary_bias`, and `resolver`.
- In-code docstring/JSDoc rows carry `owner_uid`, seed axis retrieval, and feed a bounded reverse-`USES_TYPE` bridge.
- The normal axis `PromptContext` path does not yet attach general markdown docs or propagate their anchor quality; calibration and UI surfacing remain open.

**Prompt-contract observability ✅ implemented baseline**
- ✅ **Basic scores**: `{graph_relevance, semantic_score}` per candidate
- ✅ **Provenance**: why each symbol was selected
- ✅ **Budget metadata**: `{limit, spent, reserved, pruned_count}`
- ✅ **Pruned details**: skipped candidates with reason codes
- ✅ **Ranker counts** in `metadata.ranker` plus assembly timings; the removed cascade's weight snapshot is no longer emitted by the active axis path
- ✅ **Provider protocols (v1)**: retrieval protocol fakes for tests
- 🚧 Remaining work: propagate live axis scores, intent distribution, pruning details, and ranked docs instead of relying on serializer defaults

**Supporting Infrastructure:**
- **Structured logs**: per pipeline stage with `trace_id`, `phase`, `duration_ms`
- **Metrics endpoint** (`GET /metrics`): request/stage histograms and counters, index queue counters, token/cost estimates, feedback, cache, and overlay gauges
- **Axis eval (P7):** `file_recall` on `expected_files` per question pack — no YAML `required_roles` (removed 2026-06)
- **Benchmark artifacts:** `QA/axis_benchmark.py` → `summary.json` with seed/pool/bundle recall layers

Without complete prompt-contract observability, production claims in §1.3 remain hard to debug outside benchmark runs.

---

### 2.5. Extension User Interface Layer

The VS Code extension provides a thin UI layer that exposes the context_engine's capabilities through four integrated surfaces: **Chat Panel**, **Context Inspector**, **Impact Explorer**, and **Dashboard**. The UI is deliberately transparent — users can see the exact code symbols, documentation, and metadata sent to the LLM, with token accounting and evidence trails.

**Surfaces:**

| Surface | Purpose | Type |
|---|---|---|
| **Chat Panel** | Default entry point; ask about current symbol with streaming response | WebviewView (sidebar) |
| **Context Inspector** | Inspect primary source, graph neighbors, docs, prompt JSON, token breakdown | WebviewPanel (modal) |
| **Impact Explorer** | Show callers, callees, dependencies, docs, and AFFECTS for a symbol | TreeView + WebviewPanel |
| **Dashboard** | Operational overview: context_engine health, indexing status, token savings, recent activity | WebviewPanel |

**Protocol:**

All UI communication flows through a message bridge:
- **Webview → Extension Host:** Typed messages (`chat.ask`, `chat.retry`, `accordion.toggled`, etc.)
- **Extension Host → Webview:** State updates and streaming responses
- **Extension Host → Sidecar:** HTTP/JSON API calls (proxied from webview requests)

This layering ensures webviews remain stateless and dumb; all business logic stays in the extension host and context_engine.

**Key Design Decisions:**

1. **Chat Layout:** composer docked to the bottom, response area above it, secondary info groups collapsed by default. This keeps the active task visually primary while preserving transparency.
2. **Evidence First:** The context inspector is a peer to the answer, not buried in a menu. Users can immediately verify retrieval quality and spot gaps.
3. **Dirty Awareness:** Unsaved editor content is sent via `POST /overlay` before each ask, so the answer includes in-memory changes.
4. **State Separation:** Session state (composer text, expanded groups) is retained per surface; request state (streaming progress, context) is ephemeral.

**Specs:**

- [spec_vscode_extension_ui.md](spec_vscode_extension_ui.md) — Complete UI contract: surfaces, layout rules, state model, interaction flows.
- [spec_webview_components.md](spec_webview_components.md) — Component tree, messaging protocol, DTOs, accessibility rules.
- [spec_package_contributes.md](spec_package_contributes.md) — VS Code manifest: views, commands, menus, keybindings, configuration.

---

## Section 3: Data Processing Pipelines

### 3.1. Extract — Change Monitoring
- **Git Integration (macro):** subscribes to `.git` events; on checkout/commit, reconciles local index with the configured graph provider.
- **LSP / File Watcher (micro):** `onDidChangeTextDocument` / `onDidSaveTextDocument` events feed the In-Memory Overlay in real time.

### 3.2. Transform — Analysis & Enrichment

**Syntactic (AST):**
- Symbol extraction: functions, classes, line coordinates, content hash.
- Call graph: typed function calls — `CALLS_DIRECT` (static), `CALLS_DYNAMIC` (dispatch), `CALLS_INFERRED` (string-based). Resolved within the same project.
- UID v2: `sha256(language:qualified_name|normalized_signature)[:16]` — stable across machine paths and disambiguates overloads/nested scopes.
- AFFECTS index: reverse dependency materialization (depth ≤ 4) for cascade-aware incremental reindexing.

**Semantic (Docs):**
- Chunking: section-aware (split on `#`/`##`/`###` headings); word-window fallback (400 words, 80 overlap) for oversized sections.
- Embedding: `all-MiniLM-L6-v2` (384-dim) via `sentence-transformers`. Similarity threshold: 1.5 (cosine distance scale 0–2).
- Symbol body embeddings: `symbols` LanceDB table (`uid, name, file_path, code, vector`) for semantic DocAnchor matching.
- Entity linking → DocAnchor nodes in Neo4j with rich FROM/COVERS relationships:
  - `[:FROM {type: "doc"}]` — source doc file
  - `[:FROM {type: "code"}]` — code files containing covered symbols
  - `[:FROM {type: "spec"|"architecture"|"concept"|"idea"}]` — referenced project docs
  - `[:COVERS {anchor_type, confidence, primary_bias, resolver}]` — code symbols mentioned in chunk, with link quality metadata for ranking
  - Lazy `pending` resolution for forward references (symbols indexed after docs)

**API Contracts:**
- **Implemented local slice:** Python FastAPI/Flask and TypeScript/JavaScript HTTP route/client facts are normalized to workspace-local `ApiEndpoint` nodes with `IMPLEMENTS_ENDPOINT` and `CALLS_ENDPOINT` edges.
- **Planned expansion:** OpenAPI/Swagger, GraphQL SDL, protobuf/gRPC, AsyncAPI, generated clients, gateway metadata, and service catalogs inside the current workspace.
- Local output: a `ContractManifest` containing safe service, endpoint, schema, event, and call-site metadata for this project.
- Tenant output: links between published manifests, such as `CALLS_ENDPOINT`, `EXPOSES_ENDPOINT`, `USES_SCHEMA`, `PRODUCES_EVENT`, `CONSUMES_EVENT`, and `DEPENDS_ON_SERVICE`.
- Explicit boundary: the context_engine never scans neighboring repositories from another project's context. Neighboring projects publish their own facts; the tenant graph only connects those facts.

### 3.3. Load — Incremental Upsert
- **GraphProvider:** upsert by stable UID — only changed nodes/edges are written. Neo4j is the current default implementation.
- **Workspace:** File nodes, CONTAINS edges, call edges, AFFECTS edges, and graph reads are scoped by `workspace_id`.
- **Current caveat:** changed files are handled by deleting their workspace-local file edges and re-upserting extracted symbols; symbol-level diffing is still deferred.
- **VectorProvider:** delete-then-insert per file on re-index. LanceDB is the current default implementation.
- **HistoryProvider:** append-only conversations, messages, ask snapshots, inspector snapshots, and impact snapshots. SQLite local is the default; `ephemeral` and `disabled` modes are available for local product policy.
- **Recovery:** `/index/file` writes an indexing job record before mutating stores, then marks success, failed, or dead-letter state so partial graph/vector failures are visible and retryable.

### 3.4. Dirty State Handling ✅ Implemented
`InMemoryOverlay` holds `{(workspace_id, user_id, file_path): entry}`:
- Re-parses symbols on the fly via tree-sitter — no disk I/O.
- Axis context builder merges dirty overlay bodies at fetch time (`axis/overlay_context.py`).
- Cleared on file save/editor close and bounded by `OVERLAY_MAX_ENTRIES` (default 256) plus `OVERLAY_TTL_SECONDS` (default 86,400 seconds).

### 3.5. Pipeline Priority Queue
| Priority | Trigger | Action |
|---|---|---|
| 1 — Instant | User question | Current file + direct deps only |
| 2 — High | File save | Update graph for saved file |
| 3 — Background | Cold start / git pull | Full repo re-index |

---

## Section 4: Core Workflows

### 4.1. Prompt Lifecycle
1. VS Code sends `POST /ask` with `{symbol?, file_path?, question, token_budget}`. When `file_path` is present, it is resolved under the workspace project root before any disk read.
2. Sidecar resolves user identity plus `X-Workspace` (default: `local/surgical_context@main` for development).
3. **Intent classification** (axis `classify_intent`): structural intent roles drive seed/pool routing and file-tier sign (impact vs behaviour).
4. **Axis retrieval** (`run_axis_retrieval`): workspace scan → role/vector seeds → graph pool passes → cross-role ranking → per-candidate context expansion → `ContextBundle` list.
5. **Prompt adaptation** (`axis_bundles_to_prompt_context`): bundles → `PromptContext` with provenance, tier tokens, assembly metadata.
6. **Fallback ladder** (when axis renders nothing or `ASK_AXIS_FIRST=false`): file → workspace vector → direct LLM (`AskContextBuilder.resolve_ask_context`). Disabling axis does not restore the deleted ranking cascade.
7. **Overlay merge:** dirty editor buffers from `InMemoryOverlay` override LanceDB bodies at fetch time (`axis/overlay_context.py`).
8. **LLM call:** if tiers are empty → direct mode. Else → `PromptContext.to_system_prompt()` + Ollama/local or optional cloud route.
9. Response: `{symbol, answer, context}` — full prompt contract with `intent_details`, scores, provenance, `metadata.assembly`, `feedback_token`.
10. **Streaming**: `/ask/stream` — JSON-safe SSE (`chunk`, `context`, `error`, `done`).

### 4.2. Cold Start
1. FS scan for `.py`/`.ts`/`.tsx` files (gitignore-aware, dirs pruned).
2. Phase 1: extract all symbols (functions, classes, UPPER_CASE variables) → upsert nodes.
3. Phase 2: extract all calls → upsert typed call edges (`CALLS_SCOPED`, `CALLS_IMPORTED`, `CALLS_DYNAMIC`, `CALLS_INFERRED`, fallback `CALLS_GUESS` only when unique).
4. Phase 3: embed symbol code bodies → LanceDB `symbols` table.
5. Phase 4: resolve pending DocAnchors against newly indexed symbols.
6. Doc indexing (separate trigger): section-aware chunk + embed all `.md` → LanceDB + DocAnchor graph.
7. Ready signal to VS Code.

### 4.3. Version Arbitration (Dirty State)
Scenario: user edits `process_payment`, hasn't saved.
1. VS Code sends `POST /overlay` with file content on every keypress.
2. On `POST /ask`, the context_engine detects overlay for this file.
3. Reads dirty symbol body from memory; all other dependencies from stable Neo4j graph.
4. LLM sees current work-in-progress surrounded by stable project structure.

### 4.4. Model Routing
- Default: **Ollama** (`MODEL_PREFERENCE=ollama`, `ALLOW_CLOUD_LLM=false`) — assembled context stays on the machine.
- **Cloud opt-in:** Anthropic runs only when `ALLOW_CLOUD_LLM=true`, `ANTHROPIC_API_KEY` is set, and `MODEL_PREFERENCE` is `auto` or `claude`. A key alone does not enable cloud.
- `AIEngine` (`context_engine/ai/engine.py`) scores intent + token count: large/complex contexts and design/exploration/refactor intents prefer Claude when cloud is allowed; otherwise Ollama.
- Default Anthropic model: **`claude-sonnet-4-6`** (`ANTHROPIC_MODEL` env). Retired `claude-sonnet-4-20250514` must not be used after 2026-06-15.
- Fallback: Claude failures fall back to Ollama; unreachable LLM → degraded `/ask` and `/ask/stream` still return `context`.

---

## Section 5: Data Schema

### 5.1. GraphProvider Node Labels

| Label | Properties | Description |
|---|---|---|
| File | `path, hash, last_indexed` | Repository file, entry point for indexing |
| Symbol | `uid, name, kind, range, hash, token_estimate` | Atomic code unit (function/class/variable) |
| DocAnchor | `chunk_id` | Doc chunk key — navigates to File via [:FROM], to symbols via [:COVERS] |
| ApiEndpoint | `workspace_id, fingerprint, method, path` | Workspace-local HTTP client/handler bridge. |
| Commit | `hash, author, timestamp, branch` | Version node for time-travel context (planned). |

**Planned tenant API labels:**

| Label | Properties | Description |
|---|---|---|
| Service | `service_id, tenant_id, workspace_id, name, owner, repo, version` | Project-published service identity |
| ApiEndpoint | `operation_id, method, path, protocol, version, deprecated` | HTTP/GraphQL/RPC operation |
| ApiSchema | `schema_id, name, format, version, schema_hash` | Request/response/event schema |
| ApiField | `field_id, name, type, required, sensitivity` | Optional schema-field granularity |
| EventTopic | `topic_id, name, broker, version` | Published/consumed event stream |
| ExternalSystem | `system_id, tenant_id, name, kind` | SaaS/vendor/system without a project index |
| ContractManifest | `manifest_id, workspace_id, graph_version, published_at` | Immutable publication unit from one project |

### 5.2. Relationships

| Type | Direction | Description |
|---|---|---|
| CONTAINS | (File)→(Symbol) | Symbol belongs to file |
| CALLS_DIRECT | (Symbol)→(Symbol) | Static function call |
| CALLS_DYNAMIC | (Symbol)→(Symbol) | Dynamic/receiver-based call |
| CALLS_INFERRED | (Symbol)→(Symbol) | Heuristic or reflection-like call |
| DEPENDS_ON | (Symbol)→(Symbol) | Inheritance/type dependency |
| IMPORTS | (File)→(File) | Internal project import |
| AFFECTS | (Symbol)→(Symbol) | Reverse dependency materialization |
| FROM | (DocAnchor)→(File) | Doc chunk origin — `type` property: `"doc"` (source doc file), `"code"` (code file containing covered symbols), `"spec"` / `"architecture"` / `"concept"` / `"idea"` (referenced project docs) |
| COVERS | (DocAnchor)→(Symbol) | Doc chunk describes this code symbol; properties: `anchor_type`, `confidence`, `primary_bias`, `resolver` |
| MODIFIED_IN | (Symbol)→(Commit) | Symbol change history (planned) |

**Current project-local API relationships:**

| Type | Direction | Description |
|---|---|---|
| IMPLEMENTS_ENDPOINT | (Symbol)→(ApiEndpoint) | Local handler implements a normalized HTTP endpoint. |
| CALLS_ENDPOINT | (Symbol)→(ApiEndpoint) | Local client calls a normalized HTTP endpoint. |

**Planned tenant API relationships:**

| Type | Direction | Description |
|---|---|---|
| PUBLISHES_SERVICE | (Workspace)→(Service) | Workspace owns/publishes a service manifest |
| EXPOSES_ENDPOINT | (Service)→(ApiEndpoint) | Service offers an operation |
| IMPLEMENTS_ENDPOINT | (Symbol/File)→(ApiEndpoint) | Current-project code implements an endpoint |
| USES_SCHEMA | (ApiEndpoint/EventTopic)→(ApiSchema) | Operation/topic uses a schema |
| HAS_FIELD | (ApiSchema)→(ApiField) | Field-level schema structure |
| PRODUCES_EVENT | (Service)→(EventTopic) | Service publishes an event |
| CONSUMES_EVENT | (Service)→(EventTopic) | Service consumes an event |
| DEPENDS_ON_SERVICE | (Service)→(Service/ExternalSystem) | Derived service dependency |
| VERSION_OF | (ApiEndpoint/ApiSchema)→(ApiEndpoint/ApiSchema) | Contract version lineage |
| BREAKS_CONTRACT | (ContractManifest)→(ApiEndpoint/ApiSchema) | Compatibility warning from contract diff |

Tenant API graph edges are metadata-only. They carry tenant/workspace scope, confidence, resolver, and publication timestamps, but never raw source code, request/response payloads, secrets, or auth material.

### 5.3. JSON Prompt Contract

✅ Implemented — `PromptContext.to_dict()` in `context_engine/context_types.py`. Returned under the `"context"` key in `/ask` responses and reused by the benchmark as `ready_context.contract`.

```json
{
  "primary_source": {
    "symbol": "string",
    "file_path": "string",
    "is_dirty": false,
    "code": "string"
  },
  "graph_context": [
    { "symbol": "string", "file_path": "string", "relation": "CALLS", "is_dirty": false, "code": "string" }
  ],
  "documentation": [
    {
      "chunk_id": "string",
      "source_file": "string",
      "content": "string",
      "anchor_type": "definition",
      "anchor_confidence": 0.92,
      "primary_bias": 1.0
    }
  ]
}
```

**Serializer surface:** `mode`, `intent`, `intent_details`, `metadata.query_intent`, `metadata.tiers_used`, `metadata.tier_tokens`, dependency `depth` / `direction`, per-candidate `scores`, `provenance`, doc-anchor fields, `pruned[]`, `stopped_reason`, ranker counts, and `metadata.assembly` fields such as `trace_id`, `workspace_id`, `context_pipeline_version`, `cache_hits`, `model_route`, and `feedback_token`.

**Current propagation caveat:** the active axis adapter reliably populates the selected symbols, code, relation/provenance basics, workspace, trace, and render mode. Several richer fields (score decomposition, intent distribution, pruning details, and general markdown documentation/anchor quality) are still sparse or default-valued.

**Planned metadata:** `tenant_api_context`, `api_direction`, `tenant_link_depth`, service/contract provenance, and tenant API candidate scores. See [spec_tenant_api_graph.md](spec_tenant_api_graph.md).

**Known gap:** branch identity is encoded inside `workspace_id` rather than exposed as a separate prompt field. General vector-only docs carry empty/zero anchor defaults, and the active axis success path currently returns symbol bundles without a populated `documentation` tier.

### 5.4. BFS Retrieval Cypher

```cypher
MATCH (s:Symbol {uid: $uid})-[r:CALLS|CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES]-(n:Symbol)
WHERE NOT n.uid IN $visited
OPTIONAL MATCH ()-[:CALLS|CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED]->(n)
OPTIONAL MATCH (fn:File)-[:CONTAINS]->(n)
RETURN n.uid AS uid, n.name AS name, fn.path AS file_path, type(r) AS rel_type
```

The current traversal is priority-queue BFS constrained by `token_budget`. Depth is an outcome of budget and score, not a fixed `*1..2` Cypher expansion.

---

### 5.5. Incremental Indexing

Current `/index` collects files, compares hashes against stored `File.hash`, and only re-indexes changed files. `/index/file` supports explicit single-file updates.

1. Client saves a file → `POST /index/file { path }`.
2. Sidecar hashes the file; compares to stored `File.hash`. Unchanged → no-op.
3. Re-parse file; compute hash for each extracted symbol.
4. Create a durable indexing job record for retry/dead-letter tracking.
5. Current implementation deletes symbols for changed files, then re-upserts extracted symbols.
6. Re-link calls/imports/inheritance for the changed file.
7. Re-embed modified symbols into LanceDB `symbols` table.
8. Resolve pending DocAnchors after the code update.
9. Mark the indexing job `succeeded`, `failed`, or `dead_letter`.
10. The queued path coalesces duplicate saves, applies bounded backpressure, rebuilds AFFECTS once per batch, runs axis propagation/adjacency finalization, resolves anchors, and invalidates retrieval caches. Durable job retries/dead-letter state remain separate from queue coalescing.

---

## ADR-001: Separation of Graph Topology and Source Code Content
**Status:** Accepted

Store only topology in the graph provider. Symbol node contains identity and structural metadata, not source bodies; navigate via `(File)-[:CONTAINS]->(Symbol)`. DocAnchor nodes keep `chunk_id` while text lives in LanceDB. The active axis path reads indexed symbol bodies from LanceDB and overlays unsaved buffers from memory; file fallback may read sandboxed source from disk.

**Why:** Keeps graph storage lightweight for fast topology queries. Source code never goes to the graph provider. Only `hash` update needed when function body changes without structural impact.

**Trade-off:** Extra disk I/O per prompt assembly. Mitigated by OS file cache. Hash mismatch = dirty flag = re-parse.

---

## ADR-003: Pluggable Graph Provider + Local Dirty Overlay
**Status:** Accepted, staged

Local Neo4j or Aura/fallback supplies the current graph. A complete `GraphProvider` connector abstraction and alternate backends remain staged. Local unsaved changes remain in `InMemoryOverlay` inside the context_engine process. ✅ Overlay implemented.

**Why:** Teams need one source of truth, but storage ownership varies by customer. Local edits don't pollute the configured graph provider. No full re-index per developer.

**Trade-off:** Provider abstraction adds capability checks and conformance testing. Query features must stay inside the provider contract, not vendor-specific assumptions.

---

## ADR-004: Automatic Model Routing by Task Complexity
**Status:** Accepted and partially implemented

Intent + context-size classifier routes requests to appropriate model tier in `context_engine/ai/engine.py`.

**Why:** Top-tier models for all requests is economically wasteful. Simple queries can be answered cheaper/faster locally.

**Trade-off:** Must maintain multiple provider contracts and fallback behavior.

---

## ADR-005: LanguageAdapter Protocol
**Status:** Accepted and implemented for Python/TypeScript adapters

All language-specific logic (tree-sitter queries, call resolution, identifier conventions) lives behind a `LanguageAdapter` protocol. New languages (Go, Rust, Java) are added by implementing the protocol — no edits to the indexer, axis pipeline, or extractor core.

Required methods:
- `extract_symbols(tree, source) -> list[Symbol]`
- `extract_calls(tree, source) -> list[Call]`
- `file_extensions() -> set[str]`
- `is_upper_case_constant(name) -> bool`

**Why:** The Risk Register flags multi-language support as high-complexity. Without a stable extension point, every new language forces edits to core modules, which grows surface area faster than test coverage.

**Trade-off:** Slight indirection cost; adapters must be kept in sync when core `Symbol` schema evolves.

---

## ADR-006: Quality Gates Before Managed Release
**Status:** Accepted

Managed deployments and marketplace readiness are blocked on the Local Developer Product becoming a reliable daily driver. Cloud/local graph provider fallback exists, but the current release gates are local setup, local history, extension polish, prompt-contract observability, and smoke-testable ask/inspect/impact workflows.

**Why:** The project's value proposition is measurable precision and cost savings. Scaling before the local loop is durable means scaling an unverified product.

**Trade-off:** Slower path to the enterprise story. Accepted — local daily-driver first.

---

## ADR-007: Project-Owned Indexing + Tenant API Links
**Status:** Future Team/Enterprise

Each project indexes itself and publishes safe API contract metadata into a tenant-scoped graph. Tenant-level retrieval traverses only those published facts. It does not scan neighboring repositories, read neighboring source files, or invoke live APIs.

**Why:** Microservice architectures need cross-project context, but source ownership and privacy boundaries are non-negotiable. Published service manifests let teams connect endpoints, schemas, clients, events, and ownership without turning one context_engine into a tenant-wide crawler.

**Design:**
- Project indexers extract local API facts: route declarations, OpenAPI/GraphQL/protobuf/AsyncAPI contracts, generated client calls, SDK usage, event topics, and service metadata.
- Tenant linking connects manifests by stable fingerprints: endpoint signature, schema hash, event topic, service alias, and gateway/catalog identity.
- Retrieval accepts direction and traversal policy: `api_direction` (`outbound_dependencies`, `inbound_consumers`, `contract_impact`, `internal_processing`, `bidirectional_contract`) and `tenant_link_depth` (default 1, hard-capped at 2).
- Scoring extends unified ranking with direction weight, scope weight, depth decay, edge type weight, and confidence.

**Trade-off:** The tenant graph can only be as complete as project-published manifests. If a neighboring service has not indexed/published its API facts, retrieval should surface a low-confidence or missing-contract state rather than attempting to inspect that service directly.

---

## ADR-008: Storage Provider Connectors
**Status:** Proposed, staged

Graph, vector, and user-history storage live behind provider connector interfaces. Neo4j, LanceDB, and SQLite are defaults, not product-level requirements.

**Why:** Customers differ on database standards, procurement, monitoring, privacy, and deployment models. A startup may accept local Docker defaults; a company may require its own Neo4j/Qdrant/Postgres; a larger customer may require dedicated managed storage in its cloud account. The product should preserve retrieval behavior while letting storage ownership vary.

**Design:**
- `GraphProvider` stores topology and metadata: symbols, files, edges, workspaces, DocAnchors, AFFECTS, and tenant API links.
- `VectorProvider` stores semantic retrieval indexes: docs, symbol embeddings, embedding metadata, and pending references.
- `HistoryProvider` stores product UX state: conversations, messages, ask snapshots, inspector snapshots, impact snapshots, and retention metadata.
- Local storage modes include `local`, `local_docker`, `ephemeral`, and `disabled`; customer-managed, dedicated, and enterprise-audit modes are future implementations.
- Privacy policy sits above connectors. Connectors receive already-approved payloads; they do not decide whether raw prompts, source snippets, responses, or audit data may be stored.

**Trade-off:** Provider abstraction slows early implementation and requires conformance tests. Accepted, because it prevents Neo4j/LanceDB/SQLite from becoming accidental lock-in and keeps enterprise deployment options credible.

**Staging:** local v0.1 only needs provider boundaries around Neo4j, LanceDB, and SQLite. Real alternate backends move to the Team/Enterprise horizon after those contracts are stable.

---

## Phase 4: Mechanism-Aware Retrieval Evaluation ✅ COMPLETE

**Goal:** Shift from "question pass rate optimization" to "mechanism coverage diagnosis." Classify questions by mechanism (what kind of code relationship they test: route registration, dependency injection, validation bridge, etc.) and evaluate using role-based recall + intent-stratified pass gates. This enables identifying whether failures are architectural gaps (unfixable by tuning) vs. ranking improvements (tunable).

### 4.1. Mechanism Classification
Every question in the real-repo pack is annotated with:
- **mechanism**: The code relationship being tested (e.g., `fastapi_route_registration`, `pydantic_validation_core_bridge`, `rtk_slice_generation`)
- **expected_mode**: Either `symbol` (should find by name) or `workspace` (correct answer is "not found") — human annotation only; axis benchmark does not score it.

Legacy cascade field **`required_roles`** was removed from question packs (2026-06). Axis eval uses **`file_recall`** on `expected_files` only.

This enables the benchmark to report *which mechanisms the ranker handles well* and *which gaps are actual code relationship discovery failures* vs. ranking noise.

### 4.2. Axis eval (current)

- **Metric:** `file_recall` on `expected_files` (`QA/axis_benchmark.py`, P7 gate). Also reports seed/pool/bundle recall layers.
- **Intent:** structural intent roles (`classify_intent`) drive retrieval mode; file-tier weights demote/promote by intent ([file_tier_signal.md](file_tier_signal.md)).
- **Mechanism labels** in YAML (`mechanism: fastapi_route_registration`, …) are human taxonomy only — not scored.
- **Structural profiles:** [question_structural_role_profiles.md](question_structural_role_profiles.md) is a design draft; `+`/`-` roles are not in fixtures yet.

Removed with cascade (2026-06): YAML `required_roles`, `role_recall`, intent-stratified role+file gates, `UnifiedRanker` noise_factor floors.
