# Spec — Call Resolution Pipeline (Phase 8)

> **Status:** Implemented for the current Python path, with TypeScript still using the adapter baseline plus safe database fallback. Formalizes call-edge creation as a staged resolver rather than name-match. Depends on [spec_uid_stability.md](spec_uid_stability.md); refines [spec_typed_semantic_edges.md](spec_typed_semantic_edges.md).

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
┌─────────────────────────────────────────┐
│ Tier 1:   CALLS_DIRECT (scope-resolved) │  ← AST + scope table
│ Tier 2:   CALLS_SCOPED (file/class)     │  ← same file or MRO
│ Tier 3:   CALLS_IMPORTED (alias-aware)  │  ← follows imports table
│ Tier 4:   CALLS_DYNAMIC (self/duck)     │  ← self.m / interface
│ Tier 4.5: CALLS_TYPED (attr type)       │  ← self.attr.m / local alias
│ Tier 5:   CALLS_GUESS (name-match)      │  ← fallback, current behavior
└─────────────────────────────────────────┘
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

### 2.5.1 Tier 4.5 — CALLS_TYPED (instance/local attribute types)

Call site: `self.<attr>.<method>(...)` or `<local>.<method>(...)` where `<local> = self.<attr>` earlier in the same function — i.e. dispatch **through a collaborator object**, not the receiver's own MRO. Tier 4 never reached these (`self.app.send_task` is a *nested* attribute; the old matcher dropped it), so the entire publish/consume chains of attribute-based frameworks (Celery, Django, SQLAlchemy, Flask) were severed and only reconnected by name-match luck.

Resolution: a file-local instance-attribute **type table** `(Class, attr) → qualified_type`, inferred structurally — no framework literals:

- **String-class convention:** `<base>_cls = 'mod:Class'` (paired with a `@cached_property def <base>`) → `<base> : mod.Class`. Celery/kombu's own self-description; the `module:attr` string is a precise static pointer.
- **`__init__` instantiation:** `self.x = ClassName(...)` → `x : <resolved ClassName>` (via the imports table, else same module).
- **Class/instance annotation:** `x: ClassName` → `x : <resolved ClassName>`.
- **Return type:** a function/method with `-> ClassName` or a `return ClassName(...)` body → calls assigned from it (`x = self.factory()`, `x = make()`) give `x : ClassName`. Bare-global / non-constructor returns are not inferred.

Local aliases (`amqp = self.amqp`) inherit the attribute's type within the function. The resolved edge carries `callee_qualified_name = <type>.<method>`, so `link_calls` connects it to the **exact** symbol by qualified name (no global-name-uniqueness gamble).

Confidence: **0.8**, `tier = "typed"`. The edge is emitted with **`rel_type = CALLS_DYNAMIC`** so it participates in every existing graph traversal union; `tier` carries the resolver identity for observability. A dedicated `CALLS_TYPED` edge label can be promoted later once all traversal queries enumerate it.

Implemented in [python_adapter.py](../sidecar/parser/adapters/python_adapter.py) — `_build_attr_type_table`, `_local_alias_types`, `_typed_qualified_target`. When the type is unknown, **no edge is fabricated** (precision over recall).

**Why this tier exists:** it removes the architectural reason the ranker needed hardcoded "contract symbol" tables / mechanism-packs to answer mainstream-framework questions — the collaborator chains are now real graph edges. See [mechanism_contract_unification](mechanism_contract_unification.md).

### 2.6 Tier 5 — CALLS_GUESS

Fallback: global name match (current behavior). Only used when tiers 1–4 fail **and** a unique name match exists in the graph. If multiple matches exist, **no edge is emitted** — record a resolution warning instead.

Confidence: **0.4**.

### 2.7 Non-Resolution

If no tier produces a match, the call site is recorded in a `pending_calls` table keyed by `(caller_uid, callee_name, call_site_range)`. Revisited after each index pass — a future parse may resolve the target. Prevents data loss while avoiding phantom edges.

## 3. Pipeline Shape

```python
# implemented inside `sidecar/parser/adapters/python_adapter.py`

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

## 5.1 ProxySurface — lazy-proxy forwarding (cross-file, index-time phase)

Lazy proxies are a generic Python idiom: a module-level variable forwards attribute
access to a real object resolved at runtime (werkzeug `LocalProxy`, celery `Proxy` —
celery's is literally copied from werkzeug). Flask's `current_app`/`request`/`g` and
celery's `current_app` are all `X = SomeProxy(...)`. A call `current_app.method()` in
another module cannot be resolved by the file-local Tier 4.5 (the proxy is defined
elsewhere and the proxy var carries no type by itself).

ProxySurface resolves this structurally, as a graph phase (not in per-file extraction,
because the proxy call-site is dropped at normal link time — the proxy-var qualified
name matches no symbol):

1. **Detect** (`PythonAdapter.extract_proxy_bindings`): module-level `X: T = SomeProxy(...)`
   where the class name ends in `Proxy` (a naming convention, like `_cls`). Only the
   **annotated** form yields a target type `T`.
2. **Anchor** (`Neo4jClient.link_proxy_bindings`): create a `ProxyBinding` node
   (`Symbol{kind='proxy_binding'}`) and a `PROXY_OF` edge to `T`. The node is a transit
   anchor, not a retrieval target.
3. **Forward** (`Neo4jClient.resolve_proxy_calls`): for a call whose target is
   `<proxy_var>.<method>`, follow `PROXY_OF` to `T`, find `<method>` on `T` (directly via
   `HAS_API` or via `INHERITED_API`), and wire `caller -[CALLS_DYNAMIC {via_proxy}]-> method`.
   The `via_proxy` edge property marks the hop as transparent: the ranker traverses
   through it by default and can surface the proxy when the question is about it.

The two pipeline phases (`_proxy_binding_phase`, `_proxy_call_resolution_phase`) run after
the edge-creating phases and before degree recompute, so forwarded edges are counted.
The incremental path (`index_file`) mirrors imports: delete proxy bindings for the file,
re-link, re-resolve. Un-annotated proxies need method-return-type inference (§6) before
they bridge.

## 6. Limitations (current)

- Python `getattr(obj, "method")()` — Tier 4 can detect the shape but not the method name. Recorded in `pending_calls` with a `resolver = "getattr"` hint.
- Tier 4.5 also infers types from a **method/function return**: `local = self.method()` / `local = func()` resolves when the callee has a `-> Type` annotation or a `return SomeClass(...)` body. Returns of a bare global, `self.attr`, or any non-constructor expression are **not** inferred (their type is not statically present) — e.g. celery's `get_current_app()` returns a thread-local global, so it stays untyped. Inference is file-local; cross-file attribute types resolve only when the qualified target string is self-describing (string-cls) or the class is imported.
- Lazy proxies (`X: T = SomeProxy(...)`) are handled cross-file by the ProxySurface phase (§5.1), but only when the proxy var is **annotated** with its forwarded type. An **un-annotated** proxy (e.g. Celery `current_app = Proxy(get_current_app)`) leaves the proxy's target type unknown — it would require return-type inference on the wrapped callable (`get_current_app() → Celery`), the same planned method-return source. Until then, calls through an un-annotated proxy stay name-only.
- Stdlib imports (`json`, `os`, `re`) are not indexed — edges to them are omitted, not promoted to `CALLS_GUESS`.
- TypeScript declaration merging and generic-bound method calls require a deeper TS resolver; current implementation keeps TypeScript on adapter-level extraction plus unique-name fallback in `Neo4jClient.link_calls()`.
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
