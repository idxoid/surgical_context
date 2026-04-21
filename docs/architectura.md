# Surgical Context — Architecture

> **Status:** MVP pipeline exists and post-MVP correctness hardening is active. Code indexing, typed call edges, stable UID v2, scoped call resolution, workspace/branch-scoped graph queries, AFFECTS, doc enrichment, model routing, cloud/local fallback, audit logging, request-scoped DB sessions, durable index job logging, opt-in bearer auth enforcement, VS Code extension scaffold, and **complete UI specification** (Chat Panel, Context Inspector, Impact Explorer, Dashboard) are present. The main open gaps are production auth policy/secret management, prompt-contract observability, full metrics endpoint, extension UI implementation, and backpressure hardening. See [road_map.md](road_map.md) for the canonical backlog and [project_gap_analysis.md](project_gap_analysis.md) for the analysis index.

## Section 1: Executive Summary & Goals

### 1.1. Project Overview
Surgical Context is an intelligent Context Gateway for VS Code that enhances LLM accuracy and reduces token costs through graph-based dependency analysis.

Instead of "carpet-bombing" the model with all open files, the system feeds only the specific code snippets and documentation fragments that are mathematically relevant to the user's current task.

### 1.2. The Problem
1. **Context Noise** — irrelevant code confuses the model and causes hallucinations.
2. **Token Inefficiency** — superfluous data inflates cost and hits rate limits.
3. **Knowledge Silos** — AI misses connections between code and docs unless both are open.

### 1.3. Success Metrics
- **Precision:** reduce transmitted code by 60–80% with equal or better answer quality.
- **Cost:** lower average token cost 3–5× via surgical selection + model routing.
- **Latency:** context assembly (Graph + Vector + FS) under 200ms.
- **Team Velocity:** shared SaaS graph accelerates onboarding.

### 1.4. Design Principles
- **Ownership over Hype:** robust data infrastructure, not an API wrapper.
- **Security by Design:** source code never leaves the local machine for storage.
- **Transparency:** user always sees what context was collected and what it cost.

---

## Section 2: System Architecture

### 2.1. Components

| Component | Stack | Role |
|---|---|---|
| **Extension Host** | TypeScript / VS Code API | Manages sidecar lifecycle, proxies webview messages to sidecar, manages file watchers and overlays. |
| **Webviews** | TypeScript / React (proposed) | Render Chat Panel, Context Inspector, Impact Explorer, Dashboard. No business logic — dispatch to extension host. |
| **Sidecar Binary** | Python + FastAPI | Orchestrator: indexing, graph queries, prompt assembly, LLM calls. |
| **Storage Trinity** | Neo4j + LanceDB + FS | Hybrid storage — each data type in its optimal environment. |

### 2.2. Inter-Process Communication
VS Code ↔ Sidecar via local FastAPI (HTTP/JSON). Ensures editor stays responsive even if a heavy Cypher query blocks the sidecar. Enables future replacement of Python binary with Rust without frontend changes.

### 2.3. Current Sidecar Endpoints

| Method | Path | Status |
|---|---|---|
| GET | `/health` | ✅ |
| POST | `/index` | ✅ |
| POST | `/index/docs` | ✅ |
| POST | `/ask` | ✅ |
| POST | `/ask/stream` | ✅ |
| POST | `/search` | ✅ |
| POST | `/overlay` | ✅ |
| DELETE | `/overlay` | ✅ |
| POST | `/index/file` | ✅ |
| GET | `/impact` | ✅ |
| POST | `/auth/token` | ✅ |
| GET | `/auth/users` | ✅ |
| GET | `/status/cloud` | ✅ |
| GET | `/audit/actions` | ✅ |
| GET | `/metrics` | 🔴 Planned — Prometheus text format |

---

### 2.4. Observability (Partially Implemented)

The system's value proposition rests on three measurable claims: **<200ms context assembly**, **60–80% token reduction**, and **3–5× cost savings**. The QA benchmark measures retrieval quality, token reduction, and assembly latency, but runtime observability is still incomplete. The next observability layer adds:

- **Structured logs** per pipeline stage with fields: `trace_id`, `phase`, `duration_ms`, `symbols_in`, `symbols_out`, `tokens_estimated`.
- **Metrics endpoint** (`GET /metrics`): index duration histogram, `/ask` p50/p95/p99, token counts, cache hit rates.
- **Token baselines**: every `/ask` logs both the surgical token count and an estimate of the "carpet-bomb" equivalent (all open files). The delta is the core KPI.
- **Retrieval recall@k** measured against a golden fixture set on every CI run.

Without runtime metrics and prompt-contract observability, production claims in §1.3 remain hard to validate outside benchmark runs.

---

### 2.5. Extension User Interface Layer

The VS Code extension provides a thin UI layer that exposes the sidecar's capabilities through four integrated surfaces: **Chat Panel**, **Context Inspector**, **Impact Explorer**, and **Dashboard**. The UI is deliberately transparent — users can see the exact code symbols, documentation, and metadata sent to the LLM, with token accounting and evidence trails.

**Surfaces:**

| Surface | Purpose | Type |
|---|---|---|
| **Chat Panel** | Default entry point; ask about current symbol with streaming response | WebviewView (sidebar) |
| **Context Inspector** | Inspect primary source, graph neighbors, docs, prompt JSON, token breakdown | WebviewPanel (modal) |
| **Impact Explorer** | Show callers, callees, dependencies, docs, and AFFECTS for a symbol | TreeView + WebviewPanel |
| **Dashboard** | Operational overview: sidecar health, indexing status, token savings, recent activity | WebviewPanel |

**Protocol:**

All UI communication flows through a message bridge:
- **Webview → Extension Host:** Typed messages (`chat.ask`, `chat.retry`, `accordion.toggled`, etc.)
- **Extension Host → Webview:** State updates and streaming responses
- **Extension Host → Sidecar:** HTTP/JSON API calls (proxied from webview requests)

This layering ensures webviews remain stateless and dumb; all business logic stays in the extension host and sidecar.

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
- **Git Integration (macro):** subscribes to `.git` events; on checkout/commit, reconciles local index with Neo4j SaaS.
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
  - `[:COVERS]` — code symbols mentioned in chunk
  - Lazy `pending` resolution for forward references (symbols indexed after docs)

### 3.3. Load — Incremental Upsert
- **Neo4j:** `MERGE` on uid — only changed nodes/edges are written.
- **Workspace:** File nodes, CONTAINS edges, call edges, AFFECTS edges, and graph reads are scoped by `workspace_id`.
- **Current caveat:** changed files are handled by deleting their workspace-local file edges and re-upserting extracted symbols; symbol-level diffing is still deferred.
- **LanceDB:** delete-then-insert per file on re-index.
- **Recovery:** `/index/file` writes an indexing job record before mutating stores, then marks success, failed, or dead-letter state so partial graph/vector failures are visible and retryable.

### 3.4. Dirty State Handling ✅ Implemented
`InMemoryOverlay` holds `{(workspace_id, file_path): raw_content}`:
- Re-parses symbols on the fly via tree-sitter — no disk I/O.
- `CodeResolver` checks overlay before disk during context assembly.
- Cleared on file save or editor close (TTL = session).

### 3.5. Pipeline Priority Queue
| Priority | Trigger | Action |
|---|---|---|
| 1 — Instant | User question | Current file + direct deps only |
| 2 — High | File save | Update graph for saved file |
| 3 — Background | Cold start / git pull | Full repo re-index |

---

## Section 4: Core Workflows

### 4.1. Prompt Lifecycle
1. VS Code sends `POST /ask` with `{symbol, question}`.
2. Sidecar resolves user identity plus `X-Workspace` (default: `local/surgical_context@main` for development).
3. **Intent classification**: detect query intent (navigation, debugging, refactor, exploration, new feature, design question) → choose tier priority order.
4. **Graph expansion** (`GraphExpander`): BFS from target symbol through workspace-scoped typed edges (CALLS_DIRECT, CALLS_SCOPED, CALLS_IMPORTED, CALLS_DYNAMIC, CALLS_INFERRED, CALLS_GUESS, DEPENDS_ON, IMPLEMENTS, OVERRIDES, REFERENCES) constrained by token budget + depth limit. Returns priority-scored subgraph.
5. **Deduplication** (`ContextDeduplicator`): remove redundant symbols and overlapping doc chunks.
6. **Code resolution** (`CodeResolver`): read from `InMemoryOverlay` (if dirty) or disk for each symbol. Tracks `is_dirty` flag per symbol.
7. **Doc retrieval** (`DocResolver`): semantic search in LanceDB `docs` table → top-k chunks. Matched chunks have `[:COVERS]` edges to code symbols.
8. **Prompt assembly** (`PromptCompiler`): rank tiers by intent (code → cross-refs → specs → architecture → concepts → ideas), fill budget in order.
9. **LLM call**: if tiers are empty → "standard mode" (bare query, no context). Else → `PromptContext.to_system_prompt()` + response from Ollama/Claude.
10. Response: `{symbol, answer, context}` — `context` is the full JSON Prompt Contract.
11. **Streaming**: `/ask/stream` provides JSON-safe SSE responses with `chunk`, `context`, `error`, and `done` events.

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
2. On `POST /ask`, `ContextArbitrator` detects overlay for this file.
3. Reads dirty symbol body from memory; all other dependencies from stable Neo4j graph.
4. LLM sees current work-in-progress surrounded by stable project structure.

### 4.4. Model Routing
- Pre-score intent + context token count.
- Small context + simple question → Ollama.
- Large context or complex intent → Claude when `ANTHROPIC_API_KEY` is configured.
- Fallback: Claude failures fall back to Ollama.

---

## Section 5: Data Schema

### 5.1. Neo4j Node Labels

| Label | Properties | Description |
|---|---|---|
| File | `path, hash, last_indexed` | Repository file, entry point for indexing |
| Symbol | `uid, name, kind, range, hash, token_estimate` | Atomic code unit (function/class/variable) |
| DocAnchor | `chunk_id` | Doc chunk key — navigates to File via [:FROM], to symbols via [:COVERS] |
| Commit | `hash, author, timestamp, branch` | Version node for time-travel context (planned) |

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
| COVERS | (DocAnchor)→(Symbol) | Doc chunk describes this code symbol |
| MODIFIED_IN | (Symbol)→(Commit) | Symbol change history (planned) |

### 5.3. JSON Prompt Contract

✅ Implemented — `PromptContext.to_dict()` in `sidecar/context/arbitrator.py`. Returned under `"context"` key in `/ask` response.

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
    { "chunk_id": "string", "source_file": "string", "content": "string" }
  ]
}
```

**Implemented metadata:** `mode`, `intent`, `metadata.query_intent`, `metadata.tiers_used`, `metadata.tier_tokens`, dependency `depth`, `direction`, and `relevance_score`.

**Known gap:** project/workspace/branch metadata and document relevance scores are still planned.

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
10. Debounce, stale-job cancellation, and backpressure for mass editor events remain product hardening items.

---

## ADR-001: Separation of Graph Topology and Source Code Content
**Status:** Accepted

Store only topology in Neo4j. Symbol node contains: `uid`, `name`, `kind`, `range` (start/end lines), `hash`. No `file_path` — navigate via `(File)-[:CONTAINS]->(Symbol)`. DocAnchor node contains only `chunk_id` — navigate via `[:FROM]` to File, `[:COVERS]` to Symbol. Sidecar reads code text from disk on demand using line coordinates.

**Why:** Keeps Neo4j lightweight for fast Cypher queries. Source code never goes to SaaS cloud. Only `hash` update needed when function body changes without structural impact.

**Trade-off:** Extra disk I/O per prompt assembly. Mitigated by OS file cache. Hash mismatch = dirty flag = re-parse.

---

## ADR-002: Python Sidecar for MVP
**Status:** Accepted

Python 3.12+, compiled to standalone binary with Nuitka at launch.

**Why:** Best ecosystem for tree-sitter, LanceDB, sentence-transformers. Fast iteration on arbitration logic. Compiled binary ships as single file (50MB+).

**Trade-off:** Performance ceiling on very large graphs (100k+ nodes) may require hot-path rewrite in Rust later.

---

## ADR-003: Shared SaaS Graph + Local Dirty Overlay
**Status:** Accepted

Primary graph in Neo4j Aura (shared team instance). Local unsaved changes in `InMemoryOverlay` inside the sidecar process. ✅ Overlay implemented.

**Why:** Team shares one source of truth. Local edits don't pollute the shared graph. No full re-index per developer.

**Trade-off:** Cloud access required for SaaS graph (fallback to local Docker). Cypher queries must merge cloud + local results.

---

## ADR-004: Automatic Model Routing by Task Complexity
**Status:** Accepted and partially implemented

Intent + context-size classifier routes requests to appropriate model tier in `sidecar/ai/engine.py`.

**Why:** Top-tier models for all requests is economically wasteful. Simple queries can be answered cheaper/faster locally.

**Trade-off:** Must maintain multiple provider contracts and fallback behavior.

---

## ADR-005: LanguageAdapter Protocol
**Status:** Accepted and implemented for Python/TypeScript adapters

All language-specific logic (tree-sitter queries, call resolution, identifier conventions) lives behind a `LanguageAdapter` protocol. New languages (Go, Rust, Java) are added by implementing the protocol — no edits to the indexer, arbitrator, or extractor core.

Required methods:
- `extract_symbols(tree, source) -> list[Symbol]`
- `extract_calls(tree, source) -> list[Call]`
- `file_extensions() -> set[str]`
- `is_upper_case_constant(name) -> bool`

**Why:** The Risk Register flags multi-language support as high-complexity. Without a stable extension point, every new language forces edits to core modules, which grows surface area faster than test coverage.

**Trade-off:** Slight indirection cost; adapters must be kept in sync when core `Symbol` schema evolves.

---

## ADR-006: Quality Gates Before SaaS
**Status:** Accepted

SaaS and marketplace readiness are blocked on correctness, observability, and isolation hardening. Cloud/local fallback exists, but production multi-user operation still needs stable workspace boundaries and stronger auth.

**Why:** The project's value proposition is measurable precision and cost savings. Scaling before retrieval correctness and observability are durable means scaling an unverified product.

**Trade-off:** Slower path to "enterprise story." Accepted — correctness first.
