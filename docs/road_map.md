# Surgical Context - Road Map


> **Status:** The current engine-first development line treats Surgical Context as a **local-first, model-agnostic context engine for code understanding and change impact**.
>
> **Release target:** a local context engine with a stable Python context_engine API, local graph/vector/history defaults, a trustworthy `Ask / Inspect / Impact` loop, and the VS Code extension as a reference frontend.
>
> **Principle:** measure retrieval quality and token efficiency on real repositories before expanding platform scope.
>
> **See also:** [concept.md](concept.md), [architectura.md](architectura.md), [README.md](../README.md)
>
> **Last updated:** 2026-06-19 (axis/doc-anchor indexing, context_engine module split, and current API/storage truth)

---

## Product Direction

The local product is still the canonical next milestone, but the product is now described more narrowly.

### v0.1 Goal

Deliver a local engine and reference VS Code client that can answer:

- what does this code do?
- what supports this answer?
- what might this change break?

without wasting tokens or hiding retrieval behavior.

### In Scope

- Python context_engine/API running locally
- reference VS Code surfaces: Chat, Inspector, Impact, Settings, Dashboard
- local defaults: Neo4j, LanceDB, SQLite
- retrieval ladder: `symbol -> file -> workspace -> direct_llm`
- prompt-contract transparency and route visibility
- local docs indexing from the repository
- request history and snapshots
- benchmark validation on real repositories

### Out of Scope

- managed SaaS as a requirement
- cross-tenant or cross-organization graph traversal
- broad enterprise RBAC and policy surface
- mandatory LLM proxy gateway
- microservice split of the context_engine
- parser/indexer rewrites before profiling proves a bottleneck
- "general autonomous coding agent" competition as a release goal

### Single-Tenant Default

Keep `workspace_id` and `tenant_id` in contracts, but default `tenant_id` to `local`. Multiple local workspaces are allowed; cross-project tenant graph traversal is not required for the local release.

---

## Canonical Backlog

Active work for the local release. Completed stabilization and phase history are recorded below.

### P0 — Refocus and truth
- [x] Rewrite product-facing docs around the context-engine thesis (`concept.md`, `idea_summary.md`, `road_map.md`, `README.md`).
- [x] Align the benchmark story around real repositories and real developer questions.
- [ ] **Ongoing principle:** keep the local release boundary explicit — no platform or enterprise scope expansion without a measured reason.

### P1 — Local daily-driver loop
- [x] Clean local bootstrap and smoke path (`scripts/local_dev.py`, [local_development.md](local_development.md)).
- [x] Local history and request snapshots (SQLite `local` / `ephemeral` / `disabled`).
- [x] Local-first LLM defaults: `MODEL_PREFERENCE=ollama`, `ALLOW_CLOUD_LLM=false` (cloud opt-in only).
- [x] Default cloud model `claude-sonnet-4-6` via `ANTHROPIC_MODEL` (replaces retired `claude-sonnet-4-20250514`).
- [x] `/ask/stream` returns degraded `chunk` + `context` when the LLM is unreachable (not error-only).
- [~] Finish streaming and selected-request synchronization so `Ask / Inspect / Impact` always point to the same request. **Current:** selected request is synced from webview to extension host (`request.selected`) and reused by Inspector/Impact commands; **remaining:** restore/persist this selection across reloads from stored history bundles.
- [ ] Keep dashboard, settings, and health states useful when providers are missing, local-only, or degraded.
- [ ] Add small but solid accessibility/keyboard polish for the extension surfaces.

### P2 — Retrieval quality and observability
- [x] Server-side API bounds: `limit` 1–50 on `/search*`, `token_budget` 400–32 000 on `/ask` and `/search/unified` (Pydantic → HTTP 422).
- [x] Axis retrieval is the active `/ask` provider; the legacy `UnifiedRanker` cascade was removed.
- [x] Soft fallback ladder: `symbol → file → workspace → direct_llm` (missing symbol is not HTTP 404).
- [~] Prompt-contract schema fields: `scores`, `provenance`, `pruned[]`, ranker counts, and intent details exist; the active axis adapter still leaves several score/distribution/pruning fields sparse or at defaults.
- [x] Retrieval cache visibility: `metadata.assembly.cache_hits` (L1–L3).
- [x] Per-user overlay isolation: keys `(workspace_id, user_id, file_path)`.
- [~] DocAnchor confidence/type — COVERS metadata and in-code docstring/JSDoc owner seeds are shipped; **remaining:** calibration plus propagation of general doc evidence/anchor quality into the active axis `PromptContext` and extension.
- [~] Latency SLO — request/stage metrics for `/ask`, `/ask/stream`, `/search/unified`; index queue counters exist; **remaining:** index-duration SLO gates.
- [ ] Extension UX: model route, fallback level, token/cost signals easy to inspect.
- [~] Canonical role coverage — green on control repos; **remaining:** Flask/Django/Express/Vue/NestJS tails (export shape, file recall).
- [~] Impact analysis remains **shallow by design** until proven otherwise: `AFFECTS` is bounded reverse reachability, not full blast-radius across frameworks, codegen, templates, runtime dispatch, and tests. **Current:** VS Code Impact tab shows affected symbols/files, impact counts/depth, and opens related files; **remaining:** explicit shallow-scope disclaimer and broader validation.

### P3 — Real-repo validation
- [x] Question packs under `QA/fixtures/` (`questions_python.yaml`, `questions_non_python.yaml`, `new_questions_python.yaml`).
- [x] Axis benchmark harness: `python -m QA.axis_benchmark` (see [spec_eval_harness.md](spec_eval_harness.md)).
- [x] P7 CI gate: `tests/integration/test_axis_benchmark_gate.py` on this repo (`file_recall` baseline).
- [ ] Multi-repo axis sweep in CI (currently manual via `QA/run_full_benchmark_sweep.py`).

### P4 — Provider boundaries (defaults first)
- [ ] Define `GraphProvider` protocol around the methods the context_engine already uses and wrap `Neo4jClient` as the default implementation.
- [ ] Define `VectorProvider` protocol around the methods the context_engine already uses and wrap `LanceDBClient` as the default implementation.
- [x] Implement SQLite local history with conversations, messages, ask snapshots, inspector snapshots, impact snapshots, retention pruning, and `disabled` / `ephemeral` modes.
- [~] Add provider config only for local defaults first: history has `local`, `ephemeral`, and `disabled`; graph/vector config boundaries remain.
- [~] Put storage policy above all providers for prompt text, response text, source snippets, retention, redaction, and sharing. Local history sanitization exists; broader vector/shared/audit policy remains.
- [~] Add fake/in-memory provider conformance tests before adding real alternate backends. Retrieval protocol fakes and history tests exist; full graph/vector connector conformance remains.

### P5 — Future team and enterprise horizon
- [ ] Add roles `admin` and `user`, then map permissions onto indexing controls, audit/history access, model/provider settings, and graph queries.
- [ ] Add connectable documentation sources through `DocSourceProvider`: repository docs first, then Confluence, Figma, and future sources.
- [ ] Add parallel indexing only after local profiling identifies the real bottlenecks.
- [ ] Add customer-managed/dedicated provider modes for graph, vector, and history stores.
- [ ] Add Tenant API Contract Graph for project-published API facts and tenant-level service links; no neighboring source scans.
- [ ] Add optional LLM Proxy Gateway transport for organizations that need provider-account policy, auditing, masking, quotas, or fallback outside the context_engine.
- [ ] Split the context_engine into services only when scale requires it.
- [ ] Consider Rust/Go/C parser or indexer hot paths only after a performance review proves Python orchestration is the bottleneck.

---

## Immediate focus (next iterations)

Ordered execution lanes — do not regress green control repos while working tails.

| Priority | Lane | Action |
|---|---|---|
| 1 | **Baseline lock** | Keep `fastapi`, `pydantic`, `redux_toolkit`, `sqlalchemy` green after every ranker change. |
| 2 | **Python tails** | Close role/file tails in `django` / `flask` (trace/explain only; not impact scope). |
| 3 | **JS target resolution** | `express` / `vue` / `nestjs` — export shapes and symbol resolution before weight tuning. |
| 4 | **Extension product** | Finish request-sync persistence; surface route, fallback level, `pruned[]`, cache hits in UI. |
| 5 | **Doc-anchor polish** | Calibrate confidence/type and propagate ranked doc evidence into the active axis prompt/inspector path. |
| 6 | **Impact (deferred)** | Separate iteration after non-impact lanes stabilize; document shallow `AFFECTS` in UI. |

**Validation rhythm**
- After axis engine changes: run P7 gate (`pytest tests/integration/ --run-integration`) and spot-check `python -m QA.axis_benchmark` on control repos.
- Full sweep: `python QA/run_full_benchmark_sweep.py` (manual; requires pre-indexed workspaces).

**Docs:** product thesis + safety specs current (maintenance only). Eval: [spec_eval_harness.md](spec_eval_harness.md). API/sandbox: [spec_context_engine_api.md](spec_context_engine_api.md).

---

## Completed Stabilization Backlog

This section preserves the post-MVP hardening record. Completed items remain useful context, but the active product direction is the Local Developer Product backlog above.

### P0 - Truth, Safety, and API Hardening
- [x] Refresh the root `README.md` as the current-truth entry point; archive or label historical analysis when status changes.
- [x] Fix context_engine DB lifecycle: remove mutable request identity from the global client; use request-scoped user context.
- [x] Historical cascade moved doc resolution inside arbitration before prompt compilation. That pipeline was later removed; general markdown propagation into a successful axis prompt is tracked as an active P2 gap above.
- [x] Add typed API response models and JSON-safe SSE framing for `/ask/stream`.
- [x] Add durable indexing job log with retry/dead-letter states so Neo4j and LanceDB cannot silently diverge after partial failure.
- [x] Add first endpoint tests for `/ask`, `/ask/stream`, `/index/file`, `/impact`, `/audit/actions`, and `/auth/token`.
- [x] Add auth-boundary enforcement tests for protected endpoints with `AUTH_REQUIRED=true`.
- [x] Workspace path sandboxing: caller-supplied paths and **graph-resolved** `file_path` values normalized under registered `project_path`; outside root → `403` or empty code; stale Neo4j paths pruned on manifest persist (`context_engine/workspace_paths.py`, `spec_context_engine_api.md`).
- [x] Queued `POST /index` registers workspace root immediately via `register_workspace_project_root()` (extension default `queue=true` no longer leaves `/overlay` / `/index/file` without a manifest).
- [x] Bounded public API limits: search `limit` 1–50, `token_budget` 400–32 000 (HTTP 422 when out of range).
- [x] Anthropic default model `claude-sonnet-4-6` (`ANTHROPIC_MODEL` override; retired `claude-sonnet-4-20250514`).
- [x] Per-user overlay isolation (`workspace_id`, `user_id`, `file_path`) so unsaved buffers do not leak across users.
- [x] Local-first LLM routing: default Ollama; `ALLOW_CLOUD_LLM=false` unless explicitly enabled.
- [x] `/ask/stream` degraded answer + context when LLM unreachable; L3 cache trace SSE after cache hit.
- [x] Feedback JSONL rotation and L3 cache `_file_index` cleanup on put/evict.

### P1 - Retrieval Correctness
- [x] Implement stable UID v2 from [spec_uid_stability.md](spec_uid_stability.md).
- [x] Replace name-only call linking with the scoped resolver in [spec_call_resolution_pipeline.md](spec_call_resolution_pipeline.md).
- [x] Add workspace/branch isolation from [spec_branch_isolation.md](spec_branch_isolation.md).
- [x] Add Git checkout/stash-pop invalidation strategy: graph/vector versioning plus differential branch sync.
- [x] Add adversarial fixtures for duplicate names, moved files, renamed symbols, stale docs, and branch duplication.

### P2 - Visible Product Value
- [x] Add `GET /metrics`, structured per-stage timing, trace IDs, and token/cost/latency tracking.
- [x] Extend the JSON Prompt Contract with scores, provenance, pruning reasons, model route, and resolver version.
- [x] Build an extension context inspector showing selected files/docs, scores, dirty-state badges, token budget, intent, and model route.
- [x] Add a unified search endpoint that blends symbols, graph neighbors, and docs.

### P3 - Scale and Learning
- [x] Add retrieval caching only after UID, workspace, and graph-version keys are stable.
- [x] Add feedback signals only after prompt-contract observability and privacy/redaction rules exist.
- [x] Move AFFECTS rebuild and large-repo indexing work to a background queue with backpressure and batching.
- [x] Coalesce IDE event storms (mass refactor, find/replace, stash pop) into bounded batch updates.
- [x] Add embedding recomputation controls: content-hash cache, worker throttle, and opt-in low-priority background mode.

---

## Phase 1: Foundation and Local Core ✅ Largely Complete
Goal: Working "VS Code ↔ Python Sidecar" prototype with basic parsing.

### Infrastructure
- [x] Docker container with Neo4j and schema configuration (`docker-compose.yml`)
- [x] Python environment and project scaffold
- [x] FastAPI HTTP/JSON context_engine entrypoint (`context_engine/main.py`)
- [x] Switch Docker image from `neo4j:5.12-enterprise` to `neo4j:5.12-community` for open-source dev baseline (enterprise license only where intentionally required)
- [x] Move `NEO4J_AUTH` out of `docker-compose.yml` into `.env` with `.env.example` committed

### Parsing (ETL)
- [x] tree-sitter integration for Python (`context_engine/parser/extractor.py`)
- [x] Symbol extractor: functions, classes, line coordinates
- [x] Deterministic UID hashes per symbol (ADR-001)
- [x] TypeScript language support (via adapter registry, auto-detect from extension)
- [x] Formalize `LanguageAdapter` protocol (ADR-005) so new languages plug in without core changes

> **Spec:** [spec_language_adapter.md](spec_language_adapter.md) — plugin architecture with registry, adapter discovery, migration path.

### Extension UI (Promoted to Phase 2.5)
- [x] Scaffold `extension/` workspace (TypeScript, `package.json`, build pipeline)
- [x] Basic chat window in VS Code
- [x] Cursor position capture mechanism
- [x] Wire `onDidChangeTextDocument` / `onDidSaveTextDocument` → `POST /overlay` / `DELETE /overlay`

---

## Phase 2: Graph Brain & Surgical Retrieval ✅ Largely Complete
Goal: System can navigate the graph and gather precise context.

### Graph Logic
- [x] Neo4j client: upsert file/symbol nodes (`context_engine/database/neo4j_client.py`)
- [x] Four-phase indexer: symbols → calls → symbol embeddings → pending resolution (`context_engine/indexer/code.py`)
- [x] Historical BFS dependency discovery shipped; the arbitrator was later removed and axis graph walks are now active.

### Data Contract
- [x] JSON Prompt Contract: typed `PromptContext` with `to_dict()` + `to_system_prompt()` (`context_engine/context_types.py`)
- [x] Local LLM integration via Ollama (`context_engine/ai/engine.py` — llama3, configurable via `OLLAMA_MODEL`)
- [x] Fallback behavior when Ollama is unreachable (clear error, degraded `/ask` that still returns `context`)

### Dirty State
- [x] In-Memory Overlay: parse unsaved changes and merge with axis context (`context_engine/overlay.py`, `POST /overlay`, `DELETE /overlay`)

---

## Phase 2.5: Quality Foundation & Extension UI — BASELINE SHIPPED
Goal: Make the system **measurable** before scaling it, and ship a thin client for real-world validation. Without this phase, all later performance and cost claims are unfalsifiable, and the "VS Code integration" premise remains unproven.

> **Specs:** [spec_eval_harness.md](spec_eval_harness.md) (fixture design, metric set, CI contract).

### Evaluation Harness ✅ COMPLETE
- [x] `tests/` directory with pytest for parser, indexer, axis retrieval, overlay
- [x] Question packs under `QA/fixtures/`
- [x] `QA/axis_benchmark.py` — axis `file_recall` benchmark (replaces deleted `qa_benchmark.py`)
- [x] CI config (GitHub Actions): lint, mypy, unit tests, P7 axis gate with Neo4j

### Observability
- [x] Structured logging across pipeline stages (Phase 5 prerequisite)
- [x] `GET /metrics` endpoint (Prometheus text format)
- [x] Per-request trace ID threaded through logs
- [x] Latency SLO tracking against 200ms target
- [x] Distributed tracing via OpenTelemetry (Phase 6, scale phase only)

### Token Accounting — SCHEMA SHIPPED / AXIS POPULATION PARTIAL
- [x] Token counter (tiktoken cl100k_base) on every `PromptContext`
- [x] `PromptContext.token_count()` method
- [~] Per-request fields `tokens_primary`, `tokens_graph`, `tokens_docs` are serialized; the active axis adapter does not yet populate every tier counter.
- [ ] Keep a reproducible all-files-vs-selected-context token baseline in the current axis benchmark.

### Extension UI (Promoted from Phase 1) — BASELINE SHIPPED
- [x] Scaffold `extension/` workspace (TypeScript, `package.json`, build pipeline)
- [x] Basic chat window in VS Code
- [x] Cursor position capture mechanism
- [x] Wire `onDidChangeTextDocument` / `onDidSaveTextDocument` → `POST /overlay` / `DELETE /overlay`
- [x] Demo on a real repo; measure cold-start and `/ask` latency

> **Specs (Phase 10.5 Productization):**
> - [spec_vscode_extension_ui.md](spec_vscode_extension_ui.md) — VS Code extension UI contract: Chat Panel, Context Inspector, Impact Explorer, Dashboard surfaces; interaction flows; state model.
> - [spec_webview_components.md](spec_webview_components.md) — Webview component model, layout rules, message protocol, DTOs, and accessibility guidelines.
> - [spec_package_contributes.md](spec_package_contributes.md) — VS Code manifest (package.json): viewsContainers, views, commands, menus, keybindings, configuration.

---

## Phase 3: Documentation and Vector Search ✅ Largely Complete
Goal: Connect the semantic layer via documentation.

### Vector Layer
- [x] LanceDB integration — two tables: `docs` + `symbols` (`context_engine/database/lancedb_client.py`)
- [x] Markdown processing pipeline: section-aware chunking + embedding generation (`context_engine/indexer/docs.py`)

### Semantic Connections
- [x] DocAnchor in Neo4j: `chunk_id`-only node, `[:FROM]` to File, typed/confident `[:COVERS]` to Symbols, lazy `pending` resolution via LanceDB (`context_engine/indexer/anchor.py`)

### RAG Optimization
- [~] Vector + graph retrieval are both active, but on different paths: axis uses semantic symbol seeding plus graph expansion; general markdown chunks are attached by file/workspace fallbacks, not yet by a successful axis `PromptContext`.
- [x] Symbol body embeddings: profile-specific `symbols` LanceDB tables for semantic DocAnchor matching (`context_engine/indexer/fast/pipeline.py` and `context_engine/indexer/code.py`)
- [x] Section-aware doc chunking: headings-first split, word-window fallback (`context_engine/indexer/docs.py`)
- [x] Gitignore-aware indexer: `pathspec` prunes ignored dirs/files (`context_engine/indexer/fast/collector.py` and the single-file path in `context_engine/indexer/code.py`)
- [x] ADR-001 enforced: no data on Neo4j nodes — `file_path` removed from Symbol and DocAnchor

---

## Phase 3.5: Arbitration & Indexing Robustness — HISTORICAL CASCADE MILESTONE
Goal at the time: make retrieval correct and fast on a live developer's laptop. The cascade implementation described below was later removed; current retrieval and token-credit packing live under `context_engine/axis/`.

> **Spec:** spec_token_budget_bfs.md (removed) — best-first traversal replacing hardcoded `*1..2`, with scoring function, algorithm, contract additions, and tuning protocol.

### Context Budgeting & Ranking — HISTORICAL IMPLEMENTATION REMOVED
The following checklist records the deleted cascade implementation. The active axis path keeps the public token-budget bounds and performs intent-aware token-credit packing, but it does not populate every legacy `PromptContext.budget` or per-symbol score field.

- [x] Token budget parameter on `/ask` (current default 6000; bounds 400–32,000)
- [x] Priority-queue best-first expansion (greedy by relevance score)
- [x] Re-rank: callers (1.2) > callees (1.0) — callers drive intent
- [x] Scoring function: relation_prior + fan-in bonus - token cost - distance penalty
- [x] `depth` and `direction` fields in SymbolContext
- [x] `relevance_score` per symbol (score that selected it)
- [x] `budget` metadata block: limit, spent, reserved, pruned
- [x] "Skip but keep trying" semantics: oversized symbols skipped, cheaper ones fill space
- [x] Cypher neighbor queries with caller_count

### Incremental Indexing ✅ COMPLETE
- [x] File-level dirty tracking: compare `File.hash` before re-parsing
- [x] `POST /index/file` endpoint for single-file updates (triggered by file save in client)
- [x] Delete-on-remove: prune Symbol nodes when file changes (`delete_symbols_for_file`)
- [x] Transactional recovery: write-ahead indexing job log, retry state, and dead-letter queue for partial Neo4j/LanceDB failure
- [x] Symbol-level diff: only re-upsert nodes where `Symbol.hash` changed
- [x] Background debounce queue: batch rapid-fire saves (`context_engine/indexer/queue.py`, `POST /index/files`)
- [x] Backpressure for mass IDE events: bounded queue, batch coalescing, and stale job cancellation

### Graph Completeness ✅ COMPLETE
- [x] `IMPORTS` edge between Files to enable correct cross-module call resolution
- [x] `DEPENDS_ON` edge for type / interface / import usage (Symbol→Symbol edge type for inheritance/interface dependencies)
- [x] Unit tests: 18 tests verify `CALLS`, `IMPORTS`, `DEPENDS_ON` edge extraction for Python and TypeScript
- [x] Current axis graph walks consume call, import, dependency, and structural edge profiles; the old Arbitrator BFS was removed.

### Embedding Quality (DEFERRED — Phase 5)
- [x] Add reusable embedding benchmark harness for golden-set model comparisons (`PYTHONPATH=. python -m QA.embedding_benchmark`)
- [x] Run and record `all-MiniLM-L6-v2` vs a code-native model (e.g. `bge-code`, `unixcoder`) on the golden set
  - 2026-04-21 benchmark: `all-MiniLM-L6-v2` reached `target_hit@5=1.00`, `MRR=0.78`, `expected_recall@5=0.42`, `expected_precision@5=0.52`; `microsoft/unixcoder-base` reached `target_hit@5=1.00`, `MRR=0.78`, `expected_recall@5=0.45`, `expected_precision@5=0.58`.
- [x] Embedding cache keyed by content hash to avoid recomputation on re-index

---

## Phase 4: Quality & Scaling — HISTORICAL CASCADE MILESTONE
Goal at the time: reduce token overhead and prepare for multi-model / multi-user environments. Current equivalents are owned by the axis context builder and embedding registry.

> Historical note: the detailed Phase 4 review was removed after its accepted work was folded into the specs below.

### Context Deduplication — HISTORICAL IMPLEMENTATION REMOVED
> **Spec:** spec_context_deduplicator.md (removed) — insertion point, dedup rules, budget recalculation, test matrix.
>
> The old `ContextDeduplicator`/`GraphExpander` pipeline no longer exists. The axis context builder and prompt adapter now deduplicate symbols by UID while assembling bundles; they do not emit the old `budget["dedup_saved"]` metric.
- [x] Implement `ContextDeduplicator` — pure transform between GraphExpander and PromptCompiler
- [x] Normalize symbol identity by UID; keep lowest-depth copy on duplicates
- [x] Collapse overlapping line ranges within same file
- [x] ~~Deduplicate doc chunks with >85% content overlap~~ (deferred: performance cost exceeded benefit)
- [x] Update `budget["dedup_saved"]` for observability
- [x] Integration: pipeline now expand → deduplicate → resolve → compile (9 unit tests passing)

### Embedding Versioning ✅ COMPLETE
> **Spec:** [spec_embedding_versioning.md](spec_embedding_versioning.md) — metadata schema, model registry, cross-model guard, migration CLI.
- [x] Add `embedding_metadata` JSON column to `docs` and `symbols` LanceDB tables
- [x] Model registry in `context_engine/database/embedding_registry.py` — known models + dimensions
- [x] Write path: record model_name, model_version, chunk_hash, embedding_hash per row
- [x] Read path: guard against cross-model queries (raise `EmbeddingModelMismatch`)
- [x] Model mismatch recovery: wipe LanceDB and re-index (no separate migration CLI)

### Graph Richness (Phase 5 planning) ✅ COMPLETE
- [x] Feasibility assessment: dynamic dispatch detection in Python/TypeScript parsers
  - Result: Python classifies direct/scoped/imported/dynamic/inferred calls; TypeScript now classifies top-level identifier calls as direct and member dispatch (`this.method()`, `service.method()`) as dynamic.
- [x] Spec review: [spec_typed_semantic_edges.md](spec_typed_semantic_edges.md), [spec_affects_index.md](spec_affects_index.md)
  - Result: both specs have corresponding Phase 5 implementation paths in `context_engine/parser`, `context_engine/indexer/affects.py`, and BFS typed-edge traversal.
- [x] Decision gate: prioritize typed edges vs AFFECTS index for Phase 5 first milestone
  - Result: resolved by shipping both; typed call edges feed the materialized AFFECTS index.

---

## Phase 5: Typed Semantic Edges & Reverse Dependencies — INDEXING SHIPPED / CONSUMPTION EVOLVED
Goal: Classify function calls by confidence; enable cascade-aware incremental reindexing via reverse-dependency materialization.

> **Specs:** [spec_indexer.md](spec_indexer.md) — Phase 5 AFFECTS rebuild, call type classification. [spec_affects_index.md](spec_affects_index.md) — AFFECTS materialization, cascade invalidation, `/impact` endpoint. [spec_doc_indexer.md](spec_doc_indexer.md) — enhanced FROM edges with doc type classification.

### Typed Semantic Edges ✅ COMPLETE
- [x] Python call type detection: `CALLS_DIRECT` (static, prior 1.0), `CALLS_DYNAMIC` (dispatch via self., prior 0.7), `CALLS_INFERRED` (string patterns, prior 0.4)
- [x] Neo4j schema migration: CLI tool to migrate existing `CALLS` edges → `CALLS_DIRECT`, create relationship indexes
- [x] Update GraphExpander BFS scoring with new edge type priors (14 entries: typed CALLS, IMPLEMENTS, OVERRIDES, REFERENCES, DEPENDS_ON, IMPORTS)
- [x] BFS traversal extended to all typed edges: `CALLS|CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES`

### AFFECTS Index (Reverse Dependencies) ✅ COMPLETE
- [x] AFFECTSIndexer class: reverse BFS (depth ≤ 4) to compute transitive dependents
- [x] `rebuild_affects(modified_symbol_uids)` called synchronously after file index
- [x] `GET /impact?symbol=<name>` returns affected symbols, files, and depth metrics through the current bounded axis reverse traversal; it does not read only the materialized `AFFECTS` closure.
- [x] Incremental indexing rebuilds `AFFECTS`, axis adjacency, role data, endpoint bridges, and DocAnchor resolution after coalesced batches.

### Enhanced Doc Linking ✅ COMPLETE
- [x] FROM relation enrichment: typed edges (`"doc"`, `"code"`, `"spec"`, `"architecture"`, `"concept"`, `"idea"`)
- [x] `File.doc_type` classification (spec, architecture, concept, idea, code, documentation, roadmap, review)
- [x] `_link_related_docs()` extracts cross-document references (markdown links, bare filenames)
- [x] Enables knowledge graph queries: code → docs → referenced specs/architecture/concepts

### Refinements ✅ COMPLETE
- [x] IMPORTS relation cleanup: filter stdlib/third-party, convert dot notation → file paths (ENDS WITH match)
- [x] SIMILARITY_THRESHOLD tuning: 0.4 → 1.5 (all-MiniLM cosine scale 0–2) — improves doc-code resolution 36% → 50%+
- [x] Gitignore optimization: exclude node_modules/, TypeScript stdlib, build artifacts

### Validation Results ✅
**Test run:** 35 code files, 461 doc chunks from 25 docs

| Metric | Result |
|---|---|
| Typed call edges | 49 (28 CALLS_DIRECT, 21 CALLS_DYNAMIC) |
| AFFECTS index | 196 edges (reverse dependency materialization) |
| FROM edges | 2,040 total with type classification |
| FROM breakdown | 1,542 code, 461 doc, 21 spec, 6 architecture, 5 concept, 5 roadmap |
| COVERS edges | 2,286 (doc chunks → code symbols) |
| Relation types | 8 active (CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED, AFFECTS, FROM, COVERS, DEPENDS_ON, IMPORTS) |
| File doc_type | 37 files classified (28 code, 17 spec, 2 arch, 2 docs, 1 ea: concept/idea/review/roadmap) |
| Context assembly | Working (axis `run_axis_retrieval` → `axis_bundles_to_prompt_context`; cascade removed 2026-06) |

### Deferred to Phase 6+
- [~] `REFERENCES` creation is implemented for static aliases/re-exports; dedicated `IMPLEMENTS` / `OVERRIDES` emission remains incomplete.
- [ ] Execution semantics (ExecutionEdge with runtime probability) — need empirical call-trace data
- [ ] Transitive doc reference linking (depth > 1)
- [ ] Streaming responses & model routing (moved to Phase 6)

---

## Phase 6: Intent Classification & Graceful Degradation — HISTORICAL ORCHESTRATION MILESTONE
Goal: adaptive context assembly based on query type and fallback when no surgical context is available. The original classifier/compiler orchestration described below was later replaced by the axis intent classifier, retrieval budgets, prompt adapter, and ask service.

> **Specs:** spec_intent_classifier.md (removed) — design spec complete; implementation ongoing.

### Phase 6.1: Intent Classifier — HISTORICAL IMPLEMENTATION REMOVED
- [x] `IntentClassifier` class with keyword-based intent detection (heuristics, ML upgrade in Phase 7)
- [x] 7 intent types: navigation, debugging, refactor, exploration, new_feature, design_question, impact_analysis
- [x] `IntentConfig` with 6-tier priority orderings per intent (code, cross_refs, specs, architecture, concept, idea)
- [x] Add `mode` field to `PromptContext`: "surgical_full" | "surgical_doc_only" | "standard"
- [x] Add `intent` field to `PromptContext` for tracking detected query type
- [x] `PromptCompiler.compile_with_intent()` — tier-aware context assembly with graceful degradation
- [x] Doc type inference from filename patterns (spec_*, idea_*, concept, architecture)
- [x] Unit tests: 17 intent classifier tests + 19 compiler tests (all passing)

### Phase 6.2: Graceful Degradation in Orchestrator ✅ COMPLETE
- [x] Historical `IntentClassifier`/`ContextArbitrator` integration shipped; both were later superseded by the axis intent/retrieval path.
- [x] Call intent detection in `get_context_for_symbol(question)` with optional question parameter
- [x] Pass intent to `compile_with_intent()` instead of `compile()`
- [x] Surface `mode` and `intent` in `/ask` response JSON (via PromptContext.to_dict())
- [x] Add integration tests: 11 tests for intent classification + mode field handling
- [x] Backward compatibility: question parameter optional (defaults to empty string → exploration intent)

### Phase 6.3: Streaming & Model Routing ✅ COMPLETE
- [x] Streaming LLM responses (SSE) via `/ask/stream` endpoint
- [x] Official Anthropic SDK activation (`context_engine/ai/engine.py`) with prompt caching on `graph_context` block
- [x] Model Router (ADR-004) — route by context size + intent
  - Large contexts (>= 2k tokens) → Claude (powerful, cached)
  - Complex intents (design, exploration, refactor) → Claude
  - Small/simple queries → Ollama (fast, cheap)
- [x] Support 3 preferences: `ollama` (local default) | `auto` | `claude` (requires `ALLOW_CLOUD_LLM=true`)
- [x] Prompt caching: ephemeral cache on graph_context blocks (reduce API costs)
- [x] Automatic fallback: Claude → Ollama on error
- [x] 21 new unit tests covering routing, initialization, model selection

### Phase 6.4: Integration Testing & Observability — SCHEMA SHIPPED / AXIS POPULATION PARTIAL
- [x] Test intent classification accuracy on 7 intent types plus prompt-compiler tier behavior
- [x] Test tier-based budget allocation per intent
- [x] Test graceful degradation (no matches → standard mode)
- [x] Test mode field serialization in PromptContext.to_dict()
- [x] Per-tier token counting for observability (code, cross_refs, specs, architecture, concept, idea)
- [x] Metadata block: query_intent + tiers_used in JSON response

### JSON Prompt Contract — SCHEMA COMPLETE / ACTIVE POPULATION PARTIAL
- [x] `mode` field: "surgical_full" | "surgical_doc_only" | "standard" (6.1)
- [x] `intent` field: detected query type (6.1)
- [x] `metadata` block: query_intent, tiers_used, tier_tokens (6.4)
- [x] Per-tier token counts for observability (6.4)

---

## Phase 7: Scaling & Graph Provider Modes ✅ COMPLETE
Goal: Transition from local tool to shared team solution (ADR-003).

> **Status:** Phase 5–6 complete. Phase 7 MVP implemented: Neo4j Aura/local provider support, multi-user auth, audit logging.

### Graph Provider Sync & Multi-User ✅ COMPLETE
- [x] Neo4j Aura/customer endpoint support for shared knowledge base
- [x] Provider fallback strategy: connect to configured graph endpoint, fallback to local Neo4j if unavailable
- [x] Multi-user support: user identification via headers + JWT tokens
- [x] Conflict resolution: last-write-wins (timestamps on mutations)
- [x] Local overlay per user (unsaved edits in-memory, survives graph provider outage)

### Security & Compliance ✅ COMPLETE (MVP)
- [x] User authentication: JWT tokens with 24-hour expiration
- [x] User identification: via X-User-ID header or environment variable
- [x] Audit logging: JSONL persistent trail (who, what, when, status)
- [x] Multi-user tracking: user_id on all graph mutations
- [x] Health checks: cloud connection status + fallback detection
- [ ] RBAC for graph queries (Phase 7+ enhancement)
- [ ] Secrets management for managed graph credentials (use environment variables for now)
- [ ] Metadata encryption for managed graph providers (Phase 7+ enhancement)

### Performance & Reliability
- [ ] Parallel parsing for `git pull` indexing (ThreadPoolExecutor, 4 workers default)
- [ ] Graceful degradation on graph provider outage (local cache + retry)
- [ ] Rate limiting per user
- [ ] Circuit breaker for cloud sync failures

### Analytics & Monitoring
- [x] `GET /metrics` endpoint (Prometheus text format)
- [x] Per-request trace ID threaded through logs
- [~] Latency SLO tracking: request/stage SLO checks exist through `SIDECAR_REQUEST_LATENCY_SLO_MS`; p50/p95 release gates remain.
- [x] Optional OpenTelemetry stage tracing via `SIDECAR_OTEL_ENABLED`
- [ ] Token savings visualization in VS Code (reuses Phase 2.5 metrics)

---

## Phase 8: Correctness Hardening 🚧 PARTIALLY IMPLEMENTED
Goal: Fix the load-bearing identity, resolution, and isolation gaps before retrieval quality work. Every downstream metric (AFFECTS, DocAnchor, cross-user correctness) depends on these being right.

> **Specs:** [spec_uid_stability.md](spec_uid_stability.md), [spec_call_resolution_pipeline.md](spec_call_resolution_pipeline.md), [spec_branch_isolation.md](spec_branch_isolation.md).

### 8.1 UID Stability
- [x] Replace `sha256(file_path:name)` with `sha256(language:qualified_name + normalized_signature)`
- [x] Signature normalization (strip names/defaults; keep types + keyword markers)
- [x] Qualified-name extraction in Python and TypeScript adapters (`<locals>` for nested scopes)
- [ ] Migration CLI: rebuild Symbol nodes, emit `old_uid → new_uid` map for audit log
- [x] Handle unresolved signatures (`signature_status = "unresolved"` on the node)

### 8.2 Call Resolution Pipeline
- [x] 5-tier resolver: DIRECT → SCOPED → IMPORTED → DYNAMIC → GUESS
- [x] Per-file scope table with import alias tracking
- [x] Dispatch-candidate handling for `self.m()` in Python; interface fanout remains deeper language work
- [ ] `pending_calls` store for unresolved sites (retried on next index pass)
- [x] Edge schema: `confidence`, `tier`, `resolver` properties on every CALLS_* edge
- [ ] Migration CLI: re-resolve existing CALLS edges, downgrade non-matches to CALLS_GUESS

### 8.3 Workspace / Branch Isolation
- [x] `Workspace` node (tenant + repo + ref)
- [x] `IN_WORKSPACE` edges on File / Symbol / DocAnchor
- [x] Cypher-level scope injection in arbitrator (not Python filtering)
- [x] `X-Workspace` header on graph/context endpoints with development fallback
- [x] Per-workspace AFFECTS rebuild
- [x] Overlay keyed by `(workspace_id, user_id, file_path)`
- [ ] Workspace lifecycle: create-on-index, delete cascade, TTL-based GC

---

## Phase 9: Retrieval & Observability 🚧 ACTIVE
Goal: keep the axis retrieval path measurable by carrying its intent, candidate, pruning, and document evidence into the prompt contract.

> **Specs:** spec_unified_ranking.md (removed), spec_multi_label_intent.md (removed), [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md), [spec_doc_anchor_confidence.md](spec_doc_anchor_confidence.md).
>
> **Current status:** the legacy `UnifiedRanker` work below is retained as phase history, but that implementation was removed with the ranking cascade in 2026-06. The live path is `context_engine/axis/`; intent-axis ranking and docstring-anchor retrieval are active, while prompt-contract propagation remains partial. Real-repo benchmark warnings now mostly expose file/precision and export-shape tails rather than missing framework-specific defaults.

### 9.1 Unified Ranker ✅ HISTORICAL / SUPERSEDED BY AXIS
- [x] `UnifiedRanker.rank()` — single pool from graph BFS + vector search
- [x] Blended score = α·graph + β·semantic + γ·intent + δ·overlap − ε·cost (per-track normalized)
- [x] Overlap bonus when both signals fire on the same candidate
- [x] Budget-fill loop competes symbols and doc chunks on identical terms
- [x] Weight tuning via eval harness sweep
- [x] Historical implementation was decomposed under `context_engine/context/ranker/`; the entire cascade was later removed after the axis cutover.
- [x] Target disambiguation for duplicate symbol names within one workspace
- [x] Module/package fallback targets for package-surface questions
- [x] Topic-aware subsystem noise control for focused API questions, so distant graph links through broad helpers do not crowd out relevant runtime/doc candidates
- [x] Better mechanism routing/backfill for serialization impact (`serialize_response`-style flows now route to impact roles and targeted tests)
- [ ] Budget-safe primary-source truncation/signature mode reflected consistently in benchmark + prompt contract (deferred)
- [~] Better graph/doc/recovery coverage for structurally sparse runtime mechanisms through generic semantic hints, import recovery, and dependency-flow role recovery; remaining work is precision/file-recall telemetry.

### 9.2 Multi-Label Intent 🚧 INITIAL ROUTING POLICY SHIPPED
- [x] `IntentSignal.distribution` (sum-to-1 keyword weights across supported intents)
- [x] Classifier returns partial scores per label → normalized distribution
- [x] `IntentPolicy` consumes distribution for active/secondary intents, weighted tier order, blended priors, supplemental roles, doc-first mode, and weighted floor budget
- [~] Budget split across tiers in proportion to blended tier score (soft policy shipped; hard per-tier buckets deferred pending validation)
- [x] `ambiguous` signal in the prompt contract for client UX / routing decisions
> **Decision:** Ship soft multi-label routing first: primary intent still anchors target selection, while strong secondary intents influence roles, priors, floor, and doc ordering. Phase 10 remains the place for learned classification and hard policy calibration.

### 9.3 DocAnchor Confidence & Type ✅ INDEXING COMPLETE / RETRIEVAL PARTIAL
- [x] Anchor type classification: definition / example / reference / warning / deprecated
- [x] Per-edge confidence score (resolver + name mention + heading + code-style mention signals)
- [x] Multi-symbol weighting: `primary_bias` = 1.0 for single/focal symbol, reduced for secondary symbols
- [x] Edge properties: `anchor_type`, `confidence`, `primary_bias`, `resolver`
- [x] In-code docstring/JSDoc anchors carry `owner_uid` and seed the axis path; the bounded reverse-`USES_TYPE` bridge is implemented.
- [~] `PromptContext` can serialize `documentation[].anchor_type`, `anchor_confidence`, `primary_bias`, and nested `anchor`; the active axis adapter does not yet populate general markdown documentation in its normal success path.

### 9.4 Prompt Contract Observability 🚧 IN PROGRESS
- [x] Per-candidate basic `scores` block (graph relevance / semantic score)
- [x] `provenance` list on every symbol and doc chunk
- [x] Budget-level `metadata.pruning_reasons`
- [x] `metadata.assembly.*` — per-phase latencies, trace_id, workspace_id, context_pipeline_version
- [x] Surface target-selection/disambiguation reasoning when multiple same-name symbols exist
- [x] `pruned[]` array — candidates that missed the budget, with reason, scores, cost, roles, noise factor, and provenance
- [~] `metadata.ranker` — selected/pruned counts are emitted; the removed cascade's weight snapshot is no longer part of the active axis path.
- [~] `intent.distribution` + `intent.ambiguous` + `intent.confidence` exist in the schema; active axis propagation is incomplete.
- [ ] Carry axis candidate scores, pruning decisions, intent matches, and ranked doc evidence through `axis_bundles_to_prompt_context` without reconstructing the removed cascade.

---

## Phase 10: Scale & Learning 📋 PROPOSED
Goal: Make retrieval cheap at scale and let the system get better from usage. Deliberately last — the learning loop only works once observability (Phase 9) is in place.

> **Specs:** [spec_retrieval_cache.md](spec_retrieval_cache.md), [spec_learning_loop.md](spec_learning_loop.md).

### 10.1 Three-Layer Retrieval Cache
- [x] L1 — symbol body cache keyed by `(file_path, range, file_hash)`, in-process LRU
- [x] L2 — subgraph cache keyed by `(primary_uid, intent_hash, budget, workspace_id, graph_version)`
- [x] L3 — prompt/response cache keyed by `sha256(system_prompt || user_question)`
- [x] Version-bump invalidation on graph mutations (no explicit cache walking)
- [x] Cache hits surface in `metadata.assembly.cache_hits`
- [x] Pluggable cache facade with in-memory LRU backend for single-instance local mode
- [ ] Redis backend for multi-instance deployments

### 10.2 Feedback Loop
**Decision:** implement the first feedback slice as append-only, workspace-scoped telemetry. Persist snapshots with opaque `feedback_token`s and metadata only; do not store raw prompts, code bodies, or free-text comments in training data until a redaction pass exists. Defer personalization, learned graph edges, and offline retraining until enough explicit feedback metrics exist.

- [x] `feedback_token` issued on every retrieval, bound to persisted `RetrievalSnapshot`
- [x] `POST /feedback` endpoint — implicit/explicit, accept/reject, with details
- [ ] Fast loop: per-user EMA adjustments to tier priors (capped ±20%)
- [ ] Slow loop (nightly): weight sweep + classifier retrain proposals (human-approved PRs)
- [ ] `(Symbol)-[:CO_RELEVANT]->(Symbol)` learned edges — adds candidates beyond structural reach
- [x] Privacy: per-workspace scoping and no raw prompt/code/comment storage before redaction exists
- [ ] Privacy: opt-out toggle and full PII redaction before ML training
- [x] Metrics: feedback event counters by kind and accept/reject outcome
- [ ] Metrics: coverage and harness impact

### 10.3 Performance & Reliability (carried forward from Phase 7)
- [ ] Parallel parsing for `git pull` indexing (ThreadPoolExecutor, 4 workers default)
- [x] Durable indexing queue with retries and dead-letter records for partial graph/vector writes
- [x] Backpressure and batch coalescing for mass save/refactor events from the IDE
- [ ] Git branch-switch cache invalidation via workspace graph/vector versions
- [x] Embedding recomputation throttle and content-hash cache to reduce local CPU/GPU load
- [ ] Graceful degradation on graph provider outage (local cache + retry)
- [ ] Rate limiting per user
- [ ] Circuit breaker for cloud sync failures

### 10.4 Analytics & Monitoring (carried forward from Phase 7)
- [x] `GET /metrics` endpoint (Prometheus text format)
- [x] Per-request trace ID threaded through logs
- [~] Latency SLO tracking: request/stage SLO checks exist through `SIDECAR_REQUEST_LATENCY_SLO_MS`; p50/p95 release gates remain.
- [x] Optional OpenTelemetry stage tracing via `SIDECAR_OTEL_ENABLED`
- [ ] Token savings visualization in VS Code (reuses Phase 2.5 metrics)

### 10.5 Extension Productization
> **Specs:**
> - [spec_vscode_extension_ui.md](spec_vscode_extension_ui.md) — Full UI contract with four surfaces and state model.
> - [spec_webview_components.md](spec_webview_components.md) — Webview component model, messaging protocol, and DTOs.
> - [spec_package_contributes.md](spec_package_contributes.md) — VS Code manifest declarations and activation rules.

- [x] Context inspector panel showing retrieved symbols/docs, relevance scores, and dirty-state badges.
- [x] Four UI surfaces defined: Chat Panel, Context Inspector, Impact Explorer, Dashboard.
- [x] Webview component model and layout rules (bottom-docked composer, collapsed accordions, auto-grow textarea).
- [x] Message protocol between webview ↔ extension host and extension host ↔ context_engine.
- [x] VS Code manifest structure (viewsContainers, commands, menus, keybindings, configuration).
- [~] Streaming chat integration with `/ask/stream` JSON-safe SSE events (endpoint + degradation shipped; extension wiring incomplete).
- [x] Token budget, selected mode, query intent, and model route display.
- [ ] Keyboard shortcuts and accessibility (ARIA labels, focus management, screen reader support).
- [~] VS Code settings UI exists for context_engine URL, model preference, workspace ID, token budget, auth token, overlay, storage, and keyboard shortcuts. **Remaining:** model preference is currently extension-local/display state; context_engine routing is still controlled by process environment.
- [~] Chat, Inspector, Impact, and Dashboard surfaces are implemented in TypeScript/DOM; request restoration, richer axis evidence, and accessibility remain incomplete.

### 10.6 Storage Provider Connectors
Goal: define storage boundaries without blocking the local release on alternate database backends. For v0.1, wrap the defaults first: Neo4j, LanceDB, and SQLite. Alternate graph/vector/history providers are Team/Enterprise horizon work.

> **Spec:** [spec_storage_connectors.md](spec_storage_connectors.md).

- [ ] Define `GraphProvider` protocol and wrap `Neo4jClient` as the default implementation.
- [ ] Define `VectorProvider` protocol and wrap `LanceDBClient` as the default implementation.
- [x] Implement SQLite local history for dialogs, ask snapshots, inspector snapshots, and impact snapshots.
- [~] Add local provider config with modes: `local`, `local_docker`, `ephemeral`, and `disabled`. History modes are implemented; graph/vector modes remain.
- [~] Add storage policy enforcement before any connector receives raw prompt text, response text, source snippets, or audit payloads. History snapshots are sanitized; vector/shared/audit policy remains.
- [~] Add provider capability checks and conformance tests for graph/vector/history providers. History and retrieval protocol fakes are covered; full graph/vector provider capability checks remain.
- [ ] Defer `customer_managed`, `dedicated_managed`, and `enterprise_audit` modes until the local provider contracts are stable.

---

## Phase 11: Tenant API Contract Graph 📋 FUTURE TEAM/ENTERPRISE
Goal: add tenant-level service/API awareness after the local product is stable. Each project indexes and publishes its own safe API facts; the tenant graph links those facts across services, systems, schemas, and events. This is not required for the single-tenant local release.

> **Spec:** [spec_tenant_api_graph.md](spec_tenant_api_graph.md).

### 11.1 Project API Contract Indexing
- [ ] OpenAPI/Swagger parser for endpoint, operation, and schema facts.
- [ ] GraphQL SDL parser for query/mutation/type facts.
- [ ] protobuf/gRPC parser for service, RPC, and message facts.
- [ ] AsyncAPI/event parser for topics, producers, consumers, and schemas.
- [ ] Route declaration extraction from supported language adapters.
- [ ] Generated client / SDK call-site extraction for outbound dependency edges.

### 11.2 Tenant Manifest Publication
- [ ] `ContractManifest` publication unit with tenant, workspace, ref, graph_version, service, version, and published_at.
- [ ] Manifest diffing for added/removed/renamed endpoints and schema compatibility warnings.
- [ ] Stable endpoint, schema, event, and service fingerprints for cross-project matching.
- [ ] Ambiguity handling: low-confidence links and warnings instead of silent winner selection.

### 11.3 Tenant Graph Links
- [ ] Node labels: `Service`, `ApiEndpoint`, `ApiSchema`, `ApiField`, `EventTopic`, `ExternalSystem`, `ContractManifest`.
- [ ] Edge types: `EXPOSES_ENDPOINT`, `CALLS_ENDPOINT`, `IMPLEMENTS_ENDPOINT`, `USES_SCHEMA`, `PRODUCES_EVENT`, `CONSUMES_EVENT`, `DEPENDS_ON_SERVICE`, `VERSION_OF`, `BREAKS_CONTRACT`.
- [ ] Tenant/workspace/RBAC scoping on every cross-project API query.
- [ ] External systems represented as metadata nodes, not crawled dependencies.

### 11.4 Direction-Aware Retrieval
- [ ] Retrieval ladder: `symbol -> file -> workspace -> tenant_api_graph -> direct_llm`.
- [ ] `api_direction` options: `outbound_dependencies`, `inbound_consumers`, `contract_impact`, `internal_processing`, `bidirectional_contract`.
- [ ] `tenant_link_depth` traversal cap: default 1, hard cap 2, depth means published link hops only.
- [ ] Score factors: edge type, direction weight, scope weight, depth decay, confidence, and token cost.
- [ ] Prompt Contract tier: `tenant_api_context` with service/endpoint/schema provenance and redacted metadata.

### 11.5 Privacy and Safety
- [ ] Prohibit neighboring project source reads from tenant API retrieval.
- [ ] Prohibit live external API invocation during indexing/retrieval unless a future explicit connector policy exists.
- [ ] No raw prompts, code bodies, payload examples, secrets, credentials, or auth headers in the tenant graph.
- [ ] Field-level sensitivity labels allowed; sample values forbidden by default.
- [ ] Tests prove Project A cannot trigger indexing of Project B.

---

## Risk Register

| Task | Priority | Risk | Mitigation | Status |
|---|---|---|---|---|
| Eval harness unblocker | **High** | No measurable proof of token/quality gains — all Phase 4+ claims unverified | Phase 2.5: ship fixture + CI ✅ (spec: [spec_eval_harness.md](spec_eval_harness.md)) | ✅ Resolved |
| Unmeasured quality claims | **High** | "60–80% reduction" cannot be verified without eval harness | Phase 2.5 blocks Phase 4 ✅ (ADR-006) | ✅ Resolved |
| Missing extension UI | **High** | "VS Code integration" premise unproven; `run_demo.py` doesn't validate product | Phase 2.5: promote extension scaffold from Phase 1 ✅ | ✅ Resolved |
| Token overhead limit | High | Context expansion can spend budget on redundant or low-utility bodies. | Axis bundles deduplicate by UID and use intent-aware token-credit packing; reproducible all-files-vs-selected token baselines remain open. | 🟡 Mitigated |
| Embedding model drift | High | Switching embedding models without versioning causes silent quality loss | Embedding metadata and result-set mismatch guards are implemented; recovery is wipe + reindex, not an automated migration. | 🟡 Mitigated |
| Tree-sitter multi-language | High | Complexity of supporting many languages | ADR-005 LanguageAdapter protocol (spec: [spec_language_adapter.md](spec_language_adapter.md)); formalize in Phase 1 polish, defer extra languages to Phase 3.5 | 🟢 Mitigated |
| Rigid graph traversal | High | Real questions span modules via calls, imports, inheritance, type flow, and framework bridges. | Axis edge profiles, grouped directional walks, role lookahead, and token-credit packing are active; long-tail validation remains. | 🟡 Mitigated |
| Missing incremental index | High | Full re-scan on every save breaks the <200ms SLO | Phase 3.5 file-level dirty tracking ✅ + Phase 5 AFFECTS rebuild ✅ | ✅ Resolved |
| Doc-code semantic linking | **High** | Weak or broad COVERS matches can add noisy documentation evidence. | Typed/confident COVERS edges and owner-linked docstring/JSDoc seeds are implemented; general markdown quality is not yet propagated through normal axis prompts. | 🟡 Mitigated |
| Embedding leakage to cloud | Medium | Vector inversion can recover source text — contradicts ADR-001 spirit | Phase 7: Security ADR before cloud vector sync | 🟡 Pending Phase 7 |
| Intent classification immaturity | Medium | Mixed queries can still under-serve secondary structural evidence. | The active axis classifier derives structural intent roles and uses them for graph depth and token budgets; learned classification, calibration, and richer prompt metadata remain open. | 🟡 Mitigated |
| Graceful degradation reliability | Medium | Standard mode must be robust fallback when surgical context unavailable | Tier-aware assembly, mode flag, orchestrator fallback, and degraded context-only `/ask` responses are implemented. | ✅ Resolved |
| Model Router misclassification | Medium | Misclassification can send a complex task to a cheaper model | Model routing and Claude→Ollama fallback are implemented; remaining mitigation is benchmark tuning and clearer extension surfacing. | 🟡 Mitigated |
| Enterprise Neo4j image in dev | Low | Licensing ambiguity for open-source contributors | Switch to `community` edition in Phase 1 polish ✅ | ✅ Resolved |
| **Incremental index split-brain** | **Critical** | Graph storage can commit symbol/edge changes while vector storage writes fail or the process is killed. Graph/vector stores then disagree silently. | Durable job log + retry/dead-letter states implemented; next: idempotent replay worker or rollback strategy | 🟡 Mitigated |
| **IDE event storm** | **High** | Mass refactor, find/replace across many files, or `git stash pop` can flood the context_engine with parse/embed/index work. | Bounded context_engine queue + VS Code save batching implemented; next: branch-sync enqueue integration | 🟡 Mitigated |
| **Git branch cache invalidation** | **High** | Checkout changes many ASTs at once; full reindex is slow, stale graph/vector versions are wrong. | Git state tracker + changed-file detection implemented; next: queue integration and vector cache keys | 🟡 Mitigated |
| **Local embedding compute cost** | **Medium** | Re-embedding changed symbols on every save can consume CPU/GPU and degrade editor responsiveness. | Content-hash embedding cache + configurable encode batch/throttle/low-priority mode implemented | ✅ Resolved |
| **UID instability** | **Critical** | Old `sha256(file_path:name)` broke on rename/move and collided on overloads + nested funcs. | Stable UID v2 implemented; migration CLI remains cleanup | 🟡 Mitigated |
| **Naive CALLS resolution** | **Critical** | Name-match across whole graph; collisions across modules/methods; imports ignored. Noise in BFS → precision cap. | Python scoped/imported/dynamic resolver implemented; TS deep resolver remains cleanup | 🟡 Mitigated |
| **No workspace isolation on managed graph provider** | **Critical** | Multi-user/team graph storage can collapse branches/tenants into one graph; wrong-version bodies returned silently. | Workspace node + scoped graph reads/writes implemented | 🟡 Mitigated |
| **Unsandboxed filesystem paths** | **High** | With `AUTH_REQUIRED=false`, API and stale graph nodes could read arbitrary local files. | `workspace_paths` on API paths + `CodeResolver` graph reads + Neo4j prune on manifest | ✅ Resolved |
| **Queued index without manifest** | **High** | Extension `queue=true` on `/index` left no `project_path` until batch finished → 400 on `/overlay`. | `register_workspace_project_root()` before enqueue | ✅ Resolved |
| **Unbounded API limits** | **Medium** | Huge `token_budget` / `limit` → local DoS and cloud cost spikes. | Pydantic bounds on request models (`tests/unit/test_api_bounds.py`) | ✅ Resolved |
| **Retired Claude Sonnet 4.0 model ID** | **Medium** | Hardcoded `claude-sonnet-4-20250514` fails after 2026-06-15 retirement. | Default `claude-sonnet-4-6` + `ANTHROPIC_MODEL` env | ✅ Resolved |
| **Overlay cross-user leakage** | **Medium** | Shared overlay keys could expose unsaved buffers across users in one workspace. | Overlay keyed by `(workspace_id, user_id, file_path)` | ✅ Resolved |
| Graph + semantic retrieval siloed | High | Semantic seeds, graph expansion, and documentation can compete without one budget view. | Axis combines semantic symbol seeding with graph expansion and token-credit packing; general markdown still enters only fallback paths. | 🟡 Mitigated |
| Primary-intent routing | High | Mixed queries (e.g. debugging+refactor) can under-serve secondary evidence. | The axis classifier returns structural intent roles and uses them for retrieval budgets; real-repo calibration and richer prompt serialization remain open. | 🟡 Mitigated |
| Flat DocAnchor links | Medium | Definition, example, and passing-mention links should not carry equal weight. | `anchor_type`, `confidence`, `primary_bias`, and resolver metadata are persisted; owner anchors feed axis seeds, while general markdown quality consumption remains partial ([spec_doc_anchor_confidence.md](spec_doc_anchor_confidence.md)). | 🟡 Mitigated |
| Retrieval observability polish | High | Contract fields exist, but extension/debug surfaces still need to make the retrieval story easy to inspect. | The serializer exposes scores, provenance, pruning, route, trace, and workspace fields; the active axis adapter leaves several values sparse ([spec_prompt_contract_observability.md](spec_prompt_contract_observability.md)). | 🟡 Mitigated |
| Cache activation gap | Medium | Three cache APIs exist, but only response caching is active in normal requests. | L3 avoids repeated model calls and is invalidated by indexed files; L1/L2 are dormant and shared/multi-instance backends remain future ([spec_retrieval_cache.md](spec_retrieval_cache.md)). | 🟡 Mitigated |
| Learning loop incomplete | Medium | Feedback telemetry exists, but retrieval does not yet adapt from usage. | Phase 10.2 has feedback tokens, snapshots, endpoint, privacy boundaries, and counters; EMA tuning, learned `CO_RELEVANT` edges, and training loops remain open ([spec_learning_loop.md](spec_learning_loop.md)) | 🟡 Mitigated |
