# Spec — Typed Semantic Edges (Phase 5)

> **Status:** Proposed. Replaces the single `CALLS` relationship type with semantically precise edge types. Affects indexer, BFS scoring, and graph schema.

## 1. Problem

The current graph uses three edge types for all relationships:

```
(Symbol)-[:CALLS]->(Symbol)
(Symbol)-[:DEPENDS_ON]->(Symbol)
(Symbol)-[:IMPORTS]->(Symbol)
```

All `CALLS` edges are treated equally in BFS scoring. This is wrong:

- A **direct static call** (`validate(x)`) is high-confidence — the callee always runs when the line is reached.
- A **dynamic dispatch** (`self.handler(x)`) is ambiguous — the callee depends on runtime type.
- A **decorator call** (`@cached`) is a control-flow modifier, not a data-flow dependency.
- An **inferred call** from string patterns (`getattr(obj, 'process')()`) is low-confidence.

Treating these identically means the BFS scoring function rewards or penalizes every call equally, reducing retrieval precision.

## 2. New Edge Types

### CALLS family

| Edge Type | Meaning | Confidence | BFS Prior |
|---|---|---|---|
| `CALLS_DIRECT` | Static callable reference at call site | High | 1.0 |
| `CALLS_DYNAMIC` | Method dispatch via `self.`, virtual call, duck typing | Medium | 0.7 |
| `CALLS_INFERRED` | String-pattern or heuristic call (getattr, eval, plugin) | Low | 0.4 |

### DEPENDS_ON family

| Edge Type | Meaning | Confidence | BFS Prior |
|---|---|---|---|
| `IMPLEMENTS` | Class implements interface or abstract base | High | 1.1 |
| `OVERRIDES` | Method overrides parent class method | High | 1.1 |
| `DEPENDS_ON` | General type/import dependency (unchanged) | Medium | 0.8 |
| `REFERENCES` | Weak reference (type annotation only, comment mention) | Low | 0.3 |

### Removed / Consolidated

- `CALLS` — removed; replaced by CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED
- `IMPORTS` — retained unchanged at file level (File→File edges are not split)

## 3. Detection Logic

### 3.1 CALLS_DIRECT

Condition: call site is a static reference — a simple `Name` or `Attribute` node where the object is not `self` or an interface variable.

```python
# Python examples → CALLS_DIRECT
validate_amount(x)
utils.format(x)
PaymentError()
```

Tree-sitter check: `call.function` node is `identifier` or `attribute` where attribute root is a module-level name (not a parameter or `self`).

### 3.2 CALLS_DYNAMIC

Condition: method call on `self`, an instance variable, or a known interface/abstract parameter.

```python
# Python examples → CALLS_DYNAMIC
self.validate(x)
handler.process(x)
obj.run()
```

Tree-sitter check: `call.function` node is `attribute` where root is `self`, or root name appears in constructor parameter list (dependency injection pattern).

### 3.3 CALLS_INFERRED

Condition: indirect invocation via `getattr`, `operator.methodcaller`, string formatting, or known dispatch patterns.

```python
# Python examples → CALLS_INFERRED
getattr(obj, method_name)()
operator.methodcaller('process')(obj)
globals()[fn_name]()
```

Tree-sitter check: call contains `getattr` with a variable string, or call function is itself a call expression (higher-order dispatch).

### 3.4 IMPLEMENTS / OVERRIDES

Condition: determined from class definition inheritance and method name matching.

```python
# IMPLEMENTS → class B(A) where A has abstract methods
class PaymentProcessor(BaseProcessor):  # B IMPLEMENTS A

# OVERRIDES → method defined in both B and parent A
def validate(self):  # PaymentProcessor.validate OVERRIDES BaseProcessor.validate
```

Tree-sitter check: class node has `bases`; for each base, look up existing `DEPENDS_ON` edge to parent symbols. For `OVERRIDES`, cross-reference method names in parent class body.

### 3.5 REFERENCES

Condition: symbol appears in type annotation only (not called or instantiated), or in a comment/docstring.

```python
# REFERENCES — type hint only, never instantiated or called
def process(payment: Payment) -> None:  # process REFERENCES Payment
```

Tree-sitter check: symbol appears in `type_annotation` nodes only, with no corresponding `call` or `instantiation`.

## 4. Schema Changes

### Neo4j

No new node types. New relationship types only. Backward-compatible: old `CALLS` edges can coexist with new types during migration.

```cypher
// New relationship types (all properties same as current CALLS)
CALLS_DIRECT, CALLS_DYNAMIC, CALLS_INFERRED, IMPLEMENTS, OVERRIDES, REFERENCES
```

Add indexes on all new relationship types:
```cypher
CREATE INDEX rel_calls_direct FOR ()-[r:CALLS_DIRECT]-() ON (r.uid)
```

### BFS Cypher Update

Current:
```cypher
MATCH (s:Symbol {uid: $uid})-[r:CALLS|DEPENDS_ON]-(n:Symbol)
```

Updated:
```cypher
MATCH (s:Symbol {uid: $uid})-[r:CALLS_DIRECT|CALLS_DYNAMIC|CALLS_INFERRED|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES]-(n:Symbol)
```

### Scoring Table Update

In `GraphExpander.RELATION_PRIOR`:

```python
RELATION_PRIOR = {
    "CALLS_DIRECT_out":   1.0,
    "CALLS_DIRECT_in":    1.2,
    "CALLS_DYNAMIC_out":  0.7,
    "CALLS_DYNAMIC_in":   0.9,
    "CALLS_INFERRED_out": 0.4,
    "CALLS_INFERRED_in":  0.5,
    "IMPLEMENTS_out":     1.1,  # class → interface (understand the contract)
    "IMPLEMENTS_in":      0.8,  # interface ← implementation
    "OVERRIDES_out":      1.1,  # method → parent (understand the base)
    "OVERRIDES_in":       0.9,  # parent ← override (discover extensions)
    "DEPENDS_ON_out":     0.8,
    "DEPENDS_ON_in":      0.6,
    "REFERENCES_out":     0.3,
    "REFERENCES_in":      0.2,
}
```

## 5. Migration

### From existing graph

Existing `CALLS` edges → reclassified to `CALLS_DIRECT` by default (conservative: assume static call). Dynamic edges are added on next re-index.

```cypher
// One-time migration
MATCH (a)-[r:CALLS]->(b)
CREATE (a)-[:CALLS_DIRECT {uid: r.uid}]->(b)
DELETE r
```

Run before Phase 5 indexer deployment. Can be rolled back by reverting to `CALLS` with a complementary migration.

### Incremental re-index

New `index_file()` calls emit CALLS_DIRECT / CALLS_DYNAMIC / CALLS_INFERRED based on detection logic. Old `CALLS` edges from un-re-indexed files remain as `CALLS_DIRECT` (via migration above) until the file is re-indexed.

## 6. Parser Changes

### Python Adapter

Update `PythonAdapter._extract_calls()` to return `(callee_uid, edge_type)` pairs instead of `callee_uid` strings.

Signature change:
```python
# Before
def _extract_calls(self, tree, source) -> list[str]

# After
def _extract_calls(self, tree, source) -> list[tuple[str, str]]
# Returns list of (callee_name, edge_type)
```

### TypeScript Adapter

Same signature change. TypeScript dynamic detection is simpler: any `this.method()` is CALLS_DYNAMIC; top-level function calls are CALLS_DIRECT.

### LanguageAdapter Protocol

Update `LanguageAdapter.extract_calls()` protocol signature accordingly.

## 7. Tests

`tests/unit/test_python_adapter_call_types.py`:

| Test | Condition |
|---|---|
| `test_direct_call_edge_type` | `validate(x)` → `CALLS_DIRECT` |
| `test_self_call_edge_type` | `self.process(x)` → `CALLS_DYNAMIC` |
| `test_getattr_call_edge_type` | `getattr(obj, fn)()` → `CALLS_INFERRED` |
| `test_class_implements_edge` | Class inheriting abstract base → `IMPLEMENTS` |
| `test_method_overrides_edge` | Method with same name as parent → `OVERRIDES` |
| `test_type_annotation_only` | Parameter type hint, never called → `REFERENCES` |

`tests/unit/test_graph_expander_scored_by_type.py`:

| Test | Condition |
|---|---|
| `test_direct_scores_higher_than_dynamic` | `CALLS_DIRECT` relation scores > `CALLS_DYNAMIC` at same depth |
| `test_inferred_is_lowest_priority` | `CALLS_INFERRED` scores < all other types |
| `test_implements_scores_above_depends_on` | `IMPLEMENTS` prior > `DEPENDS_ON` prior |
| `test_references_skipped_on_tight_budget` | `REFERENCES` node is dropped first when budget is tight |

## 8. Success Criteria

1. Parser unit tests: correct edge type for each call pattern.
2. BFS scoring: `CALLS_DIRECT` nodes ranked higher than `CALLS_DYNAMIC` at same depth.
3. QA benchmark: no regression in recall@k; precision@k improves by at least 0.05.
4. Migration script runs cleanly on existing development graph.

## 9. Phase Sequencing

Depends on:
- Phase 3.5 `CALLS`, `DEPENDS_ON`, `IMPORTS` edges ✅
- Phase 4 ContextDeduplicator (independent but recommended first — easier to measure precision improvement with dedup noise removed)

Does NOT require:
- LanceDB changes
- PromptCompiler changes
- Overlay changes
