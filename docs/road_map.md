# Surgical Context — Road Map

> **Status:** Pre-release development. SaaS and marketplace publication are explicitly deferred until the local dev tool is proven, measurable, and stable.
>
> **See also:** [review_findings_2026-04-17.md](review_findings_2026-04-17.md) — external review with six recommendations that inform Phase 2.5 / 3.5 sequencing.

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
- [ ] CI config (GitHub Actions) running tests + benchmark on every PR (deferred: needs Neo4j services)

### Observability (DEFERRED — Phase 5)
- [ ] Structured logging across pipeline stages
- [ ] `GET /metrics` endpoint (Prometheus text format)
- [ ] Per-request trace ID threaded through logs
- [ ] Latency SLO tracking against 200ms target

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

## Phase 4: SaaS and Team Synchronization (DEFERRED — post-MVP)
Goal: Transition from local tool to Enterprise solution (ADR-003).

> **Blocked on Phase 2.5 and 3.5.** Do not begin SaaS work until: evaluation harness is green, token savings are measured, incremental indexing works locally.

### Cloud Sync
- [ ] Migration to Neo4j Aura (SaaS) for shared knowledge base
- [ ] Multi-user sync logic: "Shared Graph + My Edits"

### Security
- [ ] ADR on embedding-inversion risk before any LanceDB data leaves the machine
- [ ] Secrets management for Aura credentials (not `.env` in repo)
- [ ] Local authn on the sidecar HTTP listener (token / loopback-only bind)
- [ ] Metadata encryption in cloud
- [ ] User authentication

### Performance
- [ ] Parallel parsing for `git pull` indexing speed

---

## Phase 5: Optimization and Launch (DEFERRED — post-MVP)
Goal: Cost savings and UX refinement.

### Smart Routing & Demo Upgrade
- [ ] Round-Robin model router (ADR-004)
- [ ] Query complexity classifier (Intent Classifier)
- [ ] Streaming LLM responses (SSE) instead of blocking
- [ ] Official Anthropic SDK activation (`sidecar/ai/engine.py`) with prompt caching on `graph_context` block
- [ ] Upgrade demo from Ollama/llama3 to Claude Sonnet 4.6 (per [review_findings_2026-04-17.md](review_findings_2026-04-17.md) recommendation #5)

### JSON Prompt Contract — Planned Additions
- [ ] `metadata` block: project, branch, query_intent
- [ ] `depth` field per `graph_context` entry
- [ ] `relevance_score` per documentation chunk

### Analytics Dashboard
- [ ] Token savings and query cost visualization in VS Code (reuses Phase 2.5 metrics)

### Final Polish
- [ ] Binary compilation (Nuitka)
- [ ] VS Code Marketplace publication (Private Beta)

---

## Risk Register

| Task | Priority | Risk | Mitigation |
|---|---|---|---|
| Eval harness unblocker | **High** | No measurable proof of token/quality gains — all Phase 4+ claims unverified | Phase 2.5: ship fixture + CI (spec: [spec_eval_harness.md](spec_eval_harness.md)) |
| Unmeasured quality claims | **High** | "60–80% reduction" cannot be verified without eval harness | Phase 2.5 blocks Phase 4 (ADR-006) |
| Missing extension UI | **High** | "VS Code integration" premise unproven; `run_demo.py` doesn't validate product | Phase 2.5: promote extension scaffold from Phase 1 (per [review_findings_2026-04-17.md](review_findings_2026-04-17.md) rec #6) |
| Tree-sitter multi-language | High | Complexity of supporting many languages | ADR-005 LanguageAdapter protocol (spec: [spec_language_adapter.md](spec_language_adapter.md)); formalize in Phase 1 polish, defer extra languages to Phase 3.5 |
| Rigid BFS depth | High | Real questions span modules via `IMPORTS`, inheritance, type flow | Phase 3.5 token-budget BFS + `IMPORTS` / `INHERITS` edges (spec: [spec_token_budget_bfs.md](spec_token_budget_bfs.md)) |
| Missing incremental index | High | Full re-scan on every save breaks the <200ms SLO | Phase 3.5 file-level dirty tracking |
| Embedding leakage to cloud | Medium | Vector inversion can recover source text — contradicts ADR-001 spirit | Security ADR in Phase 4 before any cloud vector sync |
| Neo4j/SaaS Sync | Medium | Network latency on cloud requests | Phase 4 design — local cache + merge |
| Model Router | Medium | Misclassification sends complex task to cheap model | Phase 5 — escalation fallback on empty/error |
| Enterprise Neo4j image in dev | Low | Licensing ambiguity for open-source contributors | Switch to `community` edition in Phase 1 polish |
