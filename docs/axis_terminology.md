# Axis terminology

Canonical vocabulary for the axis-based compiler stack. Every doc and module
under `context_engine/axis/` uses these terms with these meanings. When a layer talks
about its inputs and outputs it talks in terms below — not in framework names
or benchmark labels.

## Definitions

| term | definition |
|---|---|
| **fact** | A physical AST or graph observation. The raw event: this `Call` node, this `IMPORTS` edge, this attribute write. Lives in the source / index, not in the axis layer. |
| **axis bit** | A normalized fact on one of three axes (CFG, DFG, STRUCT). Emitted by the L1 extractor. Carries no semantics beyond "this physical pattern is present" and an optional structured `payload`. |
| **contract** | A provable combination of axis bits on one symbol (or a tightly-scoped neighbourhood). A contract is a structural proof pattern; it never names a framework or a benchmark role. |
| **role** | A user-facing or benchmark-facing requirement. The thing a question expects to find: `binding_surface`, `api_surface`, `error_surface`. A role is satisfied when ≥1 of its contracts is proven. See [logical_roles_structural_closure.md](logical_roles_structural_closure.md) for logical role vs structural closure. |
| **file tier** | A file's structural place in the repo (`core`, `test`, `example`, `doc`, `stub`, `reexport`), derived at index time from path topology + file shape — not semantics. The ranker demotes or promotes tiers by intent (behaviour vs impact). See [file_tier_signal.md](file_tier_signal.md). |
| **bucket** | An optimisation grouping. Buckets are how the engine batches work (which symbols to score together, which retrieval tier to draw from). Buckets are not roles and not contracts — they exist purely for performance and pruning. |

## Layer responsibilities by term

```text
L0 source / graph    → fact
L1 extractor         → fact → axis bit + payload
L2 container kind    → axis bits → container kind (a class of node fingerprint)
L3 contract compiler → axis bits + container kind → contract
L4 role resolver     → contracts → role (logical satisfaction)
L5 exposure          → role + contract + payload → human-readable answer
optimisation layer   → bucket  (cuts across L3-L5; never authors them)
```

Current implementation status:

| layer | status |
|---|---|
| L1 extractor | adapter-owned: `python_axis_extractor`, `typescript_axis_extractor`, plus shared `symbol_axis_extractor` |
| L2 container kind | implemented in `context_engine.axis.container_kind` |
| L3 contract compiler | implemented in `context_engine.axis.contract_compiler` |
| L4 role resolver | not implemented in the axis stack |
| L5 exposure | QA-only via `QA.axis_contract_report` and `QA.axis_query_smoke` |

Persisted `axis_python_v1` rows store L1-L3 materialization, but those fields are
still diagnostic/search substrate. They are not consumed by the legacy ranker
as roles.

## Rules of use

- **Never write a role at the extractor level.** The extractor emits axis bits;
  any role-shaped name in the language axis extractor is a defect.
- **Never write a framework name in a contract.** Contracts are parameterised
  by container kind; the kind discriminates Flask/FastAPI/Celery without
  naming them.
- **Never reuse `role` and `contract` interchangeably.** A role is what the
  question asks for; a contract is what the engine proves. The L4 layer is
  the only place that bridges them.
- **Buckets do not carry semantics.** A bucket like "rare role" or "cheap
  candidate pool" is a runtime convenience. If a bucket starts carrying
  meaning (e.g. "this bucket = Flask routes"), it has become a role family
  and belongs at L4.

## When ambiguous

If a piece of code lives in axis-layer modules and you cannot describe it
cleanly with one of the five terms above, that's a signal the abstraction is
slipping. Either the term inventory is incomplete (and the doc needs a sixth
term with explicit boundaries), or the code belongs in a different layer.
Both are valid outcomes; silently overloading an existing term is not.
