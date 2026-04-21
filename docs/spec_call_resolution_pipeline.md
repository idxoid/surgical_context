# Spec — Call Resolution Pipeline (Phase 8)

> **Status:** Proposed. Formalizes call-edge creation as a staged resolver rather than name-match. Depends on [spec_uid_stability.md](spec_uid_stability.md); refines [spec_typed_semantic_edges.md](spec_typed_semantic_edges.md).

## 1. Problem

Current indexer resolves callees by `MATCH (callee:Symbol {name: $name})`. This is a string match across the entire graph. Problems:

- **Name collisions:** two `parse()` functions in different modules point at each other or both.
- **Methods:** `obj.save()` resolves to every `save` in the graph — ORM models, tests, filesystem helpers all collapse together.
- **Imports / aliases:** `from utils import format as fmt` followed by `fmt(x)` — no link made.
- **Dynamic dispatch:** `self.handler()` resolved as a single static name instead of an interface method.

The existing `CALLS_DIRECT / CALLS_DYNAMIC / CALLS_INFERRED` split classifies confidence but still runs the **same naive matcher** underneath. Edge type alone does not improve precision.

## 2. Design — Resolution as a Pipeline

Each call site passes through an ordered cascade. The first tier that succeeds wins and tags its edge with the corresponding confidence.

```
call site AST
    │
    ▼
┌───────────────────────────────────────┐
│ Tier 1: CALLS_DIRECT (scope-resolved) │  ← AST + scope table
│ Tier 2: CALLS_SCOPED (file/class)     │  ← same file or MRO
│ Tier 3: CALLS_IMPORTED (alias-aware)  │  ← follows imports table
│ Tier 4: CALLS_DYNAMIC (dispatch)      │  ← self./interface/duck
│ Tier 5: CALLS_GUESS (name-match)      │  ← fallback, current behavior
└───────────────────────────────────────┘
    │
    ▼
(Symbol)-[:CALLS_* {confidence, tier, resolver}]->(Symbol)
```

Every edge carries:
- `confidence` — numeric prior used by BFS scoring (1.0, 0.9, 0.85, 0.7, 0.4).
- `tier` — which resolver produced this edge (`"direct" | "scoped" | "imported" | "dynamic" | "guess"`).
- `resolver` — version tag, e.g. `"py-scope-v1"` — lets us diff precision across resolver versions.

### 2.1 Scope Table

Built per file during parsing. For each AST position, records the visible names:

```python
@dataclass
class ScopeFrame:
    kind: str              # "module" | "class" | "function"
    bindings: dict[str, str]  # local_name → target UID (or "<unresolved>")
    parent: "ScopeFrame | None"

def resolve_name(frame: ScopeFrame, name: str) -> str | None:
    """Walk up the scope chain until name is found, else None."""
```

Populated by walking the AST in order:
- `def f(...)` — adds `f → uid(f)` to the enclosing frame.
- `class C:` — adds `C → uid(C)`; methods go into the class frame.
- `x = foo` — adds `x → uid(foo)` when `foo` resolves statically.
- `from m import a as b` — adds `b → uid(m.a)` if `m.a` is in the graph, else pending.

### 2.2 Tier 1 — CALLS_DIRECT

Call site: `name(...)` or `module.name(...)` where `name` resolves in the scope table to a concrete UID.

Confidence: **1.0**.

### 2.3 Tier 2 — CALLS_SCOPED

Call site: `name(...)` where `name` is unresolved by the scope table but matches a **unique** symbol in the same file or in the class MRO.

Confidence: **0.9**.

Example: a helper `_validate(x)` defined later in the same file.

### 2.4 Tier 3 — CALLS_IMPORTED

Call site: `alias(...)` or `module.name(...)` where the binding comes from an `import` / `from import` statement whose target exists in the graph.

Confidence: **0.85**.

Handles:
- `from .utils import format as fmt; fmt(x)` → edge to `pkg.utils.format`.
- `import json; json.dumps(x)` → edge to stdlib only if stdlib is indexed (usually skipped; see limitations).

### 2.5 Tier 4 — CALLS_DYNAMIC

Call site: `self.m(...)`, `cls.m(...)`, or call through a parameter typed as an interface / protocol.

Resolution: candidate set = all methods named `m` on the class MRO (for `self.m`) or all implementers of the protocol.

**Emits one edge per candidate**, each tagged `confidence = 0.7 / n` where `n` is the candidate count (split the mass). BFS will see all possibilities but with diluted weight.

### 2.6 Tier 5 — CALLS_GUESS

Fallback: global name match (current behavior). Only used when tiers 1–4 fail **and** a unique name match exists in the graph. If multiple matches exist, **no edge is emitted** — record a resolution warning instead.

Confidence: **0.4**.

### 2.7 Non-Resolution

If no tier produces a match, the call site is recorded in a `pending_calls` table keyed by `(caller_uid, callee_name, call_site_range)`. Revisited after each index pass — a future parse may resolve the target. Prevents data loss while avoiding phantom edges.

## 3. Pipeline Shape

```python
# sidecar/parser/call_resolver.py (new file)

class CallResolver:
    def __init__(self, scope_table: ScopeTable, graph: Neo4jClient):
        self.scope = scope_table
        self.graph = graph

    def resolve(self, call_site: CallSite) -> ResolvedCall | None:
        for tier in (self._tier_direct, self._tier_scoped,
                     self._tier_imported, self._tier_dynamic, self._tier_guess):
            result = tier(call_site)
            if result is not None:
                return result
        return None  # recorded in pending_calls
```

Tiers are pure functions over the scope table + graph snapshot. Testable in isolation.

## 4. Schema Changes

All `CALLS_*` edges gain properties:

```cypher
(c:Symbol)-[r:CALLS_DIRECT {
    confidence: 1.0,
    tier: "direct",
    resolver: "py-scope-v1",
    call_site_line: 42
}]->(callee:Symbol)
```

Existing `CALLS` edges migrated:
- `CALLS_DIRECT` if the current edge passes the new resolver → keep.
- Otherwise downgrade to `CALLS_GUESS` (honest labeling beats flattering).

Migration CLI: `python -m sidecar.indexer.migrate_calls` — walks every file, reparses, rewrites edges.

## 5. Examples

```python
# File: pkg/payments.py
from .validation import amount_ok as ok     # scope: ok → pkg.validation.amount_ok

def process(amount):
    if not ok(amount):                       # Tier 3 — CALLS_IMPORTED → pkg.validation.amount_ok
        raise PaymentError
    self.audit.log(amount)                   # Tier 4 — CALLS_DYNAMIC → all log() in Audit MRO
    _finalize(amount)                        # Tier 2 — CALLS_SCOPED → pkg.payments._finalize

def _finalize(amount):
    save(amount)                             # Tier 5 — CALLS_GUESS if save is unique; else pending
```

## 6. Limitations (current)

- Python `getattr(obj, "method")()` — Tier 4 can detect the shape but not the method name. Recorded in `pending_calls` with a `resolver = "getattr"` hint.
- Stdlib imports (`json`, `os`, `re`) are not indexed — edges to them are omitted, not promoted to `CALLS_GUESS`.
- TypeScript declaration merging and generic-bound method calls require the TS resolver; initial implementation Python-only.
- Resolution is static; does not consider runtime monkey-patching. Acceptable — that's a human-review signal anyway.

## 7. Planned Extensions

- Execution probability from trace data (the deferred `ExecutionEdge` from Phase 5): multiply confidence by observed call frequency to weight hot paths.
- Cross-file symbol table caching — avoid re-resolving unchanged imports on every file re-parse.
- Language-specific resolver registry mirrors the `LanguageAdapter` pattern; each language plugs in its own scope semantics.

## 8. Related

- [spec_uid_stability.md](spec_uid_stability.md) — resolver targets UIDs; stable UIDs are a hard prerequisite.
- [spec_typed_semantic_edges.md](spec_typed_semantic_edges.md) — edge-type taxonomy this pipeline populates.
- [spec_token_budget_bfs.md](spec_token_budget_bfs.md) — `confidence` flows into the BFS scoring function as `relation_prior`.
- [spec_language_adapter.md](spec_language_adapter.md) — resolver registry lives alongside language adapters.
