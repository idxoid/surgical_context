"""Shared constants for UnifiedRanker scoring, recovery, and pruning heuristics."""

NOISE_PATH_PATTERNS = (
    "/tests/",
    "/test_",
    "/__tests__/",
    "/docs_src/",
    "/docs/virtual/",
    "/examples/",
    "/example/",
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
    "dependency",
    "dependencies",
    "inject",
    "provider",
    "container",
)
TRACE_DEPENDENCY_RUNTIME_NAME_TOKENS = (
    "dependency",
    "dependencies",
    "inject",
    "provider",
    "container",
    "resolve",
    "solve",
)
TRACE_HOOK_RUNTIME_TRIGGER_NAMES: frozenset[str] = frozenset(
    {"before_request", "after_request", "teardown_request", "wsgi_app", "dispatch_request"}
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
RUNTIME_SIGNAL_TOKENS = (
    "run",
    "runtime",
    "execute",
    "dispatch",
    "resolve",
    "handle",
    "route",
    "dependency",
    "middleware",
    "endpoint",
)
API_SIGNAL_TOKENS = (
    "api",
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
    "before_request",
    "after_request",
    "teardown_request",
    "preprocess_request",
    "dispatch",
    "wsgi",
    "lifecycle",
    "hook",
)
HOOK_FLOW_PATH_TOKENS = (
    "/app.py",
    "/scaffold",
    "/globals",
    "/ctx",
    "/handlers/",
)
HOOK_RUNTIME_TOKENS = (
    "preprocess_request",
    "do_teardown_request",
    "full_dispatch_request",
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
