# Surgical Context - Road Map

> **Status:** This branch (`context-engine-refocus`) treats Surgical Context as a **local-first, model-agnostic context engine for code understanding and change impact**.
>
> **Release target:** a Local Developer Product in VS Code with the Python sidecar, local graph/vector/history defaults, and a trustworthy `Ask / Inspect / Impact` loop.
>
> **Principle:** measure retrieval quality and token efficiency on real repositories before expanding platform scope.
>
> **See also:** [concept.md](concept.md), [product_direction_memo.md](product_direction_memo.md), [review_findings_2026-04-17.md](review_findings_2026-04-17.md), [docs/README.md](README.md)

---

## Product Direction

The local product is still the canonical next milestone, but the product is now described more narrowly.

### v0.1 Goal

Deliver a local VS Code tool that can answer:

- what does this code do?
- what supports this answer?
- what might this change break?

without wasting tokens or hiding retrieval behavior.

### In Scope

- VS Code surfaces: Chat, Inspector, Impact, Settings, Dashboard
- Python sidecar running locally
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
- microservice split of the sidecar
- parser/indexer rewrites before profiling proves a bottleneck
- "general autonomous coding agent" competition as a release goal

### Single-Tenant Default

Keep `workspace_id` and `tenant_id` in contracts, but default `tenant_id` to `local`. Multiple local workspaces are allowed; cross-project tenant graph traversal is not required for the local release.

---

## Canonical Backlog

### P0 - Refocus and Truth
- [x] Rewrite product-facing docs around the context-engine thesis (`concept.md`, `idea_summary.md`, `road_map.md`, `README.md`).
- [ ] Keep the local release boundary explicit: no platform or enterprise scope expansion without a measured reason.
- [x] Align the benchmark story around real repositories and real developer questions.

### P1 - Local Daily-Driver Loop
- [x] Clean local bootstrap and smoke path.
- [x] Local history and request snapshots.
- [ ] Finish streaming and selected-request synchronization so `Ask / Inspect / Impact` always point to the same request.
- [ ] Keep dashboard, settings, and health states useful when providers are missing, local-only, or degraded.
- [ ] Add small but solid accessibility/keyboard polish for the extension surfaces.

### P2 - Retrieval Quality and Observability
- [x] Treat Phase 9.1 (Unified Ranker) and Phase 9.4 (Prompt Contract Observability) as the current retrieval-quality path, not deferred cleanup.
- [x] Soft fallback ladder for missing symbols.
- [x] Finish remaining prompt-contract fields: `pruned[]`, ranker weights, intent distribution/confidence, ambiguous-intent signal.
- [~] Add doc-anchor confidence/type metadata so definitions, examples, warnings, and passing mentions do not rank equally. (Core metadata exists; calibration + UI surfacing remain.)
- [x] Keep retrieval cache behavior visible in `metadata.assembly.cache_hits`.
- [~] Add latency SLO checks for local asks and index operations. `/ask`, `/ask/stream`, and `/search/unified` emit request/stage SLO metrics; index queue counters exist, but index-duration SLO gates remain.
- [ ] Make model route, fallback level, and rough token/cost signals easy to inspect from the extension.
- [~] Extend canonical role coverage beyond the current green baselines: grow capability-role inference, wrapper-body support, topic-aware impact retrieval, module/package fallback targets, and generic dependency/provider trace recovery. Flask/Django/Express tails remain.
- [ ] Treat impact analysis as shallow until proven otherwise: current `AFFECTS` is bounded reverse reachability, not a full causal blast-radius model across framework registries, generated APIs, templates, runtime dispatch, and tests.

### P3 - Real-Repo Validation
- [x] Adapt the QA harness to use the real-repo question pack in [tests/fixtures/real_repo_question_pack.yaml](../tests/fixtures/real_repo_question_pack.yaml).
- [x] Start with the `core12` subset, then expand to the full 24-question pack.
- [ ] Compare naive context vs Surgical Context vs heavy stuffing on 2-3 real repositories.
- [ ] Record token deltas, latency, fallback behavior, and human-reviewed grounding quality.
- [x] Use benchmark results to tune ranking rather than tuning by intuition.

Current snapshot:
- FastAPI local retrieval is mostly green; dependency trace roles are recovered through generic dependency/provider signals rather than framework-symbol pairs. Remaining warning-class cases are file/precision tails.
- Pydantic and Redux Toolkit local packs are broadly green with remaining precision tails on broad/doc-heavy questions.
- Flask and Django mostly pass with role/file-coverage tails; Express still needs target-resolution and JS export-shape improvements before meaningful ranker tuning.
- **surgical_context** (May 2026): **7/7 pass** with TS `object_api` indexing + `ts_http_route_hints`; query-topic recovery pulls explicit pipeline stages such as `ranking` / `PromptContext`, and the relaxed `trace_dependency` gate converts near-perfect single-axis recall to pass.
- **dathund** (May 2026): **8/8 pass** after broadening trace recovery to identity/principal resolution and time-authority clock/window flows; no Dathund-specific fixtures required.
- `UnifiedRanker` decomposition (big-bang first cut) is implemented: new `sidecar/context/ranker/*` components (`TargetSelector`, `GraphCandidateSource`, `VectorCandidateSource`, `RoleBackfill`, `BudgetSelector`, `SubgraphAssembler`) are wired, with `UnifiedRanker` kept as compatibility facade.

### P4 - Provider Boundaries, Defaults First
- [ ] Define `GraphProvider` protocol around the methods the sidecar already uses and wrap `Neo4jClient` as the default implementation.
- [ ] Define `VectorProvider` protocol around the methods the sidecar already uses and wrap `LanceDBClient` as the default implementation.
- [x] Implement SQLite local history with conversations, messages, ask snapshots, inspector snapshots, impact snapshots, retention pruning, and `disabled` / `ephemeral` modes.
- [~] Add provider config only for local defaults first: history has `local`, `ephemeral`, and `disabled`; graph/vector config boundaries remain.
- [~] Put storage policy above all providers for prompt text, response text, source snippets, retention, redaction, and sharing. Local history sanitization exists; broader vector/shared/audit policy remains.
- [~] Add fake/in-memory provider conformance tests before adding real alternate backends. Retrieval protocol fakes and history tests exist; full graph/vector connector conformance remains.

### P5 - Future Team and Enterprise Horizon
- [ ] Add roles `admin` and `user`, then map permissions onto indexing controls, audit/history access, model/provider settings, and graph queries.
- [ ] Add connectable documentation sources through `DocSourceProvider`: repository docs first, then Confluence, Figma, and future sources.
- [ ] Add parallel indexing only after local profiling identifies the real bottlenecks.
- [ ] Add customer-managed/dedicated provider modes for graph, vector, and history stores.
- [ ] Add Tenant API Contract Graph for project-published API facts and tenant-level service links; no neighboring source scans.
- [ ] Add optional LLM Proxy Gateway transport for organizations that need provider-account policy, auditing, masking, quotas, or fallback outside the sidecar.
- [ ] Split the sidecar into services only when scale requires it.
- [ ] Consider Rust/Go/C parser or indexer hot paths only after a performance review proves Python orchestration is the bottleneck.

---

## Immediate 3-Week Plan

### Next execution lanes (agreed)

1. **Baseline lock (green lanes)**  
   Keep `fastapi`, `pydantic`, `redux_toolkit`, and `sqlalchemy` as control baselines; avoid broad ranker changes that regress these packs.

2. **Python tail closure (non-impact)**  
   Prioritize residual role/file tails in `django` and `flask` (trace/explain questions) without widening the impact-analysis scope.

3. **JS framework target-resolution lane**  
   Treat `express` / `vue` / `nestjs` failures primarily as symbol/exports/target-resolution work before ranker-weight tuning.

4. **Impact-analysis lane (deferred)**  
   Continue to treat impact as a separate iteration after the non-impact retrieval lanes stabilize.

### Immediate Retrieval Focus
- keep broadening the now-shipped canonical role taxonomy beyond the current green baselines
- keep using real-repo benchmark reports plus `ready_context` payloads to debug misses before changing weights
- continue precision work on doc-heavy and broad RTK/Pydantic paths, and on Flask/Django role/file-coverage tails
- finish doc-anchor confidence/type calibration and extension surfacing so docs stop acting like undifferentiated semantic noise
- define index-time repository readiness and mechanism discovery, including an explicit impact-readiness signal so unsupported or shallow impact results are not mistaken for ranker failures

### Week 1
- tighten remaining product docs around the current local-first context-engine thesis (completed; keep as maintenance)
- finish `Ask / Inspect / Impact` synchronization and route visibility
- expose the same retrieval metadata already present in the contract more clearly in the extension surfaces

### Week 2
- run the `core12` questions on FastAPI, Pydantic, and Redux Toolkit after each retrieval change, then spot-check the full RTK pack when mechanism routing changes touch JS/TS behavior
- review which answers were grounded, weak, or overstuffed, then patch ranker and doc-link blind spots immediately
- keep benchmark snapshots by repo in `QA/benchmark_runs.jsonl` and inspect them with `QA/benchmark_runs.py` so regressions and pruned-candidate patterns are easy to spot in review
- run an independent/pre-registered role-label pass for the real-repo pack before claiming that saturated `role_recall=1.00` proves complete mechanism coverage

### Week 3
- tune ranking and fallback behavior from measured results, with canonical roles and mechanism coverage as the main lane
- decide whether the next phase is still local product hardening or extraction of a reusable context/routing backend

---

## Completed Stabilization Backlog

This section preserves the post-MVP hardening record. Completed items remain useful context, but the active product direction is the Local Developer Product backlog above.

### P0 - Truth, Safety, and API Hardening
- [x] Refresh `docs/README.md` as the current-truth entry point; archive or label historical analysis when status changes.
- [x] Fix sidecar DB lifecycle: remove mutable request identity from the global client; use request-scoped user context.
- [x] Move doc resolution inside the arbitration pipeline before `PromptCompiler.compile_with_intent()`.
- [x] Add typed API response models and JSON-safe SSE framing for `/ask/stream`.
- [x] Add durable indexing job log with retry/dead-letter states so Neo4j and LanceDB cannot silently diverge after partial failure.
- [x] Add first endpoint tests for `/ask`, `/ask/stream`, `/index/file`, `/impact`, `/audit/actions`, and `/auth/token`.
- [x] Add auth-boundary enforcement tests for protected endpoints with `AUTH_REQUIRED=true`.
- [x] Workspace path sandboxing: normalize `file_path` / index paths under the registered `project_path` from the index manifest; reject paths outside the workspace root (`403`) so local callers cannot read or index arbitrary files when `AUTH_REQUIRED=false` (`sidecar/workspace_paths.py`, `spec_sidecar_api.md`).

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
- [x] FastAPI/JSON-RPC sidecar entrypoint (`sidecar/main.py`)
- [x] Switch Docker image from `neo4j:5.12-enterprise` to `neo4j:5.12-community` for open-source dev baseline (enterprise license only where intentionally required)
- [x] Move `NEO4J_AUTH` out of `docker-compose.yml` into `.env` with `.env.example` committed

### Parsing (ETL)
- [x] tree-sitter integration for Python (`sidecar/parser/extractor.py`)
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

> **Note:** [review_findings_2026-04-17.md](review_findings_2026-04-17.md) recommends promoting this to Phase 2.5 — it blocks external validation as much as the eval harness. ✅ Complete.

---

## Phase 2: Graph Brain & Surgical Retrieval ✅ Largely Complete
Goal: System can navigate the graph and gather precise context.

### Graph Logic
- [x] Neo4j client: upsert file/symbol nodes (`sidecar/database/neo4j_client.py`)
- [x] Four-phase indexer: symbols → calls → symbol embeddings → pending resolution (`sidecar/indexer/code.py`)
- [x] BFS Cypher query for dependency discovery (`sidecar/context/arbitrator.py`)

### Data Contract
- [x] JSON Prompt Contract: typed `PromptContext` with `to_dict()` + `to_system_prompt()` (`sidecar/context/arbitrator.py`)
- [x] Local LLM integration via Ollama (`sidecar/main.py` — llama3, configurable via `OLLAMA_MODEL`)
- [x] Fallback behavior when Ollama is unreachable (clear error, degraded `/ask` that still returns `context`)

### Dirty State
- [x] In-Memory Overlay: parse unsaved changes and merge with graph (`sidecar/context/overlay.py`, `POST /overlay`, `DELETE /overlay`)

---

## Phase 2.5: Quality Foundation & Extension UI ✅ COMPLETE
Goal: Make the system **measurable** before scaling it, and ship a thin client for real-world validation. Without this phase, all later performance and cost claims are unfalsifiable, and the "VS Code integration" premise remains unproven.

> **Specs:** [spec_eval_harness.md](spec_eval_harness.md) (fixture design, metric set, CI contract), [review_findings_2026-04-17.md](review_findings_2026-04-17.md) (sequencing and rationale).

### Evaluation Harness ✅ COMPLETE
- [x] `tests/` directory with pytest for parser, arbitrator, overlay, indexer
- [x] Golden fixture repo under `tests/fixtures/sample_project/` (8 files, ~30 symbols, all topologies covered)
- [x] Retrieval benchmark: 10 curated (question → expected_symbols) pairs in `questions.yaml`
- [x] `QA/qa_benchmark.py` reframed as reproducible metric runner (emits JSON: recall@k, precision@k, tokens, latency)
- [x] CI config (GitHub Actions) running tests + benchmark on every PR (deferred: needs Neo4j services)

### Observability
- [x] Structured logging across pipeline stages (Phase 5 prerequisite)
- [x] `GET /metrics` endpoint (Prometheus text format)
- [x] Per-request trace ID threaded through logs
- [x] Latency SLO tracking against 200ms target
- [x] Distributed tracing via OpenTelemetry (Phase 6, scale phase only)

### Token Accounting ✅ COMPLETE
- [x] Token counter (tiktoken cl100k_base) on every `PromptContext`
- [x] `PromptContext.token_count()` method
- [x] Per-request breakdown: `tokens_primary`, `tokens_graph`, `tokens_docs`
- [x] Baseline: "carpet-bomb" estimation (all files) vs surgical count

### Extension UI (Promoted from Phase 1) ✅ COMPLETE
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
- [x] LanceDB integration — two tables: `docs` + `symbols` (`sidecar/database/lancedb_client.py`)
- [x] Markdown processing pipeline: section-aware chunking + embedding generation (`sidecar/indexer/docs.py`)

### Semantic Connections
- [x] DocAnchor in Neo4j: `chunk_id`-only node, `[:FROM]` to File, typed/confident `[:COVERS]` to Symbols, lazy `pending` resolution via LanceDB (`sidecar/indexer/anchor.py`)

### RAG Optimization
- [x] Hybrid Search: Vector Search (semantics) → Graph Expansion (code) (`/ask` appends top-3 doc chunks to context)
- [x] Symbol body embeddings: `symbols` LanceDB table for semantic DocAnchor matching (`indexer_main.py` Phase 3)
- [x] Section-aware doc chunking: headings-first split, word-window fallback (`sidecar/indexer/docs.py`)
- [x] Gitignore-aware indexer: `pathspec` prunes ignored dirs/files (`indexer_main.py`)
- [x] ADR-001 enforced: no data on Neo4j nodes — `file_path` removed from Symbol and DocAnchor

---

## Phase 3.5: Arbitration & Indexing Robustness ✅ COMPLETE
Goal: Make retrieval correct and fast on a live developer's laptop. This is what separates "demo" from "daily driver." Token-budget BFS is tuned against the eval harness from Phase 2.5 (now complete).

> **Spec:** [spec_token_budget_bfs.md](spec_token_budget_bfs.md) — best-first traversal replacing hardcoded `*1..2`, with scoring function, algorithm, contract additions, and tuning protocol.

### Context Budgeting & Ranking ✅ COMPLETE (Token-Budget BFS)
- [x] Token budget parameter on `/ask` (default 4000)
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
- [x] Symbol-level diff: only re-upsert nodes where `Symbol.hash` changed (optimization, deferred)
- [x] Background debounce queue: batch rapid-fire saves (`sidecar/indexer/queue.py`, `POST /index/files`)
- [x] Backpressure for mass IDE events: bounded queue, batch coalescing, and stale job cancellation

### Graph Completeness ✅ COMPLETE
- [x] `IMPORTS` edge between Files to enable correct cross-module call resolution
- [x] `DEPENDS_ON` edge for type / interface / import usage (Symbol→Symbol edge type for inheritance/interface dependencies)
- [x] Unit tests: 18 tests verify `CALLS`, `IMPORTS`, `DEPENDS_ON` edge extraction for Python and TypeScript
- [x] Arbitrator BFS expanded to traverse all three edge types for context gathering

### Embedding Quality (DEFERRED — Phase 5)
- [x] Add reusable embedding benchmark harness for golden-set model comparisons (`python -m sidecar.eval.embedding_benchmark`)
- [x] Run and record `all-MiniLM-L6-v2` vs a code-native model (e.g. `bge-code`, `unixcoder`) on the golden set
  - 2026-04-21 benchmark: `all-MiniLM-L6-v2` reached `target_hit@5=1.00`, `MRR=0.78`, `expected_recall@5=0.42`, `expected_precision@5=0.52`; `microsoft/unixcoder-base` reached `target_hit@5=1.00`, `MRR=0.78`, `expected_recall@5=0.45`, `expected_precision@5=0.58`.
- [x] Embedding cache keyed by content hash to avoid recomputation on re-index

---

## Phase 4: Quality & Scaling (NEXT ITERATION)
Goal: Reduce token overhead and prepare for multi-model / multi-user environments.

> **Reference:** [architectural_review.md](architectural_review.md#phase-4-near-term-wins) — detailed evaluation of all improvement ideas by impact/effort.

### Context Deduplication ✅ COMPLETE
> **Spec:** [spec_context_deduplicator.md](spec_context_deduplicator.md) — insertion point, dedup rules, budget recalculation, test matrix.
- [x] Implement `ContextDeduplicator` — pure transform between GraphExpander and PromptCompiler
- [x] Normalize symbol identity by UID; keep lowest-depth copy on duplicates
- [x] Collapse overlapping line ranges within same file
- [x] ~~Deduplicate doc chunks with >85% content overlap~~ (deferred: performance cost exceeded benefit)
- [x] Update `budget["dedup_saved"]` for observability
- [x] Integration: pipeline now expand → deduplicate → resolve → compile (9 unit tests passing)

### Embedding Versioning ✅ COMPLETE
> **Spec:** [spec_embedding_versioning.md](spec_embedding_versioning.md) — metadata schema, model registry, cross-model guard, migration CLI.
- [x] Add `embedding_metadata` JSON column to `docs` and `symbols` LanceDB tables
- [x] Model registry in `sidecar/database/embedding_registry.py` — known models + dimensions
- [x] Write path: record model_name, model_version, chunk_hash, embedding_hash per row
- [x] Read path: guard against cross-model queries (raise `EmbeddingModelMismatch`)
- [x] Migration CLI: `python -m sidecar.database.embedding_migration status / migrate`

### Graph Richness (Phase 5 planning) ✅ COMPLETE
- [x] Feasibility assessment: dynamic dispatch detection in Python/TypeScript parsers
  - Result: Python classifies direct/scoped/imported/dynamic/inferred calls; TypeScript now classifies top-level identifier calls as direct and member dispatch (`this.method()`, `service.method()`) as dynamic.
- [x] Spec review: [spec_typed_semantic_edges.md](spec_typed_semantic_edges.md), [spec_affects_index.md](spec_affects_index.md)
  - Result: both specs have corresponding Phase 5 implementation paths in `sidecar/parser`, `sidecar/indexer/affects.py`, and BFS typed-edge traversal.
- [x] Decision gate: prioritize typed edges vs AFFECTS index for Phase 5 first milestone
  - Result: resolved by shipping both; typed call edges feed the materialized AFFECTS index.

---

## Phase 5: Typed Semantic Edges & Reverse Dependencies ✅ COMPLETE
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
- [x] `GET /impact?symbol=<name>` endpoint — returns affected symbols, affected files, impact metrics
- [x] Enables cascade-aware incremental reindexing (Phase 3.5 dirty tracking + Phase 5 AFFECTS = full incremental pipeline)

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
| Context assembly | Working (ContextArbitrator orchestrates: expand → deduplicate → resolve → compile) |

### Deferred to Phase 6+
- [ ] IMPLEMENTS / OVERRIDES / REFERENCES edge creation (data structure exists, parser detection TODO)
- [ ] Execution semantics (ExecutionEdge with runtime probability) — need empirical call-trace data
- [ ] Transitive doc reference linking (depth > 1)
- [ ] Streaming responses & model routing (moved to Phase 6)

---

## Phase 6: Intent Classification & Graceful Degradation (IN PROGRESS — 6.1 COMPLETE)
Goal: Adaptive context assembly based on query type; fallback to standard LLM mode when no surgical context available.

> **Specs:** [spec_intent_classifier.md](spec_intent_classifier.md) — design spec complete; implementation ongoing.

### Phase 6.1: Intent Classifier ✅ COMPLETE
- [x] `IntentClassifier` class with keyword-based intent detection (heuristics, ML upgrade in Phase 7)
- [x] 7 intent types: navigation, debugging, refactor, exploration, new_feature, design_question, impact_analysis
- [x] `IntentConfig` with 6-tier priority orderings per intent (code, cross_refs, specs, architecture, concept, idea)
- [x] Add `mode` field to `PromptContext`: "surgical_full" | "surgical_doc_only" | "standard"
- [x] Add `intent` field to `PromptContext` for tracking detected query type
- [x] `PromptCompiler.compile_with_intent()` — tier-aware context assembly with graceful degradation
- [x] Doc type inference from filename patterns (spec_*, idea_*, concept, architecture)
- [x] Unit tests: 17 intent classifier tests + 19 compiler tests (all passing)

### Phase 6.2: Graceful Degradation in Orchestrator ✅ COMPLETE
- [x] Integrate `IntentClassifier` with `ContextArbitrator`
- [x] Call intent detection in `get_context_for_symbol(question)` with optional question parameter
- [x] Pass intent to `compile_with_intent()` instead of `compile()`
- [x] Surface `mode` and `intent` in `/ask` response JSON (via PromptContext.to_dict())
- [x] Add integration tests: 11 tests for intent classification + mode field handling
- [x] Backward compatibility: question parameter optional (defaults to empty string → exploration intent)

### Phase 6.3: Streaming & Model Routing ✅ COMPLETE
- [x] Streaming LLM responses (SSE) via `/ask/stream` endpoint
- [x] Official Anthropic SDK activation (`sidecar/ai/engine.py`) with prompt caching on `graph_context` block
- [x] Model Router (ADR-004) — route by context size + intent
  - Large contexts (>= 2k tokens) → Claude (powerful, cached)
  - Complex intents (design, exploration, refactor) → Claude
  - Small/simple queries → Ollama (fast, cheap)
- [x] Support 3 preferences: 'claude' (default) | 'ollama' | 'auto' (intelligent routing)
- [x] Prompt caching: ephemeral cache on graph_context blocks (reduce API costs)
- [x] Automatic fallback: Claude → Ollama on error
- [x] 21 new unit tests covering routing, initialization, model selection

### Phase 6.4: Integration Testing & Observability ✅ COMPLETE
- [x] Test intent classification accuracy on 7 intent types plus prompt-compiler tier behavior
- [x] Test tier-based budget allocation per intent
- [x] Test graceful degradation (no matches → standard mode)
- [x] Test mode field serialization in PromptContext.to_dict()
- [x] Per-tier token counting for observability (code, cross_refs, specs, architecture, concept, idea)
- [x] Metadata block: query_intent + tiers_used in JSON response

### JSON Prompt Contract — Phase 6 Complete
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
- [x] Overlay keyed by `workspace_id`
- [ ] Workspace lifecycle: create-on-index, delete cascade, TTL-based GC

---

## Phase 9: Unified Retrieval & Observability 🚧 ACTIVE (9.1, 9.3 COMPLETE; 9.4 MOSTLY SHIPPED)
Goal: Merge graph + semantic retrieval into a single ranked pool; surface the scores in the contract so we can debug, tune, and eventually learn from them.

> **Specs:** [spec_unified_ranking.md](spec_unified_ranking.md), [spec_multi_label_intent.md](spec_multi_label_intent.md), [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md), [spec_doc_anchor_confidence.md](spec_doc_anchor_confidence.md).
>
> **Current status:** 9.1 (unified ranker) and 9.3 (doc-anchor confidence) are shipped. 9.4 (contract observability) is mostly shipped; the remaining gap is consuming multi-label intent metadata in budget/routing policy. Full multi-label routing is deferred to Phase 10. Real-repo benchmark warnings now mostly expose file/precision and export-shape tails rather than missing framework-specific defaults.

### 9.1 Unified Ranker ✅ COMPLETE
- [x] `UnifiedRanker.rank()` — single pool from graph BFS + vector search
- [x] Blended score = α·graph + β·semantic + γ·intent + δ·overlap − ε·cost (per-track normalized)
- [x] Overlap bonus when both signals fire on the same candidate
- [x] Budget-fill loop competes symbols and doc chunks on identical terms
- [x] Weight tuning via eval harness sweep
- [x] Decompose `UnifiedRanker` internals into focused components under `sidecar/context/ranker/`, while preserving `get_target(...)`, `rank(...)`, `candidates_to_subgraph(...)` contracts used by Arbitrator/QA
- [x] Target disambiguation for duplicate symbol names within one workspace
- [x] Module/package fallback targets for package-surface questions
- [x] Topic-aware subsystem noise control for focused API questions, so distant graph links through broad helpers do not crowd out relevant runtime/doc candidates
- [x] Better mechanism routing/backfill for serialization impact (`serialize_response`-style flows now route to impact roles and targeted tests)
- [ ] Budget-safe primary-source truncation/signature mode reflected consistently in benchmark + prompt contract (deferred)
- [~] Better graph/doc/recovery coverage for structurally sparse runtime mechanisms through generic semantic hints, import recovery, and dependency-flow role recovery; remaining work is precision/file-recall telemetry.

### 9.2 Multi-Label Intent 🚧 METADATA DONE, ROUTING DEFERRED
- [x] `IntentSignal.distribution` (sum-to-1 keyword weights across supported intents)
- [x] Classifier returns partial scores per label → normalized distribution
- [ ] Tier priority = weighted sum across intent distribution
- [ ] Budget split across tiers in proportion to blended tier score (floor per tier)
- [x] `ambiguous` signal in the prompt contract for client UX / routing decisions
> **Decision:** Full multi-label routing is punted to Phase 10 pending real-repo validation of 9.1 performance. Phase 9.1 primary-intent routing is sufficient for local v0.1 launch, while distribution/confidence/ambiguous metadata remains visible for debugging.

### 9.3 DocAnchor Confidence & Type ✅ COMPLETE
- [x] Anchor type classification: definition / example / reference / warning / deprecated
- [x] Per-edge confidence score (resolver + name mention + heading + code-style mention signals)
- [x] Multi-symbol weighting: `primary_bias` = 1.0 for single/focal symbol, reduced for secondary symbols
- [x] Edge properties: `anchor_type`, `confidence`, `primary_bias`, `resolver`
- [x] UnifiedRanker consumes anchor quality for doc graph boost and DocAnchor bridge provenance
- [x] Prompt contract surfaces `documentation[].anchor_type`, `anchor_confidence`, `primary_bias`, and nested `anchor`

### 9.4 Prompt Contract Observability 🚧 IN PROGRESS (~85% COMPLETE)
- [x] Per-candidate basic `scores` block (graph relevance / semantic score)
- [x] `provenance` list on every symbol and doc chunk
- [x] Budget-level `metadata.pruning_reasons`
- [x] `metadata.assembly.*` — per-phase latencies, trace_id, workspace_id, resolver_version
- [x] Surface target-selection/disambiguation reasoning when multiple same-name symbols exist
- [x] `pruned[]` array — candidates that missed the budget, with reason, scores, cost, roles, noise factor, and provenance
- [x] `metadata.ranker.weights` — tuning state snapshotted with every response
- [x] `intent.distribution` + `intent.ambiguous` + `intent.confidence` in the prompt contract
- [ ] Ranker budget policy consumes multi-label intent distribution instead of primary intent only (Phase 10)

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
- [x] Message protocol between webview ↔ extension host and extension host ↔ sidecar.
- [x] VS Code manifest structure (viewsContainers, commands, menus, keybindings, configuration).
- [ ] Streaming chat integration with `/ask/stream` JSON-safe SSE events.
- [x] Token budget, selected mode, query intent, and model route display.
- [ ] Keyboard shortcuts and accessibility (ARIA labels, focus management, screen reader support).
- [ ] VS Code settings UI for sidecar URL, model preference, workspace ID, keyboard shortcuts, and auth token .
- [ ] Full implementation of all four UI surfaces in TypeScript/React.

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
| Missing extension UI | **High** | "VS Code integration" premise unproven; `run_demo.py` doesn't validate product | Phase 2.5: promote extension scaffold from Phase 1 ✅ (per [review_findings_2026-04-17.md](review_findings_2026-04-17.md) rec #6) | ✅ Resolved |
| Token overhead limit | High | 883t baseline across all queries suggests dedup opportunity | Phase 4: ContextDeduplicator (target 15–40% reduction) ✅ | ✅ Resolved |
| Embedding model drift | High | Switching embedding models without versioning causes silent quality loss | Phase 4: embedding metadata tracking + migration flag ✅ | ✅ Resolved |
| Tree-sitter multi-language | High | Complexity of supporting many languages | ADR-005 LanguageAdapter protocol (spec: [spec_language_adapter.md](spec_language_adapter.md)); formalize in Phase 1 polish, defer extra languages to Phase 3.5 | 🟢 Mitigated |
| Rigid BFS depth | High | Real questions span modules via `IMPORTS`, inheritance, type flow | Phase 3.5 token-budget BFS ✅ + `IMPORTS` / `INHERITS` edges ✅ + Phase 5 typed edges ✅ | ✅ Resolved |
| Missing incremental index | High | Full re-scan on every save breaks the <200ms SLO | Phase 3.5 file-level dirty tracking ✅ + Phase 5 AFFECTS rebuild ✅ | ✅ Resolved |
| Doc-code semantic linking | **High** | SIMILARITY_THRESHOLD mismatch (0.4 too strict) → 36% resolution rate | Phase 5: threshold tuning (0.4 → 1.5) ✅ → 50%+ resolution | ✅ Resolved |
| Embedding leakage to cloud | Medium | Vector inversion can recover source text — contradicts ADR-001 spirit | Phase 7: Security ADR before cloud vector sync | 🟡 Pending Phase 7 |
| Intent classification immaturity | Medium | Query intent classifier remains heuristic and primary-intent routing can under-serve mixed questions | Keyword classifier, impact intent, distribution/confidence/ambiguous metadata, and prompt-contract surfacing are implemented; full multi-label routing remains Phase 10 | 🟡 Mitigated |
| Graceful degradation reliability | Medium | Standard mode must be robust fallback when surgical context unavailable | Tier-aware assembly, mode flag, orchestrator fallback, and degraded context-only `/ask` responses are implemented. | ✅ Resolved |
| Model Router misclassification | Medium | Misclassification can send a complex task to a cheaper model | Model routing and Claude→Ollama fallback are implemented; remaining mitigation is benchmark tuning and clearer extension surfacing. | 🟡 Mitigated |
| Enterprise Neo4j image in dev | Low | Licensing ambiguity for open-source contributors | Switch to `community` edition in Phase 1 polish ✅ | ✅ Resolved |
| **Incremental index split-brain** | **Critical** | Graph storage can commit symbol/edge changes while vector storage writes fail or the process is killed. Graph/vector stores then disagree silently. | Durable job log + retry/dead-letter states implemented; next: idempotent replay worker or rollback strategy | 🟡 Mitigated |
| **IDE event storm** | **High** | Mass refactor, find/replace across many files, or `git stash pop` can flood the sidecar with parse/embed/index work. | Bounded sidecar queue + VS Code save batching implemented; next: branch-sync enqueue integration | 🟡 Mitigated |
| **Git branch cache invalidation** | **High** | Checkout changes many ASTs at once; full reindex is slow, stale graph/vector versions are wrong. | Git state tracker + changed-file detection implemented; next: queue integration and vector cache keys | 🟡 Mitigated |
| **Local embedding compute cost** | **Medium** | Re-embedding changed symbols on every save can consume CPU/GPU and degrade editor responsiveness. | Content-hash embedding cache + configurable encode batch/throttle/low-priority mode implemented | ✅ Resolved |
| **UID instability** | **Critical** | Old `sha256(file_path:name)` broke on rename/move and collided on overloads + nested funcs. | Stable UID v2 implemented; migration CLI remains cleanup | 🟡 Mitigated |
| **Naive CALLS resolution** | **Critical** | Name-match across whole graph; collisions across modules/methods; imports ignored. Noise in BFS → precision cap. | Python scoped/imported/dynamic resolver implemented; TS deep resolver remains cleanup | 🟡 Mitigated |
| **No workspace isolation on managed graph provider** | **Critical** | Multi-user/team graph storage can collapse branches/tenants into one graph; wrong-version bodies returned silently. | Workspace node + scoped graph reads/writes implemented | 🟡 Mitigated |
| Graph + semantic retrieval siloed | High | Two independent tracks can't arbitrate budget; strong doc hits dropped, weak graph neighbors kept. | Phase 9.1 unified ranker is implemented; current gap is precision/file-recall telemetry and long-tail export/framework shapes ([spec_unified_ranking.md](spec_unified_ranking.md)) | 🟡 Mitigated |
| Primary-intent routing | High | Mixed queries (e.g. debugging+refactor) still route budget by one primary intent even though distribution metadata is visible. | Phase 9.2 metadata is implemented; Phase 10 should consume `intent.distribution` in budget/tier policy ([spec_multi_label_intent.md](spec_multi_label_intent.md)) | ❌ Open |
| Flat DocAnchor links | Medium | All `COVERS` edges weighted equally regardless of definition vs. example vs. passing mention. | Phase 9.3 implemented: per-edge `anchor_type`, `confidence`, `primary_bias`, and resolver-aware ranker consumption ([spec_doc_anchor_confidence.md](spec_doc_anchor_confidence.md)) | ✅ Resolved |
| Retrieval observability polish | High | Contract fields exist, but extension/debug surfaces still need to make the ranker story easy to inspect. | Phase 9.4 surfaces selected scores/provenance, ranker weights, `pruned[]`, and intent metadata; remaining gap is UI consistency and multi-label budget-policy consumption ([spec_prompt_contract_observability.md](spec_prompt_contract_observability.md)) | 🟡 Mitigated |
| Cache multi-instance gap | Medium | Local cache exists, but multi-instance deployments would need a shared backend. | Phase 10.1 local three-layer cache is implemented and surfaces `metadata.assembly.cache_hits`; Redis/multi-instance cache remains future ([spec_retrieval_cache.md](spec_retrieval_cache.md)) | ✅ Resolved for local |
| Learning loop incomplete | Medium | Feedback telemetry exists, but retrieval does not yet adapt from usage. | Phase 10.2 has feedback tokens, snapshots, endpoint, privacy boundaries, and counters; EMA tuning, learned `CO_RELEVANT` edges, and training loops remain open ([spec_learning_loop.md](spec_learning_loop.md)) | 🟡 Mitigated |
