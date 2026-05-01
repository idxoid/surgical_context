# Architectural Ideas & Prioritization

> **Status:** Historical review, updated with current disposition notes.
>
> The original document grouped ideas into future phases. Since then, several items moved into the product core: `ContextDeduplicator`, embedding versioning, typed retrieval edges, AFFECTS, intent-aware retrieval, unified ranking, benchmark-driven tuning, and prompt-contract observability. The useful remaining value in this file is the shape of the trade-offs, not the old phase labels.

## Current Snapshot

What is already true in the codebase:

- unified graph + semantic ranking is the default local retrieval path
- mechanism-aware benchmark evaluation is active on real repositories
- prompt-contract observability now includes scores, provenance, pruning, ranker metadata, and benchmark `ready_context`
- FastAPI mechanism recovery is strong enough to use as a tuning baseline
- Redux Toolkit local benchmark coverage is strong enough to use as a second real-repo retrieval baseline, including listener middleware
- Pydantic now also clears the full local pack, which gives us a third real-repo retrieval baseline instead of a framework-specific holdout

What still feels architecturally important next:

1. improve precision on the now-green baselines, especially broad/doc-heavy Pydantic and Redux Toolkit retrieval paths
2. add doc-anchor confidence/type scoring so semantic docs compete more honestly with code candidates
3. keep provider boundaries local-first and thin until profiling proves a stronger abstraction need

Evaluated by **impact/effort ratio** and **dependency preconditions**. Phase assignments reflect MVP→scaling sequence.

---

## PHASE 4: NEAR-TERM WINS (implement next iteration)

### ✅ [P1] ContextDeduplicator — 15–40% token reduction
**Impact:** Immediate, visible in eval harness  
**Effort:** Low (2–3 days)  
**Insertion point:** Between `GraphExpander` and `PromptCompiler`

**Problem:** Current 883-token baseline is same across all queries, suggesting duplicate ancestors and overlapping doc chunks in results.

**Solution:**
- Normalize symbol identity by UID before BFS exits  
- Collapse overlapping line ranges before compilation  
- Deduplicate graph paths (transitive closure)  

**Why now:** Fits cleanly into existing pure-component architecture. Quick win before deeper optimizations.

---

### ✅ [P1] LanceDB Embedding Version Metadata
**Impact:** Prevents silent quality degradation  
**Effort:** Low (1–2 days)  
**Blocker for:** Multi-model switching, future model upgrades

**Problem:** No tracking of embedding model version or content hash. Queries across differently-embedded chunks are contaminated.

**Solution:**
```
EmbeddingMetadata {
  model_name: “sentence-transformers/all-MiniLM-L6-v2”
  model_version: “2.2”
  chunk_hash: SHA256(chunk)
  embedding_hash: SHA256(embedding)
}
```

Enforce: “no cross-model queries without migration flag”

**Why now:** Essential hygiene before scaling to multiple models or upgrading embeddings.

---

## PHASE 5: MEDIUM-TERM IMPROVEMENTS (next major iteration)

### [P2] Typed Semantic Edges (CALLS_DIRECT/DYNAMIC/INFERRED, etc.)
**Impact:** Better BFS ranking, reduces hallucinated deps  
**Effort:** Medium (3–4 weeks)  
**Depends on:** Parser enhancement to detect dynamic dispatch, decorators, DI patterns

**Problem:** All `CALLS` edges are equal, but dynamic dispatch, decorator chains, and DI blur true dependencies.

**Edge types:**
- `CALLS_DIRECT` — static callable reference  
- `CALLS_DYNAMIC` — dispatch via method lookup, reflection, monkey-patching  
- `CALLS_INFERRED` — heuristic from string or pattern  
- `IMPLEMENTS` — class ⟶ interface  
- `OVERRIDES` — method ⟶ parent method  
- `REFERENCES` — weak dep (import unused, comment, type-only)

**Why later:** Requires indexer changes; current BFS already has scoring hooks for `rel_type`, so graph→BFS integration is easy once edges are rich.

---

### [P2] Reverse Dependency Index (AFFECTS edges)
**Impact:** Makes incremental re-index cascade-aware  
**Effort:** Medium (2–3 days for edges; harder for invalidation logic)  
**Depends on:** Phase 3.5 incremental indexing baseline ✅

**Problem:** File A changes → you can prune its symbols, but don’t know what else indirectly depends on A. Without explicit AFFECTS, re-index must be full-graph.

**Solution:**
```
Symbol → [:AFFECTS] → Symbol  (transitive closure of CALLS, DEPENDS_ON)
File → [:AFFECTS] → File      (via contained symbols)
```

Used for: incremental cascade, cache invalidation, smart re-build.

**Why later:** Current workflow (save file → re-index that file only) is acceptable. Cascade matters at scale (20+ developers, frequent changes).

---

## PHASE 6+: EXPLORATORY (post-MVP)

### [P3] Execution Semantics Layer (ExecutionEdge with probability)
**Impact:** Reflects runtime behavior in ranking  
**Effort:** High (runtime instrumentation or statistical inference)  
**Reality check:** Static AST heuristics give ~60% coverage at best; probabilistic model needs training data

**Concept:**
```
ExecutionEdge {
  from_symbol, to_symbol
  probability: 0.0–1.0
  context: “sync | async | conditional | exception”
}
```

**Why later:** Low confidence without empirical data. Defer until you have per-repo call traces or large corpus for training.

---

### [P3] Query Planner / QueryIntentClassifier
**Impact:** Adaptive BFS depth + doc retrieval weight  
**Effort:** Medium (classifier + branching logic)  
**Reality check:** Your precision problem (0.10 avg) is NOT caused by wrong BFS settings—it’s fixture scope

**Concept:**
```
QueryIntentClassifier:
  “navigation” → deeper BFS, sparse docs
  “debugging” → tight scope, heavy doc focus
  “refactor” → module-wide deps
  “semantic search” → docs-first
  “exploration” → balanced
```

**Why later:** Fix recall/precision on golden set first. Parameter tuning matters more than intent.

---

### [P3] Context Compiler IR
**Impact:** Multi-model support, structured outputs  
**Effort:** Medium (IR design + multi-target codegen)  
**Precondition:** Need 2+ output formats (system prompt + tool use + JSON)

**Concept:**
```
ContextIR {
  symbols[]: SubgraphNode
  edges[]: (uid, uid, type)
  docs[]: DocChunk
  overlays[]: (file_path, dirty_code)
  metadata: { budget, coverage, query_intent }
}

Then compile into:
  - system_prompt (text)
  - tool_prompt (JSON)
  - json_context (structured)
```

**Why later:** Currently one output target (system prompt). Implement when targeting tool use or multi-modal reasoning.

---

### [P3] IDE Telemetry Stream
**Impact:** Smarter BFS weighting via cursor/edit signals  
**Effort:** High (VS Code extension work)  
**Scope:** Requires first-class IDE extension (not prototype)

**Concept:**
```
IDE → Sidecar:
  cursor_position
  file_open_frequency
  edit_delta_velocity
  query_to_edit_correlation
```

Then: adjust BFS weights based on recent activity patterns.

**Why later:** Architecture supports this (overlay already tracks unsaved state), but value needs real usage patterns.

---

### [P4] In-Process Caching (Redis optional later)
**Impact:** 10–25% latency reduction for repeated queries  
**Effort:** Low (LRU cache + TTL)  
**Why later:** Current 58ms assembly time doesn’t justify Redis. In-process LRU sufficient for local tool.

**Layers:**
- `symbol_context_cache` (BFS result)  
- `doc_query_cache` (LanceDB hits)  
- `bfs_result_cache` (full subgraph)  
- `overlay_merge_cache` (resolved code)

---

### [P5] Distributed Tracing & Observability
**Impact:** Debugging multi-service deployments  
**Effort:** Low (structured logging) → High (OpenTelemetry)  
**Why later:** Single-process tool. Pre-commit to structured logging now; traces valuable only at scale.

---

## Summary Table

| Idea | Phase | Impact | Effort | Why |
|---|---|---|---|---|
| ContextDeduplicator | 4 | 15–40% tokens | Low | Fits architecture, immediate visible gain |
| Embedding versioning | 4 | Prevents regression | Low | Essential hygiene |
| Typed semantic edges | 5 | Better ranking | Med | Requires parser work; BFS ready |
| AFFECTS index | 5 | Cascade-aware reindex | Med | Incremental already works; scale concern |
| Execution semantics | 6+ | Runtime accuracy | High | Needs empirical data |
| Intent classifier | 6+ | Adaptive planning | Med | Precision problem elsewhere; premature |
| Context IR | 6+ | Multi-model support | Med | Single target currently |
| IDE telemetry | 6+ | Signal-based weights | High | Needs real usage patterns |
| Caching | 6+ | Latency gains | Low | 58ms baseline not urgent |
| Distributed tracing | 5+ | Multi-service visibility | Med→High | Single-process now |
