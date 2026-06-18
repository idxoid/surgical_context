"""Stable symbol UID v2 helpers.

UIDs intentionally avoid absolute file paths. They are derived from a lexical
qualified name plus a normalized signature so moves between machines do not
rewrite graph identity.
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

UNRESOLVED_SIGNATURE = "<unresolved>"
_KNOWN_TYPE_NAMES = {
    "_",
    "Any",
    "None",
    "bool",
    "bytes",
    "dict",
    "float",
    "int",
    "list",
    "object",
    "set",
    "str",
    "tuple",
}
_DEFAULT_PROJECT_ROOT: ContextVar[str | None] = ContextVar("default_project_root", default=None)
_DEFAULT_WORKSPACE: ContextVar[str | None] = ContextVar("default_workspace", default=None)


@contextmanager
def project_root_scope(project_root: str | None, workspace_id: str | None = None):
    """Establish the indexing scope for UID computation.

    ``project_root`` relativises module names (machine-independent). ``workspace_id``
    is mixed into the symbol UID (see :func:`compute_uid`) so identical code
    indexed under different workspaces yields DISTINCT nodes — symbols are
    workspace-scoped, not deduplicated/shared across workspaces. ``workspace_id``
    is optional: the legacy code-indexer has no workspace and keeps producing
    workspace-less uids (it never shares a workspace with the fast/axis path).
    """
    token = _DEFAULT_PROJECT_ROOT.set(project_root)
    ws_token = _DEFAULT_WORKSPACE.set(workspace_id)
    try:
        yield
    finally:
        _DEFAULT_PROJECT_ROOT.reset(token)
        _DEFAULT_WORKSPACE.reset(ws_token)


def current_project_root() -> str | None:
    return _DEFAULT_PROJECT_ROOT.get()


def current_workspace() -> str | None:
    return _DEFAULT_WORKSPACE.get()


def module_name_from_path(file_path: str, project_root: str | None = None) -> str:
    """Return a dotted module-ish name without absolute machine-specific roots."""
    project_root = project_root or _DEFAULT_PROJECT_ROOT.get()
    path = Path(file_path)
    if project_root:
        try:
            path = path.resolve().relative_to(Path(project_root).resolve())
        except (OSError, ValueError):
            pass
    else:
        try:
            path = path.resolve().relative_to(Path(os.getcwd()).resolve())
        except (OSError, ValueError):
            path = Path(path.name)

    if path.suffix:
        path = path.with_suffix("")
    parts = [p for p in path.parts if p not in (".", "")]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else path.stem


def compute_uid(qualified_name: str, signature: str | None, language: str = "python") -> str:
    """Return a 16-hex-char stable UID from workspace + qualified name + signature.

    The workspace id (from the active :func:`project_root_scope`) is mixed in so
    identical code under different workspaces yields distinct nodes — symbols are
    workspace-scoped, not shared. Empty when no scope is set (legacy
    code-indexer). The workspace id is a logical, machine-independent id, so uids
    stay portable across machines (no absolute paths)."""
    signature_text = signature if signature is not None else UNRESOLVED_SIGNATURE
    normalized = normalize_signature(signature_text, language)
    workspace = _DEFAULT_WORKSPACE.get() or ""
    payload = f"{workspace}|{language}:{qualified_name}|{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def signature_hash(signature: str | None, language: str = "python") -> str:
    """Return a compact hash over the normalized signature."""
    normalized = normalize_signature(signature or UNRESOLVED_SIGNATURE, language)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def normalize_signature(raw: str, language: str = "python") -> str:
    """Normalize a Python/TypeScript-like signature by stripping names/defaults."""
    raw = (raw or UNRESOLVED_SIGNATURE).strip()
    if raw == UNRESOLVED_SIGNATURE:
        return raw

    name, params, returns = _split_signature(raw)
    normalized_params = _normalize_params(params, language)
    normalized_return = _normalize_type(returns, language) if returns else "_"
    return f"{name}({','.join(normalized_params)})->{normalized_return}"


def _node_text(node) -> str:
    """Decode a tree-sitter node using byte offsets owned by tree-sitter."""
    text = getattr(node, "text", None)
    return text.decode("utf-8") if text is not None else ""


def qualified_name_for(node, source_code: str, file_path: str) -> str:
    """Build a dotted qualified name for a tree-sitter symbol node."""
    module = module_name_from_path(file_path)
    parts: list[str] = []
    current = node
    while current is not None:
        if current.type in {
            "function_definition",
            "class_definition",
            "function_declaration",
            "method_definition",
            "class_declaration",
        }:
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                name = _node_text(name_node)
                if current is not node and current.type in {
                    "function_definition",
                    "function_declaration",
                }:
                    parts.append("<locals>")
                parts.append(name)
        current = current.parent

    return ".".join([module, *reversed(parts)]) if parts else module


def signature_from_node(node, source_code: str, language: str = "python") -> tuple[str | None, str]:
    """Extract a raw signature and status from a tree-sitter symbol node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None, "unresolved"
    name = _node_text(name_node)

    if node.type in {"class_definition", "class_declaration"}:
        return f"{name}()->_", "resolved"

    params_node = node.child_by_field_name("parameters")
    params = _node_text(params_node) if params_node else "()"
    returns = _return_annotation_from_node(node, source_code, language)
    raw = f"{name}{params}"
    if returns:
        raw = f"{raw}->{returns}"
    return raw, "resolved"


def _return_annotation_from_node(node, source_code: str, language: str) -> str:
    return_node = node.child_by_field_name("return_type") or node.child_by_field_name("type")
    if return_node is not None:
        text = _node_text(return_node)
        return text.lstrip("->:").strip()

    header = _node_text(node)[:500]
    if language == "python":
        match = re.search(r"\)\s*->\s*([^:]+):", header)
        return match.group(1).strip() if match else ""
    match = re.search(r"\)\s*:\s*([^\{;]+)", header)
    return match.group(1).strip() if match else ""


def _split_signature(raw: str) -> tuple[str, str, str]:
    match = re.match(r"\s*([\w$]+)\s*\((.*)\)\s*(?:->\s*(.+)|:\s*(.+))?\s*$", raw, re.S)
    if not match:
        return raw.split("(", 1)[0].strip() or "<anonymous>", "", "_"
    name = match.group(1)
    params = match.group(2) or ""
    returns = (match.group(3) or match.group(4) or "_").strip()
    return name, params, returns


def _normalize_params(params: str, language: str) -> list[str]:
    out: list[str] = []
    for param in _split_params(params):
        param = param.strip()
        if not param:
            continue
        if param in {"*", "/"}:
            out.append(param)
            continue
        if param.startswith("**"):
            out.append("**kwargs")
            continue
        if param.startswith("*") and ":" not in param:
            out.append("*")
            continue
        out.append(_normalize_param(param, language))
    return out


def _normalize_param(param: str, language: str) -> str:
    param = _strip_default(param)
    param = param.strip()
    param = param.lstrip("*").strip()

    if language in {"typescript", "javascript"}:
        # name?: Type -> Type?; name: A | B -> A|B with unions sorted.
        optional = "?" if re.match(r"^[\w$]+\?\s*:", param) else ""
        if ":" in param:
            param = param.split(":", 1)[1].strip()
        else:
            param = "_"
        return f"{_normalize_type(param, language)}{optional}"

    if ":" in param:
        return _normalize_type(param.split(":", 1)[1].strip(), language)
    return _normalize_type(param, language) if _looks_like_normalized_type(param) else "_"


def _normalize_type(type_text: str, language: str) -> str:
    text = (type_text or "_").strip()
    if not text:
        return "_"
    text = re.sub(r"\s+", "", text)
    if language in {"typescript", "javascript"} and "|" in text:
        return "|".join(sorted(part for part in text.split("|") if part))
    return text


def _looks_like_normalized_type(param: str) -> bool:
    if param in _KNOWN_TYPE_NAMES:
        return True
    if any(char in param for char in "[]|.<>{}"):
        return True
    return bool(param[:1].isupper())


def _strip_default(param: str) -> str:
    depth = 0
    quote: str | None = None
    for i, char in enumerate(param):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in "([{":
            depth += 1
            continue
        if char in ")]}" and depth:
            depth -= 1
            continue
        if char == "=" and depth == 0:
            return param[:i]
    return param


def _split_params(params: str) -> list[str]:
    items: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, char in enumerate(params):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in "([{<":
            depth += 1
            continue
        if char in ")]}>" and depth:
            depth -= 1
            continue
        if char == "," and depth == 0:
            items.append(params[start:i])
            start = i + 1
    tail = params[start:]
    if tail.strip():
        items.append(tail)
    return items
