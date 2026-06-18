# Spec — Typed Semantic Edges (Phase 5)


> **Status:** Implemented for typed call edges and ranker traversal. The graph now supports and consumes `CALLS_DIRECT`, `CALLS_SCOPED`, `CALLS_IMPORTED`, `CALLS_DYNAMIC`, `CALLS_INFERRED`, and `CALLS_GUESS`; legacy `CALLS` is still accepted as a compatibility fallback. `IMPLEMENTS`, `OVERRIDES`, `REFERENCES`, and `SEMANTIC_HINT` are supported by traversal/scoring, but parser emission is uneven: inheritance currently writes `DEPENDS_ON` with metadata, not dedicated `IMPLEMENTS`/`OVERRIDES` edges, and `REFERENCES` is schema/ranker-ready but not broadly emitted.

## 1. Problem

The original graph used one broad `CALLS` edge for every call-like relationship. That was too coarse:

- a scoped/static call is stronger than a guessed name match
- an imported call has better evidence than a global name collision
- a method dispatch through `self` or an object is weaker than a direct function call
- a heuristic `getattr`/`eval` pattern should not rank like ordinary control flow

Typed semantic edges give axis graph walks and AFFECTS a confidence signal before semantic seed ranking runs.

## 2. Current Edge Types

### 2.1 CALLS Family

| Edge Type | Meaning | Current source | Confidence/Prior |
|---|---|---|---|
| `CALLS_DIRECT` | Direct static call fallback | Python/TS parser, migration | 1.0 |
| `CALLS_SCOPED` | Python call resolved to a unique symbol in local scope/file graph | Python parser + scoped resolver | 0.9 |
| `CALLS_IMPORTED` | Python identifier resolved through import binding | Python parser + imports | 0.85 |
| `CALLS_DYNAMIC` | Method/member dispatch (`self.x()`, `obj.x()`, `this.x()`) | Python/TS parser | 0.7 |
| `CALLS_INFERRED` | Known indirect call pattern (`getattr`, `eval`, etc.) | Python parser | 0.4 |
| `CALLS_GUESS` | Unresolved or ambiguous name match | Python parser / Neo4j fallback | 0.4 |
| `CALLS` | Legacy compatibility edge | old graphs only | treated as direct |

### 2.2 Other Relationship Types

| Edge Type | Meaning | Current status |
|---|---|---|
| `DEPENDS_ON` | General type/inheritance/import-style symbol dependency | Implemented |
| `IMPLEMENTS` | Class implements interface/abstract contract | Traversal/scoring support; not broadly emitted |
| `OVERRIDES` | Method overrides parent method | Traversal/scoring support; not broadly emitted |
| `REFERENCES` | Weak symbol reference/type-only mention | Schema/scoring support; not broadly emitted |
| `SEMANTIC_HINT` | Legacy semantic relationship | May exist in older graphs; **no longer produced** (framework/ts_http hints removed 2026-06); not in axis `EdgeProfile` |
| `IMPORTS` | File-to-file imports | Retained at file level |

## 3. Detection Logic

### 3.1 Python Adapter

Implemented in `context_engine/parser/adapters/python_adapter.py`.

Current call classification:

- `identifier()` defaults to `CALLS_DIRECT`.
- Imported identifiers become `CALLS_IMPORTED` and carry `callee_qualified_name`.
- Unique local-scope matches become `CALLS_SCOPED` and carry `callee_uid`.
- Ambiguous unresolved identifiers become `CALLS_GUESS`.
- `self.method()` and other attribute calls become `CALLS_DYNAMIC`.
- known indirect invocation names such as `getattr`, `setattr`, `operator.methodcaller`, `exec`, `eval`, `compile`, `__import__`, and `importlib.import_module` become `CALLS_INFERRED`.

Relationship metadata written with calls:

```json
{
  "rel_type": "CALLS_SCOPED",
  "tier": "scoped",
  "confidence": 0.9,
  "resolver": "py-scope-v1",
  "call_site_line": 42
}
```

### 3.2 TypeScript Adapter

Implemented in `context_engine/parser/adapters/typescript_adapter.py`.

Current call classification:

- top-level identifier calls default to `CALLS_DIRECT`
- member expressions become `CALLS_DYNAMIC`
- `this.method()` attempts local method UID resolution when possible

TypeScript support is intentionally simpler than Python scoped/imported resolution today.

**Exported object APIs.** `export const SidecarClient = { ask(...) { ... } }` is indexed as one `object_api` symbol (`signature_status: object_api_export`). Nested methods are not separate top-level symbols. Call extraction attributes HTTP helper calls to the object surface. See `tests/unit/test_typescript_adapter.py`.

### 3.3 Cross-language HTTP (scoped out)

Cross-language TS client → Python handler linking was previously attempted via regex-based `SEMANTIC_HINT` edges (`framework_hints`, `ts_http_route_hints`). **Removed 2026-06** — not consumed by axis retrieval, not grounded in AST. Revisit when TypeScript indexing emits structural route/call edges into the shared graph.

### 3.4 Inheritance and References

Inheritance extraction exists, but `Neo4jClient.link_inheritance(...)` currently writes:

```cypher
(subclass)-[:DEPENDS_ON {is_interface, confidence: 0.9, tier: "scoped", resolver: "inheritance-v1"}]->(superclass)
```

The original design called for dedicated `IMPLEMENTS` and `OVERRIDES` edges. Consumers already include those relationship types, so emitting them later is backward-compatible.

`REFERENCES` indexes and scoring exist, but parser-side reference extraction remains future work.

## 4. Graph Writes and Schema

`Neo4jClient.link_calls(...)` groups call rows by `rel_type` and resolution mode, then writes typed relationships with:

- `workspace_id`
- `call_site_line`
- `confidence`
- `tier`
- `resolver`

Supported modes:

- `uid` — direct target UID
- `qualified_name` — imported target
- name fallback — only links if exactly one candidate exists

Pre-prod and fresh workspaces get typed edges directly from the parser/indexer
(`CALLS_DIRECT`, `CALLS_SCOPED`, `CALLS_IMPORTED`, `CALLS_DYNAMIC`, `CALLS_INFERRED`,
`CALLS_GUESS`). Legacy `CALLS` is still accepted as a traversal fallback in
`Neo4jClient`, but new indexing does not emit it. Wipe + reindex is the supported
path when graph shape drifts — no separate migration CLI.

## 5. Retrieval Consumption

### 5.1 Axis graph walks

`context_engine/axis/graph_walk.py` traverses typed edges via `EdgeProfile` whitelists (`axis_profiles.py`). Pool passes call `walk_neighbours` / `walk_neighbours_grouped`. Seed ranking blends structural + semantic scores in `role_retrieval.py`. Legacy `CALLS` remains accepted where stored. `SEMANTIC_HINT` is **not produced** (removed 2026-06) and is not in axis edge profiles.

Example relation priors (legacy cascade; axis uses profile-specific caps):

```python
CALLS_DIRECT_out:   1.0
CALLS_DIRECT_in:    1.2
CALLS_SCOPED_out:   0.9
CALLS_SCOPED_in:    1.1
CALLS_IMPORTED_out: 0.85
CALLS_IMPORTED_in:  1.0
CALLS_DYNAMIC_out:  0.7
CALLS_DYNAMIC_in:   0.9
CALLS_INFERRED_out: 0.4
CALLS_INFERRED_in:  0.5
CALLS_GUESS_out:    0.4
CALLS_GUESS_in:     0.5
CALLS_out:          1.0
CALLS_in:           1.2
DEPENDS_ON:         0.8
IMPORTS:            0.6
IMPLEMENTS:         1.1
OVERRIDES:          1.1
REFERENCES:         0.3
```

## 6. AFFECTS Integration

`context_engine/indexer/affects.py` derives reverse dependency reachability from:

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

This means even guessed/inferred edges can contribute to impact reachability, but their weaker ranking priors still reduce their chance of dominating prompt context.

## 7. Tests

Implemented coverage includes:

- `tests/unit/test_typescript_adapter.py`
  - direct calls become `CALLS_DIRECT`
  - member/`this` calls become `CALLS_DYNAMIC`
- `tests/unit/test_p1_retrieval_correctness.py`
  - Python scoped/imported call extraction
  - Neo4j `link_calls` uses `callee_uid` and batches same resolution mode
- `tests/integration/test_phase5_validation.py`
  - typed semantic edge smoke validation
- axis walk / retrieval tests under `tests/unit/test_axis_*`
- AFFECTS tests in `tests/unit/test_affects_indexer.py`

## 8. Current Success Criteria

Implemented:

1. Parser emits typed call rows for Python and TypeScript.
2. Neo4j writes typed call relationships with confidence/tier/resolver metadata.
3. Axis graph walks traverse typed edges via `EdgeProfile`.
4. AFFECTS derives reverse impact from typed edges.
5. Legacy `CALLS` remains compatible where still stored.

Still deferred:

1. Emit dedicated `IMPLEMENTS` / `OVERRIDES` edges instead of only metadata-rich `DEPENDS_ON`.
2. Emit `REFERENCES` from type annotations/comments.
3. Add more parser tests for Python dynamic/inferred edge cases.
4. Use typed-edge confidence more directly in seed ranking blend, not only hop caps.

## 9. Phase Sequencing

Depends on:

- Phase 3.5 incremental graph indexing ✅

Enables:

- AFFECTS index ✅
- Unified graph scoring ✅
- Mechanism-aware benchmark diagnosis ✅
- Future confidence-aware retrieval and cache invalidation
