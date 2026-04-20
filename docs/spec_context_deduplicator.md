# Spec — ContextDeduplicator (Phase 4)

> **Status:** Proposed. Addresses uniform 883-token baseline observed in Phase 3.5 eval harness. Expected gain: 15–40% token reduction on real repos.

## 1. Problem

The Phase 3.5 benchmark shows a nearly uniform ~883 token cost across all query symbols, regardless of their complexity. Two sources of waste are confirmed:

**1. Transitive ancestor duplication** — Symbols that appear on multiple BFS paths are included multiple times in the compiled context. A shared utility function called by 5 different nodes in the subgraph is resolved and emitted 5 times.

**2. Doc chunk overlap** — Two DocAnchor nodes covering adjacent line ranges produce overlapping chunk content. The compiler emits both in full.

**3. Redundant relatives** — In a tight call cluster (A→B, A→C, B→C), node C is included via depth 1 (B→C) and depth 2 (A→C). The second path adds no information.

## 2. Architecture

### 2.1 Insertion Point

ContextDeduplicator is a pure stateless transform inserted between `GraphExpander` and `PromptCompiler` in `ContextArbitrator.get_context_for_symbol()`:

```
GraphExpander.expand()
  → ContextDeduplicator.deduplicate(subgraph)   ← new
    → CodeResolver.resolve(...)
      → PromptCompiler.compile(...)
```

`ContextArbitrator` wires it in:
```python
subgraph = GraphExpander(self.db).expand(symbol_name, token_budget=token_budget)
if isinstance(subgraph, str):
    return subgraph

subgraph = ContextDeduplicator().deduplicate(subgraph)  # pure, in-place
```

### 2.2 Component Interface

```python
class ContextDeduplicator:
    """Stateless transform: removes redundant nodes and doc chunks from a Subgraph."""

    def deduplicate(self, subgraph: Subgraph) -> Subgraph:
        """Return a new Subgraph with duplicates removed. Never mutates input."""
        nodes = self._deduplicate_nodes(subgraph.nodes)
        ...
        return Subgraph(primary=subgraph.primary, nodes=nodes, budget=updated_budget)
```

Input and output are both `Subgraph`. Budget is updated after deduplication to reflect the new `spent` value.

### 2.3 New File

```
sidecar/context/deduplicator.py
```

## 3. Deduplication Rules

### 3.1 Symbol Identity Normalization

**Rule:** A symbol is uniquely identified by its `uid`. If the same `uid` appears more than once in `subgraph.nodes`, keep only the entry with the lowest `depth` (closest to target). If depths are equal, keep the one with the higher `relevance_score`.

```python
def _deduplicate_nodes(self, nodes: list[SubgraphNode]) -> list[SubgraphNode]:
    seen: dict[str, SubgraphNode] = {}
    for node in nodes:
        existing = seen.get(node.uid)
        if existing is None:
            seen[node.uid] = node
        elif node.depth < existing.depth:
            seen[node.uid] = node
        elif node.depth == existing.depth and node.relevance_score > existing.relevance_score:
            seen[node.uid] = node
    return list(seen.values())
```

**Important:** Never deduplicate `subgraph.primary` against `subgraph.nodes`. The primary is always kept.

### 3.2 Line Range Collapse (Same File)

**Rule:** If two nodes in the final deduplicated list have the same `file_path` and overlapping or adjacent line ranges, merge them into a single node spanning the union. The merged node inherits the lower depth and higher relevance_score of the two.

Overlap condition: `max(a.range[0], b.range[0]) <= min(a.range[1], b.range[1]) + 1`

```python
def _collapse_line_ranges(self, nodes: list[SubgraphNode]) -> list[SubgraphNode]:
    by_file: dict[str, list[SubgraphNode]] = defaultdict(list)
    for node in nodes:
        by_file[node.file_path].append(node)

    result = []
    for file_path, file_nodes in by_file.items():
        result.extend(self._merge_overlapping(file_nodes))
    return result
```

Line range merge does NOT apply across different files or when `file_path == "<unknown>"`.

### 3.3 Doc Chunk Deduplication

**Rule:** If two DocChunks in `subgraph_docs` have `chunk_id` containing the same source file and overlapping character ranges (derived from section headers), keep the larger one. If content similarity > 0.85 by character overlap ratio, keep only the one with higher `relevance_score`.

Character overlap check (fast path, no embedding needed):

```python
def _overlap_ratio(a: str, b: str) -> float:
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return shorter.count(longer[:40]) / max(1, len(shorter) // 40)
```

This is a heuristic, not exact. Acceptable false-negative rate is fine (keeps a few redundant chunks) because over-deduplication is worse than under-deduplication.

### 3.4 Budget Recalculation

After deduplication:
1. Sum `token_estimate` for remaining nodes.
2. Update `subgraph.budget["spent"]` to reflect the new total.
3. Compute `saved = original_spent - new_spent` and write to `budget["dedup_saved"]` for observability.

## 4. Contract Additions

`Subgraph.budget` gains one new key:

```python
budget = {
    "limit": 4000,
    "spent": 620,       # after dedup (was e.g. 883)
    "reserved": 100,
    "pruned": 3,
    "dedup_saved": 263, # tokens removed by deduplication
}
```

`PromptContext.budget` inherits this through `PromptCompiler.compile()` unchanged — the key passes through automatically.

## 5. Algorithm Boundaries

| Input | Guarantee |
|---|---|
| `subgraph.primary` | Never removed |
| Nodes with same uid | Exactly one survives |
| Overlapping line ranges in same file | Merged into one span |
| Doc chunks with >85% overlap | Smaller one removed |
| Budget `spent` field | Always consistent with remaining nodes |

## 6. Tests

`tests/unit/test_context_deduplicator.py`:

| Test | Condition |
|---|---|
| `test_primary_never_removed` | Primary uid also in nodes → nodes entry removed, primary untouched |
| `test_keep_lower_depth_duplicate` | Same uid, depths 1 and 2 → depth-1 kept |
| `test_keep_higher_score_equal_depth` | Same uid, same depth, scores 0.9 and 0.6 → 0.9 kept |
| `test_collapse_adjacent_ranges` | Nodes at lines [1,10] and [9,20] in same file → merged to [1,20] |
| `test_no_cross_file_collapse` | Same ranges different files → both kept |
| `test_unknown_file_path_skipped` | `file_path == "<unknown>"` → no collapse attempted |
| `test_budget_updated_after_dedup` | token estimates sum correctly, `dedup_saved` accurate |
| `test_no_duplicates_noop` | Unique subgraph → identical output, `dedup_saved == 0` |

## 7. Success Criteria

1. Unit tests green.
2. `qa_benchmark.py --no-index` shows average token count lower than 883t across 10 questions.
3. `dedup_saved` in benchmark output is non-zero for at least 6/10 questions.
4. No regressions in recall@k or precision@k (dedup must not remove symbols in `expected_symbols`).

## 8. Phase Sequencing

No changes to Neo4j schema, LanceDB, or any parser. Safe to implement independently. Depends only on current `sidecar/context/types.py` interfaces.
