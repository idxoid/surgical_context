# Spec — Prompt Contract Observability (Phase 9)

> **Status:** Partially implemented. Ranking scores, provenance, pruning details, intent metadata, and ranker metadata are already present in the current prompt contract. Remaining gaps are consistent workspace/branch population, doc-anchor type/confidence, and richer stage timing coverage.

## 1. Problem

Today's contract (`PromptContext.to_dict()`) tells the client **what** was included, not **why**. A debugging workflow looks like:

1. User asks a question.
2. Retrieval returns a context.
3. User says "the right symbol is missing."
4. Developer has to rerun retrieval manually, instrument it, inspect scores by hand.

Without scores in the contract:
- No way to tell if a missing symbol scored 0.01 (correctly deprioritized) or 0.9 (mis-ranked).
- No way to feed retrievals into a feedback loop — nothing distinguishes what the ranker *thought* was important.
- Model routing (large → Claude, small → Ollama) has no visibility into per-tier token spend.

## 2. Design

### 2.1 Current Contract Shape

```json
{
  "mode": "surgical_full",
  "intent": "debugging",
  "intent_details": {
    "primary": "debugging",
    "distribution": {"debugging": 0.6, "refactor": 0.3, "exploration": 0.1},
    "ambiguous": false,
    "confidence": 0.82
  },
  "metadata": {
    "query_intent": "debugging",
    "tiers_used": ["code", "cross_refs", "specs"],
    "tier_tokens": {"code": 820, "cross_refs": 1400, "specs": 310},
    "assembly": {
      "trace_id": "req_7f3a...",
      "workspace_id": "acme/surgical_context@main",
      "resolver_version": "context-arbitrator-v2",
      "cache_hits": ["l1_body"]
    },
    "ranker": {
      "weights": {"alpha": 1.0, "beta": 0.8, "gamma": 0.4, "delta": 0.5, "epsilon": 0.5},
      "candidates_considered": 47,
      "candidates_selected": 9,
      "pruned_total_count": 6,
      "target_selection": {"strategy": "duplicate_resolution"}
    }
  },
  "primary_source": {
    "symbol": "process_payment",
    "uid": "a4f9c1e2b7d83f56",
    "file_path": "sidecar/payments.py",
    "range": [42, 78],
    "code": "...",
    "is_dirty": false,
    "scores": {
      "graph_score": 1.0,
      "semantic_score": 0.92,
      "blended_score": 1.82,
      "intent_weight": 0.6
    },
    "provenance": ["primary:target"]
  },
  "graph_context": [
    {
      "symbol": "validate_amount",
      "uid": "...",
      "file_path": "sidecar/validation.py",
      "code": "...",
      "relation": "CALLS_DIRECT",
      "direction": "callee",
      "depth": 1,
      "scores": {
        "graph_score": 0.87,
        "semantic_score": 0.42,
        "blended_score": 1.15,
        "intent_weight": 0.6
      },
      "provenance": ["graph:CALLS_DIRECT,depth=1", "vector:sim=0.42"]
    }
  ],
  "documentation": [
    {
      "chunk_id": "spec_payments#negative-amounts",
      "source_file": "docs/spec_payments.md",
      "content": "...",
      "matched_symbols": ["process_payment", "validate_amount"],
      "scores": {
        "graph_score": 0.3,
        "semantic_score": 0.91,
        "blended_score": 1.05,
        "intent_weight": 0.3
      },
      "anchor_type": "definition",
      "anchor_confidence": 0.92,
      "primary_bias": 1.0,
      "anchor": {
        "type": "definition",
        "confidence": 0.92,
        "primary_bias": 1.0
      },
      "provenance": ["vector:sim=0.91", "graph:COVERS->process_payment,type=definition,conf=0.92"]
    }
  ],
  "pruned": [
    {
      "kind": "symbol",
      "uid": "...",
      "name": "Audit.log",
      "reason": "over_budget",
      "blended_score": 0.51,
      "token_cost": 620
    }
  ]
}
```

### 2.2 New Fields — What and Why

| Field | Type | Purpose |
|---|---|---|
| `intent_details.distribution` | dict | Multi-label signal ([spec_multi_label_intent.md](spec_multi_label_intent.md)) |
| `intent_details.confidence` | float | Distinguishes strong vs. weak classification |
| `metadata.assembly.trace_id` | str | Correlate with server logs, OpenTelemetry |
| `metadata.assembly.workspace_id` | str | Which workspace this context came from ([spec_branch_isolation.md](spec_branch_isolation.md)) |
| `metadata.assembly.cache_hits` | list[str] | Visibility into retrieval cache use |
| `metadata.ranker.weights` | dict | Current tuning state — a bisectable record of what was active |
| `metadata.ranker.candidates_*` | int | Pool-size observability — spot cases where too few candidates were generated |
| `metadata.ranker.target_selection` | dict | Duplicate target disambiguation evidence and alternatives |
| `*.scores.graph_score` | float | Raw graph score (normalized 0–1) |
| `*.scores.semantic_score` | float | Raw semantic similarity (normalized 0–1) |
| `*.scores.blended_score` | float | Final ranking score |
| `*.scores.intent_weight` | float | Intent-driven multiplier applied |
| `*.provenance` | list[str] | Human-readable track log — "why did this make it in?" |
| `documentation[].matched_symbols` | list[str] | Which graph symbols this doc COVERS |
| `documentation[].anchor_type` | string | Best linked DocAnchor type for this chunk (`definition`, `example`, `reference`, `warning`, `deprecated`) |
| `documentation[].anchor_confidence` | float | Best linked COVERS confidence for this chunk |
| `documentation[].primary_bias` | float | Best linked focal-symbol weighting |
| `documentation[].anchor` | dict | Nested mirror of type/confidence/primary_bias for UI clients that prefer grouped metadata |
| `pruned[]` | list | Candidates that missed the budget — with reason, score, cost |

**Still future in the contract:**

- `graph_context[].edge_confidence`
- `graph_context[].edge_tier`

### 2.3 `pruned` Array — Why It Matters

Current contract drops budget-pruned candidates silently. For debugging retrieval, the pruned list is often the most important part of the answer:

- "Did `validate_amount` miss because it scored low, or because it was cost-prohibitive?"
- "How many candidates were pruned — 2 or 200?" signals whether the budget is the bottleneck.

Capped at 20 entries, sorted by descending `blended_score` — only the most painful exclusions surface.

### 2.4 Backwards Compatibility

- Clients reading the old shape continue to work: the serializer still emits `intent` as a plain string, while richer intent details live under `intent_details`.
- Missing optional fields are never emitted as `null` — they're omitted. Old clients that ignore unknown keys keep working unchanged.
- `primary_source`, `graph_context`, `documentation`, `budget` top-level shape is preserved. New fields nest inside — no replacements.

### 2.5 Size Cost

Full contract for a typical query: +400 to +800 bytes vs. current. Negligible compared to prompt body. Optional `verbose=false` query param can suppress `provenance` and `pruned` for bandwidth-sensitive clients.

## 3. API / Interface

```python
# sidecar/context/types.py — extended

@dataclass
class CandidateScores:
    graph_score: float = 0.0
    semantic_score: float = 0.0
    blended_score: float = 0.0
    intent_weight: float = 0.0

@dataclass
class PrunedCandidate:
    kind: str                 # "symbol" | "doc"
    uid: str
    name: str
    reason: str               # "over_budget" | "duplicate" | "below_threshold"
    blended_score: float
    token_cost: int

@dataclass
class AssemblyMetrics:
    latency_ms: int
    graph_latency_ms: int
    vector_latency_ms: int
    rank_latency_ms: int
    compile_latency_ms: int
    trace_id: str
    workspace_id: str | None
    resolver_version: str

# PromptContext gains:
@dataclass
class PromptContext:
    # ... existing fields ...
    assembly: AssemblyMetrics | None = None
    ranker_state: dict = field(default_factory=dict)
    pruned: list[PrunedCandidate] = field(default_factory=list)
```

Every `SymbolContext` and `DocChunk` gains a `scores: CandidateScores` and `provenance: list[str]`.

## 4. Examples

```python
ctx = arbitrator.get_context_for_symbol("process_payment", ws, question="why fail?")
payload = ctx.to_dict()

# Debug: why did `validate_amount` survive but `Audit.log` get pruned?
for dep in payload["graph_context"]:
    print(dep["symbol"], dep["scores"]["blended_score"])
# validate_amount 1.15
# _charge_card 0.94

for p in payload["pruned"]:
    print(p["name"], p["reason"], p["blended_score"], p["token_cost"])
# Audit.log over_budget 0.51 620
# RateLimiter.check below_threshold 0.22 180
```

```python
# Perf dashboard: latency regression check
assembly = payload["metadata"]["assembly"]
if assembly["latency_ms"] > 200:
    log.warn(f"SLO breach: {assembly['latency_ms']}ms  trace={assembly['trace_id']}")
```

## 5. Limitations (current)

- Scores are meaningful only within the active ranker configuration; compare across runs together with `metadata.ranker.weights`.
- `pruned` capped at 20 entries can hide the long tail; `pruned_total_count` now mitigates this, but the long tail is still not serialized.
- `workspace_id` is present in the schema but not yet populated consistently by the arbitrator.
- Doc-anchor `anchor_type` / `anchor_confidence` are populated only when a selected doc has a `COVERS` overlap with ranked graph symbols; vector-only docs keep empty/zero defaults.

## 6. Planned Extensions

- **Structured feedback in contract:** add a `feedback_token` — an opaque handle the client returns when the user accepts / edits / rejects the context, so the server can attribute outcomes to this exact retrieval.
- **Delta contract:** for streaming responses, emit score updates as candidates are selected rather than only at the end.
- **Privacy filtering:** for `mode=standard` or cross-tenant queries, suppress `provenance` strings that could leak internal file paths.

## 7. Related

- [spec_unified_ranking.md](spec_unified_ranking.md) — produces the scores this exposes.
- [spec_multi_label_intent.md](spec_multi_label_intent.md) — source of `intent.distribution`.
- [spec_call_resolution_pipeline.md](spec_call_resolution_pipeline.md) — source of `edge_confidence` / `edge_tier`.
- [spec_doc_anchor_confidence.md](spec_doc_anchor_confidence.md) — source of `anchor_type` / `anchor_confidence`.
- [spec_learning_loop.md](spec_learning_loop.md) — consumer of scores + `feedback_token`.
