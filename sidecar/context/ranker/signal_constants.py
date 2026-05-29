"""Shared constants for UnifiedRanker scoring, recovery, and pruning heuristics."""

NOISE_PATH_PATTERNS = (
    "/tests/",
    "/test/",
    "/test_",
    "-test/",
    "/__tests__/",
    ".spec.",
    ".test.",
    "/docs_src/",
    "/docs/virtual/",
    "/examples/",
    "/example/",
    "/benchmarks/",
    "/types/test/",
    "/__testfixtures__/",
    "/testfixtures/",
    "/codemods/",
)
NOISE_NAME_PREFIXES = ("test_",)
NOISE_NAME_SUBSTRINGS = ("tutorial",)
NOISE_FACTOR = 0.15
EXPLORATION_NOISE_FACTOR = 0.3
FACTORY_SIGNAL_PREFIXES = (
    "build",
    "create",
    "configure",
    "compose",
    "combine",
    "register",
    "include",
    "mount",
    "setup",
    "add_",
    "add",
)
FACTORY_SIGNAL_TOKENS = (
    "factory",
    "builder",
    "route",
    "router",
    "openapi",
    "schema",
)
FACTORY_SIGNAL_PATH_TOKENS = (
    "/routing",
    "/routes",
    "/router",
    "/openapi",
    "/application",
    "/applications",
)
REPRESENTATION_SIGNAL_TOKENS = (
    "schema",
    "model",
    "response",
    "request",
    "payload",
    "json",
    "field",
    "serialize",
    "parse",
    "param",
    "params",
    "depend",
    "dependent",
    "dependant",
    "dependency",
    "annotation",
    "typing",
)
REPRESENTATION_SIGNAL_PATH_TOKENS = (
    "/dependencies/",
    "/params",
    "/param_",
    "/models",
    "/fields",
)
TRACE_DEPENDENCY_TARGET_TOKENS = (
    "depends",
    "dependent",
    "dependant",
    "dependency",
    "dependencies",
    "inject",
    "provider",
    "container",
)
TRACE_DEPENDENCY_RUNTIME_NAME_TOKENS = (
    "dependent",
    "dependant",
    "dependency",
    "dependencies",
    "inject",
    "provider",
    "container",
    "resolve",
    "solve",
)
TRACE_HOOK_RUNTIME_TRIGGER_NAMES: frozenset[str] = frozenset(
    {
        "before_request",
        "after_request",
        "teardown_request",
        "wsgi_app",
        "dispatch_request",
    }
)
TRACE_HOOK_RUNTIME_NAMES: frozenset[str] = frozenset(
    {
        "before_request",
        "after_request",
        "before_app_request",
        "after_app_request",
        "preprocess_request",
        "process_response",
        "do_teardown_request",
        "full_dispatch_request",
        "dispatch_request",
        "wsgi_app",
    }
)
# Message publish trace: app-layer dispatch APIs → broker/publisher runtime (no framework literals).
TRACE_PUBLISH_APP_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "delay",
        "apply_async",
        "send_task",
        "apply",
        "retry",
        "starmap",
        "map",
    }
)
TRACE_PUBLISH_RUNTIME_NAMES: frozenset[str] = frozenset(
    {
        "apply_async",
        "send_task",
        "Producer",
        "Router",
        "create_task_message",
        "send_task_message",
        "publish",
    }
)
TRACE_PUBLISH_SCOPE_SEGMENT = "/app/"
# Celery task registration: @app.task decorator -> generated Task -> app registry.
TRACE_TASK_REGISTRATION_TARGET_NAMES: frozenset[str] = frozenset({"task"})
# Worker consume trace: broker consumer → execution pool/strategy/request modules.
TRACE_CONSUME_TARGET_NAMES: frozenset[str] = frozenset({"Consumer", "consumer"})
TRACE_CONSUME_RUNTIME_NAMES: frozenset[str] = frozenset(
    {
        "on_task_request",
        "Strategy",
        "TaskPool",
        "Request",
        "execute",
        "start_strategy",
    }
)
TRACE_CONSUME_SCOPE_SEGMENT = "/worker/"
TRACE_CONSUME_PATH_PENALTIES = ("/backends/", "/backend/")
# Sibling modules to prefer when ranking trace import anchors (request/strategy next to consumer).
TRACE_EXECUTION_SIBLING_FILE_MARKERS: tuple[str, ...] = (
    "/request.py",
    "/strategy.py",
    "/consumer.py",
)
# Thin API wrappers (e.g. delay → apply_async): force depth-1 outgoing callees into the pool.
MANDATORY_CALLEE_RELATION = "MANDATORY_CALLEE"
THIN_DISPATCH_MAX_TOKEN_ESTIMATE = 80
THIN_DISPATCH_MAX_MANDATORY_CALLEES = 4
THIN_DISPATCH_MAX_CHAIN_CALLEES = 2
RUNTIME_SIGNAL_TOKENS = (
    "run",
    "runtime",
    "execute",
    "dispatch",
    "resolve",
    "handle",
    "route",
    "dependent",
    "dependant",
    "dependency",
    "middleware",
    "endpoint",
)
API_SIGNAL_TOKENS = (
    "api",
    "openapi",
    "route",
    "router",
    "endpoint",
    "depend",
    "dependency",
    "param",
    "request",
)
REGISTRATION_FLOW_TARGET_TOKENS = (
    "wsgi",
    "handler",
    "blueprint",
    "request",
    "middleware",
    "route",
    "app",
)
REGISTRATION_FLOW_PATH_TOKENS = (
    "/handlers/",
    "/wsgi",
    "/blueprint",
    "/globals",
    "/middleware",
    "/app",
)
ROUTING_FLOW_TARGET_TOKENS = (
    "app",
    "application",
    "express",
    "router",
    "routing",
    "middleware",
    "dispatch",
    "handle",
    "decorator",
    "resolver",
    "explorer",
)
ROUTING_FLOW_PATH_TOKENS = (
    "/lib/",
    "application",
    "express",
    "router",
    "middleware",
    "packages/core/router",
    "/decorators/",
)
IDENTITY_ENGINE_PATH_MARKERS = (
    "/identity/",
    "/engine/",
    "actor_index",
    "chain_engine",
)
IDENTITY_TRACE_EXECUTOR_NAMES = frozenset(
    {
        "same_actor",
        "ingest",
        "ingested",
    }
)
IDENTITY_TRACE_ORCHESTRATOR_NAMES = frozenset(
    {
        "same_actor",
        "ingest",
    }
)
ROUTING_COMPOSITION_SYMBOL_NAMES = frozenset(
    {
        "router",
        "use",
        "init",
        "handle",
        "layer",
        "dispatch",
    }
)
REGISTRATION_FACTORY_TOKENS = (
    "register",
    "record",
    "setup",
    "blueprint",
)
REGISTRATION_REPRESENTATION_TOKENS = (
    "state",
    "context",
    "request",
    "response",
    "setup",
)
REGISTRATION_RUNTIME_TOKENS = (
    "dispatch",
    "wsgi",
    "middleware",
    "request",
    "handle",
)
HOOK_FLOW_TARGET_TOKENS = (
    "before_",
    "after_",
    "teardown",
    "preprocess",
    "dispatch",
    "wsgi",
    "lifecycle",
    "hook",
)
HOOK_FLOW_PATH_TOKENS = (
    "/app.py",
    "/handlers/",
)
HOOK_RUNTIME_TOKENS = (
    "preprocess",
    "teardown",
    "dispatch_request",
    "wsgi_app",
)
LOW_SIGNAL_DOC_PATH_PATTERNS = (
    "/migrating-",
    "/comparison.",
    "/comparison.md",
    "/release-notes",
)
IMPACT_TOPIC_STOPWORDS = {
    "affected",
    "affect",
    "affects",
    "change",
    "changed",
    "changes",
    "docs",
    "documentation",
    "handling",
    "likely",
    "module",
    "modules",
    "test",
    "tests",
    "what",
    "when",
    "where",
    "which",
    "with",
}
FOCUS_QUERY_STOPWORDS = IMPACT_TOPIC_STOPWORDS | {
    "about",
    "actual",
    "assemble",
    "behavior",
    "build",
    "codebase",
    "does",
    "final",
    "flow",
    "from",
    "generate",
    "generated",
    "enhancer",
    "enhancers",
    "into",
    "logic",
    "middleware",
    "middlewares",
    "most",
    "operation",
    "parts",
    "passed",
    "returned",
    "turn",
    "user",
    "work",
}
