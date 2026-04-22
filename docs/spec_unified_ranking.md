# Spec — Unified Ranking (Phase 9)

> **Status:** Proposed. Merges graph traversal and semantic search into a single ranked candidate pool. Supersedes the current "graph then append top-3 docs" pattern.

## 1. Problem

Today retrieval runs in two disconnected tracks:

- **Graph track** (`GraphExpander`): BFS from the target symbol, returns up to N neighbors scored by `relation_prior + fan_in - cost - distance` ([spec_token_budget_bfs.md](spec_token_budget_bfs.md)).
- **Semantic track** (`/ask` doc append): LanceDB top-k over doc chunks by query embedding similarity; appended after graph.

They never compete for budget. Consequences:

- A doc chunk with similarity 0.95 (almost certainly the right answer) can be dropped because the graph already spent the budget.
- A graph neighbor with score 0.4 (barely relevant) gets included because it showed up early in BFS.
- The LLM receives context that is "graph-heavy or doc-light" purely by pipeline order — not by relevance.

The system has two retrieval signals and uses neither to arbitrate the other.

## 2. Design

### 2.1 Unified Candidate Pool

Both tracks emit candidates into a single pool before budget-constrained selection:

```
┌─────────────────┐       ┌─────────────────┐
│ Graph Expander  │       │ Vector Searcher │
│ (Neo4j BFS)     │       │ (LanceDB topN)  │
└────────┬────────┘       └────────┬────────┘
         │                         │
         └──────────┬──────────────┘
                    ▼
        ┌───────────────────────┐
        │  Candidate pool       │
        │  (symbols + docs)     │
        └──────────┬────────────┘
                   ▼
        ┌───────────────────────┐
        │  Unified Scorer       │
        │  (blended score)      │
        └──────────┬────────────┘
                   ▼
        ┌───────────────────────┐
        │  Budget Fill          │
        │  (greedy by score)    │
        └───────────────────────┘
```

### 2.2 Candidate Types

Current Phase 9 candidates are symbols and docs. Phase 11 adds tenant API contract candidates from published manifests, using the same scoring/budget rules.

```python
@dataclass
class Candidate:
    kind: str               # "symbol" | "doc" | "tenant_api"
    uid: str                # symbol UID, doc chunk_id, or contract candidate ID
    token_cost: int
    graph_score: float      # 0 if not reached via graph
    semantic_score: float   # 0 if not found in vector search
    intent_weight: float    # from intent-driven tier priors
    provenance: list[str]   # e.g. ["graph:depth=2,rel=CALLS_DIRECT", "vector:sim=0.83"]
```

Dual provenance is first-class: a doc chunk found via vector search **and** linked via `COVERS` to a graph neighbor is strictly more valuable than either signal alone — the scorer rewards overlap.

### 2.3 Blended Score

```
score(c) = α * graph_score(c)
        + β * semantic_score(c)
        + γ * intent_weight(c)
        + δ * overlap_bonus(c)      // non-zero when BOTH signals fired
        - ε * token_cost(c) / 100
```

Default weights (tuned via eval harness):

| Weight | Value | Intuition |
|---|---|---|
| `α` | 1.0 | Graph connection is evidence of structural relevance |
| `β` | 0.8 | Semantic match is strong but noisier |
| `γ` | 0.4 | Intent steers tier preference, not absolute rank |
| `δ` | 0.5 | Bonus when graph AND semantic agree — precision signal |
| `ε` | 0.5 | Cost penalty (same as existing BFS) |

Scores are normalized to `[0, 1]` per track before blending — otherwise raw BFS scores (~1.2 for caller edges) dominate raw cosine similarities (~0.8).

### 2.4 Candidate Generation

**Graph track:** run BFS exactly as today, but stop at a candidate *pool* size (e.g. 50) instead of a token budget. Scores preserved.

**Semantic track:** top-K over both `docs` and `symbols` LanceDB tables using the query embedding. K default: 30.

**Fusion:**
- Symbols appearing in both tracks: merge rows, sum `graph_score` + `semantic_score`, mark overlap.
- Symbols appearing only in one track: keep with the other score at 0.
- Docs appearing only in vector track: keep; `graph_score = 0` unless a `COVERS` edge links the doc to any already-pooled symbol (then graph_score = prior × best_linked_symbol_score).

### 2.5 Budget Fill

Same "skip but keep trying" loop as the current BFS, now over the unified pool sorted by blended score:

```python
pool.sort(key=blended_score, reverse=True)

chosen = [primary]
spent = cost(primary) + PREAMBLE
for c in pool:
    if c.uid == primary.uid: continue
    if spent + c.token_cost > budget: continue  # skip, try next
    chosen.append(c)
    spent += c.token_cost

return assemble(chosen)
```

No special-casing of doc vs. symbol at fill time — they compete on identical terms.

### 2.6 Intent Integration

`intent_weight(c)` comes from the multi-label intent distribution ([spec_multi_label_intent.md](spec_multi_label_intent.md)) applied to tier priors. A `debugging`-weighted query boosts callees/callers; an `architecture`-weighted query boosts architecture-tagged docs.

### 2.7 Tenant API Candidates (Planned)

Tenant API candidates enter the pool only from published contract manifests, not from neighboring project source. They are generated after current-workspace retrieval and before direct LLM fallback.

Additional policy inputs:

| Input | Values | Purpose |
|---|---|---|
| `api_direction` | `outbound_dependencies`, `inbound_consumers`, `contract_impact`, `internal_processing`, `bidirectional_contract` | Boosts the correct side of a service boundary |
| `tenant_link_depth` | `0`, `1`, `2` | Limits traversal over published tenant links |

Tenant API scoring extends the blended score with `direction_weight`, `scope_weight`, `depth_decay`, `edge_type_weight`, and `confidence`. See [spec_tenant_api_graph.md](spec_tenant_api_graph.md).

## 3. API / Interface

```python
# sidecar/context/unified_ranker.py (new file)

class UnifiedRanker:
    def __init__(self, graph: GraphExpander, vector: VectorSearcher,
                 weights: RankerWeights = DEFAULT_WEIGHTS):
        ...

    def rank(
        self,
        target_uid: str,
        query: str,
        intent_dist: dict[str, float],
        budget: int,
    ) -> list[Candidate]:
        """Return budget-fitting candidates ordered by blended score."""
```

`ContextArbitrator.get_context_for_symbol()` calls `UnifiedRanker.rank(...)` in place of the current expand-then-append-docs sequence.

## 4. Prompt Contract Impact

Each `graph_context` and `documentation` entry gains:

```json
{
  "symbol": "validate_amount",
  "graph_score": 0.87,
  "semantic_score": 0.42,
  "blended_score": 1.15,
  "provenance": ["graph:CALLS_DIRECT,depth=1", "vector:sim=0.42"]
}
```

See [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) for the full contract.

## 5. Examples

```python
# Query: "why does process_payment fail on negative amounts"
#
# Graph track finds:
#   validate_amount  (graph_score=0.9, depth=1)
#   Audit.log        (graph_score=0.5, depth=2)
#
# Vector track finds:
#   doc_chunk#42 "Negative-amount handling in payments"  (sim=0.91)
#   validate_amount body match                            (sim=0.72)
#
# Unified pool:
#   validate_amount  (graph=0.9, sem=0.72, overlap=True) → blended ≈ 1.9
#   doc_chunk#42     (graph=0,   sem=0.91, overlap=False) → blended ≈ 0.9
#   Audit.log        (graph=0.5, sem=0,    overlap=False) → blended ≈ 0.5
#
# Old pipeline: graph fills budget, doc_chunk#42 truncated last.
# New pipeline: validate_amount first (double signal), doc_chunk#42 second.
```

## 6. Limitations (current)

- Doc chunks linked via `COVERS` to multiple symbols currently pick the max symbol score; better fusion (softmax, sum-with-penalty) is a tuning open question.
- Vector search runs at query time — adds ~10ms p50. Cache at the query-embedding layer to mitigate (see [spec_retrieval_cache.md](spec_retrieval_cache.md)).
- Overlap bonus can double-count when the same content appears in both a symbol body and a doc chunk that quotes it. Mitigation: the existing `ContextDeduplicator` runs *after* ranking and before assembly.

## 7. Planned Extensions

- **Learned reranker** (Phase 10+): replace hand-tuned weights with a small cross-encoder trained on feedback-loop signal.
- **Query-adaptive weights:** `α/β` could vary by intent (exploration → more β; navigation → more α).
- **Graph score as a function of semantic distance at each hop:** rather than treating BFS as pure structural expansion, propagate semantic similarity through the graph (Personalized PageRank style).
- **Tenant API candidates:** blend published service/endpoint/schema facts into the pool after workspace context and before direct LLM fallback.

## 8. Related

- [spec_token_budget_bfs.md](spec_token_budget_bfs.md) — the graph-side scoring this wraps.
- [spec_doc_anchor.md](spec_doc_anchor.md) — `COVERS` edges link docs and symbols for overlap detection.
- [spec_multi_label_intent.md](spec_multi_label_intent.md) — intent weights feed into γ.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) — surfacing scores in the contract.
- [spec_eval_harness.md](spec_eval_harness.md) — tuning substrate for α/β/γ/δ/ε.
- [spec_tenant_api_graph.md](spec_tenant_api_graph.md) — planned tenant API contract candidates and direction/depth policy.
