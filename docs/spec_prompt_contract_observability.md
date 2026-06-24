# Spec — Prompt Contract Observability

> **Status:** Serializer baseline implemented; active axis propagation partial.
> `PromptContext.to_dict()` exposes scores, provenance, pruning, intent, ranker
> counts, trace, workspace, cache, route, cost, feedback, and manifest fields.
> The current axis adapter still leaves several rich fields sparse/default and
> does not attach general markdown documentation on its normal success path.

## 1. Purpose

The prompt contract must explain both what context was selected and enough of
why it was selected to diagnose retrieval failures. It is returned under
`context` by `/ask`, emitted by `/ask/stream`, stored only after history
sanitization, and written into benchmark `ready_context` artifacts.

Source of truth: `context_engine/context_types.py`.

## 2. Current Shape

```json
{
  "mode": "surgical_full",
  "intent": "debugging",
  "intent_details": {
    "primary": "debugging",
    "distribution": {},
    "ambiguous": false,
    "confidence": 0.0,
    "effective_mode": "",
    "resolution": {}
  },
  "metadata": {
    "query_intent": "debugging",
    "tiers_used": ["code", "cross_refs"],
    "tier_tokens": {},
    "stopped_reason": "",
    "pruned_count": 0,
    "pruning_reasons": [],
    "ranker": {
      "candidates_selected": 4,
      "pruned_total_count": 0
    },
    "index_manifest_id": "...",
    "index_manifest_schema_version": 1,
    "assembly": {
      "trace_id": "trace_...",
      "workspace_id": "local/repo@main",
      "context_pipeline_version": "context-axis-v1",
      "cache_hits": [],
      "feedback_token": "fbk_...",
      "stage_timings_ms": {},
      "token_counts": {},
      "model_route": {},
      "estimated_cost_usd": 0.0,
      "cost_basis": "not_configured"
    }
  },
  "primary_source": {
    "symbol": "process_payment",
    "uid": "...",
    "file_path": "payments.py",
    "relation": "primary",
    "direction": "callee",
    "depth": 0,
    "scores": {
      "relevance": 0.0,
      "graph_score": 0.0,
      "semantic_score": 0.0,
      "blended_score": 1.0,
      "intent_weight": 0.0
    },
    "provenance": ["axis"],
    "render_mode": "full",
    "is_dirty": false,
    "code": "..."
  },
  "graph_context": [],
  "documentation": [],
  "pruned": [],
  "budget": {}
}
```

Optional documentation entries serialize:

- `scores`
- `matched_symbols`
- `provenance`
- `anchor_type`
- `anchor_confidence`
- `primary_bias`
- grouped `anchor` metadata

## 3. What Is Populated Today

### Reliable on `/ask`

- selected symbol/file/code and related symbol bundles
- relation, depth, render mode, basic provenance
- `trace_id`, base `workspace_id`, pipeline version
- model route, token/cost metrics, cache hits
- feedback token and index manifest identifiers
- fallback metadata (`ask_level`, missing symbol, reason, warnings)

### Schema exists, but axis propagation is incomplete

- score decomposition for every candidate
- intent distribution/confidence/ambiguity from axis intent matches
- pool size and detailed pruning decisions
- tier token accounting from the axis bundle builder
- general markdown docs and their COVERS confidence/type metadata

The adapter in `context_engine/axis/prompt_provider.py` currently converts ranked
bundles into one primary symbol plus related symbols. It does not copy the full
`AxisResult` diagnostics or attach documentation. The serializer therefore emits
valid but often zero/empty values for those fields.

## 4. Pruned Candidates

`PromptContext.pruned_details` is sorted by `blended_score` (or `gain`) and
capped at 20 entries by the serializer. `metadata.ranker.pruned_total_count`
retains the uncapped count. This path is useful when a provider populates the
details; the active axis adapter does not yet do so.

## 5. Compatibility

- The top-level `primary_source`, `graph_context`, `documentation`, `budget`,
  `mode`, and `intent` shape remains stable.
- Richer fields are nested, so clients may ignore them.
- Some optional identifiers are omitted; many observability fields are emitted
  with empty/default values rather than omitted.
- There is no public `verbose=false` switch in the current API.

## 6. Next Work

1. Extend the axis-to-prompt adapter to accept the full axis result, not only bundles.
2. Preserve candidate score, role/kind/contract, and pruning provenance.
3. Attach ranked docs with COVERS metadata without reintroducing the removed cascade.
4. Populate intent distribution/confidence consistently on `/ask` and `/ask/stream`.
5. Add contract tests that assert meaningful non-default values on the live axis path.

## 7. Related

- [spec_context_engine_api.md](spec_context_engine_api.md) — `/ask` and streaming transport
- [spec_doc_anchor_confidence.md](spec_doc_anchor_confidence.md) — persisted anchor quality
- [spec_learning_loop.md](spec_learning_loop.md) — feedback consumer
- [spec_branch_isolation.md](spec_branch_isolation.md) — workspace identity
