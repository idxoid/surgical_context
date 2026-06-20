# Spec — Reverse Dependency AFFECTS Index (Phase 5)

> **Status:** Implemented for workspace-scoped indexing. `AFFECTSIndexer` materializes symbol-to-symbol reverse dependency edges; full indexing and queued incremental batches rebuild them over changed UIDs. `/impact` no longer reads only AFFECTS: it runs the axis impact surface (reverse callers, structural API/inheritance, then AFFECTS fallback). Impact remains reachability evidence, not a causal proof of behavioral breakage. File-level materialized edges, per-edge depth metadata, staleness tracking, and a rebuild CLI remain deferred.

## 1. Problem

The incremental indexer tracks **file-level** changes via SHA256 hash comparison. When file A changes, the indexer can re-process file A's symbols, but downstream contexts may also become stale:

1. **Cascade invalidation** — Symbol B in file B calls Symbol A in file A. File A changes. Symbol B's context, which may include A, can now be stale.
2. **Context cache invalidation** — cache entries for callers/dependents need a reverse dependency lookup.
3. **Impact analysis** — "what breaks if I change A?" needs a reverse traversal.

`AFFECTS` is the materialized reverse dependency index for those cases.

### 1.1 Current Scope Caveat

Today, `AFFECTS` answers "what code is reachable from this change through known reverse dependency edges?" It does not yet answer the stronger product question "what will actually break?"

That distinction matters for benchmark interpretation and product UX:

- Runtime and framework mechanisms that are not represented as graph edges are invisible to impact.
- Dynamic dispatch, decorators, registries, templates, generated APIs, and macro-like systems may be under-modeled.
- Returned affected symbols/files are candidate evidence, not a ranked causal blast-radius proof.
- Tests/examples are useful impact evidence only when retrieval can tie them to the changed surface.

The current `/impact` surface should therefore be described as **likely related dependents** or **reachability-based impact**, not definitive change fallout.

## 2. Current Design

### 2.1 Edge Type

Implemented edge:

```cypher
(source:Symbol)-[:AFFECTS {workspace_id}]->(dependent:Symbol)
```

Meaning: changing `source` may affect `dependent`, because `dependent` is reachable through reverse `CALLS*`, `DEPENDS_ON`, `IMPLEMENTS`, or `OVERRIDES` traversal.

Currently **not** materialized:

```cypher
(File)-[:AFFECTS]->(File)
```

File impact is computed on demand by taking the union of affected symbols for all symbols contained in a file.

### 2.2 Traversed Relationship Types

`context_engine/indexer/affects.py` uses:

```python
CALLS_DIRECT
CALLS_DYNAMIC
CALLS_INFERRED
CALLS_SCOPED
CALLS_IMPORTED
CALLS_GUESS
DEPENDS_ON
IMPLEMENTS
OVERRIDES
```

The index intentionally uses typed call edges rather than the old generic `CALLS` relation.

### 2.3 Materialization Strategy

Implementation: `context_engine.indexer.affects.AFFECTSIndexer`.

Constants:

```python
MAX_AFFECTS_DEPTH = 4
MAX_FANOUT_PER_LEVEL = 200
REBUILD_BATCH_SIZE = 128
```

Algorithm:

1. Deduplicate changed symbol UIDs.
2. Delete existing outgoing `AFFECTS` edges from those symbols for the active `workspace_id`.
3. Load reverse adjacency for the whole workspace once.
4. For each changed UID, run bounded reverse BFS up to `MAX_AFFECTS_DEPTH`.
5. Apply `MAX_FANOUT_PER_LEVEL` to cap broad layers.
6. Batch `MERGE` `(source)-[:AFFECTS {workspace_id}]->(target)` edges.
7. Bump `Workspace.graph_version`.

Current implementation stores reachability, not path explanation. All returned affected symbols currently report `depth = 1` from the query API even if the internal BFS reached them through a deeper chain.

## 3. Indexing Integration

### 3.1 Single-File Path

`context_engine/indexer/code.py` keeps synchronous semantics for IDE save/index-file flows:

```python
if changed_uids:
    from context_engine.indexer.affects import AFFECTSIndexer

    AFFECTSIndexer(db).rebuild_affects(changed_uids, workspace_id=workspace_id)
```

### 3.2 Fast Project Path

`context_engine/indexer/fast/pipeline.py` rebuilds AFFECTS once over the union of all changed UIDs:

```python
AFFECTSIndexer(db).rebuild_affects(
    union,
    workspace_id=workspace_id,
    progress_callback=lambda n: reporter.step("affects", n=n),
)
```

The benchmark and fast indexer can skip this phase with `--skip-affects`, which is useful for isolating indexing cost from retrieval quality.

### 3.3 Queued Incremental Path

`IndexingService.process_index_batch()` calls `index_file(..., skip_affects=True)`
for changed files, rebuilds AFFECTS once over the deduplicated UID union, then
runs `run_axis_incremental_finalize()` for profile-aware propagation and
adjacency materialization before resolving pending anchors and invalidating
caches.

## 4. Query Interface

### 4.1 `AFFECTSIndexer.get_affected_symbols(...)`

Input: `symbol_uid`, `workspace_id`.

Returns:

```json
[
  {
    "uid": "affected-symbol-uid",
    "name": "caller",
    "file_path": "/repo/caller.py",
    "depth": 1
  }
]
```

### 4.2 `AFFECTSIndexer.get_affected_files(...)`

Input: `file_path`, `workspace_id`.

The method finds all symbols contained in the file, follows their `AFFECTS` edges, maps affected symbols back to files, and returns distinct affected file paths. This replaces the originally proposed materialized file-level `AFFECTS` edge for now.

### 4.3 `/impact` Endpoint

Implemented in `context_engine/api/routes/impact.py` and
`context_engine/axis/impact_surface.py`:

```http
GET /impact?symbol=<name>
```

Behavior:

1. Resolve `workspace_id` from `X-Workspace`.
2. Look up the first symbol matching the provided name in that workspace.
3. Seed `expand_impact_neighbourhood()` with that symbol.
4. Traverse reverse calls, structural API/inheritance evidence, and AFFECTS fallback.
5. Return classified rows plus distinct affected files. `max_depth` is caller-controlled from 1 to 4 (default 3).

Response shape:

```json
{
  "symbol": "process_payment",
  "symbol_uid": "...",
  "file_path": "/repo/payment.py",
  "affected_symbols": [],
  "affected_files": [],
  "affected_count": 0,
  "affected_file_count": 0,
  "max_depth": 3
}
```

## 5. Current Schema

Implemented relationship:

```cypher
(Symbol)-[:AFFECTS {workspace_id: string}]->(Symbol)
```

Deferred relationship/properties:

```cypher
(File)-[:AFFECTS]->(File)
(Symbol)-[:AFFECTS {depth: int, derived_at: datetime}]->(Symbol)
```

Depth and staleness metadata are not currently persisted. `Workspace.graph_version` is bumped after rebuild so downstream caches can use graph-version invalidation.

## 6. Tests

Implemented coverage:

- `tests/unit/test_affects_indexer.py`
  - batched delete/compute/merge flow
  - progress callback by batch
  - reverse adjacency grouping by dependency
  - bounded reverse BFS with depth and fanout caps
  - workspace-scoped parameters
- `tests/unit/test_incremental_indexing.py`
  - single-file path triggers AFFECTS rebuild for changed UIDs
  - skip/no-change paths do not rebuild
- `tests/unit/test_context_engine_endpoints.py`
  - `/impact` returns the structured axis impact surface
- `tests/unit/test_axis_impact_traversal.py`
  - direct/reverse/structural/AFFECTS traversal behavior and caps

## 7. Current Success Criteria

Implemented:

1. Unit tests for `AFFECTSIndexer` are green.
2. Single-file indexing rebuilds AFFECTS for changed symbols.
3. Fast indexing rebuilds AFFECTS once over the union of changed symbols.
4. `/impact?symbol=<name>` returns the structured axis impact surface and `max_depth`.
5. Benchmark reports include `skip_affects` and `affects_rebuilt` indexing stats.

Still deferred:

1. Persist minimum path depth on each `AFFECTS` edge.
2. Persist `derived_at` and implement explicit staleness detection.
3. Materialize file-level `AFFECTS` edges if query-time union becomes too slow.
4. Add a CLI such as `python -m context_engine.indexer.affects rebuild --all`.
5. Use AFFECTS directly for context cache invalidation once cache policy needs it.

## 8. Phase Sequencing

Depends on:

- Phase 3.5 incremental indexer ✅
- Phase 5 typed semantic edges ✅

Enables:

- `/impact` IDE/API surface ✅
- Benchmark impact-analysis questions ✅
- Future retrieval-cache invalidation
- Future dependency-aware test selection
