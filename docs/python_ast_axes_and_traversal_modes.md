# Python AST axes and traversal modes

This note deliberately abstracts away from the current database schema, current
role names, benchmark packs, and framework-specific behavior. It describes a
clean model for Python only:

```text
Python AST
  -> physical syntax facts
  -> three independent axis bitsets
  -> cross-axis contracts
  -> traversal mode
  -> answer context
```

The goal is to avoid making broad semantic roles such as `binding_surface`,
`registration_step`, or `runtime_surface` primary. Those names are aggregates.
The primary layer should be physical and axis-specific.

## Core separation

There are three fixed physical axes.

| axis | question it answers | examples |
|---|---|---|
| Control Flow Graph, CFG | Who transfers control to whom? | calls, awaits, decorator application, exception transfer |
| Data Flow Graph, DFG | Where do values come from and how do they mutate? | assignment, return, attr write, container mutation, value projection |
| Type / Structural Graph | What is declared, what is it made of, and where does it live? | functions, classes, parameters, annotations, imports, inheritance |

Everything else is not a new axis. Registry, metadata, configuration,
dependency injection, routing, ORM mapping, event listeners, and middleware are
cross-axis contracts proven by combinations of CFG, DFG, and Structural facts.

## What Python AST physically sees

Python AST does not see framework roles. It sees syntax.

| category | AST-level facts |
|---|---|
| declarations | module, class, function, async function, lambda |
| callable shape | parameters, defaults, annotations, return annotation, decorators |
| class shape | bases, metaclass keywords, class body, class attributes, methods |
| imports | import, import-from, aliases |
| bindings | assignment, annotated assignment, augmented assignment, named expression |
| scope binders | parameter, for-target, with-as target, except-as target |
| control | if, match, for, while, try, with, return, raise, break, continue |
| execution points | call, await, yield, yield-from |
| data access | name, attribute, subscript |
| data shapes | dict, list, tuple, set, comprehensions |
| expressions | operators, comparisons, boolean expressions, literals, f-strings |

All higher-level meaning must be compiled from these facts.

## Axis 1: Control Flow Graph

CFG answers only:

```text
Who can transfer execution control to whom?
```

It does not answer what value is passed or what type owns the callable.

| CFG bit | AST evidence | meaning |
|---|---|---|
| `cfg_callable_body` | `FunctionDef`, `AsyncFunctionDef`, `Lambda` | a callable body exists |
| `cfg_call_site` | `Call` | execution can enter another callable |
| `cfg_method_dispatch` | `Call(func=Attribute(...))` | call through a receiver |
| `cfg_constructor_call` | call to a class-like expression | object construction as executable call |
| `cfg_decorator_application` | decorator list | decorator executes at definition/import time |
| `cfg_branch_selector` | `If`, `Match`, conditional expression | control can choose a branch |
| `cfg_loop_driver` | `For`, `While`, comprehension | body can execute repeatedly |
| `cfg_context_enter_exit` | `With`, `AsyncWith` | control enters and exits a context manager |
| `cfg_exception_transfer` | `Raise`, `Try`, `ExceptHandler`, `Finally` | control can move through exception paths |
| `cfg_async_suspend_resume` | `Await`, async function | async boundary exists |
| `cfg_generator_yield` | `Yield`, `YieldFrom` | callable can yield control to caller |
| `cfg_return_exit` | `Return` | callable exits with a value or empty return |

CFG roles are useful for immediate execution questions:

```text
caller -> callee -> next callee
```

They are not sufficient for inversion-of-control frameworks, because a decorator
or registry write does not directly call the future handler.

## Axis 2: Data Flow Graph

DFG answers only:

```text
Where is a value born, how is it named, how is it read, and how is it mutated?
```

It does not answer whether a callable is public or which branch executes.

| DFG bit | AST evidence | meaning |
|---|---|---|
| `dfg_literal_origin` | literal, dict, list, tuple, set | value is created directly |
| `dfg_call_result_origin` | assignment from call | value is produced by a call |
| `dfg_constructor_value` | assignment from constructor-like call | object value is created |
| `dfg_parameter_input` | function parameter | value enters through a callable boundary |
| `dfg_assignment_binding` | assignment or named expression | value receives a local/module name |
| `dfg_aliasing` | assignment from another name | two names may refer to the same value |
| `dfg_attr_read` | attribute in load context | object state is read |
| `dfg_attr_write` | attribute in store context | object state is written |
| `dfg_subscript_read` | subscript in load context | container element is read |
| `dfg_subscript_write` | subscript in store context | container element is written |
| `dfg_augmented_mutation` | augmented assignment | value is read and then written |
| `dfg_collection_assembly` | collection literal or comprehension | collection shape is assembled |
| `dfg_projection` | reads from input, returns constructed/dict output | source shape becomes output shape |
| `dfg_return_output` | return expression | value leaves a callable |
| `dfg_yield_output` | yield expression | value leaves incrementally |
| `dfg_closure_capture` | nested callable uses outer name | value is captured by closure |
| `dfg_context_resource` | `with expr as name` | resource is bound into scope |
| `dfg_exception_value` | `except X as name` | exception becomes a local value |

DFG is the natural home for binding and state questions, but only when combined
with structural declarations and sometimes with runtime control flow.

## Axis 3: Type / Structural Graph

Structural facts answer only:

```text
What is declared, where is it declared, and what does it structurally contain?
```

They do not answer who calls whom or how values mutate.

| Structural bit | AST evidence | meaning |
|---|---|---|
| `struct_module_scope` | module root | file/module scope exists |
| `struct_import_dependency` | import or import-from | module depends on another module/name |
| `struct_class_def` | class definition | type-like object is declared |
| `struct_function_def` | function definition | callable is declared |
| `struct_async_function_def` | async function definition | async callable is declared |
| `struct_method_member` | function inside class body | callable belongs to class scope |
| `struct_class_attribute` | assignment in class body | class-level member exists |
| `struct_instance_attribute_hint` | `self.x = ...` | instance likely owns field `x` |
| `struct_inheritance` | class bases | type hierarchy exists |
| `struct_metaclass` | class keyword/metaclass | class creation is structurally customized |
| `struct_parameter_decl` | function parameters | callable has declared inputs |
| `struct_annotation` | parameter, return, variable annotation | type declaration exists |
| `struct_generic_shape` | subscripted annotation | container or generic type shape exists |
| `struct_decorator_attachment` | decorator list | symbol is structurally attached to decorator |
| `struct_nested_scope` | nested def/class/lambda | declaration is nested in another scope |
| `struct_literal_shape` | dict/list/tuple/set literal | value shape is present in syntax |

Structural facts are strong for locating surfaces and contracts, but weak for
explaining runtime behavior alone.

## Bit signatures instead of atomic semantic roles

A semantic role should be treated as a bit signature across axes:

```text
role_profile = CFG bits + DFG bits + Structural bits
```

Primitive bits may overlap. The full signature should be discriminative.

Example:

| logical role | CFG bits | DFG bits | Structural bits |
|---|---|---|---|
| decorator registration | decorator application | callable value / possible state write | decorator attachment |
| route binding | handler reachability | parameter input / external value binding | route literal / parameter declaration |
| shape projection | optional call site | attr reads / collection assembly / return output | literal shape / annotation |
| state mutator | optional call site | attr write or subscript write | instance attribute hint |
| proxy context binding | proxy forward | scoped value read | proxy binding / target type |
| type contract | none | none | class definition / annotation / inheritance |

Broad names such as `binding_surface` become aggregates:

```text
binding_surface =
  route binding
  OR dependency binding
  OR proxy context binding
  OR shape projection binding
```

The engine should diagnose missing evidence at the axis-bit level:

```text
logical role: route binding
present:
  Structural.parameter_decl
  Structural.decorator_attachment
missing:
  DFG.external_value_binding
  CFG.handler_reachability
```

## Contracts are not axes

Registry, metadata, dependency injection, configuration, and routing are
contracts. A contract is a cross-axis proof pattern.

### Registry / deferred binding contract

The general shape is:

```text
declaration/import-time action
  -> write callable/value into registry, metadata, or container
  -> runtime reads registry, metadata, or container
  -> runtime dispatches callable later
```

Axis proof:

| axis | required evidence |
|---|---|
| Structural | registry-like container, metadata slot, decorator attachment, handler symbol |
| DFG | callable/value is written into the container/metadata |
| CFG | runtime later reads and invokes the stored callable/value |

This covers decorators, task registries, route tables, event listeners,
middleware chains, and callback maps without making each framework a separate
axis.

### Dependency binding contract

The general shape is:

```text
parameter declaration
  -> marker/provider reference
  -> provider result becomes argument value
  -> endpoint/consumer executes with that value
```

Axis proof:

| axis | required evidence |
|---|---|
| Structural | parameter, annotation/default marker, provider callable |
| DFG | provider result flows into parameter slot |
| CFG | dependency solver/provider executes before consumer |

### Configuration effect contract

The general shape is:

```text
config value
  -> config carrier/field
  -> read by runtime code
  -> affects branch, call, constructor, or mode
```

Axis proof:

| axis | required evidence |
|---|---|
| Structural | config field/schema/option carrier |
| DFG | config value is loaded, stored, or read |
| CFG | read value influences a branch, call, constructor, or execution path |

Config is therefore not a new axis. It is a DFG value plus Structural carrier
plus optional CFG effect.

## Two traversal modes

The graph needs at least two traversal modes. They operate over the same three
physical axes but use different expansion logic.

### Mode 1: Immediate Control Flow

Question:

```text
Who calls whom during direct execution?
```

Typical expansion:

```text
callable or call site
  -> direct/method/constructor/proxy call
  -> next callable
  -> return/exception/await boundary
```

Useful for:

- "How does this method execute the query?"
- "How does this request handler call the backend?"
- "How does this exception get converted?"
- "How does this function reach that helper?"

This mode is mostly CFG-led, with DFG and Structural facts used to explain
arguments, return values, receivers, and types.

### Mode 2: Deferred Binding Flow

Question:

```text
Who binds behavior now so runtime can call it later?
```

Typical expansion:

```text
declaration or decorator
  -> metadata/registry/container write
  -> metadata/registry/container read
  -> deferred handler/callable
  -> switch to Immediate Control Flow if the handler body is needed
```

Useful for:

- "How does `@app.task` register a function?"
- "How does a route decorator connect URL to handler?"
- "How does a dependency provider bind to an endpoint parameter?"
- "How does an event listener get called later?"
- "How does middleware become part of the runtime chain?"

This mode is not plain CFG. A future handler may not be directly called at the
registration site. The proof is a write/read/dispatch bridge across DFG,
Structural, and CFG.

## Query compiler pipeline

The compiler should not compile a question into a single broad role. It should
compile it into:

```text
intent
traversal mode
axis-bit requirements
contract candidates
target node kinds
expansion plan
stop conditions
```

Example:

```text
Question:
  How does the @app.task decorator register a function?

Intent:
  binding / registration explanation

Traversal mode:
  Deferred Binding Flow

Axis requirements:
  Structural:
    decorator attachment
    callable declaration
  DFG:
    callable value written to registry or metadata
  CFG:
    decorator application
    later runtime dispatch if reachable

Contract candidates:
  registry / metadata registration

Stop condition:
  registered callable reached, plus optional runtime dispatch bridge
```

For a direct execution question:

```text
Question:
  How does this method execute the query?

Traversal mode:
  Immediate Control Flow

Axis requirements:
  CFG:
    method call chain
  DFG:
    query object or result value flow
  Structural:
    receiver type / method owner
```

## LanceDB shape

LanceDB should support deterministic pre-filtering over physical axis facts.
It should not store framework answer keys.

Conceptual columns:

```text
node_id
language
node_kind
symbol_kind
file_path
qualified_name
text
embedding

ast_kinds
cfg_bits
dfg_bits
struct_bits
contract_candidate_bits
```

`contract_candidate_bits` are derived from axis bits, not manually authored
framework labels.

Examples:

```text
registry_candidate =
  Structural.container_like
  AND DFG.container_write

metadata_registration_candidate =
  Structural.decorator_attachment
  AND DFG.callable_value

dependency_binding_candidate =
  Structural.parameter_decl
  AND DFG.parameter_binding

config_effect_candidate =
  Structural.config_carrier
  AND DFG.config_read
  AND optional CFG.branch_selector
```

The vector search supplies semantic narrowing. The deterministic filter supplies
axis physics.

## Neo4j / graph traversal shape

After LanceDB returns seed `node_id` values, graph traversal should be compiled
from the selected mode.

Immediate Control Flow:

```text
seed callable/call site
  -> CFG call expansion
  -> optional receiver/type resolution
  -> optional DFG argument/return explanation
```

Deferred Binding Flow:

```text
seed declaration/decorator/marker
  -> Structural attachment
  -> DFG write into registry/metadata/container
  -> DFG/Structural read by runtime
  -> CFG dispatch to stored callable
  -> optional switch to Immediate Control Flow
```

The important difference is that Deferred Binding Flow searches for:

```text
write now -> read later -> execute later
```

not:

```text
caller -> callee
```

## Framework examples without framework tables

### Celery-style task decorator

Expected proof:

```text
Structural:
  decorated function
  decorator attachment
  callable declaration

DFG:
  function/callable value becomes task/registry/metadata value

CFG:
  decorator application at definition/import time
  worker/runtime dispatches stored callable later

Traversal:
  Deferred Binding Flow, then optional Immediate Control Flow inside task body
```

### FastAPI-style dependency

Expected proof:

```text
Structural:
  endpoint function
  parameter declaration
  default marker or annotation
  provider callable

DFG:
  provider result binds to endpoint parameter

CFG:
  dependency solver calls provider before endpoint

Traversal:
  Deferred Binding Flow for provider binding, then Immediate Control Flow for
  endpoint execution
```

### Configuration-dependent runtime behavior

Expected proof:

```text
Structural:
  config carrier, field, option, or schema

DFG:
  config value is loaded/stored/read

CFG:
  value influences branch, constructor, call, or runtime mode

Traversal:
  DFG-led search from config source to consumer, then CFG expansion from the
  affected execution point
```

## Design rule

Keep axes fixed and physical:

```text
CFG / DFG / Structural
```

Let contracts be numerous:

```text
registry contract
metadata contract
dependency binding contract
configuration effect contract
route binding contract
proxy context contract
factory contract
error handler contract
```

Let traversal modes be few:

```text
Immediate Control Flow
Deferred Binding Flow
```

That gives the compiler enough structure for FastAPI, Celery, Flask, ORMs, and
other inversion-of-control systems without turning framework behavior into
hardcoded graph physics.

## Implemented vertical slice

The current `axis_python_v1` implementation has the first end-to-end slice of
that model:

```text
Python source
  -> L1 AxisProfile facts
  -> L2 container kinds
  -> L3 structural contracts
  -> LanceDB deterministic prefilter
  -> Neo4j compiled expansion steps
```

Implemented runtime modules:

| layer | module | output |
|---|---|---|
| L1 axis facts | `context_engine.parser.adapters.python_axis_extractor` | `AxisFact`, `AxisProfile` |
| L2 container kinds | `context_engine.axis.container_kind` | `ContainerKindMatch` |
| graph probe | `context_engine.axis.graph_probe` | structural marker/probe answers |
| L3 contracts | `context_engine.axis.contract_compiler` | `AxisContractMatch` |
| query plan | `context_engine.axis.query_plan` | `AxisQueryPlan` |
| graph expansion | `context_engine.axis.graph_traversal` | `AxisGraphHit` |
| storage | `context_engine.database.lancedb_client` | axis prefiltered symbol search |

The physical index profile is isolated:

```text
INDEX_PROFILE=axis_python_v1
workspace suffix: +axis_python_v1
Lance tables: docs_axis_python_v1, symbols_axis_python_v1
schema_version: 4
```

`symbols_axis_python_v1` stores:

- `cfg_bits`
- `dfg_bits`
- `struct_bits`
- `container_kinds` (list-of-strings; used by the deterministic Lance prefilter
  via `array_has(container_kinds, '<kind>')`)
- `axis_evidence_json`
- `axis_container_kinds_json` (full L2 match objects for diagnostics; the
  prefilter does **not** read this column)
- `axis_contracts_json`

This is still below the old ranker. No runtime endpoint resolves benchmark
roles from contracts yet.

## QA tools

Two QA tools exercise the new stack without touching the legacy ranker:

```bash
python -m QA.axis_contract_report \
  --workspace local/surgical_context@axis-v3-smoke+axis_python_v1 \
  --out /tmp/axis_contract_report
```

This reads persisted axis rows, recompiles L3 contracts from evidence, compares
them with persisted `axis_contracts_json`, and reports drift. The output
directory contains:

- `axis_contract_report.jsonl`: one row per symbol with L2/L3 evidence.
- `axis_contract_report.md`: compact summary plus the full table.
- `axis_contract_summary.json`: counts by container kind, contract, diagnostic,
  traversal mode, persisted contract, and drift state.

The CLI also prints the same high-level counts, e.g.:

```text
rows=483 drift={"no": 483} contracts={...} diagnostics={...} out=/tmp/axis_contract_report
```

```bash
python -m QA.axis_query_smoke "metadata registration" \
  --workspace local/surgical_context@axis-v3-smoke+axis_python_v1 \
  --mode deferred_binding_flow \
  --required-bit dfg:keyed_write \
  --required-bit dfg:keyed_read \
  --required-bit struct:literal_key \
  --container-kind metadata_carrier \
  --limit 5 \
  --threshold 2.0 \
  --out /tmp/axis_query_smoke_metadata.json
```

This compiles the explicit axis request, uses LanceDB for deterministic
prefiltered seed search, then expands those seeds through Neo4j using the
compiled traversal steps.

## Precision rule now enforced

`callable_container_dispatch` is not proven by bit presence alone. The contract
requires payload identity:

```text
dfg.container_write_value.payload.container
  ==
dfg.iteration_source.payload.iterable
```

Without that identity, L2 may still report `middleware_chain`, but L3 leaves the
dispatch contract unproven. This is intentional: the diagnostic should say
"container-shaped candidate, dispatch not proven" instead of overclaiming a
deferred runtime path.

## Still not implemented

- L4 role resolver: no contract-to-role runtime bridge is active.
- Ranker integration: `/ask` and benchmark retrieval still use the legacy path.
- Library marker catalogue beyond already materialized graph facts.
- Cross-symbol DFG proof for provider result -> consumer argument.
- Full value-flow proof for route params, request bodies, and schema projection.
