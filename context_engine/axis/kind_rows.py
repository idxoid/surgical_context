"""Helpers for reading persisted axis container-kind payloads."""

from __future__ import annotations

import json
from typing import Any


def flat_kinds(raw: Any) -> set[str]:
    """Flatten ``axis_container_kinds_json`` rows into kind names."""
    if not raw:
        return set()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    out: set[str] = set()
    for item in parsed:
        if isinstance(item, dict):
            name = item.get("kind") or item.get("name")
            if name:
                out.add(str(name))
        elif item is not None:
            out.add(str(item))
    return out


__all__ = ["flat_kinds"]
