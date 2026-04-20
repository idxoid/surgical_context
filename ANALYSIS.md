# Project vs Specs Analysis: Phase 4 & 5 Status

## Summary
- **Phase 4**: ✅ COMPLETE (ContextDeduplicator + Embedding Versioning)
- **Phase 5**: ❌ NOT STARTED (Typed Semantic Edges + AFFECTS Index)
- **Total Gaps**: 7 implementation gaps across Phase 5

---

## Phase 4: ✅ COMPLETE

### ContextDeduplicator ✅
- **Status**: Fully implemented
- **Files**: `sidecar/context/deduplicator.py`, `tests/unit/test_context_deduplicator.py`
- **Tests**: 9 passing
- **Gap**: None

### Embedding Versioning ✅
- **Status**: Fully implemented
- **Files**: `sidecar/database/embedding_registry.py`, `sidecar/database/embedding_migration.py`
- **Features**: Model registry, metadata JSON column, cross-model guard, migration CLI
- **Tests**: All 61 unit tests passing
- **Gap**: None

---

## Phase 5: GAPS IDENTIFIED

### GAP 1: Typed Semantic Edges — Parser Detection Logic

**Spec Requirement**: Python/TypeScript adapters must distinguish between:
- `CALLS_DIRECT` — static callable reference (high confidence, prior=1.0)
- `CALLS_DYNAMIC` — method dispatch via `self.` or instance variable (medium, prior=0.7)
- `CALLS_INFERRED` — string patterns like `getattr()`, `operator.methodcaller()` (low, prior=0.4)

**Current State**:
- [sidecar/parser/adapters/python_adapter.py](sidecar/parser/adapters/python_adapter.py:34) — `extract_calls()` returns all calls as undifferentiated `rel_type="CALLS"`
- [sidecar/parser/adapters/typescript_adapter.py](sidecar/parser/adapters/typescript_adapter.py) — Same limitation

**Implementation Gap**:
```python
# Current (returns all as "CALLS")
MATCH (s:Symbol {uid: $uid})-[r:CALLS|DEPENDS_ON]-(n:Symbol)

# Spec requirement (distinguish 3 types)
MATCH (s:Symbol {uid: $uid})-[r:CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED|DEPENDS_ON]-(n:Symbol)
```

**Questions for User**:
1. Should we implement call type detection in Python adapter first, then TypeScript, or both in parallel?
2. For CALLS_INFERRED, how conservative should the heuristic be? The spec mentions `getattr`, `operator.methodcaller`, `globals()[name]()` — should we also detect string formatting patterns like `f"{module}.{func}"()`?
3. For CALLS_DYNAMIC, should we treat all `self.method()` calls the same, or sub-differentiate based on whether the method is overrideable?

---

### GAP 2: Typed Semantic Edges — Neo4j Schema

**Spec Requirement**: Create relationship indexes for new edge types:
```cypher
CREATE INDEX rel_calls_direct FOR ()-[r:CALLS_DIRECT]-() ON (r.uid)
CREATE INDEX rel_calls_dynamic FOR ()-[r:CALLS_DYNAMIC]-() ON (r.uid)
CREATE INDEX rel_calls_inferred FOR ()-[r:CALLS_INFERRED]-() ON (r.uid)
CREATE INDEX rel_implements FOR ()-[r:IMPLEMENTS]-() ON (r.uid)
CREATE INDEX rel_overrides FOR ()-[r:OVERRIDES]-() ON (r.uid)
CREATE INDEX rel_references FOR ()-[r:REFERENCES]-() ON (r.uid)
```

**Current State**: No schema migration script exists. Graph only has `CALLS`, `DEPENDS_ON`, `IMPORTS` edges.

**Implementation Gap**: Need to write Neo4j migration script + one-time edge conversion.

**Questions for User**:
1. Should old CALLS edges be migrated to CALLS_DIRECT (conservative default), or should we keep a parallel CALLS edge for backward compatibility during Phase 5?
2. If we migrate existing data, should this be a one-shot admin command or built into the sidecar startup?

---

### GAP 3: Typed Semantic Edges — BFS Scoring Update

**Spec Requirement**: Update [sidecar/context/graph_expander.py:13-18](sidecar/context/graph_expander.py#L13-L18) `RELATION_PRIOR`:
```python
RELATION_PRIOR = {
    "CALLS_DIRECT_out":   1.0,
    "CALLS_DIRECT_in":    1.2,
    "CALLS_DYNAMIC_out":  0.7,
    "CALLS_DYNAMIC_in":   0.9,
    "CALLS_INFERRED_out": 0.4,
    "CALLS_INFERRED_in":  0.5,
    "IMPLEMENTS":         1.1,
    "OVERRIDES":          1.1,
    "REFERENCES":         0.3,
    "DEPENDS_ON":         0.8,
}
```

**Current State**: Only has `CALLS_out`, `CALLS_in`, `DEPENDS_ON`, `IMPORTS`.

**Implementation Gap**: 
- [sidecar/context/graph_expander.py:162-170](sidecar/context/graph_expander.py#L162-L170) `_direction()` method must handle 7 new relation types
- [sidecar/context/graph_expander.py:140-160](sidecar/context/graph_expander.py#L140-L160) `_score()` method must map new relation types to priors
- [sidecar/context/graph_expander.py:174-188](sidecar/context/graph_expander.py#L174-L188) Cypher query in `_get_neighbors()` must include new edge types

**Questions for User**:
1. Are the prior weights in the spec your final tuning, or are these placeholders for empirical tuning against the Phase 2.5 eval harness?
2. Should IMPLEMENTS/OVERRIDES edges increase inbound prior above outbound (like CALLS), or keep them symmetric at 1.1?

---

### GAP 4: AFFECTS Index — Core Implementation

**Spec Requirement**: New file `sidecar/indexer/affects.py` with:
- `rebuild_affects(db: Neo4jClient, modified_symbol_uids: list[str])` — recompute transitive reverse deps
- `get_affected_symbols(db, symbol_uid) -> list[str]` — dependents of a symbol
- `get_affected_files(db, file_path) -> list[str]` — dependents of a file

**Current State**: File does not exist.

**Implementation Gap**:
```python
# Spec algorithm (pseudocode)
def rebuild_affects(db, modified_symbol_uids):
    # For each modified symbol:
    # - Walk reverse CALLS/DEPENDS_ON/IMPLEMENTS/OVERRIDES edges (incoming = who depends on me)
    # - Up to depth 4
    # - Create AFFECTS edges to all reachable dependents
```

**Questions for User**:
1. The spec mentions MAX_AFFECTS_DEPTH = 4 as "tunable." For your use case, is 4 the right tradeoff? (Deeper = more transitive coverage but slower rebuild)
2. Should AFFECTS rebuild happen **synchronously** in `index_file()` (blocking), or should we batch it for performance (queue + background worker)?
3. Should we materialize symbol-to-file AFFECTS as well, or compute it on demand from symbol-level AFFECTS?

---

### GAP 5: AFFECTS Index — Integration with Incremental Indexer

**Spec Requirement**: After `index_file()` modifies symbols, call `rebuild_affects(db, changed_uids)`.

**Current State**: [sidecar/indexer/code.py:128-138](sidecar/indexer/code.py#L128-L138) has no AFFECTS rebuild logic.

**Implementation Gap**:
```python
# Current (end of index_file):
resolve_pending_anchors(db, lance)
db.close()

# Spec requirement:
changed_uids = _get_changed_uids(file_path, db)
if changed_uids:
    from sidecar.indexer.affects import rebuild_affects
    rebuild_affects(db, changed_uids)
resolve_pending_anchors(db, lance)
db.close()
```

**Questions for User**:
1. Currently `index_file()` calls `delete_symbols_for_file()` then re-upserts. Should `_get_changed_uids()` return:
   - All UIDs that were re-upserted (optimistic: assume all changed)?
   - Or should we compute file-level hash + symbol-level hash diff to find only actual changes?
2. Should file-level AFFECTS (File→File) be computed eagerly in rebuild_affects, or lazily on first query to /impact?

---

### GAP 6: AFFECTS Index — Query Interface & Endpoint

**Spec Requirement**: 
- `GET /impact?symbol=<name>` endpoint
- Returns list of downstream dependent symbols/files that would be affected if symbol changed

**Current State**: Endpoint does not exist. [sidecar/main.py:119-148](sidecar/main.py#L119-L148) `/ask` is implemented, but no `/impact`.

**Implementation Gap**:
```python
# Spec (pseudocode)
@app.get("/impact")
def impact(symbol: str):
    affected_symbols = get_affected_symbols(db, symbol_uid)
    affected_files = get_affected_files(db, symbol_file_path)
    return {
        "symbol": symbol,
        "affected_symbols": affected_symbols,
        "affected_files": affected_files,
        "impact_depth": 4,
    }
```

**Questions for User**:
1. Should `/impact?symbol=<name>` return just the immediately-dependent symbols (depth=1), or all transitive dependents (depth≤4)?
2. Should the endpoint return a flat list or a tree structure showing the dependency chain?
3. Should `/impact` also report impact counts (e.g., "changes to this symbol affect 47 other symbols in 12 files")?

---

### GAP 7: AFFECTS Index — Cache Invalidation Use Case

**Spec Context**: "Makes incremental re-index cascade-aware" and "enables cache invalidation."

**Current State**: No caching layer exists in Phase 4. Phase 6 planning mentions cache ("cost savings").

**Implementation Gap**: AFFECTS edges are materialized but have no consumer yet.

**Questions for User**:
1. Is AFFECTS primarily for **future Phase 6 caching**, or do you want it operational in Phase 5 for a different use case (e.g., IDE "show callers" feature)?
2. If it's for Phase 6 caching, should we implement AFFECTS now (Phase 5) or defer it to Phase 6 when the cache layer is designed?

---

## Prioritization Recommendation

If starting Phase 5 now, suggest this order:

1. **GAP 1 + 2 + 3** (Typed Semantic Edges) — ~3-4 weeks
   - Implement call type detection in Python adapter
   - Migrate Neo4j schema
   - Update GraphExpander scoring

2. **GAP 4 + 5 + 6** (AFFECTS Index) — ~2-3 weeks
   - Implement affects.py
   - Integrate with incremental indexer
   - Add /impact endpoint

3. **GAP 7** (Usage clarification) — 0 weeks (dependent on Phase 6 planning)

---

## Test Impact
- Need 8-12 new unit tests for call type detection (Python adapter)
- Need 6-8 new tests for AFFECTS rebuild algorithm
- Need integration test for `/impact` endpoint
- Existing 61 tests should remain green (backward compatibility)
