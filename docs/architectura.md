# Surgical Context — Architecture

> **Status:** Phase 5 complete and validated. Full pipeline operational: code indexing with typed call edges → AFFECTS index → doc enrichment with cross-document linking. See [road_map.md](road_map.md) for validation metrics (49 typed edges, 196 AFFECTS, 2,040 FROM relations, 2,286 COVERS links).

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
| Thin Client | TypeScript / VS Code API | Captures events, renders chat and dashboard. No business logic. |
| Sidecar Binary | Python + FastAPI | Orchestrator: indexing, graph queries, prompt assembly, LLM calls. |
| Storage Trinity | Neo4j + LanceDB + FS | Hybrid storage — each data type in its optimal environment. |

### 2.2. Inter-Process Communication
VS Code ↔ Sidecar via local FastAPI (HTTP/JSON). Ensures editor stays responsive even if a heavy Cypher query blocks the sidecar. Enables future replacement of Python binary with Rust without frontend changes.

### 2.3. Current Sidecar Endpoints

| Method | Path | Status |
|---|---|---|
| GET | `/health` | ✅ |
| POST | `/index` | ✅ |
| POST | `/index/docs` | ✅ |
| POST | `/ask` | ✅ |
| POST | `/search` | ✅ |
| POST | `/overlay` | ✅ |
| DELETE | `/overlay` | ✅ |
| POST | `/index/file` | 🔴 Planned (Phase 3.5) — single-file incremental update |
| GET | `/metrics` | 🔴 Planned (Phase 2.5) — Prometheus text format |

---

### 2.4. Observability (Planned — Phase 2.5)

The system's value proposition rests on three measurable claims: **<200ms context assembly**, **60–80% token reduction**, and **3–5× cost savings**. None of these are currently instrumented. Phase 2.5 adds:

- **Structured logs** per pipeline stage with fields: `trace_id`, `phase`, `duration_ms`, `symbols_in`, `symbols_out`, `tokens_estimated`.
- **Metrics endpoint** (`GET /metrics`): index duration histogram, `/ask` p50/p95/p99, token counts, cache hit rates.
- **Token baselines**: every `/ask` logs both the surgical token count and an estimate of the "carpet-bomb" equivalent (all open files). The delta is the core KPI.
- **Retrieval recall@k** measured against a golden fixture set on every CI run.

Without this layer, claims in §1.3 are unfalsifiable and cannot be used to justify the Phase 4 SaaS transition.

---

## Section 3: Data Processing Pipelines

### 3.1. Extract — Change Monitoring
- **Git Integration (macro):** subscribes to `.git` events; on checkout/commit, reconciles local index with Neo4j SaaS.
- **LSP / File Watcher (micro):** `onDidChangeTextDocument` / `onDidSaveTextDocument` events feed the In-Memory Overlay in real time.

### 3.2. Transform — Analysis & Enrichment

**Syntactic (AST):**
- Symbol extraction: functions, classes, line coordinates, content hash.
- Call graph: typed function calls — `CALLS_DIRECT` (static), `CALLS_DYNAMIC` (dispatch), `CALLS_INFERRED` (string-based). Resolved within the same project.
- UID: `sha256(file_path:name)` — deterministic, collision-resistant.
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
- **LanceDB:** delete-then-insert per file on re-index.

### 3.4. Dirty State Handling ✅ Implemented
`InMemoryOverlay` holds `{file_path: raw_content}`:
- Re-parses symbols on the fly via tree-sitter — no disk I/O.
- `ContextArbitrator._read_code()` checks overlay before disk.
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
2. **Intent classification** (Phase 6+): detect query intent (navigation, debugging, refactor, exploration, new feature, design question) → choose tier priority order.
3. **Graph expansion** (`GraphExpander`): BFS from target symbol through typed edges (CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED, DEPENDS_ON, IMPLEMENTS, OVERRIDES, REFERENCES) constrained by token budget + depth limit. Returns priority-scored subgraph.
4. **Deduplication** (`ContextDeduplicator`): remove redundant symbols and overlapping doc chunks.
5. **Code resolution** (`CodeResolver`): read from `InMemoryOverlay` (if dirty) or disk for each symbol. Tracks `is_dirty` flag per symbol.
6. **Doc retrieval** (`DocResolver`): semantic search in LanceDB `docs` table → top-k chunks. Matched chunks have `[:COVERS]` edges to code symbols.
7. **Prompt assembly** (`PromptCompiler`): rank tiers by intent (code → cross-refs → specs → architecture → concepts → ideas), fill budget in order.
8. **LLM call**: if tiers are empty → "standard mode" (bare query, no context). Else → `PromptContext.to_system_prompt()` + response from Ollama/Claude.
9. Response: `{symbol, answer, context}` — `context` is the full JSON Prompt Contract.
10. **Streaming** (Phase 6): SSE instead of blocking (planned).

### 4.2. Cold Start
1. FS scan for `.py`/`.ts`/`.tsx` files (gitignore-aware, dirs pruned).
2. Phase 1: extract all symbols (functions, classes, UPPER_CASE variables) → upsert nodes.
3. Phase 2: extract all calls → upsert `[:CALLS]` edges.
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

### 4.4. Model Routing (Planned — Phase 5)
- Pre-score intent + context token count.
- Small context + simple question → Llama 3 / cheap API model.
- Large context + architectural change → Claude / GPT-4o.
- Fallback: escalate on empty or error response.

---

## Section 5: Data Schema

### 5.1. Neo4j Node Labels

| Label | Properties | Description |
|---|---|---|
| File | `path, hash, last_indexed` | Repository file, entry point for indexing |
| Symbol | `uid, name, kind, range, hash` | Atomic code unit (function/class/variable) |
| DocAnchor | `chunk_id` | Doc chunk key — navigates to File via [:FROM], to symbols via [:COVERS] |
| Commit | `hash, author, timestamp, branch` | Version node for time-travel context (planned) |

### 5.2. Relationships

| Type | Direction | Description |
|---|---|---|
| CONTAINS | (File)→(Symbol) | Symbol belongs to file |
| CALLS | (Symbol)→(Symbol) | Direct function call |
| DEPENDS_ON | (Symbol)→(Symbol) | Type/interface usage (planned) |
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

**Planned additions:** `metadata` (project, branch, query_intent), `depth` per dependency, `relevance_score` per doc chunk — deferred to Phase 5.

### 5.4. BFS Retrieval Cypher

```cypher
MATCH (target:Symbol {uid: $target_uid})
OPTIONAL MATCH (target)-[:CALLS|DEPENDS_ON*1..2]->(dep:Symbol)
OPTIONAL MATCH (doc:DocAnchor)-[:COVERS]->(target)
RETURN target, collect(distinct dep) as dependencies, collect(distinct doc) as anchors
```

**Note:** the hardcoded `*1..2` hop depth is a placeholder. Phase 3.5 replaces this with a **budget-driven traversal**: expand breadth-first while cumulative token estimate of fetched symbol bodies stays below the request's `token_budget`. Depth is an outcome of the budget, not an input.

---

### 5.5. Incremental Indexing (Planned — Phase 3.5)

Current `/index` is a full re-scan. Target state:

1. Client saves a file → `POST /index/file { path }`.
2. Sidecar hashes the file; compares to stored `File.hash`. Unchanged → no-op.
3. Re-parse file; compute hash for each extracted symbol.
4. For each symbol: `MERGE` only if hash differs. Unchanged symbols are not rewritten.
5. Remove Symbol nodes whose names no longer appear in the file.
6. Re-link only `[:CALLS]` edges originating from modified symbols.
7. Re-embed only modified symbols into LanceDB `symbols` table.
8. Debounce: rapid saves coalesce into one re-index per file per 500ms window.

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
**Status:** Accepted (implementation planned Phase 5)

Intent + context-size classifier routes requests to appropriate model tier.

**Why:** Top-tier models for all requests is economically wasteful. Simple queries can be answered cheaper/faster locally.

**Trade-off:** 100–300ms classification overhead. Must maintain multiple API contracts (Ollama, Anthropic, OpenAI).

---

## ADR-005: LanguageAdapter Protocol
**Status:** Proposed (target: Phase 1 polish)

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

Phase 4 (SaaS / Neo4j Aura) and Phase 5 (model routing, marketplace) are blocked until Phase 2.5 (eval harness + metrics) and Phase 3.5 (incremental indexing + token budget) are green on CI.

**Why:** The project's value proposition is measurable precision and cost savings. Shipping cloud infrastructure before those metrics exist means scaling an unverified product. The cost of delaying SaaS is small; the cost of a misleading launch is high.

**Trade-off:** Slower path to "enterprise story." Accepted — correctness first.
