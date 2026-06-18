# Contract compiler: topology over frameworks

Companion to [python_ast_axes_and_traversal_modes.md](python_ast_axes_and_traversal_modes.md)
and [python_ast_axis_fact_gap_analysis.md](python_ast_axis_fact_gap_analysis.md). This
note captures principles for the layer above the axis-fact extractor — what
turns physical bits into named contracts — and the sub-layer below contracts
that classifies the topology of containers a fact lands in.

These are not implementation specs. They are invariants the contract layer
must hold so that the model keeps generalising.

## The core inversion

The wrong framing: "how do we recognise Flask routing?"

The right framing: "what graph fingerprint is `web_route_register`?"

The compiler does not classify frameworks. It classifies **graph patterns**.
Two consequences fall out:

- If Flask and FastAPI compile to the same fingerprint, they **are** the
  same mechanism for the compiler. The context assembled for "how does this
  route work" is identical. The user gets the same kind of answer derived
  the same way — consistency a framework-aware system cannot reach.
- If two frameworks differ, their fingerprint **must** differ at the
  axis-bit level. If it does not differ on current bits, that is a
  diagnostic about the extractor — what bit is missing — not a request to
  add a framework rule.

"We don't support framework X" stops meaning "we haven't added X to a
table" and starts meaning "we cannot see what makes X structurally
distinct yet — and here is the missing bit". That shift is the whole
point.

## Layered structure

The extractor and contract compiler are not adjacent. There is a
container-classification sub-layer between them.

```text
L1 extractor          : AST → axis bits + payload         (no semantics)
L2 container kind     : bits → container_kind             (graph fingerprint)
L3 contract compiler  : bits + container_kind → contract  (cross-axis proof)
L4 role resolver      : contracts → logical role          (OR of contracts)
L5 exposure layer     : contract + payload → human view   (qualified names,
                                                            external pkg ids)
```

Each layer has one responsibility. Each can be tested in isolation. None
references the layer two above it.

## L2: Container kinds

A **container kind** is a stable graph fingerprint that recurs across
unrelated projects. It is not a name. The same kind is reached by any
container whose own bit-signature matches, regardless of class name or
module path.

### Distinguishing topology, not labels

Examples of how four registry kinds resolve through their own
connectivity, not their names:

| container kind | distinguishing topology (illustrative) |
|---|---|
| `web_route_register` | class/object with high `HAS_API` fan to handlers, USES_TYPE on HTTP-method/URL-pattern bearing external symbols, `dfg_keyed_write` of callables keyed by literal HTTP method strings or path patterns |
| `task_register` | class/object with `IMPORTS_EXTERNAL` to messaging packages (kombu, amqp, billiard, redis transport family), `INSTANTIATES` of queue/exchange-like objects, decorator writes that produce callables read by a worker loop |
| `data_model` | class with multiple class-body assignments to descriptor-shaped values, methods whose `dfg_return_shape_kind` is `constructed` of the same class, field reads in validators that share key identity |
| `signal_register` | class/object with bidirectional callable storage — receivers attached, later iterated and called — without web/task/model fingerprints |

The compiler never reads "celery" or "starlette" from a symbol name. It
reads:
- this container's own `dfg_container_write_value` shape,
- its `HAS_API`/`INHERITED_API` fan,
- its `IMPORTS_EXTERNAL` roots (already filtered through plumbing),
- its outgoing `USES_TYPE` to external symbols whose marker the library
  catalogue carries (see below).

### External library markers

A small local workspace can have a thin local fingerprint — one route is
not enough to distinguish a routing container from a generic class. The
fallback is **library-level markers**: well-known external symbols whose
own structural role is known (e.g. `starlette.routing.Router`, the base
class of any application router; `celery.app.Celery`, the base type of
any task application). A marker says "instances of this external symbol
are `web_route_register` kind" and propagates that classification through
inheritance and composition edges into the local workspace.

Library markers are not framework names. They are **graph nodes** carrying
a kind, the same way locally-classified containers do. The marker
catalogue replaces "Flask is special" with "this specific external
symbol is structurally a route table". When new versions or new
frameworks ship a different but structurally identical symbol, only the
catalogue grows, never the compiler.

The discipline: a marker is added only when its external symbol's own
structural fingerprint matches the kind. If we cannot describe why
`starlette.routing.Router` is a `web_route_register` in terms of axis
bits and topology, we have not earned the marker yet.

## L3: Contracts

A contract is a cross-axis proof pattern. Each contract names:

- the axis-bit facts it consumes,
- the container kind it requires the write/read to land in,
- the optional payload constraints (key identity, return shape, etc.).

A registration contract is, at minimum:

```text
dfg_callable_value
  flows into
dfg_container_write_value | dfg_keyed_write
  on a container of kind <X>
  later read by
dfg_container_read_key | dfg_iteration_source
  later invoked through
cfg_value_call
```

The contract is parameterised by container kind. The **same contract
shape** with container kind `web_route_register` is a route registration,
with `task_register` is a task registration, with `signal_register` is a
signal registration. Three names, one structural definition.

This is the test of whether the model is working: a new framework with a
new container kind should land **as a new container kind entry**, not as
a new contract or a new compiler branch.

## L5: Diagnostic split

The compiler classifies by shape; the exposure layer enriches by
payload.

- Compiler output: `web_route_register` (kind), `Deferred Registration`
  (contract), `binding_surface` (logical role) — shape-only.
- Exposure output: same plus payload — qualified container name, external
  package id, route literal if visible, handler symbol uid. This is what
  reaches the user (CLI, IDE webview, LLM prompt).

The two are separated because the compiler must stay structural (or it
re-introduces framework knowledge into the classifier), while the
exposure layer can use any payload the extractor already captured. The
user always knows *which instance* of a pattern they are looking at; the
compiler never does.

This split is also where per-bit diagnostics live. When a logical role
goes unmet, the exposure layer must show the missing bit signature, not
the missing role:

```text
binding_surface unsatisfied: no contract proven.
  Closest match: Deferred Registration
    Found:    cfg_decorator_application, struct_decorator_shape,
              dfg_callable_value
    Missing:  dfg_keyed_write OR dfg_container_write_value
              with a container of kind web_route_register / task_register /
              signal_register / data_model
```

This is the killer feature of the axis layer over the old cascade. It
moves "we don't know" into "here is the exact structural step that did
not prove out". Without that exposure, the whole architecture's gain is
invisible to the user. Holding the line on per-bit diagnostics from the
first contract that lands is a non-negotiable.

## Anti-patterns

These extend the [extractor anti-patterns](python_ast_axis_fact_gap_analysis.md#anti-patterns-to-avoid)
to L2 / L3.

- Do not classify a container by its symbol name (`Router`, `App`,
  `Signal`). Classify by its bit-signature and its outgoing connectivity.
- Do not split one contract into framework-specific variants
  (`flask_route_contract`, `fastapi_route_contract`). One contract, one
  container kind parameter.
- Do not introduce a container kind that is satisfied by exactly one
  external library and no local workspace structure. That is a library
  marker, not a kind. (The marker maps the library to an existing kind;
  it does not introduce a new one.)
- Do not name a container kind after a framework concept that does not
  generalise (`fastapi_dependency_injector` is wrong; `provider_resolver`
  is fine if the fingerprint genuinely recurs across DI frameworks).
- Do not add payload constraints that require keyword recognition
  (e.g. "decorator keyword `methods` equals `['GET']`") unless the value
  is a literal whose identity matters structurally. Literal payload is
  fine; semantic interpretation of payload is the exposure layer's job,
  not the compiler's.

## Health metrics for the taxonomy

These are operational checks against fixture drift. They are not
benchmark numbers; they are how to notice when the model is slipping
back into a cascade-by-other-means.

| signal | healthy band | meaning |
|---|---|---|
| Number of container kinds in catalogue | ≤ ~10 after first year | Too many kinds is fixture creep: kinds being added per framework rather than per fingerprint. |
| Frameworks served per kind | ≥ 2 from independent projects | A kind that serves exactly one framework's symbols is a fixture in disguise — either a library marker shaped wrong, or a kind boundary drawn too tight. |
| Contracts per logical role | small and stable | If `binding_surface` ends up needing 8 contracts, the role itself is overloaded (route binding vs DI vs proxy vs shape projection are different mechanisms; the logical layer should expose them separately rather than wrap them). |
| Compiler branches per contract | one structural body, parameterised by kind | A contract with `if container_kind == 'task_register': special` is a smell. Make the kind carry the difference, not the contract. |
| Library marker churn | rare additions, never removals from working markers | Markers shouldn't move. If they do, the kind was misdefined. |

## What this means for the existing cascade

The role cascade and the contract compiler are parallel layers during the
transition. The cascade keeps running; new contracts come up; once a
contract reliably proves a logical role on indexed workspaces, the
corresponding cascade predicates are retired.

There are two transition risks worth naming so they are not surprises:

- **The old benchmark will look like it regresses.** Today's benchmark is
  tuned to the cascade. As predicates retire and contracts take over, old
  numbers move. The honest success signal is *new pack file_recall*
  rising while old pack file_recall holds, not the old number going up.
- **The cascade is a temptation to short-circuit.** A new contract that
  is half-built can always be made to "work" by re-enabling the closest
  cascade predicate. Doing so re-couples the two layers and undoes the
  separation. The discipline is to ship the contract empty if it isn't
  ready and surface a per-bit diagnostic, rather than borrow from the
  cascade for the missing proof.
