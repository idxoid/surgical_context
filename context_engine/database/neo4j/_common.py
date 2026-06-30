"""Shared row-builders, constants and helpers for the Neo4j client mixins."""

from pathlib import Path

from context_engine.parser.protocol import ClassApiEdge, ImportEdge, InheritanceEdge, SymbolMetadata

_CALL_REL_TYPES = {
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
}

# Edge types counted into Symbol.in_degree / out_degree. MUST stay identical to
# the relationship list the ranker read queries aggregate (recovery.py), or the
# materialized degree will not faithfully replace their count(DISTINCT) subquery.
_DEGREE_REL_PATTERN = (
    "CALLS|CALLS_DIRECT|CALLS_SCOPED|CALLS_IMPORTED|CALLS_DYNAMIC|CALLS_INFERRED|"
    "CALLS_GUESS|DEPENDS_ON|IMPLEMENTS|OVERRIDES|REFERENCES|"
    "RESOLVES_ATTR"
)

_CLASS_API_EDGE_WRITE_BATCH_SIZE = 1000
_CLASS_API_EDGE_DELETE_BATCH_SIZE = 5000

# Precision gate for both resolution layers in ``link_hooks`` — the EVENT
# channel (name -> topic) and the HOOK wrapper (api token -> registration
# declaration). A name binds to a declaration by name (like CALLS); common verbs
# (``commit``/``append``/``send``/``connect``/…) match many declarations, and the
# target type that would disambiguate which event class a dynamic dispatch
# reaches is not statically available. Per precision-over-recall we ABSTAIN when
# more than this many declarations carry the name — an ambiguous name is an
# honest gap, not a fan of guessed edges.
HOOK_AMBIGUITY_MAX = 3

# Reflect-metadata bridge fan-out cap. A metadata key with this many producers OR
# consumers is a generic framework key (``__guards__``-style); pairing it would
# wire a dense hub instead of a precise decorator→scanner link, so skip it.
METADATA_BRIDGE_FANOUT_MAX = 12

_WORKSPACE_GRAPH_VERSION_MATCH = "MATCH (w:Workspace {id: $workspace_id})"
_WORKSPACE_GRAPH_VERSION_SET = "SET w.graph_version = coalesce(w.graph_version, 0) + 1"
_BUMP_WORKSPACE_GRAPH_VERSION = f"{_WORKSPACE_GRAPH_VERSION_MATCH}\n{_WORKSPACE_GRAPH_VERSION_SET}"


def _bump_workspace_graph_version(session, workspace_id: str) -> None:
    session.run(_BUMP_WORKSPACE_GRAPH_VERSION, workspace_id=workspace_id)


def _batched_class_api_edges(
    edges: list[ClassApiEdge], batch_size: int = _CLASS_API_EDGE_WRITE_BATCH_SIZE
) -> list[list[ClassApiEdge]]:
    size = max(1, batch_size)
    return [edges[start : start + size] for start in range(0, len(edges), size)]


def _split_workspace_id(workspace_id: str) -> dict[str, str]:
    tenant_repo, _, ref = workspace_id.partition("@")
    tenant, _, repo = tenant_repo.partition("/")
    return {
        "id": workspace_id,
        "tenant": tenant or "local",
        "repo": repo or "repo",
        "ref": ref or "main",
        "ref_kind": "commit" if _looks_like_sha(ref) else "branch",
    }


def _looks_like_sha(ref: str) -> bool:
    return len(ref) in range(7, 41) and all(c in "0123456789abcdef" for c in ref.lower())


def _default_confidence(rel_type: str) -> float:
    return {
        "CALLS_DIRECT": 1.0,
        "CALLS_SCOPED": 0.9,
        "CALLS_IMPORTED": 0.85,
        "CALLS_DYNAMIC": 0.7,
        "CALLS_INFERRED": 0.4,
        "CALLS_GUESS": 0.4,
    }.get(rel_type, 0.4)


def _default_tier(rel_type: str) -> str:
    return {
        "CALLS_DIRECT": "direct",
        "CALLS_SCOPED": "scoped",
        "CALLS_IMPORTED": "imported",
        "CALLS_DYNAMIC": "dynamic",
        "CALLS_INFERRED": "guess",
        "CALLS_GUESS": "guess",
    }.get(rel_type, "guess")


def _symbol_row(symbol: SymbolMetadata) -> dict[str, object]:
    return {
        "uid": symbol.uid,
        "name": symbol.name,
        "kind": symbol.kind,
        "content_hash": symbol.content_hash,
        "start": symbol.start_line,
        "end": symbol.end_line,
        "token_estimate": symbol.token_estimate,
        "qualified_name": symbol.qualified_name,
        "signature": symbol.signature,
        "signature_hash": symbol.signature_hash,
        "signature_status": symbol.signature_status,
        "language": symbol.language,
        "returns_function_expression": bool(symbol.returns_function_expression),
        "returns_mapping": bool(symbol.returns_mapping),
        "returns_sequence": bool(symbol.returns_sequence),
        "returns_constructed_type": bool(symbol.returns_constructed_type),
        "iterates_attr_call": bool(symbol.iterates_attr_call),
        "assembles_mapping_in_loop": bool(symbol.assembles_mapping_in_loop),
        "is_getter": bool(symbol.is_getter),
        "is_setter": bool(symbol.is_setter),
        "is_react_hook": bool(symbol.is_react_hook),
    }


def _call_row(call: dict, rel_type: str) -> dict[str, object]:
    return {
        "caller_uid": call["caller_uid"],
        "callee_uid": call.get("callee_uid"),
        "callee_name": call.get("callee_name"),
        "callee_qualified_name": call.get("callee_qualified_name"),
        "confidence": float(call.get("confidence", _default_confidence(rel_type))),
        "tier": call.get("tier", _default_tier(rel_type)),
        "resolver": call.get("resolver", "scope-v1"),
        "call_site_line": call.get("call_site_line"),
    }


def _call_mode(row: dict[str, object]) -> str:
    if row.get("callee_uid"):
        return "uid"
    if row.get("callee_qualified_name"):
        return "qualified_name"
    return "name"


def _grouped_call_rows(calls: list[dict]) -> list[tuple[str, str, list[dict[str, object]]]]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for call in calls:
        rel_type = call.get("rel_type", "CALLS_DIRECT")
        if rel_type not in _CALL_REL_TYPES:
            rel_type = "CALLS_GUESS"
        row = _call_row(call, rel_type)
        key = (rel_type, _call_mode(row))
        groups.setdefault(key, []).append(row)
    return [(rel_type, mode, rows) for (rel_type, mode), rows in groups.items()]


def _import_row(imp: ImportEdge) -> dict[str, object]:
    if imp.import_type == "relative" and imp.target_module_name.startswith("."):
        base = (Path(imp.source_file).parent / imp.target_module_name).resolve()
        module_path = str(base)
        package_paths: list[str] = []
    else:
        module_name = imp.target_module_name.lstrip("./")
        module_path = "/" + module_name.replace(".", "/")
        package_paths = _monorepo_package_import_paths(module_name)
    path_suffixes = [
        f"{module_path}{suffix}"
        for suffix in (
            ".py",
            "/__init__.py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            "/index.js",
            "/index.jsx",
            "/index.ts",
            "/index.tsx",
        )
    ]
    for package_path in package_paths:
        for suffix in (
            ".py",
            "/__init__.py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            "/index.js",
            "/index.jsx",
            "/index.ts",
            "/index.tsx",
        ):
            path_suffixes.append(f"{package_path}{suffix}")
    return {
        "source_file": imp.source_file,
        "path_suffixes": sorted(set(path_suffixes)),
        "import_type": imp.import_type,
    }


def _monorepo_package_import_paths(module_name: str) -> list[str]:
    """Return suffixes for package-manager workspace imports.

    NPM/Python package imports often point at a workspace package rather than a
    path that appears literally in the repository. For example
    ``@vue/runtime-core`` lives under ``packages/runtime-core/src/index.ts``.
    Keep this as suffix generation rather than framework-specific routing.
    """
    clean = module_name.strip().strip("/")
    if not clean:
        return []

    parts = [part for part in clean.split("/") if part]
    if not parts:
        return []
    if parts[0].startswith("@") and len(parts) >= 2:
        package_name = parts[1]
        subpath = parts[2:]
    else:
        package_name = parts[0]
        subpath = parts[1:]

    if not package_name:
        return []

    candidates = [
        f"/packages/{package_name}",
        f"/packages/{package_name}/src",
    ]
    if subpath:
        suffix = "/".join(subpath)
        candidates.extend(
            [
                f"/packages/{package_name}/{suffix}",
                f"/packages/{package_name}/src/{suffix}",
            ]
        )
    return candidates


# Python builtin exception hierarchy. A class inheriting one of these is an error
# type, but the base is a builtin (not an in-graph symbol), so the inheritance edge
# is never materialized — leaving the error-ness structurally invisible. This is the
# standard library's own taxonomy, not a project/benchmark fixture: it lets the
# cascade derive `error_surface` from a real AST fact (`class X(..., ValueError)`).
_BUILTIN_EXCEPTION_BASES: frozenset[str] = frozenset(
    {
        "BaseException",
        "Exception",
        "ArithmeticError",
        "AssertionError",
        "AttributeError",
        "BufferError",
        "EOFError",
        "ImportError",
        "ModuleNotFoundError",
        "LookupError",
        "IndexError",
        "KeyError",
        "MemoryError",
        "NameError",
        "UnboundLocalError",
        "OSError",
        "IOError",
        "FileNotFoundError",
        "FileExistsError",
        "PermissionError",
        "NotADirectoryError",
        "IsADirectoryError",
        "InterruptedError",
        "ConnectionError",
        "BrokenPipeError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "TimeoutError",
        "ReferenceError",
        "RuntimeError",
        "NotImplementedError",
        "RecursionError",
        "StopIteration",
        "StopAsyncIteration",
        "SyntaxError",
        "IndentationError",
        "TabError",
        "SystemError",
        "TypeError",
        "ValueError",
        "UnicodeError",
        "UnicodeDecodeError",
        "UnicodeEncodeError",
        "UnicodeTranslateError",
        "Warning",
        "DeprecationWarning",
        "UserWarning",
        "RuntimeWarning",
        "FloatingPointError",
        "OverflowError",
        "ZeroDivisionError",
        "EnvironmentError",
        "GeneratorExit",
        "KeyboardInterrupt",
        "SystemExit",
    }
)

# Standard JS/TS event-dispatcher bases (Node ``events.EventEmitter``, RxJS
# ``Subject`` family). Like builtin exceptions these are usually external types;
# direct ``extends EventEmitter`` is an AST fact for dispatch-surface topology.
_EVENT_DISPATCH_BASES: frozenset[str] = frozenset(
    {
        "EventEmitter",
        "Subject",
        "BehaviorSubject",
        "ReplaySubject",
        "AsyncSubject",
    }
)


def _inheritance_row(edge: InheritanceEdge) -> dict[str, object]:
    return {
        "subclass_uid": edge.subclass_uid,
        "superclass_name": edge.superclass_name,
        "is_interface": edge.is_interface,
        "superclass_path": edge.superclass_path or edge.superclass_name,
    }
