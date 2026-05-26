# Context Arbitrator — Spec

> **Status:** Implemented. This document describes the current orchestrator behavior in `sidecar/context/arbitrator.py`.

## Overview

`ContextArbitrator` assembles the JSON Prompt Contract for a target symbol. It is still an orchestrator rather than a planner: it composes intent classification, retrieval, code resolution, and prompt compilation, but it does not call the LLM itself.

**Single execution path.** All requests go through the unified path. When `vector_db` is absent `VectorSearcher` returns empty results — vector scores are zero but normalization, role backfill, and noise factors still run. A `WARNING` is logged and `budget.ranker` is set to `"unified_graph_only"` so callers can see the reduced quality without a different code path.

## Public API

```python
ContextArbitrator(
    neo4j_client,
    overlay: InMemoryOverlay | None = None,
    vector_db=None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    user_id: str = "anonymous",
    cache: LayeredCache | None = None,
    ranker_weights: RankerWeights | None = None,
    *,
    vector_search: VectorSearchProvider | None = None,
    workspace_meta: WorkspaceMetaProvider | None = None,
)
```

### `get_context_for_symbol(symbol_name, question="", token_budget=4000) -> PromptContext | str`

Returns a `PromptContext`, or an error string like `Error: Symbol '...' not found in graph.` Callers translate that string into endpoint-level errors.

## Unified Path

The pipeline for all requests:

1. **Intent classification** — `IntentClassifier.classify_with_metadata(question)` returns the primary intent plus distribution, confidence, and ambiguity signal.
2. **Target selection** — `UnifiedRanker.get_target(...)` resolves the symbol in the active workspace and records duplicate-resolution metadata when needed.
3. **Concept anchor fallback** — when the target is missing or low-quality, `_resolve_concept_anchor_target` asks the ranker's dynamic `concept_anchor_candidates()` method for alternative entry points. There is no static hardcoded framework map; all candidates come from the graph.
4. **Unified ranking** — `UnifiedRanker.rank(...)` blends graph and semantic candidates, applies mechanism-aware role backfill, and returns:
   - selected candidates
   - budget info
   - stop reason
   - pruned candidate details
   - missing roles
5. **Subgraph/doc split** — `UnifiedRanker.candidates_to_subgraph(...)` converts the chosen candidates back into `SubgraphNode` plus `DocChunk` objects for compilation.
6. **Code resolution** — `CodeResolver.resolve(...)` reads dirty overlay content for `(workspace_id, user_id, file_path)` first, then falls back to disk. Massive targets and low-gain distant neighbors are resolved in signature-only mode.
7. **Prompt compilation** — `PromptCompiler.compile_with_intent(...)` builds the base `PromptContext`.
8. **Observability enrichment** — arbitrator writes mechanism, missing roles, ranker weights, target-selection metadata, cache hits, and intent metadata into the contract.

## `budget.ranker` Values

| Value | Meaning |
|---|---|
| `"unified"` | Full unified path with vector search active |
| `"unified_graph_only"` | Unified path but `vector_db` is absent — semantic scores are zero |

## PromptContext Fields the Arbitrator Owns

The arbitrator is responsible for setting or enriching:

- `stopped_reason`
- `mechanism`
- `missing_roles`
- `intent_distribution`
- `intent_confidence`
- `intent_ambiguous`
- `budget.cache_hits`
- `budget.ranker`
- `budget.ranker_weights`
- `ranker_state.strategy`
- `ranker_state.weights`
- `ranker_state.candidates_considered`
- `ranker_state.candidates_selected`
- `ranker_state.pruned_total_count`
- `ranker_state.required_roles`
- `ranker_state.target_selection`

The compiler owns the structural shape of `primary_source`, `graph_context`, `documentation`, and tier token accounting.

## Overlay Priority Rule

| State | Source |
| --- | --- |
| File is dirty | `InMemoryOverlay` |
| File is clean | Local filesystem |

The graph supplies symbol file paths and line ranges. Only the source of text changes when a dirty overlay exists.

## Current Limitations

- When `vector_db` is absent, semantic scores are zero so ranking is graph-score dominated. Logging reports this; callers can detect it via `budget.ranker == "unified_graph_only"`.
- Doc-anchor type/confidence is injected into `documentation[]` when a selected doc overlaps ranked graph symbols through `COVERS`; vector-only docs still carry empty/zero defaults.
- Mechanism inference now relies on repository profiles, role catalogs, and generic recovery signals rather than bundled framework dispatch tables. Coverage is still uneven for dynamic export/registration patterns.
- `_find_symbol_line` (used by the file-fallback path) matches on `def/class/function/const/let/var` keywords only. It is a fast heuristic, not a parser — may misfire on uncommon definition patterns.

## Planned Extensions

- richer UI presentation of doc confidence/type metadata
- clearer model-route and fallback-level surfacing in the extension
- future tenant API expansion between workspace retrieval and direct LLM fallback
