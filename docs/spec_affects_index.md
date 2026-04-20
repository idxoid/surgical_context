# Spec — Reverse Dependency AFFECTS Index (Phase 5)

> **Status:** Proposed. Enables cascade-aware incremental re-indexing, cache invalidation, and dependency-aware build ordering.

## 1. Problem

The Phase 3.5 incremental indexer tracks **file-level** changes via SHA256 hash comparison. When file A changes, the indexer re-processes file A's symbols.

What it does NOT do:

1. **Cascade invalidation** — Symbol B in file B calls Symbol A in file A. File A changes. Symbol B's context (which includes A) is now stale, but the indexer does not know to re-derive or invalidate B.

2. **Context cache invalidation** — If a caching layer is added (Phase 6), it cannot invalidate cache entries for B without knowing that B depends on A.

3. **Impact analysis** — The question "what breaks if I change A?" requires a reverse traversal. Currently no edge supports this.

Without AFFECTS, correct incremental behavior requires a full graph rebuild on every file change — which defeats the purpose of incremental indexing at scale.

## 2. Design

### 2.1 Edge Type

```
(Symbol)-[:AFFECTS]->(Symbol)
(File)-[:AFFECTS]->(File)
```

`AFFECTS` is a derived edge — computed from existing `CALLS*`, `DEPENDS_ON`, `IMPLEMENTS`, `OVERRIDES` edges. It represents transitive reachability: A AFFECTS B if changing A could change the semantics of B.

It is **materialized** (stored in Neo4j), not computed on demand, because reverse traversal queries at query time are expensive at depth > 2.

### 2.2 Materialization Strategy

**Trigger:** After every `index_file()` call that modifies at least one symbol.

**Algorithm:** From each modified symbol, walk outgoing CALLS/DEPENDS_ON/IMPLEMENTS/OVERRIDES edges in reverse (incoming direction = who depends on me) up to depth `MAX_AFFECTS_DEPTH = 4`. Create/refresh AFFECTS edges for all reachable dependents.

```python
def rebuild_affects(db: Neo4jClient, modified_symbol_uids: list[str]):
    """Recompute AFFECTS edges from modified symbols outward."""
    with db.driver.session() as session:
        session.run("""
            UNWIND $uids AS uid
            MATCH (modified:Symbol {uid: uid})
            // Walk reverse dependencies up to depth 4
            MATCH (modified)<-[:CALLS_DIRECT|CALLS_DYNAMIC|DEPENDS_ON|IMPLEMENTS|OVERRIDES*1..4]-(dependent:Symbol)
            // Remove old AFFECTS edges from modified (will recreate)
            WITH modified, collect(DISTINCT dependent) AS deps
            OPTIONAL MATCH (modified)-[old:AFFECTS]->()
            DELETE old
            WITH modified, deps
            UNWIND deps AS dep
            CREATE (modified)-[:AFFECTS {depth: 1, derived_at: datetime()}]->(dep)
        """, uids=modified_symbol_uids)
```

**Note on depth:** MAX_AFFECTS_DEPTH = 4 is a tunable constant. Deep chains (A→B→C→D) are valid but rare; above depth 4 the signal-to-noise ratio drops.

### 2.3 File-Level AFFECTS

Derived from symbol-level AFFECTS:

```cypher
// File AFFECTS: if any symbol in file A affects any symbol in file B, then A AFFECTS B
MATCH (fa:File)-[:CONTAINS]->(sa:Symbol)-[:AFFECTS]->(sb:Symbol)<-[:CONTAINS]-(fb:File)
WHERE fa <> fb
MERGE (fa)-[:AFFECTS]->(fb)
```

Run after symbol AFFECTS rebuild.

### 2.4 Integration with Incremental Indexer

In `sidecar/indexer/code.py`, after `index_file()` completes:

```python
def index_file(file_path: str, db: Neo4jClient, vector_db, extractor):
    # ... existing indexing logic ...

    # Phase 5: AFFECTS rebuild for changed symbols
    changed_uids = _get_changed_uids(file_path, db)
    if changed_uids:
        from sidecar.indexer.affects import rebuild_affects
        rebuild_affects(db, changed_uids)
```

`_get_changed_uids()` returns UIDs of symbols that were added, updated, or deleted in this index_file() call. Current indexer already tracks this implicitly via `delete_symbols_for_file` + re-upsert.

### 2.5 New File

```
sidecar/indexer/affects.py
```

Exports:
- `rebuild_affects(db, symbol_uids)` — recompute AFFECTS from changed symbols
- `get_affected_symbols(db, symbol_uid) -> list[str]` — return UIDs of all dependents
- `get_affected_files(db, file_path) -> list[str]` — return paths of all dependent files

## 3. Query Interface

### Impact Analysis (API endpoint: `/impact`)

```python
@app.get("/impact")
def impact_analysis(symbol: str):
    db = get_db()
    try:
        with db.driver.session() as session:
            result = session.run("""
                MATCH (s:Symbol {name: $name})-[:AFFECTS*1..]->(dep:Symbol)
                OPTIONAL MATCH (f:File)-[:CONTAINS]->(dep)
                RETURN dep.name AS name, coalesce(f.path, '<unknown>') AS file_path,
                       length(shortestPath((s)-[:AFFECTS*]->(dep))) AS depth
                ORDER BY depth
            """, name=symbol)
            return {"impacts": [dict(r) for r in result]}
    finally:
        db.close()
```

### Cache Invalidation (internal)

```python
def invalidate_context_cache(db, file_path: str, cache: LRUCache):
    """Remove all cached contexts that depend on file_path."""
    affected = get_affected_files(db, file_path)
    for path in [file_path] + affected:
        cache.invalidate_by_file(path)
```

## 4. Schema Changes

### Neo4j New Relationship Type

```
(Symbol)-[:AFFECTS {depth: int, derived_at: datetime}]->(Symbol)
(File)-[:AFFECTS {derived_at: datetime}]->(File)
```

Properties:
- `depth` — minimum path length via CALLS/DEPENDS_ON/etc. that produces this AFFECTS edge
- `derived_at` — timestamp of last materialization (for staleness detection)

### Index

```cypher
CREATE INDEX affects_symbol FOR ()-[r:AFFECTS]-() ON (r.derived_at)
```

## 5. Staleness and Rebuild

AFFECTS edges can be stale if:
- A CALLS/DEPENDS_ON edge is added or removed and the AFFECTS rebuild was skipped
- The indexer crashed mid-run

Staleness detection: any Symbol with `modified_at > AFFECTS.derived_at` on incoming AFFECTS edges is considered stale.

Full rebuild command:
```bash
python -m sidecar.indexer.affects rebuild --all
```

Partial rebuild (default, triggered by index_file):
```bash
python -m sidecar.indexer.affects rebuild --file path/to/file.py
```

## 6. Tests

`tests/unit/test_affects_index.py`:

| Test | Condition |
|---|---|
| `test_affects_created_after_index_file` | After indexing a file, AFFECTS edges exist for downstream dependents |
| `test_affects_depth_is_minimum_path` | A→B→C: C's AFFECTS depth from A is 2 |
| `test_affects_deleted_on_edge_removal` | Remove CALLS A→B: AFFECTS A→B removed on next rebuild |
| `test_get_affected_files_transitive` | File A changes → B and C returned if B depends on A and C depends on B |
| `test_rebuild_all_is_idempotent` | Running full rebuild twice → same AFFECTS set |
| `test_staleness_detected` | Symbol modified_at newer than AFFECTS derived_at → flagged as stale |

`tests/integration/test_affects_incremental.py`:

| Test | Condition |
|---|---|
| `test_edit_payment_py_invalidates_processor_context` | Modify payment.py → get_affected_files includes processor.py |
| `test_add_new_caller_extends_affects` | New call site in file C to symbol A → C appears in AFFECTS(A) |

## 7. API Additions

```
GET /impact?symbol=<name>
```

Returns all symbols and files that depend on the given symbol, with depth from the symbol. Used by IDE extension for "show impact" feature.

## 8. Success Criteria

1. Unit tests green.
2. After indexing `tests/fixtures/sample_project/`, AFFECTS edges exist for at least 5 symbol pairs.
3. `GET /impact?symbol=process_payment` returns at least 2 downstream symbols.
4. Modifying a fixture file and re-indexing updates AFFECTS edges correctly.
5. Full rebuild is idempotent.

## 9. Phase Sequencing

Depends on:
- Phase 3.5 incremental indexer ✅
- Phase 5 typed semantic edges (AFFECTS traversal uses CALLS_DIRECT/DYNAMIC/etc. — more precise than CALLS alone). Can be implemented before typed edges if using `CALLS|DEPENDS_ON` temporarily.

Enables:
- Phase 6 caching layer (cache invalidation requires AFFECTS)
- Future: `GET /impact` IDE feature, dependency-aware test selection
