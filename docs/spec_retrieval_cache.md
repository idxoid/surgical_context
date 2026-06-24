# Spec - Retrieval Cache

> **Status:** Partially active. `context_engine/cache/layered.py` implements three
> in-memory cache types. L3 response caching is wired into `/ask` and
> `/ask/stream`; L1 body and L2 subgraph caches are implemented and unit-tested
> but are not called by the current axis retrieval path. File indexing invalidates
> L1 and targeted L3 entries.

## 1. Current Components

### L1 - Symbol Body Cache

`InMemoryBodyCache` stores:

```text
key   = (file_path, line_range, file_hash)
value = CachedBody(code, token_count, is_dirty)
```

The default capacity is 10,000 entries. Entries are evicted by in-process LRU,
and `LayeredCache.invalidate_files(...)` removes every entry for an indexed file.

The implementation is currently dormant: active body resolution does not call
`LayeredCache.get_body()` or `put_body()`. Its hit/miss counters therefore remain
zero in ordinary context_engine requests.

### L2 - Subgraph Cache

`InMemorySubgraphCache` stores a `Subgraph` under:

```text
(workspace_id, graph_version, primary_uid, intent_hash, budget)
```

The default capacity is 1,000 entries. Including `workspace_id` and
`graph_version` makes the key suitable for isolated, versioned graph reads.

The current axis pipeline does not call this cache. There is no Redis backend,
version-rollover eviction metric, or active overlay-specific bypass in the
checked-in implementation.

### L3 - Prompt/Response Cache

`InMemoryResponseCache` is active. It stores:

```text
key   = (workspace_id, sha256(system_prompt + "\n" + question))
value = CachedResponse(answer, metadata, expires_at)
```

Defaults:

- capacity: 1,000 entries
- TTL: 86,400 seconds
- backend: process-local memory

The prompt hash covers the exact system prompt, including selected code and
dirty-overlay content, plus the user question. Workspace scope is a separate key
component.

## 2. Request Flow

Both `AskService.ask()` and `AskService.stream()`:

1. resolve axis/file/workspace/direct context
2. render the system prompt and calculate token/model-route metadata
3. look up L3 by workspace and prompt hash
4. skip the model call on a hit
5. cache a successful non-degraded model response on a miss

L3 therefore avoids a repeated model call, but it does **not** currently avoid
context retrieval or prompt assembly. A stream cache hit emits the cached answer
as one `chunk` event before the normal context/done events.

On a hit, `PromptContext.budget["cache_hits"]` includes `l3_response`, which is
serialized under `metadata.assembly.cache_hits`.

## 3. Targeted Invalidation

When L3 stores an answer, `AskContextBuilder.context_file_paths(ctx)` supplies the
files represented in that prompt. `InMemoryResponseCache` maintains a reverse
index from file path to response keys.

`IndexingService.index_file_now()` and `process_index_batch()` call:

```python
default_cache.invalidate_files(indexed_paths, base_workspace_id)
```

This operation:

- clears L1 entries whose key begins with each file path
- removes L3 responses tagged with those paths in the same workspace
- records L1/L3 invalidation counters

L2 relies on `graph_version` in its key if it is wired back into retrieval; the
current invalidation method does not explicitly clear L2.

## 4. Observability

Calls through `LayeredCache` emit:

```text
cache_hits_total{layer="l1_body|l2_subgraph|l3_response"}
cache_misses_total{layer="l1_body|l2_subgraph|l3_response"}
cache_invalidations_total{layer="l1_body|l3_response",workspace="..."}
```

Only L3 hit/miss activity is expected in normal requests today. The implementation
does not emit the previously proposed per-layer eviction reasons.

## 5. Isolation and Lifecycle

- L2 and L3 keys include `workspace_id`; L1 keys use absolute file path + hash.
- L3 can be shared by users in the same workspace when the rendered prompt is
  identical; `user_id` is not part of the key.
- All layers are process-local and are lost on context_engine restart.
- LRU bounds memory; only L3 also expires entries by time.
- No workspace-delete cache flush API is implemented because workspace deletion
  itself is not implemented.

## 6. Current Gaps

1. Decide whether L1 improves the axis context builder enough to wire it in.
2. Either connect L2 to a stable axis bundle/subgraph contract or remove the
   dormant API.
3. Move L3 lookup earlier only if a retrieval-independent key can preserve dirty
   overlay and graph-version correctness.
4. Add configurable capacity/TTL and a persistent/shared backend only after local
   measurements justify it.
5. Add explicit user scope if same-workspace response sharing is not acceptable.
6. Add a direct expiry test for the implemented L3 TTL path.

## 7. Tests

`tests/unit/test_retrieval_cache.py` covers:

- L1 hash/range keys and file invalidation
- workspace-separated L2 keys
- L3 workspace isolation, overwrite reindexing, and LRU bookkeeping
- targeted L3 invalidation without evicting another workspace
- the combined `LayeredCache.invalidate_files(...)` path

Ask endpoint tests cover L3 behavior through the service/API layer.

## 8. Related

- [spec_branch_isolation.md](spec_branch_isolation.md) - workspace identity and isolation.
- [spec_prompt_contract_observability.md](spec_prompt_contract_observability.md) - cache-hit serialization.
- [spec_indexer.md](spec_indexer.md) - index completion and invalidation hooks.
