# Spec — Reverse Dependency AFFECTS Index (Phase 5)

> **Status:** Implemented for local workspace indexing. `AFFECTSIndexer` materializes workspace-scoped symbol-to-symbol reverse dependency edges, the fast indexer rebuilds them once per project pass, the single-file index path rebuilds them synchronously for changed symbols, and `/impact` exposes affected symbols/files. Current impact analysis is intentionally shallow: it is bounded reverse reachability over the relationships the indexer already understands, not a causal model of behavioral breakage. File-level materialized edges, per-edge depth metadata, staleness tracking, and rebuild CLI remain deferred.

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

`sidecar/indexer/affects.py` uses:

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

Implementation: `sidecar.indexer.affects.AFFECTSIndexer`.

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

`sidecar/indexer/code.py` keeps synchronous semantics for IDE save/index-file flows:

```python
if changed_uids:
    from sidecar.indexer.affects import AFFECTSIndexer

    AFFECTSIndexer(db).rebuild_affects(changed_uids, workspace_id=workspace_id)
```

### 3.2 Fast Project Path

`sidecar/indexer/fast/pipeline.py` rebuilds AFFECTS once over the union of all changed UIDs:

```python
AFFECTSIndexer(db).rebuild_affects(
    union,
    workspace_id=workspace_id,
    progress_callback=lambda n: reporter.step("affects", n=n),
)
```

The benchmark and fast indexer can skip this phase with `--skip-affects`, which is useful for isolating indexing cost from retrieval quality.

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

Implemented in `sidecar/main.py`:

```http
GET /impact?symbol=<name>
```

Behavior:

1. Resolve `workspace_id` from `X-Workspace`.
2. Look up the first symbol matching the provided name in that workspace.
3. Return affected symbols via `get_affected_symbols`.
4. Return affected files via `get_affected_files`.

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
  "max_depth": 4
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
- `tests/unit/test_sidecar_endpoints.py`
  - `/impact` returns affected symbols/files
- `tests/integration/test_phase5_validation.py`
  - integration smoke for populated AFFECTS and impact endpoint

## 7. Current Success Criteria

Implemented:

1. Unit tests for `AFFECTSIndexer` are green.
2. Single-file indexing rebuilds AFFECTS for changed symbols.
3. Fast indexing rebuilds AFFECTS once over the union of changed symbols.
4. `/impact?symbol=<name>` returns affected symbols/files and `max_depth`.
5. Benchmark reports include `skip_affects` and `affects_rebuilt` indexing stats.

Still deferred:

1. Persist minimum path depth on each `AFFECTS` edge.
2. Persist `derived_at` and implement explicit staleness detection.
3. Materialize file-level `AFFECTS` edges if query-time union becomes too slow.
4. Add a CLI such as `python -m sidecar.indexer.affects rebuild --all`.
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
