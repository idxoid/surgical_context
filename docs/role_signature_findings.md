# Role signature findings — feature duplication & discriminator collisions

> **Historical snapshot.** This document predates the cascade removal (2026-06-15): the legacy ranking cascade it references is gone — axis is the sole context path (see `cascade_cleanup_inventory.md`). Kept as a dated record; the findings/benchmarks below are as-of their date.


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
  from the `@deco` AST fact (`context_engine/database/neo4j_client.py`
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
- **decision:** `registration_step = handle_fan_out > 0` (clean, no competing role).
  `request_router` uses resolved `handler_call_fan_out` (CALLS* onto HANDLES targets)
  without `handle_fan_out`/`handle_fan_in` — partial signal; dynamic dict-lookup
  dispatch remains an honest gap until points-to resolution.

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
  `context_engine/context/role_taxonomy.py` maps `store_integration → integration_surface`.
- **why:** one canonical name, two incompatible structural profiles (internal
  cross-package fan-out vs out-degree to *external* nodes). Taxonomy/ranker cannot
  distinguish them.
- **decision:** rename the §3 sense to `module_composition` (maps to
  `composition_surface` in `role_taxonomy.py`); reserve `integration_surface` /
  `gateway` for §9. **Done** — `store_integration → composition_surface`;
  `module_composition` alias added.

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
  `context_engine/indexer/role_clustering.py` aggregates a single `type_fan_in` /
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
  `state_registry`. **Done** in `role_taxonomy.py` — handler-registration
  `*_registry` → `registration_step`; DI `provider_registry` → `orchestrator`;
  module `module_registry` → `composition_surface`; state `state_registry` /
  `metadata_registry` / `table_registry` → `runtime_surface`.

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
- **how:** `assign_l1` (`context_engine/indexer/role_cascade.py`) tests
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
  `context_engine/indexer/role_cascade.py`: (1) `assign_l1` exempts a documented class exposing an API
  surface (`is_class and (api_fan_out > eps or has_documentation)`) from the noise
  sink and routes it to `state_types` (added `api_fan_out` to the bucket gate);
  (2) `api_surface` L2 predicate now also fires on `is_class and api_fan_out > eps
  and has_documentation and api_fan_out > type_fan_in` — the surviving "this class
  *is* the surface" signal, since `depth_from_public`/`api_fan_in` are unreliable
  under F13. Result: `FastAPI` orphan→`api_surface` (qa_ok); `orphan` 42→23;
  phantom-win intact. **Residual:** `api_surface` leaks as a tertiary supporting
  label onto documented type-classes (`EmailStr`, `Settings`) whose `type_fan_in`
  is empty — a `USES_TYPE` coverage gap (F4/F10), not a cascade threshold to tune.

### F13 — Pass-1 test-exclusion strips framework-entrypoint edges ✅ (depth + api_fan_in)
- **what:** excluding `NOISE_PATH_PATTERNS` (tests/examples/docs_src) from the Pass-1
  symbol set — which keeps test-fixture pollution out of role assignment — also
  removes the edges by which **user code exercises framework public surfaces**.
- **how:** `_query_pass1_symbols` / `_query_symbols` filter `NOISE_PATH_PATTERNS`
  (`context_engine/indexer/role_clustering.py`); the framework's public API is used in
  `docs_src/`/`tests/`, now invisible to the in-degree/`depth_from_public` signals.
- **why:** genuine tension — test exclusion is correct for *role-shape* hygiene
  (don't assign roles from test fixtures) but wrong for *entrypoint reachability*
  (a public API's in-degree lives in the excluded callers). Same root cause as F12.
- **decision:** keep Pass-1 input test-free but compute `api_fan_in` and
  `depth_from_public` over the **full** call graph (incl. tests) so entrypoints
  retain reachability. Implemented in ``assemble_symbol_rows`` /
  ``_depth_from_public_full_graph`` (`context_engine/indexer/role_clustering.py`).

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
- **P5 done:** `add_api_route → factory_surface` now resolves — intra-procedural
  class-object copy propagation in `extract_instantiations` follows
  `route_class = route_class_override or self.route_class; route_class(...)` through
  the `type[APIRoute]`-typed parameter operand to emit `INSTANTIATES → APIRoute`.
  Flow-insensitive union, bounded fixpoint; copy / `or`-`and` / ternary only.
  Honest residual: an operand sourced only from `self.<attr>` stays unresolved.
  `dependency_solver` (isinstance-dispatch on `analyze_param`, not
  `solve_dependencies`) and `Param → config_surface` (F4 kind-split) also remain.

### F14 — arbiter (`ROLE_ALIASES`) desynced from the cascade vocabulary 🔴
- **what:** `normalize_roles` (`context_engine/context/role_taxonomy.py`) is applied to **both**
  the YAML `required_roles` *and* the engine's `indexed_roles` and the ranker's role
  plan. A miss is manufactured whenever a concept and the engine's emitted role for the
  same symbol normalize to **different** canonicals. It also feeds the ranker's plan, so
  the desync depresses `role_recall` **and** skews retrieval (the ranker hunts roles the
  engine never emits → omits the symbol).
- **how (three classes):**
  1. **Arbiter defect (fixable in the map):** the cascade now emits
     `registration_step`, `dependency_solver`, `request_router`, `proxy_mechanism` as
     distinct catalog-correct discriminators, but `ROLE_ALIASES` collapses them
     (`→factory_surface` / `→orchestrator` / `→representation_surface` / `→binding_surface`).
     The map is also **non-idempotent**: `registration_step` is both a source (`→factory_surface`)
     and a target (`*_registry → registration_step`), so the engine's `registration_step`
     output canonicalizes to `factory_surface` while the expectation stays
     `registration_step` → mismatch. The collapse is also **falsely lenient** (any
     `orchestrator` matches a `dependency_solver` expectation).
  2. **Engine gaps (not arbiter):** `runtime_surface` (35 slots), `error_surface`,
     `serializer_handle`, `integration_surface`, `binding_surface` are catalog-correct
     expectations with **no discriminator**. Notably `handler_or_lifecycle → runtime_surface`
     is correct — the catalog separates the request handler (`runtime_surface`) from hot
     internal machinery (`core_runtime`, e.g. `lenient_issubclass`); the engine conflates
     them into `core_runtime`. A real engine gap (needs a runtime-reachability discriminator),
     **not** an alias bug.
  3. **Eval concepts, not roles:** `docs_or_concept`, `negative_lookup`,
     `nearest_real_mechanism` — never structurally producible; should not score as
     role-recall misses.
- **why:** ~39% of expected-role slots were structurally unreachable as a discriminator;
  the prior `role_recall=0.41` is measured against a partly-invalid reference. We were at
  risk of bending the engine to satisfy a buggy answer-key (inverse of P1/P2).
- **fix (this session):** Fix-1 — identity-map the four now-distinct cascade roles in
  `ROLE_ALIASES` (idempotent + stricter; **may lower** the number by removing false
  matches — that is the honest baseline). Class 2 stays as documented engine gaps (P5);
  class 3 to be excluded from role accounting. Re-measure before any further engine work.

### F15 — the question pack (gold) is materially stale/misaligned 🔴
- **what:** auditing `tests/fixtures/real_repo_question_pack.yaml` against the indexed
  repos, **42/65 questions** have an `expected_symbol` or `expected_file` that does not
  exist in the indexed graph. Source-grep confirms three distinct root causes:
  1. **Version-stale gold** — the symbol was removed/renamed in the checked-out
     version: flask `_request_ctx_stack` (0 source files; removed Flask ≥2.3), vue
     `Watcher` (0 files; Vue 2 concept, Vue 3 uses `ReactiveEffect`).
  2. **Wrong name/case** — sqlalchemy `SessionMaker` (0) vs `sessionmaker` (3 files).
  3. **Indexing gap, not pack** — rtk `combineReducers` exists in 3 source files but
     was **not extracted** as a Symbol (TS/JS extraction gap).
  Plus: 5 `required_roles` use vocab absent from `ROLE_ALIASES`
  (`migration_loader`, `deferred_registration`, `import_system`,
  `cleanup_handler`); the 7 `surgical_context` self-questions failed workspace
  resolution (self-index issue, separate). Some file-only "misses" are partly the
  audit's stricter matcher vs `_expected_file_matches`.
- **why:** `recall_at_5` / `file_recall` / `role_recall` are partly measured against a
  gold that no longer matches the code. Optimizing the engine to it is the P1/P7 trap
  (chasing stale answers). The sweep would produce a misleading baseline.
- **decision:** **refresh the pack against the indexed repo versions before sweeping**
  — fix version-stale + wrong-case symbols, drop/realias unmapped role vocab, fix the
  self-repo workspace. Track the TS/JS extraction gaps (combineReducers, …) separately
  as **engine** issues, not pack fixes. Hold the full sweep until the gold is validated.
- **done (grounded rewrite, all repos except surgical_context):** fixed the genuinely
  stale/fork-specific symbols — fastapi (`routes`→`get_openapi_path`,
  `response_model`→`_serialize_data`), flask (custom-fork proxies:
  `RequestContext`→`from_environ`, `deferred_functions`→`BlueprintSetupState`,
  `LocalProxy`→`RequestProxy`, `_request_ctx_stack`→`_get_current_object`,
  `url_map`→`create_url_adapter`, `Map`→`dispatch_request`), vue
  (`Watcher`→`doWatch`, Vue3), sqlalchemy (`SessionMaker`→`sessionmaker`), nestjs
  (file paths), celery (`Publisher`→`_create_task_sender`), click
  (`_make_command`→`get_command`).
- **dominant residual = INDEXER GAPS, not stale pack:** after the rewrite, **14/15**
  remaining symbol misses are symbols that **exist in source but were not extracted**
  → an engine (symbol-extraction) debt, not a pack fix. Three classes:
  (a) **TS/JS extraction** — rtk `getDefaultMiddleware`/`combineReducers`, express
  `router`/`next`/`mount`, vue `patch` (const/arrow/middleware exports the TS adapter
  misses); (b) **Python class dunders / instance attributes** — pydantic
  `__pydantic_validator__`/`__pydantic_serializer__`, django `_view_middleware`, celery
  `on_task_request`; (c) **module / external-import names** — pydantic `v1` (submodule),
  celery `Producer` (kombu). The one non-gap residual, dathund `require_lineage_path`,
  is flagged for owner confirmation (its own question text embeds the renamed symbol).
  **So the pack was mostly correct; the real debt these questions expose is
  symbol-extraction coverage.**

### F16 — intent→roles table (`_SECONDARY_INTENT_ROLES`) names unreachable roles 🟡
- **what:** `IntentClassifier._SECONDARY_INTENT_ROLES` (`context_engine/context/intent_classifier.py`)
  maps a query intent to supplemental role-types the ranker should prioritize. It is a
  query-side *strategy* hint (not graph/role authoring — not a P1/P2 violation), but it
  names roles the engine cannot produce: `runtime_surface`, `error_surface` (DEBUGGING),
  `integration_surface` (REFACTORING), `docs_or_concept` (NEW_FEATURE / DESIGN_QUESTION).
- **how:** these flow into `IntentPolicy.supplemental_roles` → the ranker plan. None has
  a discriminator, so the supplemental-role guidance is **inert** for DEBUGGING /
  REFACTORING / NEW_FEATURE / DESIGN — the same vocab desync as F14, second location.
  (IMPACT_ANALYSIS's `impact_*` are fine — path-derived.)
- **decision:** align the table to emittable/structural roles (e.g. DEBUGGING →
  `executor` + `core_runtime`; drop `docs_or_concept`), or add the missing
  discriminators (`error_surface`=RAISES/CATCHES) before naming them. Until then the
  unreachable entries are dead weight.

### F17 — symbol-extraction coverage for attributes fails alone (needs a connecting edge) 🔴
- **what:** the dominant "indexer-gap" residual (F15) was tested by extracting Python
  class-body attributes as symbols (`module.Class.attr`, all annotated+plain). It
  **failed empirical validation** and was reverted.
- **how (measured):** fastapi reindex showed the cascade stayed stable (attrs are
  structurally disconnected → the `structurally_connected` filter drops them from
  Pass-1; `orphan` only 23→27 despite attrs = 20% of symbols; targets unchanged, Param
  even improved). But pydantic showed the payoff is **negative**: `__pydantic_validator__`
  / `__pydantic_serializer__` became symbols yet were **not retrieved**
  (`retrieved=NO`), and recall@5 **dropped** 0.458→0.417 (+20% symbols diluted top-k).
- **why:** an attribute symbol has **no incoming edges** — its annotation's `USES_TYPE`
  is attributed to the enclosing class, and attribute *access* (`self.attr`) is not a
  call edge. The same disconnection that protects the cascade makes the attribute
  unreachable by the ranker. **Symbol extraction without connectivity is dead weight.**
- **decision:** revert (done). Attribute-level answer symbols are retrievable only with
  a **connecting edge** — `READS_FIELD`/`WRITES_FIELD` (§10/§11 Family C, 🟡): a method
  that reads/writes an attribute, plus attributing the annotation `USES_TYPE` to the
  attribute. That is a separate, larger investment. The cheaper alternative is to accept
  that such gold (pydantic `__pydantic_validator__`, django `_view_middleware`) **over-
  specifies attributes** and retarget those questions to the enclosing class/method that
  is actually retrievable. The TS residuals (`getDefaultMiddleware` nested-local,
  `combineReducers` redux re-export) are likewise not clean extraction holes.

### F18 — facade reachability + dynamic delegation-following role plan 🟢
- **what (q01 root cause, fully traced):** "FastAPI registers a route" needs
  `registration_step`, which lives on `APIRouter.api_route/get/post` — reached only
  through the FastAPI→APIRouter **facade delegation** (`self.router.get(...)`). Two
  defects compounded:
  1. **Reachability:** `_build_attr_type_table` did not resolve instance attributes
     from `__init__` with a qualified annotation (`self.router: routing.APIRouter`) or
     `self.x = mod.Class(...)`. So `self.router.<m>(...)` produced no edge — the entire
     public facade was structurally detached from `APIRouter`. Fixed (annotation via
     `_type_ref_targets` keeps the module; plus attribute-callee instantiation). Edges
     now land: `FastAPI.get -CALLS_DYNAMIC-> APIRouter.get [registration_step]`.
  2. **Plan depth:** `target_role_supply_counts` sampled a fixed 1 hop, so it saw the
     thin delegator (`FastAPI.get = orchestrator`) but not the role behind it. Now it
     follows CALLS-out delegation with **dynamic depth bounded by role-closure** (keep
     expanding only while new role *types* appear; hard cap 3). registration_step now
     enters the plan → the ranker surfaces its carrier.
- **measured (fastapi):** pass_rate 0.25→**0.50**, role_recall 0.477→**0.581**; q01
  0.50→0.75 (registration_step closed; only `runtime_surface` left — no discriminator),
  q03 0.0→0.5, q07 0.67→1.0. q05 0.25→0.0 is **noise** (a coincidental
  representation_surface match dropped; its expected roles are unreachable by either
  plan — a real engine gap, not a delegation regression).
- **why:** facade/delegation is a general pattern (express/django/sqlalchemy delegate to
  a composed object the same way); fixing it at the type-table + plan-depth level is
  structural (P4), not a fastapi special-case.
- **deferred:** weight the role supply by a symbol's role-strength (not just
  presence/frequency). Skipped for now — it requires the selection margin to return
  candidates to the pool. Tracked as a refinement.

### F19 — DI resolver is reachable but low-ranked; the marker→resolver link is dynamic 🟢 fixed (9c20e38)
- **q02 (`Depends`, trace_dependency)**: role_recall=1.0 but recall@5=0.25, file_recall=0.0
  — the answer chain (`get_dependant`, `solve_dependencies`, `Dependant` in
  `dependencies/utils.py`/`models.py`) is missed.
- **traced (empirically, several false roots ruled out):**
  - *Not stop-policy alone:* a chain-depth gate on `role_complete` did nothing —
    `total pruned=0`, the solver was never even a candidate. Reverted.
  - *Not isolation:* `get_dependant` in_deg=4/out_deg=7, `solve_dependencies`
    in_deg=2/out_deg=11 — they are called (`routing.py:784 self.dependant =
    get_dependant(...)`), edges extracted.
  - *Not an INJECTS extraction bug:* INJECTS works (147 edges in fastapi, e.g.
    `api_route INJECTS generate_unique_id`), but `get_dependant`/`solve_dependencies`
    have **no INJECTS either direction**, and `extract_injections(utils.py)=0`.
    **Why:** INJECTS captures the *declaration* of DI (`def f(x = Depends(provider))`
    — provider in a parameter default), but the resolver does not declare deps in its
    signature; it **iterates `Dependant.dependencies` at runtime**. The marker→resolver
    link is *dynamic execution*, not a declarative AST fact — out of INJECTS' reach
    (P5: needs dataflow, not an edge).
  - *Real reachability:* from target `Depends` (a marker class with almost no
    outgoing CALLS) the resolver is 2-3 hops away, via `Security -DEPENDS_ON→ Depends`
    + `Security -USES_TYPE→ get_dependant`, or via `APIRoute` (routing.py) calling
    `get_dependant`. EXPLORATION is already a chain-pursuit intent, but chain-pursuit
    deepens along the *target's outgoing CALLS*; a marker target has none, so the BFS
    ranks the resolver below the marker's 1-hop neighbours and `role_complete` stops
    before it surfaces.
- **fix (implemented, commit `9c20e38`):** a *marker→consumer* chain-pursuit added to
  `_graph_candidates_impl`, mirroring the existing HAS_API→CALLS registration chain
  but inverted: from a marker target the BFS follows the inverse `USES_TYPE` edge
  ("who consumes this marker as a type") and then the consumer's outgoing CALLS /
  HANDLES. The marker target is identified **structurally** (`_is_marker_surface_uid`:
  roles ⊇ {api_surface, config_surface, representation_surface} ∧ `call_fan_out ≤ 1.5`)
  — no name match (P3). Stop-policy guard (`_marker_chain_pending_from`) relaxes
  `role_complete` / `marginal_gain_threshold` only when `dependency_solver ∈ required_roles`
  and the pool carries a relevant marker_chain candidate (`marker_chain_roles_are_relevant`:
  ≥2 required-role overlap or `dependency_solver` match) — narrow gate keeps the
  relaxation off the dominant EXPLORATION path. Tests `test_budget_pruner_waits_for_
  marker_chain_on_dependency_solver_surfaces` and `test_graph_candidates_follow_marker
  _consumer_chain_from_config_surface` lock the contract.
- **measured (fastapi reindex):** pass_rate **0.875→1.000** (q06 warn→pass), q02
  `Depends` file_recall **0.0→0.5** (`get_parameterless_sub_dependant` surfaced),
  zero regressions on q01/q03/q04/q07 (narrow gate). Symbol-recall@5 inside top-5
  did not shift — that is HAS_API method ranking (F23), a separate retrieval-track
  concern, not marker-chain.

### F20 — `error_surface` has no structural signal: exception inheritance is to builtins 🟠
- **pydantic_q07 (`ValidationError`, trace_dependency)**: recall@5=1.0, file_recall=1.0,
  target retrieved — a **pure role miss** (`core_runtime`, `error_surface`), no
  retrieval problem.
- **traced:**
  - *Symbol ambiguity:* two `ValidationError` — v2 (`pydantic-core/..._pydantic_core.pyi`)
    and v1 (`v1/error_wrappers.py`, `class ValidationError(Representation, ValueError)`).
    Both index as `representation_surface` (class with type_fan_in); neither carries an
    error role.
  - *Why no `error_surface`:* the catalog's intended discriminator was
    "inherits-from-exception", but a Python exception inherits a **builtin**
    (`ValueError`/`Exception`), and builtins are not in-graph symbols — so the
    inheritance `DEPENDS_ON` edge to the exception base is never materialized
    (v1 ValidationError only has `DEPENDS_ON→ Representation`, the builtin base is
    invisible). Measured: **28 of 124** `*Error`/`*Exception` classes have **zero
    in-graph bases** — their is-exception signal is structurally empty.
  - *`core_runtime`* is likely **over-specified gold**: ValidationError is an error
    *contract* (representation_surface is structurally right), not hot internal
    runtime machinery.
- **fixed (implemented):** chose the cheaper option — `inherits_builtin_exception`
  marker. `link_inheritance` (`neo4j_client.py`) sets `s.inherits_builtin_exception=true`
  when a class's base is in `_BUILTIN_EXCEPTION_BASES` (the standard exception
  hierarchy, not a fixture — the base is a real AST token, just a builtin so no
  DEPENDS_ON edge forms). Threaded through `_query_symbols` → `SymbolRow` →
  `FanProfile`; added an `error_surface` L2 predicate (state_types, spec 88) plus an
  L1 guard so an exception with near-zero type_fan_in still routes to state_types.
- **measured (pydantic reindex):** 25 classes marked; both `ValidationError`
  instances + `SchemaError`/`PydanticCustomError`/… now `primary=error_surface`.
  pydantic pass_rate 0.375→**0.500**, role_recall 0.531→**0.615**, q07 0.33→**1.00**
  (miss=[]), zero regressions. One structural marker gave `error_surface` to the
  whole exception family at once (P4).
- **transitive fix (click):** a subclass of an *in-graph* exception
  (`UsageError → ClickException → Exception`) inherited the error-ness only one level
  deep — `ClickException` was marked but `UsageError`/`FileError` were not. Added a
  transitive propagation in `link_inheritance`: mark any class reaching a marked base
  along a `DEPENDS_ON*` inheritance chain. **Bug found while wiring it:** Symbol nodes
  carry no `workspace_id` property (workspace is scoped via `File-[:CONTAINS]->Symbol`),
  so the first `MATCH (base:Symbol {workspace_id:$w})` matched nothing — fixed to match
  the base/sub through their File. click: marked 3→12, `UsageError` → `error_surface`,
  q05 `error_surface` miss closed (residual `representation_surface` is a supporting-slot
  contention, MAX_SUPPORTING=3, not an error_surface failure).

### F21 — serializer_handle vs validator_handle is not structurally discriminable 🔴
- **pydantic q01 (validator) / q03 (serializer)**: miss `validator_handle` /
  `serializer_handle`.
- **measured:** `model_dump` (serializer), `model_validate` (validator),
  `model_dump_json`, `to_python` all share an **identical structural profile** —
  `call_leaf=True`, high `call_fan_in` (model_validate=121, to_python=595), and
  `type_fan_in = type_fan_in_return = type_fan_out_return = 0`. They are
  topologically indistinguishable hot leaves → all read as `core_runtime`. There is
  **no edge/feature** that separates "serializes an object to dict/json" from
  "validates raw input into a typed object": that distinction is the *semantics of
  what the method does with data* (the shape it returns vs accepts), not a call/type
  topology fact.
- **why no cheap fix (unlike error_surface):** error_surface had a real structural
  anchor (builtin-exception inheritance). Serializer/validator have none — `model_dump`
  and `model_validate` are byte-for-byte the same in the graph. The only hooks are
  `__pydantic_serializer__`/`__pydantic_validator__` class-attrs (the F17 attr-gap,
  reverted — not retrievable) or a `RETURNS_SERIALIZED`/dataflow edge (analysis, not an
  edge — 🔴, P5). Inferring from the method name would be a P3 violation.
- **decision:** honest gap — do **not** fake it. Treat `serializer_handle` /
  `validator_handle` as structurally unreachable and exclude them from role-recall
  scoring (same spirit as F15 / pre-fix F20), so they are not counted as engine misses
  the topology cannot produce. Phase A return-shape markers are too coarse for this
  pair because the pydantic handles are hot leaves with no visible typed return shape;
  revisit only when a real data-shape/dataflow signal can separate accepted input
  shape from emitted serialized shape.

| # | Finding | Severity | Decision |
|---|---|---|---|
| F1 | `handle_fan_out` = registration (clean); `request_router` via `handler_call_fan_out` | 🟢 / 🟡 | registration_step clean; dynamic dispatch still partial |
| F2 | leaf+fan_in blob (6 roles) | 🔴 | resolve via L1/L2 hierarchy |
| F5 | `integration_surface` name reused | 🟢 fixed | §3 → `module_composition`; §9 keeps `integration_surface`/`gateway` |
| F3 | `call_fan_out` coordinators (4) | 🟡 | L1 Control + L2 cascade on target-kind |
| F4 | `type_fan_in` no kind-split | 🟡 | split feature into param/isinstance/return |
| F6 | `registry` term overloaded | 🟢 fixed | prefixed aliases in `role_taxonomy.py` |
| F7 | factory vs lazy_loader return | 🟡 | needs DescriptorSurface; factory primary |
| F8 | multi-role symbols | 🟢 | primary + supporting model |
| F9 | summary table gaps | 🟢 | add 3 rows + setup/runtime caveat |
| F10 | cascade feature wiring | 🟢 mostly done | SymbolRow + role_cascade predicates |
| F11 | Pass-1 archetype tier | 🟡 done | retired (D5); L1/L2 + present_roles |
| F12 | L1 noise sink captures public entrypoints | 🟢 fixed | guard noise sink + `api_surface` on `reexport_in` / `api_fan_out` (`role_cascade.py`); `FastAPI` recovered |
| F13 | Pass-1 test-exclusion strips entrypoint edges | 🟢 fixed | full-graph `api_fan_in` + `depth_from_public` |

**Critical path:** honest dataflow residuals (`request_router` dynamic dispatch,
`self.<attr>`-only construction, field/iteration-local binding). Phase A added
return-shape AST markers as a foundation, but not value-flow. Re-validate with
`QA/prototype_role_cascade.py` after engine changes (fastapi only).

### F22 — schema_builder works for builder *functions*, not for builder *classes* 🟡
- **pydantic q04 (`model_json_schema`)**: misses `schema_builder`.
- **traced:** target `model_json_schema`@main.py is a thin delegator
  (`CALLS_IMPORTED→ model_json_schema`@core, `USES_TYPE→ GenerateJsonSchema`); the real
  schema machinery is `GenerateJsonSchema` — a class with **87 HAS_API methods**, tfi=9,
  leaf, out_degree=0 → lands `state_types`/`representation_surface`. The `schema_builder`
  predicate is in `control_flow` (needs `call_fan_out>0`), so it fires for builder
  *functions* (164 assignments in pydantic) but never for a builder *class*.
- **why no clean class discriminator (measured):** separating a schema-builder class
  from a data model by `api_fan_out / type_fan_in` ratio does not split —
  GenerateJsonSchema=9.17 vs RootModel=5.51 / BaseModel=4.16 / FieldInfo=4.58 (models),
  and ModelMetaclass=95 / GenerateJsonSchemaHandler=185 straddle both. "Builds a schema"
  vs "is a model" is *data semantics*, not api/type topology (same class as F21).
- **decision:** keep `schema_builder` (live role for builder functions); do **not** add
  a class-level predicate (a fit to GenerateJsonSchema with false hits on
  ModelMetaclass/RootModel — P4 violation). Phase A return-shape markers give
  functions a cheap mapping/sequence/constructed-type signal, but the builder-class
  case remains a partial gap: a class like `GenerateJsonSchema` needs data-shape or
  method-level evidence, not a class-level name/topology fit. Left honest, not scored
  away (the role is reachable, unlike F21's fully-unreachable pair).
- **same family on django q03 (`ModelForm`, trace_dependency):** misses
  `factory_surface` on `fields_for_model`. Empirically tested the hypothesis "extend
  `factory_surface` to module-level functions with `type_fan_out_return > 0`": django
  has **0 functions** with `type_fan_out_return > 0` (the codebase barely uses return
  annotations), so the extension would do nothing. Falling back to
  `construct_fan_out > 0` is non-starter (1140 candidates in django alone — would
  flip a thousand `core_runtime` / `runtime_surface` leaves to `factory_surface`,
  same class as F22's class-level false-hits). `fields_for_model` itself is
  structurally a leaf (`call_fan_in=2.1, call_fan_out=1.3, tfo_ret=0, construct=0`):
  it builds a dict by iterating `model._meta.concrete_fields` and calling
  `f.formfield()` where `f` is a parameter with no static type — INSTANTIATES /
  USES_TYPE can't see the construction (the same dynamic-attr family as F18/F24).
  Phase A can now mark "this function returns a mapping" when that shape is visible
  in the AST. That is necessary but not sufficient here: the missing discriminator is
  still "mapping values derive from field iteration / calls on iteration locals".
  q03 therefore needs field-read + iteration-local value-flow, not only a return-shape
  boolean. The role miss stays honest; not faked from a name.

### F23 — class HAS_API method ranking: query picks legacy over the relevant method 🟡
- **pydantic q01 (`BaseModel`, "validation flow in v2")**: misses `core_runtime`; the
  expected `model_validate` is **not retrieved** though it is a **direct
  `BaseModel -HAS_API-> model_validate`** (1 hop). BFS surfaced the legacy v1 surface
  instead (`parse_obj`, `from_orm`, `validate`, `update_forward_refs`) and stopped on
  `role_complete`.
- **two roots:** (1) `core_runtime` on BaseModel is **over-specified gold** — BaseModel
  is `abstract_contract`+[config/representation/api_surface] (in=518, a data contract /
  public surface), not hot internal runtime machinery; (2) the real miss is **method
  ranking among a class's 50+ HAS_API methods** — the query asks for *v2*, the ranker
  ranked v1/legacy methods higher. This is query-semantics vs symbol-set selection, not
  a 1-hop reachability or plan-depth issue.
- **decision:** leave q01. A method-ranking fix (prefer the query-relevant method over
  legacy siblings under the same class) is a broad retrieval change, not a targeted
  structural signal — out of scope for the role pass. Recorded for the retrieval track.

### F24 — integration_surface invisible when the boundary is a dynamic backend attr 🟠
- **celery q04 (`AsyncResult`)**: misses `integration_surface`.
- **traced:** the benchmark target resolves to *import instances* of AsyncResult
  (api_surface/representation), not the class@`result.py` (which is `executor`,
  in=14/out=1) — a duplicate target-selection issue. More fundamentally, the class's
  only materialized external edges are to `dateutil`/`collections` (stdlib plumbing,
  correctly excluded by `_integration_boundary_signal`). Its real integration boundary
  — `self.backend.get_task_meta(...)` reaching Redis/AMQP — is **invisible**: `backend`
  is a runtime-injected attribute (Redis vs AMQP vs DB chosen by config), it has no
  static type, so the external call to the result store is never an edge.
  `external_integration_call_fan_out = 0` → boundary_integration never fires.
- **why honest gap:** the integration classifier is fine (`is_integration_external_root`
  would count redis/kombu); the symbol simply does not *statically call* them. Same
  class as celery message publish/consume and the dynamic-backend proxy — a
  runtime/dataflow boundary, not a missing edge (P5). Telling it from a name would be a
  P3 violation.
- **decision:** left as a documented dynamic-boundary gap. Reachable only with backend
  type resolution (the `self.<attr>` typing family, like F18's facade fix but for an
  injected/config-selected attribute) or a dataflow pass — out of scope here.

## Related
- [role_catalog.md](role_catalog.md) — the role vocabulary and per-role signatures.
- [role_clustering_architecture.md](role_clustering_architecture.md) — pipeline decision.
- spec_unified_ranking.md (removed) — consumer of derived roles.
