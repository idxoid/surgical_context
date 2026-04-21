# Changelog

All notable changes to Surgical Context are documented here.

---

## [Phase 6.1] — 2026-04-20

### Intent Classifier Implementation ✅

**Added:**
- `sidecar/context/intent_classifier.py` — Intent classification engine
  - `Intent` enum: 6 query types (navigation, debugging, refactor, exploration, new_feature, design_question)
  - `IntentClassifier.classify_intent(query: str) -> Intent` — keyword-based heuristic detection
  - `IntentConfig` — maps each intent to 6-tier priority ordering (code, cross_refs, specs, architecture, concept, idea)
  - Example: navigation prioritizes [code, cross_refs, architecture, specs, concept, idea]; new_feature prioritizes [idea, concept, architecture, specs, cross_refs, code]

- `PromptContext` enhancements (`sidecar/context/types.py`):
  - `mode` field: "surgical_full" | "surgical_doc_only" | "standard" (tracks which context tier(s) populated the response)
  - `intent` field: detected query intent (for observability and Phase 6.3 model routing)
  - Updated `to_dict()` to serialize both fields in JSON Prompt Contract

- `PromptCompiler.compile_with_intent()` (`sidecar/context/prompt_compiler.py`):
  - Tier-aware context assembly based on detected intent
  - Doc type inference: patterns (spec_*, idea_*, concept, architecture) map to tiers
  - Graceful degradation: if tier is empty, proceeds to next tier; if all tiers empty → mode="standard"
  - Maintains backward compatibility: existing `compile()` method unchanged

**Test Coverage:**
- 17 new intent classifier tests:
  - All 6 intent types detected correctly (navigation, debugging, refactor, exploration, new_feature, design_question)
  - Keyword matching case-insensitive
  - Ambiguous queries follow priority order (first match wins)
  - IntentConfig validates all intents have proper tier orderings
- 19 new PromptCompiler tests:
  - `compile_with_intent()` creates valid PromptContext
  - Mode field correctly determined (surgical_full, surgical_doc_only, standard)
  - Tier priority respected per intent
  - Doc type inference from filename patterns
  - Graceful degradation on empty tiers
  - Both `compile()` and `compile_with_intent()` work without breaking each other

**Metrics:**
- All 97 unit tests passing (61 existing + 17 intent + 19 compiler)
- Intent classifier latency: <1ms per query (keyword heuristics)
- Doc type inference: pattern-based, O(1) per doc

---

## [Phase 5] — 2026-04-17

### Typed Semantic Edges & Reverse Dependencies ✅

**Added:**
- Typed call edges: `CALLS_DIRECT`, `CALLS_DYNAMIC`, `CALLS_INFERRED` with confidence priors
- `AFFECTS` index: reverse-dependency materialization (transitive dependents per symbol)
- Enhanced `FROM` relations: doc-to-file edges now typed (doc, code, spec, architecture, concept, idea)
- `File.doc_type` classification: 8 types (code, spec, architecture, documentation, concept, idea, review, roadmap)
- `/impact` endpoint: cascade-aware impact analysis

**Validation Results:**
- Typed call edges: 49 (28 CALLS_DIRECT, 21 CALLS_DYNAMIC)
- AFFECTS index: 196 edges
- FROM relations: 2,040 edges with type classification
- COVERS edges: 2,286 (doc chunk → code symbol links)
- Benchmark: 100% pass rate (10/10 queries), Recall 1.0, Precision 1.0

---

## [Phase 4] — 2026-04-10

### Context Deduplication & Embedding Versioning ✅

**Added:**
- `ContextDeduplicator`: pure transform removing duplicate symbols from expanded subgraph
- Embedding versioning: metadata tracking on LanceDB tables (model_name, model_version, chunk_hash, embedding_hash)
- Migration CLI: `python -m sidecar.database.embedding_migration status / migrate`

**Results:**
- Token reduction: 15–40% via dedup (avg 25% observed)
- Embedding model drift protection: guard against cross-model queries

---

## [Phase 3.5] — 2026-03-28

### Token-Budget BFS & Incremental Indexing ✅

**Added:**
- Token-budget constrained BFS: best-first expansion with relevance scoring
- Incremental indexing: file-level dirty tracking via content hash
- `POST /index/file` endpoint for single-file updates
- Enhanced edge traversal: CALLS, IMPORTS, DEPENDS_ON, IMPLEMENTS, OVERRIDES, REFERENCES

**Results:**
- Token budget tuned to 4000 (default), assembly latency 13.9ms avg
- Incremental indexing enables <200ms response SLO on save

---

## [Phase 3] — 2026-03-10

### Documentation & Vector Search ✅

**Added:**
- LanceDB vector index: `docs` and `symbols` tables with all-MiniLM-L6-v2 embeddings (384-dim)
- DocAnchor nodes: chunk_id-only graph nodes linking docs to code
- Markdown processing: section-aware chunking + embedding

---

## [Phase 2.5] — 2026-02-28

### Quality Foundation & Extension UI ✅

**Added:**
- Evaluation harness: 10-question golden fixture in `tests/fixtures/sample_project/`
- QA benchmark: `QA/qa_benchmark.py` (recall@k, precision@k, tokens, latency metrics)
- Extension scaffold: `extension/` workspace with TypeScript chat window

---

## [Phase 2] — 2026-02-15

### Graph Brain & Surgical Retrieval ✅

**Added:**
- Neo4j integration: file/symbol node upserts, CALLS edge creation
- 4-phase indexer: symbols → calls → embeddings → pending resolution
- BFS graph expansion for context gathering
- In-Memory Overlay: `sidecar/context/overlay.py` for unsaved file handling
- JSON Prompt Contract: `PromptContext` dataclass with token accounting

---

## [Phase 1] — 2026-01-20

### Foundation & Local Core ✅

**Added:**
- Docker container (Neo4j community edition)
- Python sidecar with FastAPI
- tree-sitter integration: Python + TypeScript parsing
- Symbol extraction: functions, classes, constants
- LanguageAdapter protocol (ADR-005)

---

## Notes

- Semantic versioning: phases map to major version increments (Phase 1 → v1.0, Phase 2 → v2.0, etc.)
- Each phase is independently deployable and measurable
- All metrics tracked in `docs/road_map.md` — see section "Validation Results"
