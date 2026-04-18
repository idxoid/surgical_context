# Spec — Token-Budget BFS (Phase 3.5)

> **Status:** Proposed. Replaces hardcoded `*1..2` traversal in [spec_arbitrator.md](spec_arbitrator.md) / [architectura.md §5.4](architectura.md).

## 1. Why a Budget, Not a Depth

Current BFS: `MATCH (target)-[:CALLS|DEPENDS_ON*1..2]->(dep)`. Depth is a constant. Problems:

- On a small target (leaf function), depth 2 over-fetches — wastes tokens on irrelevant siblings.
- On a hub target (5+ callees, each with 5 callees), depth 2 explodes to 25+ symbols, blows the context window.
- Users pay per token. The knob they care about is "how many tokens can I afford", not "how many hops".

Budget inverts the control: **the caller sets a token ceiling; depth is whatever fits**.

## 2. Algorithm

### 2.1 Inputs

```python
def assemble(target_uid: str, token_budget: int = 4000) -> PromptContext: ...
```

- `token_budget` — absolute ceiling on `to_system_prompt()` output (tiktoken estimate).
- Reserved: `target` body + system preamble ≈ 500 tokens fixed.
- Available for expansion: `token_budget - reserved`.

### 2.2 Priority Queue

Classic best-first expansion over the graph. Each candidate symbol has a score:

```
score(s) = w_rel * relation_prior(s)
        + w_fan * log(1 + caller_count(s))
        + w_rec * recency_score(s)
        - w_cost * token_cost(s) / 100
        - w_dist * distance_from_target(s)
```

Weights (v1 defaults, tuned via eval harness):

| Weight | Value | Intuition |
|---|---|---|
| `w_rel` | 1.0 | Relation type prior |
| `w_fan` | 0.3 | Hubs are worth visiting |
| `w_rec` | 0.2 | Co-changed symbols are load-bearing |
| `w_cost` | 0.5 | Big symbols need to earn their tokens |
| `w_dist` | 0.4 | Prefer nearby over far |

`relation_prior`:
- `CALLS` outgoing (callee): 1.0
- `CALLS` incoming (caller): 1.2 — callers drive intent, often more signal than callees
- `DEPENDS_ON` (types/imports): 0.8
- `COVERS` (DocAnchor): 0.6 — doc context is nice-to-have
- `INHERITS` (future): 1.1

### 2.3 Expansion Loop

```python
frontier = MaxHeap()
visited  = {target_uid}
chosen   = [target]
spent    = token_cost(target) + PREAMBLE_TOKENS

for neighbor in graph.neighbors(target, depth=1):
    frontier.push(score(neighbor, distance=1), neighbor)

while frontier and spent < budget:
    s = frontier.pop()
    if s.uid in visited:
        continue
    cost = token_cost(s)
    if spent + cost > budget:
        continue  # too big — skip but keep trying smaller ones
    chosen.append(s)
    visited.add(s.uid)
    spent += cost
    for n in graph.neighbors(s, depth=1):
        if n.uid not in visited:
            frontier.push(score(n, distance=s.distance + 1), n)

return assemble_context(chosen, spent, budget)
```

**Key property:** "skip but keep trying" — if a high-score symbol exceeds the remaining budget, we don't abort; we let cheaper symbols fill the remaining space. This avoids the pathological case where one fat symbol starves ten small ones.

### 2.4 `token_cost` Estimation

- Pre-computed at index time: each `Symbol` node gets a `token_estimate` property (tiktoken count of its body).
- Cheaper than running tiktoken on every assembly. Invalidated on hash change.
- First-query cold path: if missing, estimate = `(end_line - start_line) * 8` (empirical constant; ~8 tokens/line for Python).

### 2.5 `recency_score`

Optional, requires git integration (Phase 4). For now: constant 0. When lit up:

```
recency_score(s) = exp(-days_since_last_touch / 30)
```

Co-changed symbols (appear in the same commit as target within last 90 days) get a +0.3 bump. Catches implicit coupling that `CALLS` misses.

## 3. Cypher Shape

The hardcoded traversal is replaced with bounded neighbor-expansion per pop, driven from Python:

```cypher
// Called for each popped node; returns its unvisited neighbors with metadata.
MATCH (s:Symbol {uid: $uid})-[r:CALLS|DEPENDS_ON|INHERITS]-(n:Symbol)
WHERE NOT n.uid IN $visited
RETURN n.uid, n.name, n.token_estimate, type(r), startNode(r) = s AS outgoing
```

Each call is O(degree(s)), not O(degree^depth). Worst case over the whole traversal: O(budget / avg_cost * avg_degree). For budget=4000, avg_cost=200, avg_degree=5 → ~100 Cypher calls. Acceptable.

**Optimization (Phase 4):** batch pops — on each iteration, pop the top-k from the frontier (k=5) and issue one `UNWIND` query. Cuts round-trips 5×.

## 4. Output Contract Changes

`PromptContext.graph_context[i]` gains three fields:

```json
{
  "symbol": "db_save",
  "file_path": "sidecar/db.py",
  "relation": "CALLS",
  "direction": "callee",
  "depth": 1,
  "relevance_score": 0.87,
  "is_dirty": false,
  "code": "..."
}
```

- `direction`: `callee` / `caller` / `sibling` — tells the LLM whether this symbol uses the target or is used by it.
- `depth`: actual graph distance (not a cap, a measurement).
- `relevance_score`: the final score that got this symbol included. Useful for debugging and for the future token dashboard.

A top-level `budget` block is added:

```json
{
  "budget": { "limit": 4000, "spent": 3840, "reserved": 500, "pruned": 7 }
}
```

## 5. Edge Cases

- **Target has zero neighbors.** Return just the target. `spent` may be well below `budget` — that's fine, we don't pad.
- **Budget smaller than target body.** Hard error: `BudgetTooSmall`. Do not truncate source code silently — truncation breaks the LLM's parse and hides the problem.
- **Cycle: A calls B calls A.** `visited` set prevents re-entry. Second occurrence in heap is popped and dropped.
- **Orphan symbol (no File).** Skipped with a warning. Should be impossible post-indexer, but defensive.
- **Dirty overlay adds a symbol not yet in graph.** Overlay symbols are appended post-BFS as `depth=0, direction=overlay, relevance_score=1.0` — they're always included (user is actively working on them).

## 6. Tuning Protocol

Weights are not magic numbers — they're hyperparameters tuned against the eval harness ([spec_eval_harness.md](spec_eval_harness.md)).

1. Hold all but one weight fixed at defaults.
2. Sweep the target weight over `[0.0, 0.25, 0.5, 1.0, 2.0]`.
3. Plot `recall@5` vs `tokens_surgical` — find the Pareto frontier.
4. Pick the point closest to (recall=0.85, reduction=0.70).
5. Commit the new default with a `tuning_notes.md` entry.

Do **not** tune weights without the harness — you'll over-fit to whatever question you had in your head.

## 7. Non-Goals

- **Not** a learned ranker. No ML-trained weights in v1 — handcrafted, interpretable, tunable. ML re-ranker is a Phase 5 consideration if the harness shows the heuristic plateauing.
- **Not** a global optimum. Best-first is greedy; it does not guarantee the best budget-constrained subgraph (that's NP-hard via Knapsack). "Good enough fast" beats "optimal slow" for a 200ms SLO.
- **Not** token-exact. Tiktoken estimate ≠ API token count exactly. Leave a 5% margin under the budget.

## 8. Open Questions

- **Should the LLM see the budget block?** Probably not in the system prompt — it's metadata for the client / dashboard. Keep it in the JSON contract but strip it from `to_system_prompt()`.
- **Per-file token caps?** Possibly: prevent one bloated file from dominating. Defer until the harness shows it matters.
- **Streaming assembly?** Emit symbols as they're chosen, so the client can show progressive context. Cute but premature — revisit in Phase 5 alongside SSE streaming.

## 9. Related

- [spec_arbitrator.md](spec_arbitrator.md) — current arbitrator design (pre-budget).
- [spec_eval_harness.md](spec_eval_harness.md) — the tuning substrate for weights.
- [architectura.md §5.4](architectura.md) — the hardcoded Cypher this replaces.
- [road_map.md](road_map.md) — Phase 3.5 context.
