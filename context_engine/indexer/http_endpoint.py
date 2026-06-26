"""HTTP endpoint fingerprinting for cross-language client↔handler bridges."""

from __future__ import annotations

import re

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ALL"})

DECORATOR_METHOD_MAP: dict[str, str] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "all": "ALL",
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
    "Head": "HEAD",
    "Options": "OPTIONS",
    "All": "ALL",
}

HTTP_ROUTE_REGISTER_CALLEES = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "all", "route"}
)

HTTP_CLIENT_CALLEES = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "fetch", "request"}
)

_PATH_FRAGMENT_RE = re.compile(r"(/[\w./:$-]+)")


def normalize_http_method(raw: str) -> str:
    token = (raw or "").strip()
    if not token:
        return ""
    mapped = DECORATOR_METHOD_MAP.get(token) or DECORATOR_METHOD_MAP.get(token.lower())
    if mapped:
        return mapped
    upper = token.upper()
    return upper if upper in HTTP_METHODS else ""


def normalize_http_path(raw: str) -> str:
    path = (raw or "").strip()
    if not path:
        return ""
    if not path.startswith("/"):
        path = f"/{path}"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def combine_controller_path(prefix: str, subpath: str) -> str:
    left = normalize_http_path(prefix) if prefix else ""
    right = normalize_http_path(subpath) if subpath else ""
    if not left:
        return right or "/"
    if not right or right == "/":
        return left
    return normalize_http_path(f"{left.rstrip('/')}{right}")


def endpoint_fingerprint(method: str, path: str) -> str:
    return f"{normalize_http_method(method)}:{normalize_http_path(path)}"


def path_from_template_text(raw: str) -> str:
    """Extract a static HTTP path suffix from a template literal body."""
    text = raw or ""
    match = _PATH_FRAGMENT_RE.search(text)
    if match:
        return normalize_http_path(match.group(1))
    if text.startswith("/"):
        return normalize_http_path(text)
    return ""
