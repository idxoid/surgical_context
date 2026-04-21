"""Server-sent event formatting helpers."""

import json
from typing import Any


def format_sse(event: str, payload: dict[str, Any]) -> str:
    """Format one JSON-safe server-sent event frame."""
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"
