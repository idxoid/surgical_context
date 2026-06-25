"""Shared regex fallbacks for JavaScript and TypeScript adapter scans."""

import re

_IDENT = r"[A-Za-z_$][\w$]*"
_PROP_ASSIGN = rf"({_IDENT})\.({_IDENT})\s*=\s*"
_ASYNC = r"(?:async )?"
_LINE = r"(?m)^[ \t]*"
_FUNC_TAIL = rf"{_ASYNC}function(?:\s+({_IDENT}))?\b"

PROPERTY_FUNC_API_RE = re.compile(rf"{_LINE}{_PROP_ASSIGN}{_FUNC_TAIL}")
PROPERTY_ARROW_API_RE = re.compile(
    rf"{_LINE}{_PROP_ASSIGN}{_ASYNC}(?:\([^)]*\)|{_IDENT})\s*=>"
)
CHAINED_PROPERTY_FUNC_API_RE = re.compile(rf"{_LINE}{_PROP_ASSIGN}{_PROP_ASSIGN}{_FUNC_TAIL}")
