# Surgical Context — Road Map

> **Status:** ✅ Phase 5 COMPLETE & VALIDATED. ✅ Phase 6 PROGRESSING: 6.1–6.2 COMPLETE.
> Next: Phase 6.3 (Streaming & Model Routing).
>
> **Phase 6.1–6.2 Results:**
> - Intent Classifier: 6 types (navigation, debugging, refactor, exploration, new_feature, design_question)
> - Tier Priority Orders: IntentConfig maps each intent to 6-tier priority (code, cross_refs, specs, architecture, concept, idea)
> - PromptCompiler.compile_with_intent(): tier-aware assembly with graceful degradation
> - ContextArbitrator Integration: question parameter drives intent detection, mode field in response
> - Test Coverage: 17 intent classifier + 19 compiler + 11 orchestrator tests = 47 new tests (108 total unit tests passing)
> - Doc Type Inference: pattern-based (spec_*, idea_*, concept, architecture)
>
> **Phase 5 Graph Validation Metrics (full codebase):**
> - Typed call edges: 49 (28 CALLS_DIRECT, 21 CALLS_DYNAMIC)
> - AFFECTS index: 196 edges (reverse dependency materialization)
> - FROM relations: 2,040 edges with type classification (doc, code, spec, architecture, concept, roadmap, review)
> - COVERS edges: 2,286 (doc chunk → code symbol links)
> - Relation types: 8 (CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED, AFFECTS, FROM, COVERS, DEPENDS_ON, IMPORTS)
> - File doc_type: 37 files classified (28 code, 17 spec, 2 architecture, 2 documentation, 1 each: concept, idea, review, roadmap)
>
> **Retrieval Quality (qa_benchmark.py):**
> - Pass rate: 100% (10/10 questions)
> - Recall@5: 1.00 (all expected symbols retrieved)
> - Precision@5: 1.00 (no false positives)
> - Token reduction: 50% (surgical vs carpet-bomb baseline)
> - Assembly latency: 13.9ms avg (target: <200ms) ✅
>
> **Next:** Phase 6 implementation (intent classification + graceful degradation).
>
> **See also:** [review_findings_2026-04-17.md](review_findings_2026-04-17.md) (all recommendations complete ✅), [DOCS_STYLE_GUIDE.md](DOCS_STYLE_GUIDE.md), [docs/README.md](README.md)

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
- [ ] Fallback behavior when Ollama is unreachable (clear error, degraded `/ask` that still returns `context`)

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

### Observability (DEFERRED — Phase 5+)
- [ ] Structured logging across pipeline stages (Phase 5 prerequisite)
- [ ] `GET /metrics` endpoint (Prometheus text format)
- [ ] Per-request trace ID threaded through logs
- [ ] Latency SLO tracking against 200ms target
- [ ] Distributed tracing via OpenTelemetry (Phase 6, scale phase only)

### Token Accounting ✅ COMPLETE
- [x] Token counter (tiktoken cl100k_base) on every `PromptContext`
- [x] `PromptContext.token_count()` method
- [ ] Per-request breakdown: `tokens_primary`, `tokens_graph`, `tokens_docs`
- [x] Baseline: "carpet-bomb" estimation (all files) vs surgical count

### Extension UI (Promoted from Phase 1) ✅ COMPLETE
- [x] Scaffold `extension/` workspace (TypeScript, `package.json`, build pipeline)
- [x] Basic chat window in VS Code
- [x] Cursor position capture mechanism
- [x] Wire `onDidChangeTextDocument` / `onDidSaveTextDocument` → `POST /overlay` / `DELETE /overlay`
- [x] Demo on a real repo; measure cold-start and `/ask` latency

---

## Phase 3: Documentation and Vector Search ✅ Largely Complete
Goal: Connect the semantic layer via documentation.

### Vector Layer
- [x] LanceDB integration — two tables: `docs` + `symbols` (`sidecar/database/lancedb_client.py`)
- [x] Markdown processing pipeline: section-aware chunking + embedding generation (`sidecar/indexer/docs.py`)

### Semantic Connections
- [x] DocAnchor in Neo4j: `chunk_id`-only node, `[:FROM]` to File, `[:COVERS]` to Symbols, lazy `pending` resolution via LanceDB (`sidecar/indexer/anchor.py`)

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
- [ ] Symbol-level diff: only re-upsert nodes where `Symbol.hash` changed (optimization, deferred)
- [ ] Background debounce queue: batch rapid-fire saves (deferred)

### Graph Completeness ✅ COMPLETE
- [x] `IMPORTS` edge between Files to enable correct cross-module call resolution
- [x] `DEPENDS_ON` edge for type / interface / import usage (Symbol→Symbol edge type for inheritance/interface dependencies)
- [x] Unit tests: 18 tests verify `CALLS`, `IMPORTS`, `DEPENDS_ON` edge extraction for Python and TypeScript
- [x] Arbitrator BFS expanded to traverse all three edge types for context gathering

### Embedding Quality (DEFERRED — Phase 5)
- [ ] Benchmark `all-MiniLM-L6-v2` vs a code-native model (e.g. `bge-code`, `unixcoder`) on the golden set
- [ ] Embedding cache keyed by content hash to avoid recomputation on re-index

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

### Graph Richness (Phase 5 planning)
- [ ] Feasibility assessment: dynamic dispatch detection in Python/TypeScript parsers
- [ ] Spec review: [spec_typed_semantic_edges.md](spec_typed_semantic_edges.md), [spec_affects_index.md](spec_affects_index.md)
- [ ] Decision gate: prioritize typed edges vs AFFECTS index for Phase 5 first milestone

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
- [x] 6 intent types: navigation, debugging, refactor, exploration, new_feature, design_question
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

### Phase 6.3: Streaming & Model Routing (PLANNED)
- [ ] Streaming LLM responses (SSE) instead of blocking
- [ ] Official Anthropic SDK activation (`sidecar/ai/engine.py`) with prompt caching on `graph_context` block
- [ ] Round-Robin model router (ADR-004) — route by context size + intent
- [ ] Upgrade demo from Ollama/llama3 to Claude Sonnet 4.6 (per [review_findings_2026-04-17.md](review_findings_2026-04-17.md) recommendation #5)

### Phase 6.4: Integration Testing (PLANNED)
- [ ] Test intent classification accuracy on 6 intent types
- [ ] Test tier-based budget allocation per intent
- [ ] Test graceful degradation (no matches → standard mode)
- [ ] Test mode field serialization in PromptContext.to_dict()

### JSON Prompt Contract — Phase 6 Additions
- [x] `mode` field: "surgical_full" | "surgical_doc_only" | "standard" (6.1)
- [x] `intent` field: detected query type (6.1)
- [ ] `metadata` block: query_intent, tiers_used (6.2+)
- [ ] Per-tier token counts for observability (6.3+)

---

## Phase 7: Scaling & SaaS (DEFERRED — post-MVP)
Goal: Transition from local tool to shared team solution (ADR-003).

> **Blocked on Phase 6 completion.** Do not begin SaaS work until: intent classifier is validated, graceful degradation is reliable, doc-code precision is >70%.

### Cloud Sync & Multi-User
- [ ] Migration to Neo4j Aura (SaaS) for shared knowledge base
- [ ] Multi-user sync logic: "Shared Graph + My Edits" (local overlay + cloud merge)
- [ ] Conflict resolution: last-write-wins with version tagging

### Security & Compliance
- [ ] ADR on embedding-inversion risk before any LanceDB data leaves the machine
- [ ] Secrets management for Aura credentials (HashiCorp Vault integration)
- [ ] Local authn on the sidecar HTTP listener (JWT tokens, loopback-only bind optional)
- [ ] Metadata encryption in cloud (AES-256 at rest)
- [ ] User authentication & authorization (RBAC for graph queries)
- [ ] Audit logging (who queried what, when)

### Performance & Reliability
- [ ] Parallel parsing for `git pull` indexing (ThreadPoolExecutor, 4 workers default)
- [ ] Graceful degradation on Neo4j outage (local cache + retry)
- [ ] Rate limiting per user
- [ ] Circuit breaker for cloud sync failures

### Analytics & Monitoring
- [ ] `GET /metrics` endpoint (Prometheus text format)
- [ ] Per-request trace ID threaded through logs
- [ ] Latency SLO tracking (50ms p50, 200ms p95 target)
- [ ] Distributed tracing via OpenTelemetry
- [ ] Token savings visualization in VS Code (reuses Phase 2.5 metrics)

### Final Polish
- [ ] Binary compilation (Nuitka)
- [ ] VS Code Marketplace publication (Public Release)

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
| Intent classification immaturity | Medium | Query intent classifier premature before precision improves | Phase 6.1 complete: keyword heuristics classifier implemented + 36 unit tests ✅; ML upgrade in Phase 7 | 🟢 In Progress (6.1 ✅) |
| Graceful degradation reliability | Medium | Standard mode must be robust fallback when surgical context unavailable | Phase 6.1: tier-aware assembly + mode flag implemented ✅; Phase 6.2: orchestrator integration | 🟡 In Progress (6.1 ✅) |
| Model Router misclassification | Medium | Misclassification sends complex task to cheap model | Phase 6 — escalation fallback on empty/error; Phase 7 RBAC | 🟡 Pending Phase 6+ |
| Enterprise Neo4j image in dev | Low | Licensing ambiguity for open-source contributors | Switch to `community` edition in Phase 1 polish ✅ | ✅ Resolved |
