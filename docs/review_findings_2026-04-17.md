# Review Findings — 2026-04-17

> **Historical snapshot.** This document predates the cascade removal (2026-06-15): the legacy ranking cascade it references is gone — axis is the sole context path (see `cascade_cleanup_inventory.md`). Kept as a dated record; the findings/benchmarks below are as-of their date.


> **Context:** External review of the Surgical Context project at the end of Phase 2. Captures the standout strengths, honest risks, and six concrete recommendations surfaced during the review. Deep-dive specs exist for recommendations #1 and #2 ([spec_eval_harness.md](spec_eval_harness.md), spec_token_budget_bfs.md (removed)).

## Current Disposition

This review still captures the right product pressure, but several recommendations have already landed.

| Recommendation | Current state |
| --- | --- |
| Ship the eval harness first | Landed. The QA harness now runs against fixture and real-repo packs; benchmark reports include role/file metrics, precision, and full `ready_context`. |
| Token-budget BFS beats depth-BFS | Landed in the current retrieval stack through the unified ranker and token-budgeted candidate fill. |
| Add `IMPORTS` / `INHERITS` before more languages | Largely landed on the retrieval side through typed/canonical edges and scoped resolution; still worth preserving as a quality principle. |
| Kill Ollama as the demo default | Partially landed. Model routing and fallback exist, but demo/default provider choice is still a product decision rather than a solved architecture issue. |
| `extension/` is the real gap | Landed. The extension exists; the remaining gap is request synchronization and polish, not absence. |
| Positioning refresh | Landed in the current local-first context-engine docs, though product language still needs occasional pruning to avoid drifting back toward “general AI coding platform.” |

## Standout strengths

- **ADR-001 (topology-only Neo4j)** is the real moat. Most "AI code assistants" ship source into embeddings or cloud graphs. This project can meet enterprise data-residency requirements without compromise.
- **Dirty overlay** is rare and high-value. Most tools only see saved state, which breaks the live-coding loop.
- **DocAnchor pending resolution** — docs indexed before their referenced symbols exist, resolved lazily — is an elegant handle on chicken-and-egg ordering.

## Honest risks

- **Market fit vs. Cursor / Copilot / Continue.dev / Aider.** All have retrieval. The wedge here is *not* "better answers" — it's **"answers without shipping code off-box + lower tokens."** Lead with that or it gets lost.
- **Cross-framework tails remain.** FastAPI/Pydantic/RTK improved strongly, but Flask/Django/Express still expose retrieval and target-resolution gaps.
- **Language coverage is still narrow.** Python + TypeScript dominate; broader adapter coverage remains a practical adoption limiter.
- **Impact analysis remains conservative.** Current `AFFECTS` is bounded and useful, but not a full causal blast-radius model.

## Six recommendations

### 1. Ship the eval harness first ✅ implemented

Pick 30–50 real questions from open repos (Django, FastAPI, VS Code), measure tokens + answer quality vs. whole-file baseline. This gates everything downstream — you cannot tune the BFS, swap embedding models, or justify SaaS without it.

**→ Deep dive: [spec_eval_harness.md](spec_eval_harness.md).** Already wired into the road map as Phase 2.5; the spec formalizes the metric set, fixture design, and CI contract.

### 2. Token-budget BFS beats depth-BFS ✅ implemented

Replace hardcoded `*1..2` with a best-first traversal: expand by relevance score until cumulative token cost hits the caller's budget. Depth becomes an outcome, not an input.

**→ Deep dive: spec_token_budget_bfs.md (removed).** Includes the scoring function, algorithm, Cypher shape, contract additions (`direction`, `depth`, `relevance_score`, `budget` block), and tuning protocol.

### 3. Add `IMPORTS` and `INHERITS` edges before more languages ✅ largely implemented

Two new edge types unlock more real-world questions than a third language. Tracking inheritance and imports also makes re-ranking honest — a subclass that overrides `process` is more relevant than a sibling that calls it.

Integration point: extend [spec_parser.md](spec_parser.md) LanguageAdapter with `extract_imports(tree, source)` and `extract_inheritance(tree, source)`. Schema slot is already carved out in [architectura.md §5.2](architectura.md).

### 4. Token-budget BFS > depth-BFS (see #2) ✅ obsolete duplicate

Collapsed into #2. Left numbered for traceability.

### 5. Kill Ollama/llama3 as the demo model 🚧 partially addressed

It undersells the product. Local llama3 on `/ask` produces mediocre answers that distract reviewers from the real story — the *context assembly*. Two fixes:

- Demo against **Claude Sonnet 4.6** via the Anthropic SDK with prompt caching on the `graph_context` block. This is where the "surgical" story sings: cache hits on stable symbols, only the target body invalidates per query.
- Keep Ollama as the offline/air-gapped fallback, not the default.

Implementation: [sidecar/ai/engine.py](../sidecar/ai/engine.py) already has a stubbed Anthropic path. Activate it, add cache-breakpoint markers in `PromptContext.to_system_prompt()` at the boundary between `primary_source` (hot) and `graph_context` (cache-cold after first hit).

### 6. `extension/` is the real gap ✅ implemented, now polish/sync gap

The entire "thin client" premise is unvalidated until a user can ask a question inside VS Code. Even an ugly webview chat beats `run_demo.py`. Prioritize scaffold-and-ship over polish — this is the demo that sells the idea.

Historical note: this was correct at review time. The extension now exists on disk; the live problem has shifted to synchronization, observability presentation, and accessibility polish.

### 7. Positioning ✅ implemented baseline, keep pruning language drift

"Surgical Context" sounds like a library, not a product. What actually exists is closer to *"a grounded-answer layer for codebases that can't leave the premises."* Consider leading with the privacy story — it's the only story competitors can't match.

Not a code change, but worth capturing before the README and marketing copy are written.

## Sequencing (historical)

This sequence was useful at review time but is no longer the active plan. The
canonical execution order now lives in `road_map.md`.

## Related

- [road_map.md](road_map.md) — phase structure these slot into.
- [idea_summary.md](idea_summary.md) — condensed product pitch.
- [concept.md](concept.md) — technical concept reference.
- ADR-006 — quality gates blocking SaaS.
