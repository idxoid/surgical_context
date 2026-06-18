# Role catalog — structural roles grounded in the benchmark repos

Purpose: a role vocabulary where **every role maps onto real code and has a
characteristic *structural* signature** — no symbol-name / keyword matching.
Each role lists its distinctive feature collection (the "признаки") and 1–2
concrete examples from the benchmark repos (celery, fastapi, flask, django,
sqlalchemy, express, nestjs, pydantic, redux_toolkit, vue, click).

> **Companion analysis:** [role_signature_findings.md](role_signature_findings.md)
> records feature-duplication / discriminator collisions in this vocabulary, and
> [role_clustering_architecture.md](role_clustering_architecture.md) records the
> Pass-1 pipeline (discriminator-first L1/L2; k-means retired).

The signatures are written in the feature vocabulary Pass-1 assembles per symbol
(`SymbolRow` / cascade predicates in `role_cascade.py`), fed by derived edges:

| feature | meaning |
|---|---|
| `call_fan_in / call_fan_out` | incoming / outgoing CALLS* edges |
| `call_leaf_score` | high = called but calls ~nothing (terminal) |
| `call_fan_in_ratio` | fan_in / (fan_in+fan_out) |
| `type_fan_in / type_fan_out` | USES_TYPE in/out (+ `kind`: param/annotation/return/isinstance) |
| `api_fan_in / api_fan_out` | HAS_API / INHERITED_API |
| `inject_fan_in` | INJECTS (DI binding) incoming |
| `depend_fan_in/out` | DEPENDS_ON — **also carries class→class inheritance** (`subclass -[DEPENDS_ON]-> superclass`), so a base class shows high `depend_fan_in` |
| `handle_fan_in` | HANDLES incoming — this symbol is a dispatched handler |
| `handle_fan_out` | HANDLES outgoing — this symbol dispatches to handlers (dispatcher/registry) |
| `handler_call_fan_out` | CALLS* fan-out onto symbols with `handle_fan_in > 0` (resolved dispatch to registered handlers) |
| `decorated` | DECORATED_BY outgoing — this symbol → the decorator applied to it |
| `decorated_in` | DECORATED_BY incoming — how many symbols decorate *with* this (a widely-applied decorator/interceptor) |
| `proxy_of` | node is a `proxy_binding` (PROXY_OF edge) |
| `depth_from_public` | BFS distance from a cross-package public entry |
| `cross_package_call_in/out_ratio` | edges crossing package boundaries |
| `import_in` | how many files import this symbol's file |
| `has_documentation / doc_*_weight` | COVERS doc anchors (definition/reference/example) |
| `is_class / is_function` | symbol kind |

**Distinctiveness principle.** Several roles share a coarse degree profile
(leaf + fan_in); they are separated by a *single discriminating edge signal*.
Those discriminators are called out per role — that is what the old name tables
faked and what the derived edges now provide structurally.

---

## 1. Entry / surface roles

### `public_entrypoint` (api_surface)
The public thing the framework hands to its user.
- **признаки:** very low `depth_from_public` (≈0); high `api_fan_in` (HAS_API);
  documented (`has_documentation`, high `doc_definition_weight`); often
  `is_class` or a module-stem export; high `cross_package_call_in_ratio`
  (called from outside its package), low `call_fan_in_ratio` vs out.
- **discriminator:** `depth_from_public == 0` **and** documented. Nothing deeper
  in the call chain is a public entrypoint.
- **examples:** `FastAPI` (fastapi/applications.py); `Celery` (celery/app/base.py);
  `Flask` (flask/app.py); `configureStore` (redux_toolkit).

### `config_surface` / `marker_or_config`
Declarative marker or configuration object consumed elsewhere by type.
- **признаки:** high `type_fan_in` with **kind ∈ {param, annotation}** (many
  functions take it as a typed parameter); low `call_fan_in/out`; documented;
  often `is_class` or a thin factory function.
- **discriminator:** `type_fan_in(param/annotation) ≫ call_fan_in` for exported
  markers (`Query`, `Depends`, …). **Marker base classes** (`Param`) where
  param-use lives on subclasses: `depend_fan_in > 0` (inheritance) +
  `type_fan_in_isinstance > 0` (runtime type dispatch) with low aggregate
  `type_fan_in` vs `call_fan_in` — not a representation type hub.
- **examples:** `Depends`, `Query`, `Path`, `Header` (fastapi/param_functions.py);
  `Param` (fastapi/params.py — base marker); pydantic `Field`.

---

## 2. Dispatch / runtime roles

### `executor` / `runtime_executor` / `operation_executor` / `handler_or_lifecycle`
The code that actually runs when something is dispatched/invoked.
- **признаки:** **`handle_fan_in > 0`** (target of a HANDLES edge = a registered
  handler the framework dispatches to) OR deep in the runtime call chain
  (`depth_from_public` high) with `call_leaf_score` moderate; usually
  undocumented (`has_documentation` low).
- **discriminator:** `handle_fan_in > 0` — being a *dispatch target* is unique to
  executors and separates them from generic leaf utilities and from
  `core_runtime` (which is leaf+fan_in but **not** a HANDLES target).
- **examples:** `run_endpoint_function` (fastapi/routing.py); `trace_task`,
  `on_task_request` (celery worker); `BaseCommand.invoke` (click).

### `orchestrator`
Coordinates a step by calling out to several collaborators.
- **признаки:** high `call_fan_out`, low `call_leaf_score`, high
  `cross_package_call_out_ratio`; `call_fan_in_ratio` low (drives more than it is
  driven).
- **discriminator:** `call_fan_out` high **and** `call_fan_out > call_fan_in`.
- **examples:** `solve_dependencies` (fastapi — calls get_dependant,
  request_params_to_args, …); `full_dispatch_request` (flask).

### `core_runtime`
Hot internal machinery on the runtime path, heavily reused, terminal-ish.
- **признаки:** very high `call_fan_in`, `call_leaf_score` high, low doc, often
  `cross_package_call_in_ratio` high (used across the codebase).
- **discriminator:** highest `call_fan_in` of the leaf-runtime group, **no**
  `handle_fan_in` (distinguishes from `executor`).
- **examples:** `lenient_issubclass`, `get_typed_signature` (fastapi/_compat);
  sqlalchemy `Compiler.process`.

---

## 3. Construction / registration roles

### `factory_surface` / `builder_pattern` / `route_builder`
Constructs and returns an artifact object.
- **признаки:** `type_fan_out` with **kind=return** (declares/returns a built
  type) OR constructs a class in its body; high `call_fan_out`; result type has
  high `type_fan_in` elsewhere.
- **discriminator:** outgoing **RETURNS/USES_TYPE(return)** to an artifact class.
- **examples:** `add_api_route` → builds `APIRoute` (fastapi); `relationship` →
  returns `RelationshipProperty` (sqlalchemy); redux `createSlice`.

### `registration_step` / `route_registry` / `middleware_registry` / `hook_registry`
The dispatcher side that registers handlers and later invokes them.
- **признаки:** **`handle_fan_out > 0`** (HANDLES → the handlers it dispatches);
  mutates/holds a registry; called from `public_entrypoint`.
- **discriminator:** `handle_fan_out > 0` only (F1 — no setup/runtime split; that
  was a phantom collision with `request_router`). The decorator that registers
  (`@app.route`, `@app.task`) is the registry; its `decorated` consumers are the
  executors.
- **legacy aliases (F6):** `handler_registry`, `route_registry`, `middleware_registry`,
  `hook_registry` → this role (handler-registration sense). Do not conflate with
  `provider_registry` (DI) or `state_registry` (mutable singleton).
- **examples:** `app.route`/`add_url_rule` (flask); `@app.task` (celery);
  `add_api_route` registering into `app.router` (fastapi).

### `composition_surface` / `composition_pattern` / `module_composition`
Wires modules / composes behavior from parts. (Legacy YAML may say
`composition_pattern`; do **not** alias this role as `integration_surface` — that
name is reserved for the §9 gateway role.)
- **признаки:** high `call_fan_out` + high `import_in`/`cross_package_call_out`;
  pulls many providers together; orchestrator-like but at module-composition
  scale.
- **discriminator:** high `cross_package_call_out_ratio` with broad fan-out into
  *distinct packages*.
- **examples:** `configureStore` / `combineReducers` (redux_toolkit);
  nestjs module wiring; `create_app` factories.

---

## 4. Data / representation roles

### `representation_surface` / `route_object` / `model_class` / `schema_module` / `intermediate_model`
A data/artifact class others reference by type.
- **признаки:** `is_class`; high `type_fan_in` (USES_TYPE incoming — annotated /
  isinstance'd / returned widely); `call_leaf_score` high, `call_fan_out` low.
- **discriminator:** `is_class` **and** `type_fan_in ≫ call_fan_out`. It is
  *named as a type*, it does not drive control flow.
- **examples:** `APIRoute`, `Dependant` (fastapi); `RelationshipProperty`
  (sqlalchemy); pydantic `BaseModel` subclasses.

### `schema_generator` / `schema_builder` / `field_generator`
Builds a schema/spec by walking models/routes.
- **признаки:** `call_fan_out` into model/route symbols + `type_fan_in/out` on
  schema types; produces a representation; moderate `depth_from_public`.
- **discriminator:** fan-out concentrated onto `representation_surface` nodes
  (consumes models to emit a spec).
- **examples:** `get_openapi`, `get_fields_from_routes` (fastapi/openapi);
  pydantic `GenerateJsonSchema`.

### `validator_handle` / `serializer_handle`
Validates or serializes instances of a type.
- **признаки:** `type_fan_in` on a schema/model type (it operates on that type) +
  `call_fan_in` from the runtime path; leaf-ish; the handle is *invoked*, not
  registered.
- **discriminator:** `type_fan_in` to a representation type **and**
  `call_fan_in` from runtime (operates-on + invoked), but **no**
  `handle_fan_in` (not a dispatch target itself).
- **examples:** pydantic `SchemaValidator`, `model_dump`/`to_python`;
  sqlalchemy type serializers.

---

## 5. Dynamic-boundary roles (the proxy/DI/lazy family)

### `dependency_solver` / `di_container` / `provider_registry` / `instance_resolver`
Resolves which provider satisfies an injection.
- **признаки:** **USES_TYPE with kind=isinstance** to a marker type (dispatches
  on the marker) and/or `inject_fan_in` adjacency; high `call_fan_out` into the
  resolution chain.
- **discriminator:** **isinstance-dispatch on a marker** (`type_fan_in/out` of
  kind=isinstance) — the resolver branches on the DI marker type. Unique signal.
- **examples:** `solve_dependencies`, `get_dependant`, `analyze_param`
  (fastapi/dependencies); dependency-injector `Provide` resolution.

### `proxy_mechanism` / `context_accessor` / `thread_local`
Lazy global / context proxy that forwards to a real object at runtime.
- **признаки:** node is a **`proxy_binding`** (has a PROXY_OF edge to its target
  type); referenced widely; not itself a call hub.
- **discriminator:** `proxy_of` present. Categorically distinct — it is a transit
  anchor, not a normal symbol.
- **examples:** `current_app`, `request`, `g` (flask/celery globals).

### `lazy_loader` / `lazy_executor` (descriptor)
Attribute access triggers a runtime side-effect (load/query).
- **признаки:** returns / is a **descriptor** (factory returns a descriptor type
  via USES_TYPE(return)); attribute access ≠ call (no direct CALLS edge to the
  effect); `type_fan_out` to the descriptor class.
- **discriminator:** descriptor-return signature (RETURNS to a descriptor class);
  the access is mediated, so there is a *type* edge but not a *call* edge to the
  loaded work. (Needs a DescriptorSurface edge to be fully structural — open.)
- **examples:** `relationship` → `RelationshipProperty` (sqlalchemy); `@property`
  / `class_property` (celery `Task.app`).

---

## 6. Cross-runtime / messaging roles

### `request_router` / `route_matcher` / `view_dispatcher` / `request_lifecycle`
Selects and invokes the handler for an incoming request.
- **признаки (desired):** on the runtime request path; calls the *registered
  handlers* (the symbols with `handle_fan_in`) by lookup.
- **discriminator:** partial on current edges: `handler_call_fan_out > 0` (resolved
  CALLS* onto HANDLES targets) **and** no `handle_fan_out` / `handle_fan_in`.
  Dynamic dict-lookup dispatch (`view_functions[endpoint](…)`) still has **no**
  resolved call edge → honest gap until points-to on the lookup table.
- **examples:** `dispatch_request`/`full_dispatch_request` (flask); express
  `Router.handle`; django `URLResolver.resolve`.

### message publish / consume (celery family — currently un-edged)
Producer enqueues; worker consumes. Connected through a **message/task
identity**, not a call — no structural edge exists yet.
- **признаки (desired):** a publish-site (`apply_async`/`send_task`) and a
  consume-site (`Consumer`/`Strategy`) sharing a task-identity node.
- **discriminator:** would require a `PUBLISHES`/`CONSUMES` edge via a task-id
  node (not yet materialized — see open work).
- **examples:** `Task.apply_async`, `Producer` (publish); `Consumer`,
  `on_task_request`, `TaskPool` (consume) — celery.

---

## 7. Impact roles (change-analysis, path + reachability, not clustering)

### `affected_runtime` (impact_runtime)
Production runtime reachable from the changed symbol.
- **признаки:** non-doc/non-test path; reachable via CALLS/AFFECTS from target;
  production primary role.

### `affected_public_api` (impact_public_api)
Public surface that would break.
- **признаки:** `api_surface` primary + reachable; `depth_from_public` ≈ 0.

### `affected_tests` (impact_test_surface)
Tests exercising the changed symbol.
- **признаки:** **path under `/tests/`** (structural location) + reachable from
  target via callers/AFFECTS. Path-derived, independent of clustering.

---

## 8. Meta / control roles (not code-structural)

- `docs_or_concept` — documentation anchors (COVERS), no code signature.
- `negative_lookup` / `nearest_real_mechanism` — benchmark control roles for
  absent symbols / fallback; not structural.

---

## 9. Cross-cutting, boundary & stateful roles (gap analysis)

Roles the first eight sections missed or smeared into neighbours. Each carries a
**feasibility tier**: 🟢 separable on edges already in the graph · 🟡 needs a new
edge for the full role (a partial shape is detectable now) · 🟠 needs new *nodes*
· 🔴 needs a new *analysis* (not just an edge).

### 🟢 `interceptor` / `wrapper` / `middleware_wrap`
Cross-cutting wrap: takes a callable, runs pre/post logic, calls through. Today
smeared into `orchestrator` (decorator_processor, action_interceptor) and
`registration_step` (middleware_registry).
- **признаки:** is the **target** of `DECORATED_BY` (high decorated-in: many
  symbols decorate with it) and/or a higher-order function that returns a closure
  calling its argument; **no** `handle_fan_out`; **not** a marker (`type_fan_in`
  low).
- **discriminator (separates from registration & marker):**
  `DECORATED_BY-in > 0` **AND** `handle_fan_out == 0` **AND** `type_fan_in` low.
  A *registration* decorator (`@app.route`) emits a HANDLES edge; a *wrapping*
  decorator (`@auth_required`, `@timed`) does not — it returns a wrapper closure.
- **feasibility:** 🟢 first cut works on current edges (decorated-in is the
  inverse of the existing `decorated` feature). Full pre/post-hook semantics want
  a `WRAPS` / `BEFORE_CALL` / `AFTER_CALL` edge, but the *role* separates without it.
- **examples:** flask `@login_required`, `before_request` hooks; express
  middleware `(req, res, next)`; nestjs `@UseGuards` / interceptors; `functools.wraps`
  timing/auth decorators.

### 🟠 `integration_surface` / `gateway` / `adapter` / `repository`
A module at a layer boundary that encapsulates network / FS / external-SDK access.
(`integration_surface` is the canonical §9 gateway role — distinct from §3
`module_composition` / `composition_surface`.)
- **признаки:** high `cross_package_call_out_ratio` directed at **external**
  targets (boto3, stripe, requests, a driver); thin internal fan-in; often the
  only place an external import is used.
- **discriminator:** high call/import-out into **external** (non-project) nodes.
- **feasibility:** 🟠 **external calls don't currently resolve to nodes** — an
  import of `boto3` is unresolved, so there is no Symbol and no edge; the out is
  *invisible*, not merely untagged. Needs external import targets **materialized
  as `external`-tagged nodes** (the import resolver already half-knows them:
  unresolved import = external). Then "high out-degree to external nodes" = gateway.
- **examples:** sqlalchemy `Engine` / `Dialect` (DB I/O boundary); celery broker
  transport / `Producer` (kombu boundary); any `*Client` wrapping an external SDK.

### 🟡 `stateful_surface` / `singleton` / `cache` / `registry`
Long-lived mutable state read by many, written by few. Today smeared into
`core_runtime` (reused leaf), `proxy_mechanism` (lazy forward), and
`factory_surface` (provider_registry).
- **признаки:** module-level variable bound to an **instance** (not a lazy proxy);
  high **ref-in** = `import_in` (its module imported widely) + `call_fan_in` (its
  methods invoked across the codebase); few writers; persists across calls.
- **discriminator:** module-level instance + high ref-in (`import_in` + `call_fan_in`)
  — the singleton/registry *shape*. Distinguishing a mutable **cache** from an
  immutable **config** needs write information.
- **feasibility:** 🟡 **the existing module-level-instance extractor is
  Proxy-specific** — `_build_proxy_binding_table` only fires on the
  `X = SomeProxy(...)` class-name convention (werkzeug/celery). Arbitrary
  singletons (`MetaData()`, a connection pool, a registry object) are **not**
  caught and need a broader "module-level `X = SomeClass(...)`" extractor. The
  mutation/cache vs config distinction additionally needs a `MUTATES` / `STORES`
  edge (absent).
- **examples:** sqlalchemy `Session` identity map, `MetaData` table registry
  (benchmark `session_identity_map`, `table_registry`); django app `registry`;
  an in-memory cache / connection pool singleton.

### 🔴 `pure_transformer`
A deterministic, side-effect-free transformation. Today mapped onto
`validator_handle`. This is **not** an edge gap — it is an *analysis* gap.
- **признаки (semantic):** no I/O, no global mutation, deterministic; calls only
  other pure functions.
- **discriminator:** purity / effect inference — the call/type/handle graph does
  not encode side effects. A degree-only signature ("low out-degree + not a
  dispatch target + not a type hub") is *negative/residual* and weak: it catches
  any quiet leaf.
- **feasibility:** 🔴 needs `READS` / `WRITES` / `EXTERNAL_CALL` edges or a purity
  pass (no IO builtins, no global writes, transitively pure callees). Hardest and
  least reliable of the four — defer.
- **examples:** redux reducers (canonical pure transforms); pydantic field
  coercers; pure string/shape helpers.

---

## 10. Shadow elements (graph blind spots)

Symbols that are real in the AST but easy to mis-assign or lose because their
*structural* footprint is unusual: a contract with no callers, a value-only
lexicon, test scaffolding, or an orphan with edges going only one way. Same
feasibility tiers as §9.

### 🟢 `abstract_contract` / `interface`
A base class / Protocol / ABC that *defines a contract* but holds no data and
runs no logic. Today smeared into `representation_surface` (also a class) or
dropped as a quiet leaf.
- **признаки:** high `depend_fan_in` (subclasses `DEPENDS_ON` it — inheritance is
  modelled as `DEPENDS_ON` here, **not** `USES_TYPE`) and/or high `api_fan_in`
  via `INHERITED_API` (its methods are inherited); `call_fan_in ≈ 0` (methods are
  never called *on it directly*); `call_fan_out ≈ 0` (no implementation);
  `is_class`.
- **discriminator (separates from `representation_surface` / DTO):**
  `depend_fan_in ≫ type_fan_in`. A DTO is consumed *as a type* (`USES_TYPE`
  param/annotation → `type_fan_in`); a contract is consumed *as a parent*
  (`DEPENDS_ON`/`INHERITED_API` → `depend_fan_in`/`api_fan_in`), with no call
  flow of its own.
- **feasibility:** 🟢 both signals are already in `_FEATURE_NAMES`
  (`log_depend_fan_in`, `log_api_fan_in`). Needs only a role; an explicit
  `is_abstract` flag (the `abstractmethod` decorator is already parsed) would
  sharpen it but is not required.
- **examples:** `BaseAdapter` (tree-sitter adapters in this repo); sqlalchemy
  `Dialect` / `ExecutionContext` bases; nestjs `*Interface`; any `Protocol`.

### 🔴 `domain_lexicon` / `enum` / `magic_value`
Enums and module-level constants used as *values* in branching — the domain's
vocabulary, not framework configuration. Today **invisible**: not merely
mis-assigned.
- **признаки:** high `import_in`; referenced inside `if/==/match` branches; no
  complex internal structure.
- **discriminator (separates from `config_surface`):** a value-reference signal.
  `config_surface` is consumed *as a type* (`type_fan_in(param/annotation)`); a
  lexicon is consumed *as a value* — which currently produces **no edge at all**.
- **feasibility:** 🔴 two problems compound. (1) There is no `USES_VALUE` edge, so
  value references are unrepresented. (2) A value-only constant has no edge in any
  family, so `filter_clustering_rows` drops it before Pass-1 assignment. Needs a
  `USES_VALUE` edge *and* either folding `import_in` into the connectivity test or
  a dedicated lexicon pass. Hardest; defer with `pure_transformer`.
- **examples:** an `Enum` of order states matched in a `match` statement; a
  module of HTTP status / error-code constants; redux action-type string consts.

### 🟢 `test_scaffold` / `fixture` / `mock` (already handled by path exclusion)
Fixtures and mocks have high `call_fan_in` bounded to a test subtree; left in,
  they flood `executor`/`core_runtime` assignments.
- **признаки:** path under a test/example/benchmark directory; high `call_fan_in`
  confined to that subtree.
- **discriminator / handling:** **path-based exclusion at Pass-1 input** —
  `_query_symbols` filters every `NOISE_PATH_PATTERNS` hit (`/tests/`,
  `/__testfixtures__/`, `/testfixtures/`, `/examples/`, `/benchmarks/`, …) out of
  the Pass-1 symbol set, so the product taxonomy never sees them. They still get
  `impact_test_surface` at query time via `infer_supporting_roles`, so impact
  analysis is unaffected.
- **feasibility:** 🟢 done for in-path scaffolding. 🟡 residual gap: a mock/fixture
  **outside** those paths (a root `conftest.py`, an in-`src/` `mock_*`) is not
  excluded — there is no content-based mock detection, only path.
- **examples:** `conftest.py` fixtures; `unittest.mock` factories under `/tests/`;
  fixture builders in `/__testfixtures__/`.

### 🟡 `orphan` / `dead_code` (noise sink)
Unreachable / obsolete symbols. The fully-isolated case is dropped; the
"only-outgoing" case is **not**, and it distorts Pass-1 assignments.
- **признаки:** zero `call_fan_in`, zero `type_fan_in`, zero `handle_fan_in`,
  zero `depend_fan_in`/`api_fan_in`/`inject_fan_in`; often only *outgoing* calls.
- **discriminator:** zero in-degree across **all** incoming families. A symbol
  with only outgoing edges still has `effective_call_fan_out > 0`, so
  `structurally_connected` is true and it can be mis-assigned as orchestrator-like
  (high fan-out, no fan-in).
- **feasibility:** 🟡 the all-zero orphan is already removed by
  `filter_clustering_rows`; the only-outgoing case needs an explicit sink rule
  ("zero in-degree across every incoming family → route to the noise bucket
  instead of normal L1/L2 assignment"). All inputs are already computed — no
  new edge required, just a routing branch.
- **examples:** a helper kept after its only caller was deleted; a CLI subcommand
  no longer registered; legacy code reachable from nothing.

---

## Distinctiveness summary (the discriminators)

The leaf+fan_in degree profile is shared by `executor`, `core_runtime`,
`runtime_surface`, `representation_surface`, `validator_handle`. They separate
**only** by a single edge signal. Feasibility: 🟢 = separable on current edges.

| role | discriminating signal | feasibility |
|---|---|---|
| `executor` | `handle_fan_in > 0` (dispatch target) | 🟢 |
| `registration_step` | `handle_fan_out > 0` (dispatcher) | 🟢 |
| `interceptor` / wrap | `DECORATED_BY-in > 0` + no `handle_fan_out` + not marker | 🟢 |
| `representation_surface` | `is_class` + `type_fan_in ≫ call_fan_out` | 🟢 |
| `config_surface` / marker | `type_fan_in(param/annotation) ≫ call_fan_in` | 🟢 |
| `dependency_solver` | `type_*(kind=isinstance)` on a marker | 🟢 |
| `factory_surface` | `type_fan_out(kind=return)` to an artifact | 🟢 |
| `proxy_mechanism` | `proxy_of` (proxy_binding node) | 🟢 |
| `orchestrator` | `call_fan_out > call_fan_in`, cross-package out | 🟢 |
| `public_entrypoint` | `depth_from_public == 0` + documented | 🟢 |
| `stateful_surface` / singleton | module-level instance + high ref-in | 🟡 (`MUTATES` for cache) |
| `lazy_loader` | descriptor-return | 🟡 (DescriptorSurface edge) |
| `integration_surface` / gateway | high out-degree to **external** nodes | 🟠 (materialize external nodes) |
| `pure_transformer` | purity / effect inference | 🔴 (READS/WRITES analysis) |
| message publish/consume | shared task-identity | 🟠 (task-identity node) |
| `abstract_contract` / interface | `depend_fan_in ≫ type_fan_in`, `call_*≈0` | 🟢 |
| `test_scaffold` / fixture | path under `NOISE_PATH_PATTERNS` (excluded at input) | 🟢 (🟡 out-of-path) |
| `orphan` / dead_code | zero in-degree across **all** incoming families | 🟡 (sink-routing rule) |
| `domain_lexicon` / enum | value-reference, not type-reference | 🔴 (`USES_VALUE` edge + connectivity) |

**Implementation order by cost:** the 🟢 group is realizable on edges already in
the graph (CALLS / USES_TYPE+kind / HANDLES / DECORATED_BY / INJECTS / PROXY_OF /
HAS_API). Then 🟡 (`MUTATES`, DescriptorSurface), 🟠 (external nodes, task-identity
node), and finally 🔴 (`pure_transformer`, which needs effect analysis, not an edge).

---

## 11. Missing structural edges (what the graph still can't see)

Edges the graph does **not** create today, grouped by cost. Existing edges:
`CALLS*`, `IMPORTS`, `CONTAINS`, `DEPENDS_ON` (+inheritance), `HAS_API`,
`INHERITED_API`, `USES_TYPE` (kind: param / annotation / return / isinstance),
`INJECTS`, `HANDLES`, `DECORATED_BY`, `PROXY_OF`, `RE_EXPORTS`, `INSTANTIATES`,
`AFFECTS`, `IMPLEMENTS`, `OVERRIDES`, `REFERENCES`, `SEMANTIC_HINT`, `COVERS`;
external-pkg topology (`ExternalPkg` node + `CALLS_EXTERNAL` / `IMPORTS_EXTERNAL`,
fed by `package.json` / installed-package roots — `external_boundary.py`);
TS/JS parity (TS `USES_TYPE`, JS property-API + CommonJS alias references via
`link_symbol_api_edges` / `link_symbol_references`); the
`inherits_builtin_exception` Symbol marker for the standard exception hierarchy
(transitively propagated along in-graph `DEPENDS_ON`); and Python return-shape
Symbol markers (`returns_mapping`, `returns_sequence`, `returns_constructed_type`,
`returns_function_expression`) from top-level return statements. All verified in
`context_engine/database/neo4j_client.py` / `context_engine/indexer/`. The §9/§10 gaps
(`WRAPS`/`BEFORE_CALL`/`AFTER_CALL`, `MUTATES`/`STORES`,
`READS`/`WRITES`/`EXTERNAL_CALL`, `PUBLISHES`/`CONSUMES`, `DescriptorSurface`,
`USES_VALUE`, `HANDLES.phase`) are not repeated here.

Tiers as elsewhere: 🟢 AST-extractable now · 🟡 AST but noisy / partial · 🟠 needs
an intermediate identity node · 🔴 needs analysis (dataflow), not an edge.

### Family A — cheap AST-extractable secondary edges

#### 🟢 `RAISES` / `CATCHES`
`function -[RAISES]-> exception type` (raise site) and `function -[CATCHES]->
exception type` (except clause).
- **unblocks:** the `error_surface` role — present in the role catalog
  (`role_cascade.py`; `error_model`/`error_handling → error_surface`) but with **no structural edge
  today**, so it is inferred from nothing; plus the *debugging* intent (where does
  an error propagate) and impact analysis.
- **why invisible now:** `raise`/`except` are control-flow inside a body, neither
  a call nor a type annotation.
- **examples:** fastapi `HTTPException` raise sites; django `Http404`; any
  `try/except` boundary that translates external errors to internal ones.

#### 🟢 `PASSES_CALLABLE` (higher-order)
`caller` passes function `F` as an *argument* to `G` (does not call it).
- **unblocks:** `interceptor`/middleware **more structurally than the proposed
  `WRAPS` edge** (§9) — express `next()`, callbacks, HOCs, `.then(handler)`, an
  event handler passed as an argument. Function-as-value is the real root signal.
- **why invisible now:** in a CALLS graph the passed function is not "called" by
  the passer, so the edge is lost entirely — the largest invisible control-flow
  class.
- **examples:** express `app.use(mw)`; `Array.map(fn)`; redux `applyMiddleware`;
  any `functools.partial(cb)` handoff.

#### ✅ `INSTANTIATES` / `CONSTRUCTS` (implemented)
`caller -[INSTANTIATES]-> class` via its constructor (distinct from a call).
- **status:** implemented — `python_adapter.extract_instantiations` →
  `neo4j_client.link_instantiations` (`_create_instantiation_relations`,
  resolver `instantiate-v1`, `kind=class` filter), pipeline `_instantiation_phase`.
  Cascade feature `construct_fan_out` feeds `factory_surface`. On fastapi: 1268
  edges; `factory_surface` present 28→60, grounded in real construction
  (`get_dependant→Dependant`).
- **scope:** literal `X(...)` (local class def / import); `v(...)` where `v` is a
  local *directly* annotated `type[X]`/`Type[X]`; and (P5) `v(...)` where `v`
  receives a class object via intra-procedural copy propagation — a plain
  `v = <expr>` that copies / disjoins (`a or b`) / selects (`a if c else b`) an
  already-known class value. `add_api_route` does
  `route_class = route_class_override or self.route_class; route_class(...)` →
  now resolves to `APIRoute` through the `type[APIRoute]`-typed parameter operand
  (flow-insensitive union, bounded fixpoint). **Still out of scope:** an operand
  sourced only from `self.<attr>` (no instance-attribute typing) — contributes
  nothing rather than being faked.
- **unblocks:** `factory_surface` (explicit construction sites) vs a plain caller —
  no longer only the `type_fan_out(return)` heuristic.

#### 🟡 `AWAITS` (async boundary)
`caller -[AWAITS]-> callee`; an `is_async` flag on the symbol.
- **unblocks:** the `concurrency_decision` role (present in taxonomy, no signal);
  separation of sync vs async runtime paths; debugging async issues.
- **examples:** fastapi async endpoints; celery async result; any `await`ed I/O.

#### ✅ `RE_EXPORTS` (barrel / package surface — implemented)
`package __init__ -[RE_EXPORTS]-> symbol` (`from .submodule import Name`).
- **status:** implemented — `python_adapter.extract_reexports` (only `__init__`
  files) → `neo4j_client.link_reexports` (`_create_reexport_relations`, resolver
  `reexport-v1`), pipeline `_reexport_phase`. Source is the `File` node (an
  `__init__` has no Symbol). Cascade feature `reexport_in` (pulled like `import_in`).
  On fastapi: 51 edges; `FastAPI reexport_in=1` (+ APIRouter/Body/Depends/…).
- **unblocks:** the **orthogonal** "I am the package public surface" axis —
  independent of `type_fan_in` (under which `FastAPI` is also consumed as a type)
  and of pre-F13 `depth`/`api_fan_in` (now full-graph in indexer). The `api_surface` L2 predicate
  fires on `is_class and reexport_in > 0 and api_fan_out > 0`, recovering
  `FastAPI → api_surface` (qa_ok) without a magnitude threshold.
- **examples:** `fastapi/__init__.py` re-exporting the public API; a TS barrel
  `index.ts` (not yet — TS adapter).

### Family B — decoupled-identity edges (need an intermediate node)

#### 🟠 `EMITS_EVENT` / `LISTENS_EVENT` (via an event-name node)
Producer `emit('x')` and listener `on('x')` linked through the **event name**,
not a call.
- **unblocks:** event-driven frameworks — a **complete blind spot** today: Django
  signals, Vue `$emit`/`$on`, Node `EventEmitter`, nestjs `EventEmitter`. The
  in-process sibling of the message publish/consume gap (§6).
- **why invisible now:** decoupling is by string identity; no direct edge.
- **node:** materialize an `EventName` node (same shape as the task-identity node).
- **examples:** Vue component `$emit('update')` ↔ parent `@update`; django
  `post_save` signal ↔ receiver.

#### 🟠 `ROUTES_TO` (via a url-pattern node)
url string `'/users/:id'` → handler. Broader than `HANDLES` (which is
decorator-based).
- **unblocks:** `request_router` where routing is not via a decorator — django
  `urls.py`, express `app.get('/x', h)`, explicit route tables.
- **node:** a `RoutePattern` node keyed by the path template.
- **examples:** django `path()`/`re_path()`; express router tables.

#### 🟠 `SCHEDULES` / `TRIGGERS` (via a schedule node)
cron / celery beat / periodic task → the task it fires.
- **unblocks:** time-based invocation, currently invisible.
- **examples:** celery beat schedule; APScheduler jobs; cron entrypoints.

### Family C — field-level / dataflow

#### ✅ Return-shape markers (implemented, Phase A)
`Symbol.returns_mapping`, `returns_sequence`, and `returns_constructed_type`
record the outer function body's top-level return shape. These are node facts,
not edges.
- **unblocks:** a cheap first discriminator for data-shape roles: mapping/sequence
  builders can be separated from pure coordinators before adding heavier dataflow.
- **scope:** AST shape only. `return {}` / `dict(...)` / dict-comprehension,
  sequence literals/constructors/comprehensions, and capitalized constructed calls.
- **does not solve:** where the returned values came from. A mapping builder that
  iterates `model._meta.fields` and calls `f.formfield()` still needs field-read /
  iteration-local / value-flow analysis before `binding_surface` is structurally
  complete.

#### 🟡 `READS_FIELD` / `WRITES_FIELD` (attribute access)
Attribute access on an instance/class — finer granularity than `USES_TYPE`.
- **unblocks (one edge, three open gaps):** `MUTATES` for `stateful_surface`
  (cache vs config), purity for DTO/`pure_transformer`, descriptor detection for
  `lazy_loader`.
- **why partial:** AST-extractable but noisy (many edges); needs a self/instance
  filter to be useful.
- **examples:** `self._cache[k] = v` (write → stateful); `obj.field` read on a DTO.

#### 🔴 `DATA_FLOW` / `TAINTS`
Value flows param → return → another call's argument.
- **unblocks:** precise impact analysis, security/taint.
- **why hardest:** needs interprocedural dataflow, not an edge; same tier as the
  purity pass for `pure_transformer`. ?Defer?.

### Priority summary

| edge | family | unblocks | tier |
|---|---|---|---|
| `RAISES` / `CATCHES` | A | `error_surface` (role w/o edge), debugging | 🟢 |
| `PASSES_CALLABLE` | A | interceptor/middleware, callbacks (root of `WRAPS`) | 🟢 |
| `INSTANTIATES` | A | factory vs caller | ✅ done (literal + `type[X]` local; disjunction out of scope) |
| `AWAITS` | A | `concurrency_decision` (role w/o edge), async paths | 🟡 |
| `RE_EXPORTS` | A | `public_entrypoint`/`api_surface` orthogonal axis | ✅ done |
| `EMITS`/`LISTENS` | B | event-driven (Vue/Django/Node) blind spot | 🟠 |
| `ROUTES_TO` | B | non-decorator routing | 🟠 |
| `SCHEDULES` | B | cron / beat | 🟠 |
| return-shape markers | C | binding/schema foundation | ✅ done (node facts, not dataflow) |
| `READS`/`WRITES_FIELD` | C | MUTATES + purity + descriptor (3 gaps) | 🟡 |
| `DATA_FLOW` | C | impact / security | 🔴 |

**Highest leverage:** `RAISES`/`CATCHES` and `AWAITS` give the only structural
signal to two roles that exist in the taxonomy with none today (`error_surface`,
`concurrency_decision`). `PASSES_CALLABLE` is more fundamental than the proposed
`WRAPS` (function-as-argument is otherwise invisible). `READS`/`WRITES_FIELD` is
the best single edge by reach (feeds MUTATES, purity, and descriptor detection at
once).

---

## Related analysis

- [role_signature_findings.md](role_signature_findings.md) — duplicate features
  and discriminator collisions in this catalog, each with a what/how/why + decision.
- [role_clustering_architecture.md](role_clustering_architecture.md) — Pass-1
  discriminator-first L1/L2 cascade + presence gate (replaces retired k-means).
