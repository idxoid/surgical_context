# Spec — Retrieval Cache (Phase 10)

> **Status:** Proposed. Three-layer cache between parser / graph / LLM. Not critical for MVP — becomes mandatory once repo size or user count grows past a single-instance Neo4j's comfort zone.

## 1. Problem

Every `/ask` today repeats work that could be memoized:

- Read symbol body from disk (or overlay) → tiktoken encode → 5–20ms per symbol.
- Graph BFS for `(symbol, intent)` → 20–80ms at depth 2 on a 10k-symbol graph.
- LLM round trip for identical `(context, question)` → 500–3000ms.

At 10 requests/s the Neo4j pool saturates before the model does. Latency rises even though 80% of the work is re-computation.

## 2. Design — Three Layers

### 2.1 Layer 1 — Symbol Body Cache

**Key:** `(file_path, range, file_hash)`
**Value:** `(code_text, token_count)`
**TTL:** indefinite; invalidated on file hash change.
**Backend:** in-process LRU (default 10k entries ≈ 20 MB).

Populated on every body resolve. Invalidated by the indexer when `File.hash` changes.

Reasoning: source code is already hashed; cache key is free correctness. No stale reads possible — the hash is the version.

### 2.2 Layer 2 — Subgraph Cache

**Key:** `(primary_uid, intent_distribution_hash, budget, workspace_id, graph_version)`
**Value:** serialized `Subgraph` (list of `SubgraphNode`, scores, metadata).
**TTL:** until `graph_version` changes.
**Backend:** Redis (shared across sidecar instances) or in-process LRU (single-instance).

`graph_version` is a monotonically incrementing integer stored on the `Workspace` node. Incremented atomically on any mutation to that workspace (index, overlay commit, AFFECTS rebuild). Key includes `graph_version` so invalidation is implicit — cache entries become unreachable once the graph moves forward.

**Why not invalidate by key?** Graph changes fan out unpredictably. Explicit invalidation requires tracking reverse indexes; version-keyed entries garbage-collect themselves via LRU.

### 2.3 Layer 3 — Prompt/Response Cache

**Key:** `sha256(system_prompt || user_question)` — bound to the exact text the model sees.
**Value:** assistant response (full text + metadata).
**TTL:** 24 hours default, configurable.
**Backend:** Redis with TTL, or disk-backed SQLite for local mode.

Distinct from Anthropic's ephemeral prompt caching — that layer is provider-side and short-lived. L3 is our own observable, replayable cache.

**Honesty guard:** L3 entries carry the intent and mode used to build the prompt. Cross-user sharing is only safe when `workspace_id` matches — embed it in the key.

### 2.4 Hit / Miss Semantics

```
/ask
 ├─ L1 body cache            (hit: skip disk + tiktoken)
 ├─ L2 subgraph cache        (hit: skip BFS + Cypher)
 │   └─ L1 for each body     (nested)
 └─ L3 response cache        (hit: skip model call entirely)
```

L3 is checked first because it's the cheapest short-circuit. L1/L2 fire during compose when L3 misses.

### 2.5 Observability

Every cache layer exports counters to the metrics endpoint:

```
cache_hits_total{layer="l1_body"} ...
cache_misses_total{layer="l1_body"} ...
cache_hits_total{layer="l2_subgraph"} ...
cache_evictions_total{layer="l2_subgraph", reason="version_rollover"} ...
cache_hits_total{layer="l3_response"} ...
```

Prompt contract's `metadata.assembly` gains `cache_hits: ["l1_body", "l2_subgraph"]` — each request records which layers short-circuited it.

## 3. API / Interface

```python
# sidecar/cache/layered.py (new file)

class LayeredCache:
    def __init__(self, l1: BodyCache, l2: SubgraphCache, l3: ResponseCache):
        ...

    # L1
    def get_body(self, file_path: str, range: tuple[int, int],
                 file_hash: str) -> tuple[str, int] | None: ...
    def put_body(self, ...) -> None: ...

    # L2
    def get_subgraph(self, primary_uid: str, intent_hash: str,
                     budget: int, workspace_id: str,
                     graph_version: int) -> Subgraph | None: ...
    def put_subgraph(self, ..., subgraph: Subgraph) -> None: ...

    # L3
    def get_response(self, prompt_hash: str) -> CachedResponse | None: ...
    def put_response(self, prompt_hash: str, response: CachedResponse) -> None: ...
```

Backend abstraction lets dev run in-memory, prod run Redis.

## 4. Invalidation Rules

| Event | L1 action | L2 action | L3 action |
|---|---|---|---|
| File re-indexed (hash change) | Invalidate entries for that path | — | — |
| Symbol body edited (overlay) | Key is overlay-path; separate LRU namespace | — | — |
| Graph mutation (any node/edge) | — | Bump `graph_version` | — |
| Ranker weights changed | — | Bump a global `ranker_version` in keys | Invalidate all |
| Time passes | LRU evict | LRU evict | TTL expire |
| Workspace deleted | Flush entries with that `workspace_id` | Flush | Flush |

## 5. Examples

```python
cache = LayeredCache(l1=InMemoryBodyCache(capacity=10_000),
                     l2=RedisSubgraphCache(client=redis),
                     l3=RedisResponseCache(client=redis, ttl_s=86400))

# On /ask
prompt_hash = sha256((system_prompt + question).encode()).hexdigest()
cached = cache.get_response(prompt_hash)
if cached:
    return cached.with_meta(cache_hits=["l3_response"])

# L3 miss — build context
subgraph = cache.get_subgraph(primary_uid, intent_hash, budget, ws_id, graph_v)
if subgraph is None:
    subgraph = expander.expand(...)
    cache.put_subgraph(primary_uid, intent_hash, budget, ws_id, graph_v, subgraph)

# For each symbol body
for node in subgraph.nodes:
    body = cache.get_body(node.file_path, node.range, file_hash)
    if body is None:
        body = resolver.read(node)
        cache.put_body(node.file_path, node.range, file_hash, body)
```

## 6. Limitations (current)

- **L2 cache misses on overlay queries.** Dirty state makes the effective subgraph workspace-specific in a way version alone doesn't capture. Mitigation: overlay-affected requests skip L2 (correctness > hit rate).
- **L3 is per-workspace.** Two users on the same branch asking the same question share cache; users on different branches don't. Acceptable — workspace isolation mandate trumps sharing.
- **Cold start is cold.** First query after a restart hits every layer empty. Warmup pre-fetch for top-N most-asked-about symbols is Planned.
- **Memory blowup risk at large repos.** 10k body entries × 2KB avg = 20MB; at 100k repo with long files this can reach GBs. Use on-disk LRU (lmdb) if profiling shows it matters.

## 7. Planned Extensions

- **Negative cache:** remember queries that returned `mode=standard` (no surgical context) — skip expensive retrieval for repeats.
- **Warm-up on deploy:** pre-populate L2 for hot symbols from the prior instance's metrics.
- **Shared-tenant L3:** for public docs / open-source repos, allow cross-user L3 hits with a `public=true` flag.
- **Etag / If-None-Match at the HTTP layer:** expose `graph_version` to clients; they send `If-None-Match`; 304 on hit. Cheapest possible cache.

## 8. Related

- [spec_token_budget_bfs.md](spec_token_budget_bfs.md) — subgraph shape cached at L2.
- [spec_branch_isolation.md](spec_branch_isolation.md) — workspace ID must be in every L2/L3 key.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) — cache hits surface in `metadata.assembly.cache_hits`.
- [spec_arbitrator.md](spec_arbitrator.md) — arbitrator is the cache integration point.
