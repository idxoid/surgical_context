# Logical roles and structural closure

> **Type:** C — Concept. Eval / Pass-1 vocabulary; not a cascade remnant.

Separates two things that are easy to collapse in eval and Pass-1 work:

**See also:**
- [question_structural_role_profiles.md](question_structural_role_profiles.md) — per-question gold `+`/`-` profiles (116 questions)
- [role_catalog.md](role_catalog.md) / [role_predicates.md](role_predicates.md) — current engine vocabulary and L1/L2 rules
- [axis_terminology.md](axis_terminology.md) — axis-layer terms (fact, contract, role, …)
- [engineering_principles.md](engineering_principles.md) — structural-only invariants

- **Logical role**: what the question needs to explain. This is the role a
  benchmark pack or query planner may ask for.
- **Structural closure value**: the code-derived fact that proves the answer has
  that kind of evidence. This must come from AST facts, resolved graph edges, and
  topology only.

The important rule is not "one logical role equals one Pass-1 role". The safer
rule is:

```text
logical role is satisfied by one or more structural closure values
```

That lets the pack stay semantic while the engine stays structural. It also
explains what the per-symbol table exposed: the same logical role often contains
several different mechanisms, and the same current structural predicate often
serves several logical roles.

## Why this exists

Per-symbol role diagnostics (e.g. `QA/axis_role_report.py`, benchmark seed vs
expected analysis) show three problems.

1. One logical role can mean different structures.
   `binding_surface` currently covers route params, request body arguments,
   context proxies, DI tokens, macro bindings, and data-shape projection. These
   are not one topology.

2. One structural shape can be labeled with different roles.
   Higher-order decorators, metadata writers, callback registrars, and factories
   overlap in code shape. A function that returns a decorator can be both
   `factory_surface` and `registration_step`, but those are different logical
   questions.

3. Broad buckets dominate.
   `api_surface` and `runtime_surface` are useful background facts, but weak
   discriminators. Narrow roles such as `binding_surface`, `registration_step`,
   `request_router`, `interceptor`, and `error_surface` carry the explanatory
   load and need finer structural closure.

## Proposed model

Keep the existing canonical roles for compatibility, but treat them as logical
roles at evaluation time. Add a lower layer of structural closure values.

```text
question / pack
  -> logical required roles
  -> acceptable structural closure values per role
  -> selected symbols/edges prove those values
```

Pass-1 can still persist `derived_primary_role` and supporting roles. The
important addition is that role fulfilment should be able to say:

```text
binding_surface closed by route_param_binding
registration_step closed by metadata_write_read_contract
runtime_surface closed by dispatch_loop_participant
```

instead of only:

```text
binding_surface present / missing
```

## Structural closure values

These are intentionally more numerous than today's canonical roles. They are not
answer-key labels; each must be backed by a precise extraction rule.

| structural value | structural evidence only | current role family |
|---|---|---|
| `public_symbol_surface` | re-export, documented public definition, API owner/member fan | `api_surface` |
| `member_api_surface` | `HAS_API` owner/member topology | `api_surface` |
| `runtime_participant` | call fan-in and fan-out on a reachable runtime path | `runtime_surface` |
| `handler_executor` | inverse handler edge, dynamic dispatch target, or call leaf invoked by dispatcher | `executor` |
| `dispatch_loop` | handler-call fan-out or repeated dispatch over registered handlers | `request_router`, `executor` |
| `decorator_factory` | function returns function expression/decorator value | `factory_surface` |
| `metadata_writer` | AST call writes metadata/keyed annotation to a target | `registration_step` |
| `metadata_reader` | AST call reads same metadata/keyed annotation | `runtime_surface`, `orchestrator` |
| `metadata_write_read_contract` | writer and reader share resolved key identity | `registration_step` |
| `callback_registry_writer` | function stores callback/handler into a collection/table | `registration_step` |
| `middleware_chain_append` | function appends/wraps callable into ordered chain | `registration_step`, `composition_surface` |
| `route_table_mutator` | function adds route/pattern/handler record to route table | `registration_step`, `request_router` |
| `event_listener_attach` | function attaches event listener to external/runtime source | `registration_step`, `integration_surface` |
| `request_arg_binder` | request/body/query/header data becomes endpoint/function arguments | `binding_surface` |
| `route_param_binder` | route pattern capture becomes request params or handler args | `binding_surface`, `request_router` |
| `context_proxy_binding` | proxy binding resolves to scoped context object | `proxy_mechanism`, `binding_surface` |
| `di_token_binder` | token/type/metadata dependency resolves to provider/argument | `binding_surface`, `orchestrator` |
| `shape_projector` | reads fields/attrs and returns mapping or constructed output shape | `binding_surface`, `representation_surface` |
| `macro_binding_transform` | compile-time declaration becomes runtime binding record | `binding_surface`, `representation_surface` |
| `data_contract_type` | high type fan-in / dependency fan-in data contract | `representation_surface` |
| `config_contract` | config object/typed option carrier consumed by other code | `config_surface` |
| `schema_emitter` | builds schema/field/table shape consumed elsewhere | `schema_builder` |
| `error_contract` | exception inheritance or error object propagated through handlers | `error_surface` |
| `integration_gateway` | filtered external integration call/import fan, not stdlib plumbing | `integration_surface` |
| `async_compat_bridge` | async API delegates through sync bridge/greenlet/thread adapter | `compat_bridge`, `executor` |
| `object_property_factory_bridge` | returned object property resolves to factory/callee target | `factory_surface`, `composition_surface` |
| `module_composer` | object/decorator metadata composes imports/providers/controllers/etc. | `composition_surface` |
| `fluent_composer` | self-returning API chain or builder composition topology | `composition_surface` |

## Logical roles as mappings

The table below treats existing canonical roles as logical roles. A future pack
may ask for narrower logical roles directly, but this gives a migration path.

| logical role | what it means logically | structural closure values that can satisfy it |
|---|---|---|
| `api_surface` | user-visible entry point or public API object | `public_symbol_surface`, `member_api_surface` |
| `runtime_surface` | code participates in the runtime path being explained | `runtime_participant`, `metadata_reader`, `dispatch_loop`, `handler_executor` |
| `executor` | code actually invokes/executes work | `handler_executor`, `dispatch_loop`, `async_compat_bridge` |
| `orchestrator` | code coordinates several runtime pieces | `runtime_participant`, `dispatch_loop`, `metadata_reader`, `di_token_binder` |
| `composition_surface` | code assembles a chain/module/object graph | `module_composer`, `middleware_chain_append`, `fluent_composer`, `object_property_factory_bridge` |
| `factory_surface` | code produces another callable/object/API surface | `decorator_factory`, `object_property_factory_bridge`, constructor/factory return topology |
| `registration_step` | code records future behavior for a runtime to consume | `metadata_writer`, `metadata_write_read_contract`, `callback_registry_writer`, `route_table_mutator`, `middleware_chain_append`, `event_listener_attach` |
| `request_router` | code matches a request/event and chooses a handler | `route_table_mutator`, `route_param_binder`, `dispatch_loop` |
| `interceptor` | code wraps/filters execution before or around handler work | `middleware_chain_append`, `dispatch_loop`, decorator/wrapper topology with downstream handler execution |
| `binding_surface` | code binds an external/contextual/source value to an argument or output slot | `request_arg_binder`, `route_param_binder`, `context_proxy_binding`, `di_token_binder`, `shape_projector`, `macro_binding_transform` |
| `representation_surface` | code defines or carries the data shape being operated on | `data_contract_type`, `shape_projector`, `macro_binding_transform` |
| `config_surface` | code carries configuration that controls later behavior | `config_contract`, metadata/config object fan-in |
| `schema_builder` | code emits a schema/field/table representation | `schema_emitter`, shape construction topology |
| `error_surface` | code defines, throws, catches, or converts error state | `error_contract`, error handler dispatch topology |
| `proxy_mechanism` | code exposes scoped object through proxy indirection | `context_proxy_binding` |
| `integration_surface` | code crosses package/process/network/library boundary | `integration_gateway`, `event_listener_attach` |
| `compat_bridge` | code adapts old/new or sync/async runtime model | `async_compat_bridge`, compatibility wrapper topology |
| `core_runtime` | hot internal primitive used by runtime machinery | call-leaf/fan-in primitive topology |

## Nicer logical subroles

Some current roles should probably remain canonical compatibility labels, but
question packs could become clearer if they used narrower logical roles.

| proposed logical role | maps to canonical | structural closure |
|---|---|---|
| `decorator_metadata_registration` | `registration_step` | `decorator_factory` + `metadata_writer` or `metadata_write_read_contract` |
| `callback_registration` | `registration_step` | `callback_registry_writer` |
| `route_registration` | `registration_step` | `route_table_mutator` |
| `middleware_registration` | `registration_step`, `composition_surface` | `middleware_chain_append` |
| `event_registration` | `registration_step`, `integration_surface` | `event_listener_attach` |
| `request_argument_binding` | `binding_surface` | `request_arg_binder` |
| `route_parameter_binding` | `binding_surface`, `request_router` | `route_param_binder` |
| `context_scope_binding` | `binding_surface`, `proxy_mechanism` | `context_proxy_binding` |
| `dependency_token_binding` | `binding_surface`, `orchestrator` | `di_token_binder` |
| `shape_projection_binding` | `binding_surface`, `representation_surface` | `shape_projector` |
| `compile_macro_binding` | `binding_surface`, `representation_surface` | `macro_binding_transform` |
| `async_runtime_bridge` | `compat_bridge`, `executor` | `async_compat_bridge` |
| `external_runtime_gateway` | `integration_surface` | `integration_gateway` |
| `error_handler_mapping` | `error_surface`, `registration_step` | `error_contract` + `callback_registry_writer` or metadata contract |

## What the table implies for current weak spots

### `binding_surface`

Current `binding_surface` should not be one predicate. It needs several closure
values:

- route params: `route_param_binder`
- request/body/header args: `request_arg_binder`
- Flask/Vue context: `context_proxy_binding`
- Nest/FastAPI dependency arguments: `di_token_binder`
- SQLAlchemy/Django shape projection: `shape_projector`
- Vue SFC compiler macros: `macro_binding_transform`

The existing return-shape/attr-read predicates are a good start only for
`shape_projector`; they should not be expected to close every binding question.

### `registration_step`

Registration is a family:

- decorator returns function: `decorator_factory`
- decorator writes metadata: `metadata_writer`
- runtime reads same metadata: `metadata_write_read_contract`
- app/router stores handler: `route_table_mutator`
- listener setup stores event callbacks: `event_listener_attach`
- middleware setup appends wrappers: `middleware_chain_append`

This is why one `registration_step` rule will keep colliding with
`factory_surface`, `composition_surface`, and `runtime_surface`.

### `runtime_surface`

`runtime_surface` is too broad to be a high-value required role by itself. It is
better as a background/supporting role unless closed by a narrow value:

- `dispatch_loop`
- `metadata_reader`
- `handler_executor`
- `runtime_participant`

If all we know is "some runtime fan-in/out exists", the role is weak evidence.

### `api_surface`

`api_surface` is also a weak discriminator. It is useful to select the target,
but it should rarely be the role that decides whether a question is complete.

### `error_surface`

There are at least two closure forms:

- `error_contract`: exception/error type definition
- `error_handler_mapping`: handler registry or dispatcher maps error to response

Questions like "map exception to HTTP response" need both, not just exception
inheritance.

## Implementation shape

This can be done without framework tables.

1. Persist structural closure values separately from current roles.
   A symbol can have `derived_structural_values_json`, or edges can carry
   structural relation types that fulfilment can read.

2. Keep current roles as compatibility labels.
   `derived_primary_role` remains useful for ranking, but role recall should be
   able to explain which structural value closed a logical role.

3. Add closure rules as OR-sets.
   For example:

   ```text
   binding_surface :=
     request_arg_binder
     OR route_param_binder
     OR context_proxy_binding
     OR di_token_binder
     OR shape_projector
     OR macro_binding_transform
   ```

4. Report role fulfilment with reason.

   ```text
   expected binding_surface
   closed_by: route_param_binder
   evidence: pattern capture -> request params -> handler args
   ```

5. Validate at the structural-value level.
   A new benchmark table should include:

   ```text
   expected logical role
   accepted structural closures
   observed structural closure
   selected symbols/files
   ```

## Candidate structural extractors

These are ordered by usefulness and precision.

| extractor | closes | cost | notes |
|---|---|---|---|
| metadata read/write key identity | NestJS decorators, Pydantic decorators, SQLAlchemy events | AST, medium | Resolve imported constants and literal keys; create writer-reader contract only when key identity matches. |
| registry mutation detection | Flask handlers, Express routes, Click commands, RTK listeners | AST, medium | Detect writes/appends/sets into collections owned by receiver/self/module. |
| route parameter binding | Express/Flask/FastAPI routing params | AST + bounded literal pattern parsing | Treat route pattern capture as structural code literal, not symbol-name matching. |
| context proxy origin | Flask globals, Vue provide/inject-style scoped lookup | AST + existing proxy edges | Extend proxy edge with scoped storage read/write evidence. |
| object property factory bridge | Vue `ensureRenderer().createApp`-style returned object property | AST + bounded value flow | Resolve returned object property to initializer target. |
| shape projection | SQLAlchemy/Django/FastAPI data-shape transforms | AST/dataflow, medium-high | Use return-shape markers, attr reads, loop assignment, constructed output. |
| async compatibility bridge | SQLAlchemy async/sync bridge | AST + call graph | Detect async public method delegating through sync adapter/greenlet/thread bridge structurally. |
| error handler mapping | FastAPI/Flask/Nest exception handling | AST + registry/metadata | Needs error contract plus handler registry/dispatch read. |

## Anti-patterns to avoid

- Do not make `binding_surface` mean "anything with fields".
- Do not make `registration_step` mean "anything decorator-like".
- Do not use symbol names such as `params`, `locals`, `Controller`, `Catch`, or
  file names to assign closure.
- Do not use benchmark expected files to author edges.
- Do not solve broad bucket imbalance by lowering thresholds. Add the missing
  structural value instead.

## Success criterion

The desired per-symbol table should stop showing only:

```text
expected role: binding_surface
seed primary: orchestrator
missing: binding_surface
```

and start showing:

```text
logical role: binding_surface
accepted closures: route_param_binder | request_arg_binder | context_proxy_binding | ...
observed closure: route_param_binder
evidence path: route pattern -> params object -> handler argument path
```

That is the clean separation: questions stay logical; the graph stays structural.
