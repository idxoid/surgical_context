# Role signature findings — feature duplication & discriminator collisions

Session findings on the role vocabulary in [role_catalog.md](role_catalog.md):
where a single structural feature serves several roles, where a discriminator is
weak, and where the catalog text runs ahead of the code. Companion to
[role_clustering_architecture.md](role_clustering_architecture.md), which records
the Pass-1 cascade decision these findings motivate.

Each finding is **what → how (where in code) → why it matters → decision**.
Severity: 🔴 hard collision (ambiguous role assignment) · 🟡 soft (resolvable with
a second signal) · 🟢 already clean / informational.

Feature vocabulary and per-role discriminators referenced below live in
[role_catalog.md](role_catalog.md) (intro table + §1–§10 + summary).

---

## Coverage findings (already folded into the catalog)

### F0a — Cross-cutting / boundary / stateful gaps → §9
- **what:** `interceptor`, `integration_surface`/gateway, `stateful_surface`,
  `pure_transformer` had no role; they were smeared into `orchestrator`,
  `registration_step`, `core_runtime`, `proxy_mechanism`, `validator_handle`.
- **how:** written up as [role_catalog.md](role_catalog.md) §9 with feasibility
  tiers (🟢 edges exist · 🟡 new edge · 🟠 new nodes · 🔴 new analysis).
- **decision:** keep as catalog §9; the only 🟢 (separable today) is
  `interceptor` via `DECORATED_BY-in > 0` + `handle_fan_out == 0`.

### F0b — Shadow elements (graph blind spots) → §10
- **what:** `abstract_contract`/interface, `domain_lexicon`/enum,
  `test_scaffold`/fixture, `orphan`/dead_code.
- **how:** [role_catalog.md](role_catalog.md) §10.
- **decision:** `abstract_contract` is 🟢 (signals already in `_FEATURE_NAMES`);
  `test_scaffold` already handled by path exclusion; `orphan` needs a sink rule;
  `domain_lexicon` is 🔴 (no `USES_VALUE` edge + dropped by connectivity filter).

---

## Hard collisions 🔴

### F1 — `handle_fan_out` is `registration_step`; `request_router` is unmapped
- **what:** earlier drafts claimed `registration_step` and `request_router` share
  the primary discriminator `handle_fan_out > 0`. **They do not — there is no
  collision.**
- **how:** HANDLES is created **decoration-only** — `MERGE (deco)-[h:HANDLES]->(decorated)`
  from the `@deco` AST fact (`sidecar/database/neo4j_client.py`
  `_create_decorator_relations`). So `handle_fan_out` lives on the **decorator**
  (`Flask.route`, `@app.task`), which *is* `registration_step`. A runtime router
  (`dispatch_request`) is not a decorator → emits **no** HANDLES edge → has **no**
  `handle_fan_out`; and it calls the handler via a dynamic dict-lookup
  (`view_functions[endpoint](...)`) → **no resolved call edge** to the handler.
- **why:** `registration_step = handle_fan_out > 0` is a clean 🟢 discriminator (no
  second role contends for it). The real gap is `request_router`: with neither
  `handle_fan_out` nor a resolved `call_fan_out` onto its handlers, it is
  **structurally near-invisible**. Its only honest signal — "high `call_fan_out`
  onto `handle_fan_in` targets" — needs the dynamic dispatch resolved (points-to
  from the lookup table to the handlers).
- **decision:** treat `registration_step = handle_fan_out > 0` as clean (drop the
  depth/`call_fan_in` heuristic — it was disambiguating a non-existent collision).
  `request_router` is the genuine unmapped role → needs dynamic-dispatch
  resolution or a new signal; defer (it is not a registration collision). The
  earlier "setup-vs-runtime" framing was wrong.

### F2 — `leaf + high call_fan_in` blob (six roles)
- **what:** `executor`, `core_runtime`, `validator_handle`,
  `representation_surface`, `stateful_surface`, `abstract_contract` all share the
  leaf + fan-in degree profile. In the retired k-means path these competed for one
  centroid; under the L1/L2 cascade they separate via L2 edge signals (see
  [role_clustering_architecture.md](role_clustering_architecture.md)).

### F5 — `integration_surface` name used for two different roles
- **what:** `integration_surface` is an alias of `composition_surface` (§3,
  module wiring) **and** the canonical name of the gateway role (§9, external-SDK
  boundary).
- **how:** §3 alias list vs §9 heading in [role_catalog.md](role_catalog.md);
  `sidecar/context/role_taxonomy.py` maps `store_integration → integration_surface`.
- **why:** one canonical name, two incompatible structural profiles (internal
  cross-package fan-out vs out-degree to *external* nodes). Taxonomy/ranker cannot
  distinguish them.
- **decision:** rename the §3 sense to `module_composition` (or fold into
  `composition_surface`); reserve `integration_surface`/`gateway` for §9. Naming
  fix, not a feature fix.

---

## Soft collisions 🟡 (need a second signal)

### F3 — `call_fan_out` coordinator overlap (four roles)
- **what:** `orchestrator`, `composition_surface`, `dependency_solver`,
  `schema_generator` all key on high `call_fan_out`.
- **how:** §2/§3/§5/§4 discriminators in [role_catalog.md](role_catalog.md).
- **why:**   secondary signals differ (cross-package *distinct* packages;
  isinstance-dispatch; fan-out *onto representation_surface*), but a flat
  feature vector collapses them on one `call_fan_out` axis.
- **decision:** L1 `Control Flow` parent + L2 cascade on the secondary signal;
  needs target-kind breakdown of `call_fan_out` (onto whom) to be crisp.

### F4 — `type_fan_in` axis without kind-split (doc ahead of code)
- **what:** `config_surface`, `representation_surface`, `validator_handle`,
  `dependency_solver` all consume types; catalog separates them by USES_TYPE
  `kind` (param/annotation/return/isinstance).
- **how:** catalog uses `type_fan_in(kind=…)`, but
  `sidecar/indexer/role_clustering.py` aggregates a single `type_fan_in` /
  `type_fan_out` in `_FEATURE_NAMES` — **kind is not a feature**. The kind weights
  exist (`USES_TYPE_KIND_WEIGHT`) but only scale a scalar, they do not split axes.
- **why:** the doc's four-way separation is not realizable on the current feature
  vector; it is aspirational.
- **decision:** split `type_fan_in` → `type_fan_in_param` /
  `type_fan_in_isinstance` / `type_fan_in_return` in both the intro table and
  `_FEATURE_NAMES`. Until then, mark these discriminators "planned".

### F6 — `registry` term overloaded (three roles)
- **what:** "registry" appears in `registration_step` (handler registry),
  `dependency_solver`/`provider_registry` (DI), and `stateful_surface`/`registry`
  (mutable singleton).
- **how:** alias lists across §3/§5/§9; `role_taxonomy.py` aliases
  (`provider_registry`, `*_registry → factory_surface`, etc.).
- **why:** semantic dup — same word, three structural signatures.
- **decision:** prefix the aliases: `handler_registry`, `provider_registry`,
  `state_registry`. Documentation/taxonomy hygiene.

### F7 — `factory_surface` vs `lazy_loader` share `type_fan_out(return)`
- **what:** both declare a return type via USES_TYPE(return).
- **how:** §3 vs §5 in [role_catalog.md](role_catalog.md); example
  `relationship → RelationshipProperty` appears under both.
- **why:** factory builds an artifact (call edge to construction exists); lazy
  loader returns a descriptor (access is mediated, no call edge). The graph has
  the return-type edge for both but not the descriptor distinction.
- **decision:** needs the (open) `DescriptorSurface` edge to separate; until then
  accept overlap and prefer `factory_surface` as primary, `lazy_loader` as
  supporting.

---

## Multi-role & doc-hygiene findings

### F8 — Multi-role example symbols (by design)
- **what:** several benchmark symbols legitimately satisfy two roles.
- **how / examples:** `solve_dependencies` (orchestrator + dependency_solver);
  `full_dispatch_request` (orchestrator + request_router); `add_api_route`
  (factory + registration_step); `relationship` (factory + lazy_loader);
  `configureStore` (public_entrypoint + composition_surface); celery `Producer`
  (gateway + message-publish); `on_task_request` (executor + message-consume).
- **why:** under the old k-means path the second role was lost via single cluster id;
  under cascade, multi-label is modeled as `primary + supporting[]` via
  `role_cascade.py` predicates.
- **decision:** `primary + supporting[]` in `role_fulfilment.py` — **implemented**.
  See M1 in [role_clustering_architecture.md](role_clustering_architecture.md).

### F9 — Summary table gaps
- **what:** the "Distinctiveness summary" table omits `request_router`,
  `schema_generator`, `composition_surface`; `registration_step` appears without
  the setup-vs-runtime caveat (masking F1).
- **how:** [role_catalog.md](role_catalog.md) summary table.
- **decision:** add rows: `request_router` (**unmapped** — needs dynamic-dispatch
  resolution; not `handle_fan_out`, see F1), `schema_generator` (fan-out ⊆
  `representation_surface`), `composition_surface` (cross-package out + high
  `import_in`, internal); keep `registration_step` as the clean `handle_fan_out`
  owner (no setup-vs-runtime pairing).

### F11 — Retired Pass-1 archetype tier *(historical, D5 done)* 🟡
- **what:** the old k-means `_ARCHETYPE_TEMPLATES` / `_ROLE_TO_ARCHETYPES` layer
  overlapped canonical role names and existed only to stabilise cluster ids.
- **decision:** retired — schema v3 uses L1 buckets + L2 roles + `present_roles`
  only. See [role_clustering_architecture.md](role_clustering_architecture.md) D5.

### F10 — Structural features for cascade predicates *(mostly wired)*
- **what:** `decorated_in`, `handle_fan_out`, kind-split `type_fan_in`, `reexport_in`,
  `construct_fan_out`, etc. are catalog discriminators consumed by `role_cascade.py`.
- **status:** wired in `extract_symbol_rows` / `SymbolRow` for Pass-1 assignment.
  Remaining gaps: honest dataflow holes (F1 `request_router`, factory disjunction)
  — not missing aggregation.

---

## Empirical validation (`QA/prototype_role_cascade.py`)

Inspect Pass-1 on an indexed workspace: L1 distribution, presence-gated
`present_roles`, QA target symbols (`QA_EXPECTED`), multi-label samples. Uses
the same code path as the indexer (`extract_symbol_rows` → `assign_role_taxonomy`).

### Historical note (pre-cascade baseline, removed)
Early prototype runs compared the cascade against a k-means + archetype catalog.
The presence gate cut ~12 phantom roles (catalog entries with no structural support
in the repo). That comparison code was removed; the design decision stands on
structural grounds (C1/C2/D1–D5 below), validated by re-running the QA script
after each engine change.

### Per-symbol accuracy (ongoing QA targets)
| symbol | cascade result | qa_missing | cause |
|---|---|---|---|
| `FastAPI` | **orphan / noise** | `api_surface` | F12 — noise sink fires before any surface predicate |
| `add_api_route` | `orchestrator` | `factory_surface`, `registration_step` | F1 — `handle_fan_out` is on the decorator, not on the method it calls |
| `Param` | `representation_surface` | `config_surface` | F4 — `type_fan_in` not kind-split (param vs general) |
| `solve_dependencies` | `orchestrator` + `factory_surface` | `dependency_solver` | isinstance-dispatch fires on `analyze_param`, not here |
| `run_endpoint_function` | `core_runtime` + `executor` | `runtime_surface` | partial (executor satisfied) |
| `APIRoute` | `config_surface` + `representation_surface` | — | qa_ok |

`registration_step`, `request_router`, `dependency_solver`, `proxy_mechanism`,
`interceptor` — check current run; several depend on F10 edges + F12/F13 fixes
(documented above).

### F12 — L1 noise sink captures public entrypoints 🔴
- **what:** `FastAPI` — the canonical `public_entrypoint`/`api_surface` — lands in
  L1 `noise` → `orphan`.
- **how:** `assign_l1` (`sidecar/indexer/role_cascade.py`) tests
  `zero_in_degree and call_fan_out <= eps` **first**. A framework's public class is
  instantiated by *user* code (`docs_src/`, `tests/`) which Pass-1 excludes, so its
  *internal* in-degree is zero — it hits the noise sink before the `state_types`
  bucket (`:269`, `is_class and (type_fan_in | depend_fan_in | api_fan_in)`) that
  would have caught it.
- **why:** the single most important role (the entry the framework hands out) is
  systematically dropped. Zero *internal* in-degree is the signature of a public
  entry, not of dead code.
- **fixed (prototype):** a pure reorder is *not* enough — `FastAPI` has
  `depth_from_public=6` (F13 makes depth unreliable) and `api_fan_in=0`, so the
  existing `state_types`/`api_surface` predicates still miss it. Two-part fix in
  `sidecar/indexer/role_cascade.py`: (1) `assign_l1` exempts a documented class exposing an API
  surface (`is_class and (api_fan_out > eps or has_documentation)`) from the noise
  sink and routes it to `state_types` (added `api_fan_out` to the bucket gate);
  (2) `api_surface` L2 predicate now also fires on `is_class and api_fan_out > eps
  and has_documentation and api_fan_out > type_fan_in` — the surviving "this class
  *is* the surface" signal, since `depth_from_public`/`api_fan_in` are unreliable
  under F13. Result: `FastAPI` orphan→`api_surface` (qa_ok); `orphan` 42→23;
  phantom-win intact. **Residual:** `api_surface` leaks as a tertiary supporting
  label onto documented type-classes (`EmailStr`, `Settings`) whose `type_fan_in`
  is empty — a `USES_TYPE` coverage gap (F4/F10), not a cascade threshold to tune.

### F13 — Pass-1 test-exclusion strips framework-entrypoint edges (trade-off) ⚠️
- **what:** excluding `NOISE_PATH_PATTERNS` (tests/examples/docs_src) from the Pass-1
  symbol set — which keeps test-fixture pollution out of role assignment — also
  removes the edges by which **user code exercises framework public surfaces**.
- **how:** `_query_pass1_symbols` / `_query_symbols` filter `NOISE_PATH_PATTERNS`
  (`sidecar/indexer/role_clustering.py`); the framework's public API is used in
  `docs_src/`/`tests/`, now invisible to the in-degree/`depth_from_public` signals.
- **why:** genuine tension — test exclusion is correct for *role-shape* hygiene
  (don't assign roles from test fixtures) but wrong for *entrypoint reachability*
  (a public API's in-degree lives in the excluded callers). Same root cause as F12.
- **decision:** keep Pass-1 input test-free but compute `api_fan_in` /
  `depth_from_public` over the **full** graph (incl. tests) so entrypoints retain
  their reachability signal. Fixes the signal at source for `public_entrypoint`
  and `gateway` both; F12's L1 reorder is the cheap interim guard.

### Implemented since: two derived edges (RE_EXPORTS, INSTANTIATES)
Both added to the production indexer (extractor → `link_*` → pipeline phase),
fed into the cascade as features. Engine fixes, not threshold tuning (P4).
- **`RE_EXPORTS`** (`reexport-v1`): `__init__ -[RE_EXPORTS]-> surfaced symbol`,
  feature `reexport_in`. Gives the **orthogonal** public-surface axis. `api_surface`
  now fires on `is_class and reexport_in > 0 and api_fan_out > 0` → `FastAPI`
  recovered to `api_surface` (qa_ok) without a magnitude threshold; replaced the
  fragile `api_fan_out > type_fan_in` guard. fastapi: 51 edges.
- **`INSTANTIATES`** (`instantiate-v1`): `caller -[INSTANTIATES]-> class`, feature
  `construct_fan_out` → `factory_surface`. Grounds factory in real construction
  (present 28→60; `get_dependant→Dependant`). fastapi: 1268 edges.
- **Still open (honest):** `add_api_route → factory_surface` is **not** fixed —
  it constructs via `route_class = route_class_override or self.route_class;
  route_class(...)` (disjunction local), which needs dataflow and is scoped out per
  P5. `dependency_solver` (isinstance-dispatch on `analyze_param`, not
  `solve_dependencies`) and `Param → config_surface` (F4 kind-split) also remain.

---

## Decision summary

| # | Finding | Severity | Decision |
|---|---|---|---|
| F1 | `handle_fan_out` = registration (clean); `request_router` unmapped | 🟢 / 🟠 | registration_step clean; router needs dynamic-dispatch resolution |
| F2 | leaf+fan_in blob (6 roles) | 🔴 | resolve via L1/L2 hierarchy |
| F5 | `integration_surface` name reused | 🔴 | rename §3 → `module_composition` |
| F3 | `call_fan_out` coordinators (4) | 🟡 | L1 Control + L2 cascade on target-kind |
| F4 | `type_fan_in` no kind-split | 🟡 | split feature into param/isinstance/return |
| F6 | `registry` term overloaded | 🟡 | prefix aliases |
| F7 | factory vs lazy_loader return | 🟡 | needs DescriptorSurface; factory primary |
| F8 | multi-role symbols | 🟢 | primary + supporting model |
| F9 | summary table gaps | 🟢 | add 3 rows + setup/runtime caveat |
| F10 | cascade feature wiring | 🟢 mostly done | SymbolRow + role_cascade predicates |
| F11 | Pass-1 archetype tier | 🟡 done | retired (D5); L1/L2 + present_roles |
| F12 | L1 noise sink captures public entrypoints | 🟢 fixed | guard noise sink + `api_surface` on `reexport_in` / `api_fan_out` (`role_cascade.py`); `FastAPI` recovered |
| F13 | Pass-1 test-exclusion strips entrypoint edges | ⚠️ | compute `api_fan_in`/`depth_from_public` over full graph |

**Critical path:** fix remaining public-surface gaps (F12/F13 where still open) →
naming fixes (F5/F6) → honest dataflow holes (F1 `request_router`). Re-validate
with `QA/prototype_role_cascade.py` after each structural edge change.

## Related
- [role_catalog.md](role_catalog.md) — the role vocabulary and per-role signatures.
- [role_clustering_architecture.md](role_clustering_architecture.md) — pipeline decision.
- [spec_unified_ranking.md](spec_unified_ranking.md) — consumer of derived roles.
