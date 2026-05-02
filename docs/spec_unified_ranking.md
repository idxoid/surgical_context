# Spec — Unified Ranking (Phase 9)

> **Status:** Implemented for local workspace retrieval. The current `UnifiedRanker` is the default path when a vector DB is available. Tenant API candidates remain future work.

## 1. Problem

Historically retrieval ran in two disconnected tracks:

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

Current candidates are symbols and docs. A future phase adds tenant API contract candidates from published manifests, using the same scoring and budget rules. The current local ranker also lets a candidate satisfy certain canonical roles through inferred capability support, so role fulfillment is not tied to one framework's exact symbol layout. That includes thin wrapper APIs whose own body is enough to prove orchestration or execution behavior even when nested helpers are not indexed as separate top-level symbols.

```python
@dataclass
class Candidate:
    kind: str               # "symbol" | "doc" | "tenant_api" (future)
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

Current behavior is slightly richer than the original greedy draft:

- fill token costs for vector-only symbols before judging readiness
- infer a mechanism from the target plus query
- route lookalike APIs to the right mechanism path instead of relying on one keyword bucket; e.g. Redux Toolkit listener middleware no longer falls into generic store-configuration handling just because the word `middleware` appears in the query
- resolve package/module-level targets when no symbol exists; e.g. `pydantic.v1` can use `pydantic/v1/__init__.py` as a synthetic primary module target instead of returning a false "symbol not found" success
- compute required roles on a canonical cross-framework taxonomy
- treat some roles as capability slots as well as identity slots; e.g. a runtime symbol like `SchemaValidator` can fulfill `validator_handle` if the dedicated wrapper/member symbol is absent
- let some primary APIs carry supporting roles directly when their implementation body already contains the relevant orchestration path
- let generic runtime/test signals fulfill impact-analysis roles so benchmark coverage is not tied to one framework's exact naming scheme, while applying topic-sensitive noise control so unrelated tests do not satisfy impact roles by accident
- apply topic-focused subsystem penalties for non-impact questions, preserving explicit role-fillers while downranking unrelated distant candidates from sibling subsystems such as query, listener, entity, and tooling internals
- apply sibling-subsystem penalties before fuzzy role bypass, so a candidate from an unrelated subsystem cannot survive merely because its name looks like a generic role such as `middleware` or `enhancer`; explicit `ROLE_BACKFILL` candidates still bypass the penalty
- use mechanism-aware role backfill before final selection
- sort by blended score with a bonus for role-filling candidates
- apply marginal-gain gating, intent floors, `context_complete_below_floor`, and signature-only fallback for low-gain distant candidates

The original greedy fill still describes the backbone, but the implementation now protects coverage quality rather than only raw score order.

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
# sidecar/context/unified_ranker.py

class UnifiedRanker:
    def __init__(self, neo4j_client, vector: VectorSearcher,
                 workspace_id: str = DEFAULT_WORKSPACE_ID,
                 weights: RankerWeights = DEFAULT_WEIGHTS):
        ...

    def get_target(...):
        """Workspace-scoped target resolution with duplicate disambiguation and module/package fallback."""

    def rank(
        self,
        target: SubgraphNode,
        query: str,
        intent: Intent,
        budget: int,
    ) -> tuple[list[Candidate], dict, str, list[dict], list[str]]:
        """Return selected candidates, budget info, stop reason, pruned details, and missing roles."""
```

`ContextArbitrator.get_context_for_symbol()` calls `UnifiedRanker.rank(...)` in place of the current expand-then-append-docs sequence.

## 4. Prompt Contract Impact

Each `graph_context` and `documentation` entry now carries ranking metadata. The contract also exposes `metadata.ranker`, `pruned[]`, and target-selection metadata.

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

- Tenant API candidates are not implemented yet; the ranker is still workspace-local.
- Canonical role normalization is in place, and handle-style capability inference now reduces dependence on framework-specific dunder/member indexing. The current generic fingerprint set is still narrow and should expand carefully before we rely on it for broader framework families.
- Doc chunks linked via `COVERS` to multiple symbols currently pick the max symbol score; better fusion (softmax, sum-with-penalty) is still open.
- Vector search runs at query time; cache at the query-embedding layer remains a future optimization (see [spec_retrieval_cache.md](spec_retrieval_cache.md)).

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


PS: 
Meanings:
## PS: Marginal Utility Context Selection

Current model:

```text
pool → sort by blended_score → greedy fill

Proposed change:

pool → sort by blended_score → incremental selection with stop condition
Utility Function
utility(c) =
    blended_score(c)
  - redundancy(c, chosen)
  - λ * token_cost(c)
Redundancy

Start with cheap deterministic checks:

same symbol UID
overlapping line ranges
doc chunk covers an already selected symbol

Optional later enhancement:

embedding similarity against already selected candidates

This should be added carefully because it increases runtime complexity.

Token Cost Penalty
λ ≈ 0.003 – 0.01

Tune through eval harness.

Stop Condition
for c in pool:
    u = utility(c)

    if u < min_utility:
        break

    if spent + c.token_cost > hard_cap:
        continue

    chosen.append(c)
    spent += c.token_cost
Expected Behavior

Simple questions should stop after a few high-utility candidates:

~800–1500 tokens

Complex questions should continue longer because useful candidates keep passing the utility threshold:

larger context, possibly above the base budget

However, expansion should still be bounded by an adaptive cap:

effective_cap = base_budget + trust_credit

or:

effective_cap = base_budget + cumulative_debit_allowance

The goal is not to fill the budget.
The goal is to stop when additional context no longer pays for its token cost.
