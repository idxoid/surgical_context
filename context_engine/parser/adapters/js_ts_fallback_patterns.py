"""Shared regex fallbacks for JavaScript and TypeScript adapter scans."""

import re

_IDENT = r"[A-Za-z_$][\w$]*"
_PROP_ASSIGN = rf"({_IDENT})\.({_IDENT})\s*=\s*"
_ASYNC = r"(?:async )?"
_LINE = r"(?m)^[ \t]*"
_FUNC_TAIL = rf"{_ASYNC}function(?:\s+({_IDENT}))?\b"
_EXPORT = r"(?m)^export\s+"
_EXPORTS = r"(?m)^exports\."

PROPERTY_FUNC_API_RE = re.compile(rf"{_LINE}{_PROP_ASSIGN}{_FUNC_TAIL}")
PROPERTY_ARROW_API_RE = re.compile(rf"{_LINE}{_PROP_ASSIGN}{_ASYNC}(?:\([^)]*\)|{_IDENT})\s*=>")
CHAINED_PROPERTY_FUNC_API_RE = re.compile(rf"{_LINE}{_PROP_ASSIGN}{_PROP_ASSIGN}{_FUNC_TAIL}")

EXPORTS_VAR_FALLBACK_RE = re.compile(rf"{_EXPORTS}({_IDENT})\s*=")
EXPORT_DECL_VAR_FALLBACK_RE = re.compile(
    rf"{_EXPORT}(?:const|let|var)\s+({_IDENT})\b",
)
EXPORTS_FUNC_FALLBACK_RE = re.compile(rf"{_EXPORTS}({_IDENT})\s*=\s*{_ASYNC}function")
EXPORT_DECL_FUNC_FALLBACK_RE = re.compile(rf"{_EXPORT}{_ASYNC}function\s+({_IDENT})\b")
MODULE_EXPORT_FUNC_FALLBACK_RE = re.compile(
    rf"(?m)^module\.exports\s*=\s*{_ASYNC}function\s+({_IDENT})\b",
)
COMMONJS_REQUIRE_DEFAULT_RE = re.compile(
    rf"{_LINE}(?:const|let|var)\s+({_IDENT})\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)",
)
COMMONJS_EXPORT_ALIAS_RE = re.compile(rf"{_LINE}(?:module\.)?exports\.({_IDENT})\s*=\s*([^;\n]+)")
