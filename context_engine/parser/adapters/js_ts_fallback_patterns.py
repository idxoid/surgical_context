"""Shared regex fallbacks for JavaScript and TypeScript adapter scans."""

import re

PROPERTY_FUNC_API_RE = re.compile(
    r"(?m)^[ \t]*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function(?:\s+([A-Za-z_$][\w$]*))?\b"
)
PROPERTY_ARROW_API_RE = re.compile(
    r"(?m)^[ \t]*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
CHAINED_PROPERTY_FUNC_API_RE = re.compile(
    r"(?m)^[ \t]*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function(?:\s+([A-Za-z_$][\w$]*))?\b"
)
