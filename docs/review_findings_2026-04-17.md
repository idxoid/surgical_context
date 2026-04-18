# Review Findings — 2026-04-17

> **Context:** External review of the Surgical Context project at the end of Phase 2. Captures the standout strengths, honest risks, and six concrete recommendations surfaced during the review. Deep-dive specs exist for recommendations #1 and #2 ([spec_eval_harness.md](spec_eval_harness.md), [spec_token_budget_bfs.md](spec_token_budget_bfs.md)).

## Standout strengths

- **ADR-001 (topology-only Neo4j)** is the real moat. Most "AI code assistants" ship source into embeddings or cloud graphs. This project can meet enterprise data-residency requirements without compromise.
- **Dirty overlay** is rare and high-value. Most tools only see saved state, which breaks the live-coding loop.
- **DocAnchor pending resolution** — docs indexed before their referenced symbols exist, resolved lazily — is an elegant handle on chicken-and-egg ordering.

## Honest risks

- **Market fit vs. Cursor / Copilot / Continue.dev / Aider.** All have retrieval. The wedge here is *not* "better answers" — it's **"answers without shipping code off-box + lower tokens."** Lead with that or it gets lost.
- **BFS depth 1–2 is too rigid.** Real questions span modules via `IMPORTS`, inheritance, and type flow. `CALLS` alone misses `@decorator`, `Protocol`, dependency injection, event buses.
- **Only Python + TypeScript.** Every missing language is a dead prospect.
- **Eval harness is Phase 2.5 and blocking.** Without numbers, the "60–80% token reduction" claim is marketing, not a product.

## Six recommendations

### 1. Ship the eval harness first

Pick 30–50 real questions from open repos (Django, FastAPI, VS Code), measure tokens + answer quality vs. whole-file baseline. This gates everything downstream — you cannot tune the BFS, swap embedding models, or justify SaaS without it.

**→ Deep dive: [spec_eval_harness.md](spec_eval_harness.md).** Already wired into the road map as Phase 2.5; the spec formalizes the metric set, fixture design, and CI contract.

### 2. Token-budget BFS beats depth-BFS

Replace hardcoded `*1..2` with a best-first traversal: expand by relevance score until cumulative token cost hits the caller's budget. Depth becomes an outcome, not an input.

**→ Deep dive: [spec_token_budget_bfs.md](spec_token_budget_bfs.md).** Includes the scoring function, algorithm, Cypher shape, contract additions (`direction`, `depth`, `relevance_score`, `budget` block), and tuning protocol.

### 3. Add `IMPORTS` and `INHERITS` edges before more languages

Two new edge types unlock more real-world questions than a third language. Tracking inheritance and imports also makes re-ranking honest — a subclass that overrides `process` is more relevant than a sibling that calls it.

Integration point: extend [spec_parser.md](spec_parser.md) LanguageAdapter with `extract_imports(tree, source)` and `extract_inheritance(tree, source)`. Schema slot is already carved out in [architectura.md §5.2](architectura.md).

### 4. Token-budget BFS > depth-BFS (see #2)

Collapsed into #2. Left numbered for traceability.

### 5. Kill Ollama/llama3 as the demo model

It undersells the product. Local llama3 on `/ask` produces mediocre answers that distract reviewers from the real story — the *context assembly*. Two fixes:

- Demo against **Claude Sonnet 4.6** via the Anthropic SDK with prompt caching on the `graph_context` block. This is where the "surgical" story sings: cache hits on stable symbols, only the target body invalidates per query.
- Keep Ollama as the offline/air-gapped fallback, not the default.

Implementation: [sidecar/ai/engine.py](../sidecar/ai/engine.py) already has a stubbed Anthropic path. Activate it, add cache-breakpoint markers in `PromptContext.to_system_prompt()` at the boundary between `primary_source` (hot) and `graph_context` (cache-cold after first hit).

### 6. `extension/` is the real gap

The entire "thin client" premise is unvalidated until a user can ask a question inside VS Code. Even an ugly webview chat beats `run_demo.py`. Prioritize scaffold-and-ship over polish — this is the demo that sells the idea.

Current state: no `extension/` directory exists on disk. Road map has it as a Phase 1 checkbox. It should be reclassified as Phase 2.5 alongside the eval harness — both are unblockers for external validation.

### 7. Positioning

"Surgical Context" sounds like a library, not a product. What actually exists is closer to *"a grounded-answer layer for codebases that can't leave the premises."* Consider leading with the privacy story — it's the only story competitors can't match.

Not a code change, but worth capturing before the README and marketing copy are written.

## Sequencing

Recommended execution order (with current road-map phase in brackets):

1. #1 Eval harness [Phase 2.5]
2. #6 Extension scaffold [Phase 2.5 — promote from Phase 1]
3. #3 `IMPORTS` / `INHERITS` edges [Phase 3.5]
4. #2 Token-budget BFS [Phase 3.5, tuned against #1]
5. #5 Anthropic SDK + prompt caching [Phase 5, but promote demo-only usage earlier]
6. #7 Positioning refresh [anytime; costs nothing]

## Related

- [road_map.md](road_map.md) — phase structure these slot into.
- [idea_summary.md](idea_summary.md) — condensed product pitch.
- [concept.md](concept.md) — technical concept reference.
- ADR-006 — quality gates blocking SaaS.
