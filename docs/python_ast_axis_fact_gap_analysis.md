# Python AST axis fact gap analysis

This note reviews the first `axis_python_v1` extractor against the facts needed
for Python-only query compilation. It intentionally stays below roles, buckets,
and contracts.

Vocabulary used here:

```text
AST fact      = raw syntactic observation
axis bit      = normalized physical fact on CFG / DFG / Structural axis
contract      = later cross-axis proof pattern
role/bucket   = later query or benchmark grouping
```

Only the first two belong in the extractor.

## Current extractor coverage

Implemented in [python_extractor.py](../sidecar/axis/python_extractor.py).

### CFG bits present

| bit | status | notes |
|---|---|---|
| `callable_body` | present | Function, async function, lambda. |
| `call_site` | present | Any `Call`. Payload includes callee text/name only. |
| `method_dispatch` | present | `Call(func=Attribute(...))`. |
| `constructor_call` | present | Capitalized callee heuristic. |
| `decorator_application` | present | Function/class decorator syntax. |
| `branch_selector` | present | `if`, `match`, conditional expression. |
| `loop_driver` | present | loops and comprehensions. |
| `context_enter_exit` | present | `with` / `async with`. |
| `exception_transfer` | present | `try`, `except`, `raise`. |
| `async_suspend_resume` | present | async function, await, async for/with. |
| `generator_yield` | present | `yield`, `yield from`. |
| `return_exit` | present | `return`. |
| `value_call` | present | Call where callee is a value expression (`Name`, `Subscript`, etc.), not method dispatch. |
| `branch_condition` | present | Branch/loop condition payload with expression and physical reads. |
| `exception_raise_value` | present | `raise` payload with raised expression/call and cause. |
| `exception_handler_type` | present | `except` payload with caught type(s) and bound name. |

### DFG bits present

| bit | status | notes |
|---|---|---|
| `parameter_input` | present | Function args. |
| `parameter_default_value` | present | Parameter default expression tied to parameter name. |
| `assignment_binding` | present | Assign, ann-assign, named expr, loop targets. |
| `aliasing` | present | Simple `name = name`. |
| `call_argument` | present | Positional/keyword argument value entering a call. |
| `callable_value` | present | Function/class/lambda or known callable name used as data. |
| `call_result_origin` | present | Assignment/named expr from call. |
| `constructor_value` | present | Capitalized constructor-like call. |
| `attr_read` | present | Attribute load, excluding method callee position. |
| `attr_write` | present | Attribute store. |
| `subscript_read` | present | Subscript load. |
| `subscript_write` | present | Subscript store. |
| `container_write_value` | present | Subscript assignment and generic container mutator calls. |
| `container_read_key` | present | Subscript load and generic `.get(key)` read. |
| `keyed_write` | present | Key/value write in subscript assignment and dict literal entries. |
| `keyed_read` | present | Keyed read in subscript load and generic `.get(key)` read. |
| `iteration_source` | present | `for target in iterable` / `async for`. |
| `augmented_mutation` | present | `+=` etc. |
| `collection_assembly` | present | Literal/comprehension collection shape. |
| `branch_influence` | present | Physical reads participating in a branch/loop condition. |
| `projection` | partial | Return expression containing attr read. |
| `return_output` | present | Return value. |
| `return_shape_kind` | present | Return expression classified as mapping/sequence/constructed/name/etc. |
| `constructed_output` | present | Constructor-like call used as assignment or return output. |
| `yield_output` | present | Yield value. |
| `context_resource` | present | `with expr as name`. |
| `exception_value` | present | `except X as name`. |

### Structural bits present

| bit | status | notes |
|---|---|---|
| `module_scope` | present | Synthetic module profile. |
| `import_dependency` | present | Import/from-import syntax. |
| `class_def` | present | Class declaration. |
| `function_def` | present | Function declaration. |
| `async_function_def` | present | Async function declaration. |
| `method_member` | present | Function declared inside class. |
| `class_attribute` | present | Assignment in class body. |
| `instance_attribute_hint` | present | `self.x` / `cls.x` assignment target. |
| `inheritance` | present | Class bases. |
| `metaclass` | present | Class metaclass keyword. |
| `parameter_decl` | present | Function parameters. |
| `annotation` | present | Parameter, return, ann-assign. |
| `generic_shape` | present | Subscripted annotation shape (`dict[K, V]`, `list[T]`, etc.). |
| `decorator_attachment` | present | Decorator syntax. |
| `decorator_shape` | present | Decorator callee/args/keywords payload. |
| `parameter_default` | present | Parameter default expression shape. |
| `literal_shape` | present | Collection literal/comprehension shape. |
| `literal_key` | present | Static literal key in dict/subscript contexts. |
| `base_keyword` | present | Non-metaclass class keyword payload. |

## Needed contract families

The Python public benchmark questions cluster into a small number of contract
families. These names are not extractor facts; they explain what facts are
needed.

| contract family | examples | needs from extractor |
|---|---|---|
| Deferred registration | Celery task decorators, Flask hooks, Django signals, SQLAlchemy events, Pydantic validators, Click commands | decorator shape, callable-as-value, registry/container write, later value call |
| Dependency binding | FastAPI `Depends`, security dependencies, Click context decorators | parameter default/metadata, provider callable reference, provider result to argument slot |
| Registry read dispatch | signal send loop, route/error handler dispatch, task worker lookup | keyed/container read, iteration over stored values, call of read value |
| Metadata write/read | Pydantic decorators, dataclass/field descriptors, SQLAlchemy mapper/event metadata | keyed write/read with literal/key identity payload |
| Config effect | Pydantic `ConfigDict`, Django settings, Celery routes, middleware options | config carrier, key/field read, branch/call/constructor influence |
| Shape projection | serializers, schema emitters, ORM mapping/composite shapes | source field reads, mapping/constructed output, loop item binding |
| Context/proxy scope | Flask globals, current app/request objects, transaction/lifespan contexts | context storage set/reset/read, scoped resource lifetime |
| Runtime adapter | async/sync bridge, greenlet/thread pool, middleware wrapper | await/thread/greenlet call boundary, wrapper continuation call |
| Error handling | FastAPI/Flask errors, Celery retry/timeout, Pydantic validation errors | raise value/type, except type, handler registry, error object construction |

## Fact gaps and implementation slices

The tables below started as a gap inventory. Slices A-C are now implemented in
the extractor; remaining rows still describe future structural work or sharper
contracts on top of the physical facts.

### P0: facts required before Deferred Binding Flow

These are the most important because regular CFG cannot see inversion of
control. Without them, FastAPI/Celery/Flask/Pydantic/SQLAlchemy event questions
will keep collapsing into broad guesses.

| missing fact | axis | why needed | structural extraction rule |
|---|---|---|---|
| `dfg_callable_value` | DFG | A function/class/lambda is passed or returned as data. Needed for decorators, callbacks, registries, providers. | Emit when a `Name`, nested `FunctionDef`, `Lambda`, class/function symbol, or decorated target is used as an argument, return value, assignment value, dict/list value, or decorator input. |
| `dfg_call_argument` | DFG | Need to know which values enter a call. Current `call_site` only knows callee. | For every `Call`, emit positional/keyword argument facts with expression text, AST kind, literal/name/attribute/subscript classification. |
| `struct_decorator_shape` | Structural | Decorator attachment string is too coarse. Need callee, args, keywords, literal keys. | For each decorator, emit callee kind/name, whether it is call/name/attribute, positional args, keyword args, literal payloads. |
| `struct_parameter_default` | Structural | FastAPI/Click/security bindings live in defaults. Current parameter/default relation is implicit. | For each parameter default or kw-only default, emit param name plus default expression shape. |
| `dfg_parameter_default_value` | DFG | Default marker/provider becomes value source for dependency binding. | Pair parameter slot with default expression and visit default as value origin. |
| `dfg_container_write_value` | DFG | Registration is often dict/list/set mutation carrying handler/callback. | For subscript assignment and collection-mutating calls, emit target container, key/index when present, and value/argument expression. |
| `cfg_value_call` | CFG | Runtime dispatch often calls a value read from registry: `handler(...)`, `callbacks[k](...)`. | Emit when call callee is `Name`, `Subscript`, or non-method expression; payload marks callee expression kind. |
| `dfg_container_read_key` | DFG | Registry read/dispatch needs lookup key and read source. | For subscript load, emit container expression and key expression/literal shape. |
| `dfg_iteration_source` | DFG | Dispatch loops iterate registered callbacks/routes/tasks. | For `for target in iterable`, emit target pattern plus iterable expression and shape. |

Important discipline: do not name these `registry_write` in the extractor. The
extractor should emit container/key/value facts. The later contract compiler can
decide whether a specific container mutation participates in registry binding.

### P1: facts needed for metadata/config/error contracts

These are next after P0 because they make contracts precise rather than broad.
Most key/branch/error/generic rows are now implemented as physical facts; the
contract compiler still has to prove cross-axis meaning.

| missing fact | axis | why needed | structural extraction rule |
|---|---|---|---|
| `dfg_keyed_write` | DFG | Metadata/config/registry often has a key identity. | Emit for `obj[key] = value`, `dict.update`, `setdefault`, and standard mapping literal entries; include literal key if statically present. |
| `dfg_keyed_read` | DFG | Runtime reads same key later. | Emit for `obj[key]`, `get(key)`, `getattr(obj, literal)`, mapping iteration over items. |
| `struct_literal_key` | Structural | Need deterministic key identity without framework names. | Emit string/int/enum-like literals used as dict keys, subscript keys, keyword values, decorator args. |
| `cfg_branch_condition` | CFG | Config effect needs to know what value controls a branch. | Payload on `branch_selector`: condition expression, referenced names/attrs/subscripts. |
| `dfg_branch_influence` | DFG | Config values affect branch/call choice. | Emit condition value reads as value-flow facts, not as semantic config labels. |
| `cfg_exception_raise_value` | CFG | Retry/timeout/error contracts need raised object/type. | Payload on `Raise`: expression kind, callee/type if `raise X(...)`. |
| `cfg_exception_handler_type` | CFG | Error dispatch needs except classes. | Payload on `ExceptHandler`: caught type expression and bound name. |
| `dfg_error_object_origin` | DFG | Error response mapping needs error value construction. | Emit when raise value is a constructor-like call or caught value is transformed/returned. |
| `struct_generic_shape` | Structural | Needed for typed containers/providers. | For subscripted annotations (`list[T]`, `dict[K,V]`, `Annotated[...]`), emit base and args. |
| `struct_base_keyword` | Structural | Class creation/config sometimes lives in class keywords beyond metaclass. | Emit all class keywords, not only `metaclass`. |

### P2: facts useful for shape projection and runtime adapters

These can follow once registration/dependency/config gaps are covered. Return
shape and constructed output are now implemented; closure/context/stream facts
remain future work.

| missing fact | axis | why needed | structural extraction rule |
|---|---|---|---|
| `dfg_attr_write_value` | DFG | Current attr write does not include written value. Needed for object composition. | Add value expression to attr/subscript write payloads. |
| `dfg_return_shape_kind` | DFG | Return shape should be directly visible. | On `Return`, classify returned expression: mapping, sequence, constructed, callable, name, attr, subscript. |
| `dfg_constructed_output` | DFG | Shape projection often returns `Result(a=x.y)`. | For constructor-like calls in return/assignment, emit callee plus keyword/arg source expressions. |
| `dfg_closure_capture` | DFG | Decorator factories/wrappers use outer values. | For nested function/lambda, compare used names against enclosing scope bindings. |
| `cfg_wrapper_continuation_call` | CFG | Middleware/interceptor wrappers call `next`/continuation/inner app. | Emit call of a parameter/local callable as value call; later contract can classify wrapper. |
| `cfg_context_expression` | CFG | Context managers are important for transactions/lifespan. | Payload on with/async-with: manager expression, optional var, async flag. |
| `dfg_context_lifetime` | DFG | Scoped resources are set/read/cleared around context. | Track `with expr as name`, `try/finally`, reset/close calls as generic resource lifetime facts. |
| `dfg_stream_io` | DFG | CLI/files/body parsing questions need stream reads/writes. | Physical call-argument facts plus value-call boundaries; semantic stream classification is later. |

## Contract-to-fact matrix

The contract compiler should consume facts like this:

| later contract | minimal facts needed |
|---|---|
| Deferred registration | `struct_decorator_shape` OR `dfg_call_argument`; `dfg_callable_value`; `dfg_container_write_value` OR `dfg_keyed_write`; later `dfg_container_read_key`; `cfg_value_call` |
| Dependency binding | `struct_parameter_decl`; `struct_parameter_default`; `dfg_parameter_default_value`; provider `dfg_callable_value`; solver `cfg_value_call` or `call_site`; argument `dfg_call_argument` |
| Metadata contract | `struct_decorator_shape`; `dfg_keyed_write`; `struct_literal_key`; `dfg_keyed_read` with same key identity |
| Config effect | config carrier from `struct_annotation`/`struct_class_attribute`; `dfg_keyed_read`/`attr_read`; `cfg_branch_condition`; affected `call_site`/`constructor_call` |
| Shape projection | source `attr_read`/`subscript_read`; `dfg_iteration_source`; `dfg_return_shape_kind`; `dfg_constructed_output` |
| Error mapping | `cfg_exception_raise_value`; `cfg_exception_handler_type`; `dfg_error_object_origin`; handler `dfg_container_write_value`; dispatch `cfg_value_call` |
| Context/proxy scope | `cfg_context_expression`; `dfg_context_lifetime`; `attr_read`/`attr_write`; later graph-level proxy edges |

## Immediate implementation order

### Slice A: call/decorator/parameter payloads

Add these facts first:

```text
dfg_call_argument
struct_decorator_shape
struct_parameter_default
dfg_parameter_default_value
cfg_value_call
```

Why: these do not require real dataflow, are high precision, and unlock DI,
decorator registration, and callback detection.

Status: implemented.

### Slice B: container/key/value writes and reads

Add:

```text
dfg_container_write_value
dfg_container_read_key
dfg_keyed_write
dfg_keyed_read
struct_literal_key
dfg_iteration_source
```

Why: this is the physical substrate for registry and metadata write/read
bridges. Keep names generic; do not label them registry/metadata in extractor.

Status: implemented.

### Slice C: branch/error/return detail

Add:

```text
cfg_branch_condition
dfg_branch_influence
cfg_exception_raise_value
cfg_exception_handler_type
dfg_return_shape_kind
dfg_constructed_output
struct_generic_shape
```

Why: these cover config effect, error mapping, and shape projection. They also
make benchmark diagnostics sharper: "missing key read" is much better than
"missing binding".

Status: implemented as `branch_condition`, `branch_influence`,
`exception_raise_value`, `exception_handler_type`, `return_shape_kind`,
`constructed_output`, and `generic_shape`.

## Anti-patterns to avoid

- Do not emit `registry_write` from the extractor.
- Do not emit framework names such as `fastapi_depends`, `celery_task`, or
  `flask_route`.
- Do not classify a call as registration because the method is named
  `register`, `route`, `task`, or `command`.
- Do not infer value flow across functions yet. Cross-function flow belongs in
  the later graph/query layer.
- Do not make `ConfigDict`, `Depends`, `Blueprint`, or `Signal` special cases.

The extractor may emit literal payloads and structural call shapes. The contract
compiler may later use those payloads together with graph expansion and vector
seeds to decide which facts close a question.

## Success criterion

After Slice A and B, a decorated callback registration should no longer look
like only:

```text
CFG: decorator_application
STRUCT: decorator_attachment
```

It should look like:

```text
STRUCT:
  decorator_shape(callee=..., args=..., keywords=...)
  parameter_decl/default if present

DFG:
  callable_value(function object enters decorator/call)
  call_argument(callback/provider/key/value)
  container_write_value or keyed_write
  container_read_key or keyed_read

CFG:
  decorator_application
  value_call(read callback invoked later)
```

That is enough physical evidence for `Deferred Binding Flow` without inventing a
fourth axis or reintroducing framework tables.
