# Question structural role profiles

This document intentionally ignores the current implementation. It is a gold
design draft for benchmark questions as structural mechanism signatures.

Scope:

- Source packs:
  - `QA/fixtures/questions_python.yaml`
  - `QA/fixtures/questions_non_python.yaml`
  - `QA/fixtures/new_questions_python.yaml`
- Excluded repos: `dathund`, `surgical_context`.
- Included questions: 116 public-repo questions.

Intent is not modeled here. Intent changes breadth and ordering, but the
structural mechanism that makes a question answerable stays mostly the same.

## Rule of this draft

Each question gets an isolated structural profile:

- `+` means the structural evidence that should close the question.
- `-` means neighbouring structural evidence that must not be used as the
  explanation for this question.
- The signature is allowed to use new roles that do not exist in the project
  today.

The role names below are not framework tables. They describe code shapes:
registration writes, runtime reads, route dispatch, schema emission, value
binding, async bridge, retry signal, and so on.

## Structural vocabulary

| structural role | What it does | How to reach it structurally |
|---|---|---|
| `public_entry` | Public symbol/API used as the user-facing entry. | Re-export, documented public definition, package export, or owner/member API edge. |
| `route_registration_write` | Stores route/path/method/handler data for later dispatch. | AST write/append/call into route table or router registry with handler reference. |
| `route_dispatch_read` | Reads the route table and chooses a handler. | Runtime function iterates/matches route records and invokes selected handler. |
| `middleware_chain_build` | Adds ordered middleware/wrapper functions. | API call appends/wraps callable into an ordered chain. |
| `middleware_chain_execute` | Runs the middleware chain. | Runtime loop calls next wrapper/handler with continuation. |
| `decorator_factory` | Produces a decorator or wrapper function. | Function returns a function expression/callable used as decorator. |
| `metadata_write` | Writes keyed metadata/annotation to a target. | `defineMetadata`/descriptor/custom attribute write with resolved key identity. |
| `metadata_read` | Reads keyed metadata/annotation at runtime. | `getMetadata`/descriptor/custom attribute read with same key identity. |
| `metadata_contract` | Connects a metadata writer to runtime reader. | Writer and reader share resolved key/literal identity. |
| `registry_write` | Stores callback/provider/handler into a registry. | Collection mutation, dict assignment, append, set, or registration call with callback/provider. |
| `registry_read_dispatch` | Reads registry and dispatches to stored entry. | Registry lookup/iteration followed by call/invocation. |
| `argument_binding` | Converts source values into function/handler arguments. | Request/body/query/header/context fields flow into parameter slots. |
| `route_param_binding` | Converts route captures into params/handler args. | Route pattern capture maps to params object or handler arguments. |
| `context_scope_binding` | Resolves scoped/global object from request/app/component context. | Proxy/context stack/current-instance lookup to scoped storage. |
| `dependency_graph_build` | Builds dependency/provider graph from declarations. | Type/metadata/parameter declarations become dependency nodes/edges. |
| `dependency_resolution` | Resolves dependency/provider before invocation. | Runtime traverses dependency graph and injects result into call. |
| `schema_shape_emit` | Builds schema/field/table representation. | Type/model/route metadata flows into schema object or table/field graph. |
| `schema_shape_consume` | Runtime consumes emitted schema/shape. | Validator/serializer/compiler reads schema or shape record. |
| `validation_execute` | Performs validation or transformation. | Runtime call into validator/core/pipe converting untrusted input to typed value. |
| `serialization_execute` | Serializes runtime object to response/output shape. | Runtime call into serializer/encoder/dump path. |
| `error_contract` | Defines/propagates structured errors. | Exception inheritance, error object construction, or typed error payload. |
| `error_to_response_map` | Converts error into response/result. | Error handler registry/dispatcher maps error class/code to response builder. |
| `async_boundary_decision` | Chooses sync vs async execution path. | Branch or adapter based on coroutine/sync callable. |
| `async_compat_bridge` | Adapts sync execution to async runtime or reverse. | Greenlet/thread/await bridge around sync operation. |
| `post_response_executor` | Executes work after response is committed. | Background task queue/collection drained after response path. |
| `external_boundary_call` | Crosses package/process/network/broker/transport boundary. | Filtered external integration call/import followed by data handoff. |
| `broker_publish` | Publishes message/task to broker. | Message envelope construction and external broker producer call. |
| `worker_pool_delegate` | Delegates work to worker pool/process/thread. | Runtime passes task/callable to pool implementation. |
| `scheduler_tick` | Periodic scheduler decides due work. | Tick loop computes due entries and triggers publish/execute. |
| `retry_signal` | Signals retry/requeue path. | Method raises/returns retry exception/state consumed by worker. |
| `timeout_signal` | Enforces timeout by signal/exception. | Timer/process signal produces timeout exception/kill path. |
| `state_shape_normalize` | Maintains normalized state shape. | IDs array plus entity map or equivalent normalized store. |
| `cache_tag_graph` | Maintains cache dependency tags. | Provided/invalidated tags are stored and used to refetch/invalidate. |
| `optimistic_patch_lifecycle` | Applies and rolls back optimistic state patch. | Dispatch update patch before async result, rollback on rejection. |
| `selector_memo_cache` | Memoizes derived state. | Input selectors and cached output comparator/identity gate. |
| `builder_callback_fold` | Folds builder callback declarations into runtime table. | `addCase`/`addMatcher` style calls populate reducer/action matcher registry. |
| `event_listener_install` | Installs runtime event listeners. | Adds listener to window/process/runtime event source. |
| `command_parse_execute` | Parses CLI args and invokes command handler. | Parser builds options/args then calls command callback/handle. |
| `interactive_io` | Reads/writes terminal/user interaction. | Prompt/read/hidden-input/stream call with type conversion. |
| `file_stream_open` | Lazily opens stdin/stdout/file streams. | Parameter conversion chooses stream and open mode at invocation time. |
| `test_runtime_isolation` | Creates isolated test runtime. | Captures stdio/env/filesystem state and restores after command execution. |
| `query_ast_build` | Builds query/statement AST without executing. | Chain/factory constructs query object/Clause/Select tree. |
| `sql_compile` | Converts query/expression AST to SQL. | Compiler visits expression/query nodes into SQL string/bind params. |
| `sql_execute` | Executes compiled SQL or DB operation. | Engine/session/connection execution boundary. |
| `orm_mapper_build` | Maps Python class/object shape to table/columns. | Declarative/table/relationship config becomes mapper metadata. |
| `orm_identity_map` | Tracks object identity/session state. | Session stores instance identity keys and state transitions. |
| `lazy_load_trigger` | Defers DB load until attribute/access path. | Descriptor/loader reads unloaded relation/field and emits query. |
| `transaction_boundary` | Opens/commits/rolls back transaction/savepoint. | Context manager/decorator/session state emits commit/rollback/savepoint calls. |
| `reflection_catalog_read` | Reads DB catalog to build metadata. | Inspector/dialect queries database schema and emits table metadata. |
| `compiler_transform` | Transforms source syntax into runtime representation. | Compiler AST pass rewrites/outputs runtime object/function/IR. |
| `reactive_track` | Tracks dependency reads. | Getter/effect records dependency edge. |
| `reactive_trigger` | Notifies dependents on writes. | Setter/change path invalidates/reruns dependent effects. |
| `vdom_create` | Builds virtual node/component representation. | Runtime creates VNode/component instance from input. |
| `vdom_patch` | Diffs/applies virtual DOM to host DOM. | Patch/mount/update function mutates host nodes. |
| `component_lifecycle_gate` | Coordinates component lifecycle/async readiness. | Lifecycle hooks/async deps decide when to mount/update/resolve. |
| `template_compile` | Compiles template/source to render function. | Parser/compiler creates render function or codegen output. |
| `impact_runtime_fanout` | Runtime code likely affected by a change. | Reverse call/type/use reachability from changed symbol to runtime surfaces. |
| `impact_public_api_fanout` | Public API likely affected by a change. | Reverse reachability to public exports or documented API surfaces. |
| `impact_test_fanout` | Tests likely affected by a change. | Reverse reachability to test files/symbols, marked only as impact partition. |
| `negative_symbol_absence` | Asked symbol does not exist. | Workspace symbol lookup returns no exact target. |
| `nearest_real_mechanism` | Real mechanism closest to nonexistent symbol. | Structural search finds neighbouring real symbols by concept/role/path, without inventing missing symbol. |

Auxiliary roles used in the question profiles:

| structural role | What it does | How to reach it structurally |
|---|---|---|
| `async_lifecycle_action` | Models async request/action lifecycle states. | Factory emits pending/fulfilled/rejected or equivalent state transitions around async execution. |
| `canvas_signature_chain` | Links task signatures into a chain. | Signature object stores next callback/argument propagation edge. |
| `collection_instrumentation` | Manages mapped collection behavior. | Relationship/collection wrapper intercepts append/remove/load operations. |
| `compat_namespace_bridge` | Exposes older API namespace through compatibility layer. | Public module re-exports or delegates to legacy-compatible implementation. |
| `compat_reexport_surface` | Re-export surface specifically for compatibility. | Compatibility package/module exports legacy public symbols. |
| `config_contract` | Carries configuration that controls later runtime behavior. | Typed option object/setting is read by runtime/schema/builder code. |
| `context_manager_lifecycle` | Lifecycle controlled by context manager entry/exit. | `with`/async context manager enter/exit wraps startup/shutdown or transaction path. |
| `data_contract_type` | Structural type or object shape consumed by many paths. | High type/dependency fan-in or schema/model object passed to validators/runtime. |
| `deferred_registration_replay` | Stores registration actions and replays them later. | Deferred function list is populated then consumed during app/module registration. |
| `documentation_surface` | Runtime/API emits documentation-facing output. | Docs route/schema/HTML generation path is reachable from public API. |
| `endpoint_definition_graph` | API endpoint declarations become runtime graph. | Endpoint definitions are stored and consumed by generated middleware/reducer/runtime. |
| `enhancer_composition` | Composes store/application enhancers. | Ordered enhancer functions wrap/create runtime store or app object. |
| `error_prevention_contract` | Type prevents accidental unsafe output. | Representation/serialization hooks mask or block unsafe implicit value exposure. |
| `execution_order_gate` | Enforces relative ordering of runtime pipeline phases. | Runtime sequence invokes phase A before phase B by call order or explicit pipeline list. |
| `explicit_value_unwrap` | Explicit accessor returns protected raw value. | Public method/property unwraps value that implicit serializers hide. |
| `external_dom_target` | Runtime mutates a host DOM target outside normal tree. | Component resolves external DOM element and patches/mounts children there. |
| `fluent_composer` | Builds behavior through chainable self-returning API. | Methods return same receiver type and mutate/build cumulative state. |
| `form_field_binding` | Maps model/schema fields to form fields. | Model field metadata is transformed into form field declarations and validators. |
| `handler_executor` | Invokes final user/runtime handler. | Dispatcher/consumer calls selected callback, endpoint, command, task, or handler. |
| `header_mutation` | Writes HTTP/header metadata. | Response/static/body path calls header setter or mutates header map. |
| `http_route_dispatch` | HTTP-specific request route dispatch. | Method/path route table match selects HTTP handler. |
| `interceptor_wrapper` | Wraps downstream app/handler execution. | Callable receives continuation/next handler and invokes it inside wrapper logic. |
| `invented_symbol_definition` | Forbidden evidence for nonexistent-symbol questions. | Any exact definition of the queried nonexistent symbol would violate negative lookup. |
| `lifecycle_cleanup` | Cleans scoped state at lifecycle end. | Pop/teardown/close path clears context/session/resources. |
| `loader_strategy_selection` | Chooses ORM loading strategy. | Option/config selects loader class/path that changes emitted SQL/loading behavior. |
| `macro_binding_transform` | Compile-time macro becomes runtime binding. | Compiler pass rewrites macro/declaration into setup/binding metadata or return object. |
| `matcher_dispatch` | Dispatches by predicate/matcher rather than exact key. | Runtime iterates registered matchers and invokes matching case. |
| `migration_graph_build` | Builds migration dependency/application graph. | Migration loader/executor creates graph/plan from migration files. |
| `model_class_construction` | Requires constructing/subclassing a model class. | Runtime path depends on class-based model construction rather than arbitrary type adapter. |
| `module_composition` | Module metadata composes imports/providers/controllers. | Object/decorator metadata lists module parts and compiler/container consumes them. |
| `mount_path_composition` | Composes mounted sub-app/router under path prefix. | Middleware/router registration stores mount path and nested app/router handler. |
| `object_property_factory_bridge` | Returned object property points to factory/callee. | Return object property initializer resolves to factory used by later property call. |
| `observable_stream_chain` | Wraps execution in observable/stream pipeline. | Handler result is converted to stream and operators/wrappers compose around it. |
| `protocol_handshake` | Accepts or rejects protocol-level connection start. | WebSocket/RPC/transport handshake call transitions connection state. |
| `prototype_api_surface` | Public API attached to prototype/object facade. | Prototype/object members form exported user-facing methods. |
| `provide_inject_scope` | Resolves dependency through component parent/provide chain. | Current instance parent chain or provides map is traversed to value. |
| `provider_registry` | Stores providers/classes/tokens for dependency container. | Module/container registry records provider token to factory/value/class. |
| `proxy_resolution` | Proxy object resolves real current object. | Proxy getter/call dereferences scoped storage/current object. |
| `relationship_mapper` | Maps ORM relationship between object types. | Mapper property links foreign keys, target mapper, loader, and collection behavior. |
| `response_lifecycle_boundary` | Marks boundary before/after response send. | Code path attaches/drains work or mutates response at send/finalize phase. |
| `runtime_context_stack` | Maintains stack/current runtime context. | Push/pop/current lookup edges define scoped context lifetime. |
| `runtime_package_partition` | Separates runtime packages from docs/examples/support packages. | Package/module graph partitions runtime imports from docs/example/test surfaces. |
| `security_scope_contract` | Carries security scopes/authorization contract. | Security dependency reads declared scopes and validates credentials against them. |
| `security_signing` | Signs/verifies value for integrity. | Serializer calls signer/verifier around stored cookie/token/session data. |
| `serialization_guard` | Prevents unsafe implicit serialization/logging. | Repr/dump/serializer masks or blocks protected value unless explicitly unwrapped. |
| `shape_projector` | Projects multiple source fields into output object/shape. | Attribute/field reads populate mapping or constructed object. |
| `single_symbol_mechanism` | Forbidden evidence for broad package-partition questions. | Treating a package-level question as one target-symbol mechanism is not sufficient. |
| `state_read_projection` | Derives read-only state view from input state. | Selector/projection reads input state and returns derived value. |
| `state_tracking` | Records durable runtime/application state. | Recorder/store/session persists progress or applied state. |
| `state_transition_reducer` | Transforms state in response to action/event. | Reducer/action handler consumes action and returns/mutates next state. |
| `static_asset_response` | Serves static documentation/asset response. | Route or middleware returns static HTML/assets with response headers/body. |
| `stream_body_parse` | Parses incoming request stream into body value. | Middleware buffers stream and writes parsed body field. |
| `supporting_surface_partition` | Identifies docs/examples/supporting surfaces. | Package/path role partition marks non-runtime support areas, not semantic roles. |
| `template_render_context` | Builds context passed to template engine. | Locals/processors/options merge into template render call. |
| `time_window_decision` | Decides due/eligible work from time window. | Scheduler compares clock against interval/ETA/deadline and gates execution. |
| `transaction_safe_update` | Performs update atomically inside database expression. | Expression compiles into DB-side update without Python read-modify-write. |
| `transport_route_registration` | Registers non-HTTP transport message route. | Metadata/registry stores pattern/handler for RPC/message transport server. |
| `validation_phase_order` | Distinguishes before/after validation phases. | Metadata or call order invokes validator before raw parsing or after model construction. |
| `weak_reference_lifecycle` | Manages weak receiver references. | Registry stores weakref and cleans/skips dead receivers at dispatch. |

## Question profiles

### FastAPI

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `fastapi_q01` | path operation becomes `APIRoute` and is registered | `public_entry`, `route_registration_write`, `schema_shape_emit`, `route_dispatch_read` | `dependency_resolution`, `serialization_execute` | Close on app/decorator API creating route object, writing it to router, and runtime router consuming it. |
| `fastapi_q02` | `Depends` resolved before endpoint call | `public_entry`, `dependency_graph_build`, `dependency_resolution`, `argument_binding` | `route_registration_write`, `schema_shape_emit` | Close on dependency marker becoming graph node, solver traversal, and injected values entering endpoint args. |
| `fastapi_q03` | sync vs async endpoint decision | `async_boundary_decision`, `handler_executor`, `route_dispatch_read` | `dependency_graph_build`, `schema_shape_emit` | Close on runtime branch choosing await/direct/thread path and invoking endpoint. |
| `fastapi_q04` | request body models validated into endpoint params | `schema_shape_consume`, `validation_execute`, `argument_binding`, `dependency_resolution` | `route_registration_write`, `serialization_execute` | Close on body schema/field use, validator call, and validated values mapped to args. |
| `fastapi_q05` | OpenAPI generated for registered routes | `schema_shape_emit`, `route_dispatch_read`, `public_entry`, `documentation_surface` | `handler_executor`, `dependency_resolution` | Close on traversal of route metadata into OpenAPI schema and docs-facing output. |
| `fastapi_q06` | response serialization impact | `impact_runtime_fanout`, `impact_public_api_fanout`, `impact_test_fanout`, `serialization_execute` | `route_registration_write` | Close on reverse reachability from serializer to runtime routes, public API, and tests. |
| `fastapi_q07` | docs/redoc routes wired into app | `public_entry`, `route_registration_write`, `documentation_surface`, `static_asset_response` | `dependency_resolution`, `validation_execute` | Close on docs HTML builders and route registration for docs endpoints. |
| `fastapi_q08` | nonexistent `RouteContext` nearest real mechanism | `negative_symbol_absence`, `nearest_real_mechanism`, `route_dispatch_read`, `dependency_graph_build` | `invented_symbol_definition` | Close by proving symbol absence, then pointing to real route/dependency context structures. |
| `fastapi_new_q01` | validation errors become 422 responses | `error_contract`, `error_to_response_map`, `serialization_execute`, `public_entry` | `route_registration_write`, `post_response_executor` | Close on validation exception class, registered handler, and response payload/status builder. |
| `fastapi_new_q02` | background tasks after response | `post_response_executor`, `registry_write`, `handler_executor`, `response_lifecycle_boundary` | `async_boundary_decision`, `error_to_response_map` | Close on task collection, response attachment, and drain after response send. |
| `fastapi_new_q03` | lifespan replaces startup/shutdown | `context_manager_lifecycle`, `registry_write`, `component_lifecycle_gate`, `public_entry` | `route_dispatch_read`, `post_response_executor` | Close on async context manager registered as app lifecycle and consumed by startup/shutdown runtime. |
| `fastapi_new_q04` | `BaseHTTPMiddleware` execution order | `middleware_chain_build`, `middleware_chain_execute`, `async_boundary_decision`, `interceptor_wrapper` | `route_registration_write`, `dependency_resolution` | Close on middleware wrapper chain and call-next style execution around ASGI app. |
| `fastapi_new_q05` | WebSocket handshake and disconnect | `protocol_handshake`, `handler_executor`, `error_contract`, `route_dispatch_read` | `serialization_execute`, `post_response_executor` | Close on websocket accept/receive path, disconnect exception, and route handler invocation. |
| `fastapi_new_q06` | OAuth token header extraction with Depends | `argument_binding`, `dependency_graph_build`, `validation_execute`, `security_scope_contract` | `route_registration_write`, `schema_shape_emit` | Close on security dependency reading header, validating scheme/scopes, and binding token into dependency result. |

### Pydantic

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `pydantic_q01` | `BaseModel` validation flow | `public_entry`, `schema_shape_consume`, `validation_execute`, `data_contract_type` | `serialization_execute`, `schema_shape_emit` | Close on model construction/validate API invoking core validator over model schema. |
| `pydantic_q02` | Python wrapper vs pydantic-core | `public_entry`, `external_boundary_call`, `validation_execute`, `serialization_execute` | `route_dispatch_read`, `registry_write` | Close on Python API delegating across core boundary for validation/serialization. |
| `pydantic_q03` | `model_dump()` serialization | `public_entry`, `serialization_execute`, `schema_shape_consume` | `validation_execute`, `schema_shape_emit` | Close on high-level dump API calling serializer bound to model schema. |
| `pydantic_q04` | JSON schema generation | `public_entry`, `schema_shape_emit`, `data_contract_type` | `validation_execute`, `serialization_execute` | Close on model/type schema traversal into JSON schema output. |
| `pydantic_q05` | Pydantic v1 compatibility | `public_entry`, `compat_namespace_bridge`, `compat_reexport_surface` | `validation_execute`, `schema_shape_emit` | Close on compatibility namespace/export layer pointing to legacy API surface. |
| `pydantic_q06` | alias handling impact | `impact_runtime_fanout`, `impact_public_api_fanout`, `impact_test_fanout`, `argument_binding` | `route_dispatch_read` | Close on alias config/field reachability to runtime consumers, public API, and tests. |
| `pydantic_q07` | validation error final structure | `error_contract`, `validation_execute`, `schema_shape_consume`, `serialization_execute` | `route_registration_write` | Close on validator errors assembled into structured error object/output. |
| `pydantic_q08` | nonexistent `SchemaRouter` nearest schema mechanism | `negative_symbol_absence`, `nearest_real_mechanism`, `schema_shape_emit`, `data_contract_type` | `invented_symbol_definition` | Close by proving absence and pointing to real schema generator/coordinator. |
| `pydantic_new_q01` | `field_validator` hooks into core validation | `decorator_factory`, `metadata_write`, `metadata_contract`, `validation_execute` | `serialization_execute`, `schema_shape_emit` | Close on decorator storing validator metadata and schema builder/core consuming it. |
| `pydantic_new_q02` | `computed_field` during dump | `decorator_factory`, `metadata_write`, `metadata_contract`, `serialization_execute` | `validation_execute`, `route_dispatch_read` | Close on computed field metadata captured and serializer reading it during dump. |
| `pydantic_new_q03` | `ConfigDict(strict=True)` to core schema | `config_contract`, `schema_shape_emit`, `external_boundary_call`, `validation_execute` | `serialization_execute`, `registry_write` | Close on strict config flowing into generated core schema and validator behavior. |
| `pydantic_new_q04` | secrets prevent accidental logging | `data_contract_type`, `serialization_guard`, `explicit_value_unwrap`, `error_prevention_contract` | `validation_execute`, `schema_shape_emit` | Close on repr/serialization masking plus explicit accessor returning raw value. |
| `pydantic_new_q05` | `TypeAdapter` without `BaseModel` | `public_entry`, `schema_shape_emit`, `validation_execute`, `external_boundary_call` | `model_class_construction`, `serialization_execute` | Close on arbitrary type schema generation and core validation without model subclass. |
| `pydantic_new_q06` | model validator before vs after | `decorator_factory`, `metadata_write`, `validation_phase_order`, `validation_execute` | `serialization_execute`, `schema_shape_emit` | Close on mode metadata causing validator invocation before raw parsing or after model construction. |

### Redux Toolkit

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `rtk_q01` | `createSlice` reducers to actions/final reducer | `builder_callback_fold`, `registry_write`, `state_transition_reducer`, `public_entry` | `cache_tag_graph`, `async_lifecycle_action` | Close on reducer definitions producing action creators and reducer dispatch table. |
| `rtk_q02` | `configureStore` middleware/enhancers/devtools | `middleware_chain_build`, `enhancer_composition`, `config_contract`, `public_entry` | `cache_tag_graph`, `state_shape_normalize` | Close on store config assembling middleware and enhancer chain. |
| `rtk_q03` | `createAsyncThunk` lifecycle actions | `async_lifecycle_action`, `registry_write`, `handler_executor`, `state_transition_reducer` | `cache_tag_graph`, `selector_memo_cache` | Close on pending/fulfilled/rejected action creators and thunk execution path. |
| `rtk_q04` | RTK Query API slice and endpoints | `endpoint_definition_graph`, `cache_tag_graph`, `middleware_chain_build`, `state_transition_reducer` | `selector_memo_cache`, `state_shape_normalize` | Close on endpoint declarations generating API runtime reducer/middleware hooks. |
| `rtk_q05` | generated action type format impact | `impact_runtime_fanout`, `impact_public_api_fanout`, `impact_test_fanout`, `builder_callback_fold` | `cache_tag_graph` | Close on reverse reachability from action type format to reducers, exported API, and tests. |
| `rtk_q06` | listener middleware side effects | `event_listener_install`, `registry_write`, `handler_executor`, `state_transition_reducer` | `cache_tag_graph`, `selector_memo_cache` | Close on listener registration, action interception, and effect execution. |
| `rtk_q07` | monorepo runtime vs docs/examples | `public_entry`, `runtime_package_partition`, `documentation_surface`, `supporting_surface_partition` | `single_symbol_mechanism` | Close on package-level structural partitioning, not one mechanism path. |
| `rtk_q08` | nonexistent `SliceRouter` nearest mechanism | `negative_symbol_absence`, `nearest_real_mechanism`, `builder_callback_fold`, `state_transition_reducer` | `invented_symbol_definition` | Close by proving absence and mapping to real slice composition/reducer mechanism. |
| `rtk_new_q01` | entity adapter normalized state | `state_shape_normalize`, `state_transition_reducer`, `public_entry`, `builder_callback_fold` | `cache_tag_graph`, `selector_memo_cache` | Close on IDs plus entity map shape and adapter mutators. |
| `rtk_new_q02` | tags invalidate cache | `cache_tag_graph`, `registry_write`, `state_transition_reducer`, `endpoint_definition_graph` | `state_shape_normalize`, `selector_memo_cache` | Close on provides/invalidates tag records driving cache invalidation/refetch. |
| `rtk_new_q03` | optimistic updates | `optimistic_patch_lifecycle`, `async_lifecycle_action`, `state_transition_reducer`, `handler_executor` | `selector_memo_cache`, `state_shape_normalize` | Close on update patch dispatch before query result and rollback/settle path. |
| `rtk_new_q04` | selector memoization | `selector_memo_cache`, `public_entry`, `state_read_projection` | `state_transition_reducer`, `cache_tag_graph` | Close on input selectors plus cached derived result preventing recomputation. |
| `rtk_new_q05` | `extraReducers` builder callback | `builder_callback_fold`, `registry_write`, `state_transition_reducer`, `matcher_dispatch` | `cache_tag_graph`, `selector_memo_cache` | Close on `addCase`/`addMatcher` declarations folded into reducer table. |
| `rtk_new_q06` | setupListeners focus/reconnect | `event_listener_install`, `cache_tag_graph`, `handler_executor`, `external_boundary_call` | `state_shape_normalize`, `selector_memo_cache` | Close on window/network listeners dispatching refetch-related actions. |

### Django

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `django_q01` | WSGI through middleware to view | `public_entry`, `middleware_chain_build`, `middleware_chain_execute`, `route_dispatch_read` | `schema_shape_emit`, `orm_mapper_build` | Close on WSGI app entry building middleware chain and dispatching to view. |
| `django_q02` | QuerySet lazy evaluation | `query_ast_build`, `lazy_load_trigger`, `sql_compile`, `sql_execute` | `form_field_binding`, `route_dispatch_read` | Close on chained query object held until evaluation triggers compiler/execution. |
| `django_q03` | ModelForm fields and validation | `form_field_binding`, `schema_shape_emit`, `validation_execute`, `argument_binding` | `route_dispatch_read`, `transaction_boundary` | Close on model fields converted to form fields and form validation path. |
| `django_q04` | migrations apply/track schema changes | `migration_graph_build`, `schema_shape_emit`, `sql_execute`, `state_tracking` | `route_dispatch_read`, `form_field_binding` | Close on migration graph/plans, operation execution, and recorder state. |
| `django_q05` | ORM descriptor impact | `impact_runtime_fanout`, `impact_public_api_fanout`, `impact_test_fanout`, `lazy_load_trigger` | `route_dispatch_read` | Close on descriptor reverse reachability to ORM runtime/public API/tests. |
| `django_new_q01` | `F()` expressions to atomic SQL | `query_ast_build`, `sql_compile`, `transaction_safe_update`, `public_entry` | `lazy_load_trigger`, `form_field_binding` | Close on expression AST compiled into SQL update without Python read-modify-write. |
| `django_new_q02` | signals dispatch and weak refs | `registry_write`, `registry_read_dispatch`, `weak_reference_lifecycle`, `handler_executor` | `route_dispatch_read`, `middleware_chain_execute` | Close on receiver registration, weakref handling, and synchronous send loop. |
| `django_new_q03` | management command parse and handle | `command_parse_execute`, `config_contract`, `handler_executor`, `public_entry` | `route_dispatch_read`, `query_ast_build` | Close on argparse parser construction, command execution, and `handle` invocation. |
| `django_new_q04` | GenericForeignKey resolution | `orm_mapper_build`, `argument_binding`, `lazy_load_trigger`, `data_contract_type` | `form_field_binding`, `route_dispatch_read` | Close on content type id/object id resolving target model/object. |
| `django_new_q05` | active user model lookup | `config_contract`, `registry_read_dispatch`, `data_contract_type`, `public_entry` | `route_dispatch_read`, `sql_compile` | Close on setting value used to look up model in app registry. |
| `django_new_q06` | atomic/savepoint transaction wrapping | `transaction_boundary`, `handler_executor`, `sql_execute`, `error_contract` | `route_dispatch_read`, `form_field_binding` | Close on decorator/context manager opening savepoint and commit/rollback path. |

### Flask

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `flask_q01` | app factory and request context | `public_entry`, `context_scope_binding`, `route_registration_write`, `route_dispatch_read` | `middleware_chain_build`, `schema_shape_emit` | Close on app construction plus context push/pop and request dispatch. |
| `flask_q02` | Blueprint deferred registration | `route_registration_write`, `registry_write`, `deferred_registration_replay`, `public_entry` | `context_scope_binding`, `serialization_execute` | Close on blueprint storing deferred functions and replaying them into app. |
| `flask_q03` | `request` local proxy | `context_scope_binding`, `public_entry`, `proxy_resolution`, `runtime_context_stack` | `route_registration_write`, `error_to_response_map` | Close on proxy object resolving current request from context storage. |
| `flask_q04` | before/after request decorators | `decorator_factory`, `registry_write`, `registry_read_dispatch`, `handler_executor` | `route_param_binding`, `schema_shape_emit` | Close on decorator registration and request lifecycle consuming callbacks. |
| `flask_q05` | routing dispatch impact | `impact_runtime_fanout`, `impact_public_api_fanout`, `impact_test_fanout`, `route_dispatch_read` | `serialization_execute` | Close on reverse reachability from routing machinery to runtime/public/tests. |
| `flask_new_q01` | signed client-side session cookie | `context_scope_binding`, `serialization_execute`, `security_signing`, `response_lifecycle_boundary` | `route_registration_write`, `error_to_response_map` | Close on session proxy, open/save session, serializer/signature, and cookie response write. |
| `flask_new_q02` | `g` scope and clearing | `context_scope_binding`, `runtime_context_stack`, `lifecycle_cleanup`, `public_entry` | `serialization_execute`, `route_registration_write` | Close on app/request context local storage and teardown/pop cleanup. |
| `flask_new_q03` | errorhandler maps codes/classes | `decorator_factory`, `registry_write`, `error_to_response_map`, `error_contract` | `route_param_binding`, `context_scope_binding` | Close on handler registration keyed by code/class and runtime exception dispatch. |
| `flask_new_q04` | context processors into templates | `decorator_factory`, `registry_write`, `argument_binding`, `template_render_context` | `route_dispatch_read`, `error_to_response_map` | Close on processor registry and template context merge before render. |
| `flask_new_q05` | URL converters parse route segments | `route_param_binding`, `validation_execute`, `route_dispatch_read`, `data_contract_type` | `context_scope_binding`, `serialization_execute` | Close on converter object parsing captured segment into typed view arg. |
| `flask_new_q06` | Click CLI command registration | `command_parse_execute`, `registry_write`, `context_scope_binding`, `external_boundary_call` | `route_dispatch_read`, `template_render_context` | Close on app CLI group registering commands and injecting app context. |

### Express

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `express_q01` | create app and delegate to router | `public_entry`, `route_dispatch_read`, `middleware_chain_execute`, `prototype_api_surface` | `serialization_execute`, `template_render_context` | Close on app object facade delegating request handling to router. |
| `express_q02` | `app.use()` middleware registration | `middleware_chain_build`, `route_registration_write`, `middleware_chain_execute`, `public_entry` | `template_render_context`, `serialization_execute` | Close on `use` storing middleware and router executing ordered stack. |
| `express_q03` | Router and sub-app mounting | `public_entry`, `route_registration_write`, `mount_path_composition`, `middleware_chain_build` | `serialization_execute`, `template_render_context` | Close on Router factory and mounted app/router composition. |
| `express_q04` | response methods chaining | `public_entry`, `serialization_execute`, `header_mutation`, `fluent_composer` | `route_registration_write`, `middleware_chain_build` | Close on response prototype methods writing body/headers and returning response for chaining. |
| `express_new_q01` | `express.json()` parses req.body | `middleware_chain_build`, `stream_body_parse`, `argument_binding`, `external_boundary_call` | `route_param_binding`, `template_render_context` | Close on middleware buffering stream, parsing JSON, and writing body field. |
| `express_new_q02` | static file middleware | `middleware_chain_build`, `file_stream_open`, `header_mutation`, `external_boundary_call` | `route_param_binding`, `template_render_context` | Close on static middleware resolving file path and setting cache headers. |
| `express_new_q03` | route params to `req.params` | `route_param_binding`, `route_dispatch_read`, `data_contract_type`, `middleware_chain_execute` | `stream_body_parse`, `template_render_context` | Close on route layer pattern match extracting captures into params object. |
| `express_new_q04` | `res.locals` into render | `template_render_context`, `argument_binding`, `serialization_execute`, `data_contract_type` | `route_param_binding`, `stream_body_parse` | Close on locals object merged into render options passed to template engine. |
| `express_new_q05` | `next(err)` error layer iteration | `middleware_chain_execute`, `error_contract`, `error_to_response_map`, `handler_executor` | `template_render_context`, `stream_body_parse` | Close on error argument selecting error handlers while skipping normal layers. |
| `express_new_q06` | view engine resolution/compile | `registry_write`, `config_contract`, `template_render_context`, `external_boundary_call` | `route_param_binding`, `middleware_chain_execute` | Close on engine registration/config and view lookup/render call. |

### NestJS

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `nestjs_q01` | controller decorators map routes | `decorator_factory`, `metadata_write`, `metadata_contract`, `route_dispatch_read` | `dependency_resolution`, `error_to_response_map` | Close on decorator metadata keys written and router explorer reading same keys. |
| `nestjs_q02` | DI container resolve/inject | `metadata_read`, `dependency_graph_build`, `dependency_resolution`, `provider_registry` | `route_dispatch_read`, `error_to_response_map` | Close on provider/module metadata forming graph and injector resolving constructor args. |
| `nestjs_q03` | module decorators compose features | `decorator_factory`, `metadata_write`, `module_composition`, `provider_registry` | `route_dispatch_read`, `validation_execute` | Close on module metadata listing imports/controllers/providers consumed by scanner/container. |
| `nestjs_q04` | pipes validation/transformation | `metadata_contract`, `validation_execute`, `argument_binding`, `handler_executor` | `provider_registry`, `route_registration_write` | Close on pipe metadata/context and runtime pipe consumer transforming arguments. |
| `nestjs_new_q01` | exception filters map HTTP exceptions | `decorator_factory`, `metadata_write`, `error_to_response_map`, `error_contract` | `route_param_binding`, `dependency_resolution` | Close on filter metadata and exception filter runtime converting error to response. |
| `nestjs_new_q02` | guards before interceptors/pipes | `metadata_read`, `execution_order_gate`, `handler_executor`, `dependency_resolution` | `error_to_response_map`, `route_param_binding` | Close on context creators/consumers ordering guard checks before later pipeline stages. |
| `nestjs_new_q03` | interceptors wrap CallHandler stream | `interceptor_wrapper`, `observable_stream_chain`, `handler_executor`, `metadata_read` | `validation_execute`, `error_to_response_map` | Close on interceptor consumer passing CallHandler observable and wrapping execution stream. |
| `nestjs_new_q04` | custom param decorator injects args | `decorator_factory`, `metadata_write`, `route_param_binding`, `argument_binding` | `provider_registry`, `error_to_response_map` | Close on param metadata recorded and route params factory applying it to args. |
| `nestjs_new_q05` | DynamicModule forRoot/register | `module_composition`, `provider_registry`, `config_contract`, `dependency_graph_build` | `route_dispatch_read`, `error_to_response_map` | Close on dynamic module object carrying providers/imports consumed by compiler/container. |
| `nestjs_new_q06` | MessagePattern RPC routes | `decorator_factory`, `metadata_write`, `transport_route_registration`, `external_boundary_call` | `http_route_dispatch`, `validation_execute` | Close on message pattern metadata consumed by microservice server transport. |

### SQLAlchemy

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `sqlalchemy_q01` | declarative base maps classes to tables | `public_entry`, `orm_mapper_build`, `data_contract_type`, `registry_write` | `query_ast_build`, `transaction_boundary` | Close on declarative class/table metadata producing mapper registry. |
| `sqlalchemy_q02` | Query builder lazy SQL | `query_ast_build`, `lazy_load_trigger`, `sql_compile`, `sql_execute` | `orm_identity_map`, `relationship_mapper` | Close on query chain object compiling/executing only at evaluation. |
| `sqlalchemy_q03` | Session identity/lazy/transactions | `orm_identity_map`, `lazy_load_trigger`, `transaction_boundary`, `sql_execute` | `schema_shape_emit`, `event_listener_install` | Close on session state tracking, lazy loaders, and transaction execution. |
| `sqlalchemy_q04` | relationships FK and collections | `relationship_mapper`, `route_param_binding`, `lazy_load_trigger`, `collection_instrumentation` | `query_ast_build`, `reflection_catalog_read` | Close on relationship property resolving FKs and managing related collection loaders. |
| `sqlalchemy_new_q01` | `select()` statement compilation | `query_ast_build`, `sql_compile`, `sql_execute`, `public_entry` | `orm_identity_map`, `relationship_mapper` | Close on Select object replacing Query as statement AST and execution input. |
| `sqlalchemy_new_q02` | selectinload vs joinedload | `loader_strategy_selection`, `lazy_load_trigger`, `sql_execute`, `relationship_mapper` | `reflection_catalog_read`, `transaction_boundary` | Close on loader option selecting secondary query strategy vs join strategy. |
| `sqlalchemy_new_q03` | event listener decorator | `decorator_factory`, `event_listener_install`, `registry_write`, `registry_read_dispatch` | `query_ast_build`, `reflection_catalog_read` | Close on listens_for/listen storing listener and dispatch invoking it. |
| `sqlalchemy_new_q04` | table reflection from DB catalog | `reflection_catalog_read`, `schema_shape_emit`, `external_boundary_call`, `orm_mapper_build` | `query_ast_build`, `event_listener_install` | Close on inspector/dialect reading catalog and emitting table metadata. |
| `sqlalchemy_new_q05` | AsyncSession sync greenlet bridge | `async_compat_bridge`, `sql_execute`, `transaction_boundary`, `public_entry` | `reflection_catalog_read`, `event_listener_install` | Close on async API delegating through greenlet/sync session execution path. |
| `sqlalchemy_new_q06` | composite maps columns to object | `relationship_mapper`, `shape_projector`, `data_contract_type`, `orm_mapper_build` | `reflection_catalog_read`, `event_listener_install` | Close on multiple columns projected into one composite Python object property. |

### Vue Core

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `vue_q01` | `createApp` initialize and mount | `public_entry`, `object_property_factory_bridge`, `vdom_create`, `vdom_patch` | `template_compile`, `reactive_trigger` | Close on runtime-dom entry resolving renderer createApp and mount patching host DOM. |
| `vue_q02` | Ref tracks dependencies | `public_entry`, `reactive_track`, `reactive_trigger`, `data_contract_type` | `vdom_patch`, `template_compile` | Close on ref getter tracking effects and setter triggering them. |
| `vue_q03` | render compile templates and update DOM | `template_compile`, `vdom_create`, `vdom_patch`, `component_lifecycle_gate` | `reactive_track`, `provide_inject_scope` | Close on compile-to-render plus renderer patch path. If target is raw `render`, template compile is a separate structural branch. |
| `vue_q04` | watch setup and effects | `reactive_track`, `reactive_trigger`, `handler_executor`, `component_lifecycle_gate` | `template_compile`, `vdom_patch` | Close on watcher effect collecting dependencies and executing callback/scheduler. |
| `vue_new_q01` | SFC script setup macro bindings | `compiler_transform`, `macro_binding_transform`, `data_contract_type`, `public_entry` | `vdom_patch`, `reactive_trigger` | Close on compiler pass rewriting macros into setup return/binding metadata. |
| `vue_new_q02` | provide/inject parent chain | `provide_inject_scope`, `context_scope_binding`, `dependency_resolution`, `component_lifecycle_gate` | `template_compile`, `vdom_patch` | Close on current instance parent chain lookup and provides map resolution. |
| `vue_new_q03` | computed cached re-evaluation | `reactive_track`, `reactive_trigger`, `selector_memo_cache`, `handler_executor` | `template_compile`, `vdom_patch` | Close on dirty flag/dependency tracking and cached value invalidation. |
| `vue_new_q04` | Teleport moves children to target DOM | `vdom_create`, `vdom_patch`, `external_dom_target`, `component_lifecycle_gate` | `template_compile`, `provide_inject_scope` | Close on Teleport process/mount moving children to resolved target. |
| `vue_new_q05` | Suspense async dependencies | `component_lifecycle_gate`, `async_boundary_decision`, `handler_executor`, `vdom_patch` | `template_compile`, `provide_inject_scope` | Close on async dep registration, fallback/default switch, and resolve path. |
| `vue_new_q06` | custom directives lifecycle hooks | `metadata_write`, `vdom_create`, `component_lifecycle_gate`, `handler_executor` | `template_compile`, `provide_inject_scope` | Close on directive bindings attached to vnode and hooks invoked during mount/update. |

### Click

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `click_new_q01` | interactive prompt | `interactive_io`, `validation_execute`, `argument_binding`, `public_entry` | `file_stream_open`, `test_runtime_isolation` | Close on prompt IO, hidden input branch, and type conversion. |
| `click_new_q02` | File parameter lazy open | `file_stream_open`, `argument_binding`, `validation_execute`, `public_entry` | `interactive_io`, `test_runtime_isolation` | Close on parameter conversion choosing stdin/stdout or lazy file open. |
| `click_new_q03` | pass current Context decorator | `decorator_factory`, `context_scope_binding`, `argument_binding`, `handler_executor` | `file_stream_open`, `interactive_io` | Close on wrapper fetching current context and injecting it as first arg. |
| `click_new_q04` | CliRunner test isolation | `test_runtime_isolation`, `command_parse_execute`, `interactive_io`, `handler_executor` | `file_stream_open`, `decorator_factory` | Close on isolated env/stdio and command invocation result capture. |
| `click_new_q05` | lazy dynamic subcommands | `command_parse_execute`, `registry_read_dispatch`, `lazy_load_trigger`, `public_entry` | `interactive_io`, `test_runtime_isolation` | Close on group resolving command name dynamically before invocation. |
| `click_new_q06` | Argument vs Option parsing | `command_parse_execute`, `argument_binding`, `config_contract`, `validation_execute` | `interactive_io`, `test_runtime_isolation` | Close on parser assigning positional args vs option flags into command params. |

### Celery

| id | focus | + required structural roles | - isolation guards | Structural closure recipe |
|---|---|---|---|---|
| `celery_new_q01` | task retry requeues with ETA | `retry_signal`, `error_contract`, `broker_publish`, `worker_pool_delegate` | `scheduler_tick`, `canvas_signature_chain` | Close on task retry raising/signal state consumed by worker and routed back to broker. |
| `celery_new_q02` | routes evaluated before publish | `config_contract`, `route_dispatch_read`, `broker_publish`, `argument_binding` | `scheduler_tick`, `worker_pool_delegate` | Close on route config lookup producing queue/exchange/routing key for publish. |
| `celery_new_q03` | canvas chain links results | `canvas_signature_chain`, `argument_binding`, `broker_publish`, `data_contract_type` | `scheduler_tick`, `timeout_signal` | Close on signatures carrying previous result into next task args and publish path. |
| `celery_new_q04` | worker delegates to pool | `worker_pool_delegate`, `handler_executor`, `external_boundary_call`, `async_boundary_decision` | `scheduler_tick`, `canvas_signature_chain` | Close on worker passing task execution to prefork/gevent/etc pool implementation. |
| `celery_new_q05` | beat schedules periodic tasks | `scheduler_tick`, `config_contract`, `broker_publish`, `time_window_decision` | `worker_pool_delegate`, `retry_signal` | Close on periodic schedule tick deciding due entry and sending task to broker. |
| `celery_new_q06` | soft time limit enforcement | `timeout_signal`, `error_contract`, `worker_pool_delegate`, `external_boundary_call` | `scheduler_tick`, `canvas_signature_chain` | Close on worker/pool timeout signal causing soft limit exception path. |

## Matrix isolation check

At this stage, isolation is conceptual, not computed by the current engine.
Every row above is intended to have a unique signature when both `+` and `-`
columns are considered.

Examples:

| mechanism | + shape | - guards |
|---|---|---|
| FastAPI route registration | `route_registration_write`, `schema_shape_emit`, `route_dispatch_read` | no `dependency_resolution`, no `serialization_execute` |
| FastAPI dependency resolution | `dependency_graph_build`, `dependency_resolution`, `argument_binding` | no `route_registration_write`, no `schema_shape_emit` |
| NestJS decorator routing | `decorator_factory`, `metadata_write`, `metadata_contract`, `route_dispatch_read` | no `dependency_resolution`, no `error_to_response_map` |
| NestJS DI resolution | `metadata_read`, `dependency_graph_build`, `dependency_resolution`, `provider_registry` | no `route_dispatch_read`, no `error_to_response_map` |
| Express body parser | `middleware_chain_build`, `stream_body_parse`, `argument_binding` | no `route_param_binding`, no `template_render_context` |
| Express route params | `route_param_binding`, `route_dispatch_read`, `data_contract_type` | no `stream_body_parse`, no `template_render_context` |

If two rows collapse to the same signature during implementation, do not fix that
by a framework name or a threshold. Add a missing structural dimension or an
explicit negative guard.

## Next step

The next practical artifact should be a machine-readable schema:

```yaml
question_id:
  positive_structural_roles: [...]
  negative_structural_guards: [...]
  closure_recipe: ...
```

Only after that should any of these names be mapped onto current project roles or
engine predicates.
