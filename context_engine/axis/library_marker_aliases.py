"""Loader for ``library_marker_aliases.json`` — re-export alias map.

When a consumer writes ``from flask import Flask``, the parser resolves the
external QN to ``flask.Flask`` (the import-table form). The literal
catalogue lists ``flask.app.Flask`` (the canonical definition site). This
loader carries the bridge: the alias map is structurally derived from the
``RE_EXPORTS`` edges of the indexed library workspace
(:mod:`QA.build_library_marker_aliases`), so re-export aliases never need
to be hand-authored alongside the catalogue.

Any code looking up a marker should first try the literal catalogue, then
resolve through this alias map and re-try the catalogue with the canonical
QN. Direct catalogue hits remain authoritative — the alias map is only
consulted on miss.
"""

from __future__ import annotations

import json
from pathlib import Path

_ALIAS_FILE = Path(__file__).with_name("library_marker_aliases.json")


def _load_aliases() -> dict[str, str]:
    if not _ALIAS_FILE.exists():
        return {}
    try:
        payload = json.loads(_ALIAS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = payload.get("aliases")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


LIBRARY_MARKER_ALIASES: dict[str, str] = _load_aliases()


def resolve_alias(qualified_name: str) -> str | None:
    """Return the canonical QN for an alias, or ``None`` if not aliased."""
    return LIBRARY_MARKER_ALIASES.get(qualified_name)


__all__ = ["LIBRARY_MARKER_ALIASES", "resolve_alias"]
