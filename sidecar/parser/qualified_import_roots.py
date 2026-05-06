"""Configurable whitelist for resolving qualified callee names across languages.

Graph indexing skips most third-party imports as ``external``. Some stacks still need
import bindings so ``Depends(...)``-style rules can see ``callee_qualified_name``.
Languages register roots in ``qualified_import_roots.yaml`` (keys = adapter
``language_name``); Python reads this today; C++/C# adapters can reuse the same helper.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).with_name("qualified_import_roots.yaml")


def get_qualified_import_roots(language: str) -> frozenset[str]:
    """Return top-level module/namespace roots to retain bindings for, per language."""
    key = (language or "").strip().lower()
    return _cached_qualified_import_roots(key)


@functools.lru_cache(maxsize=32)
def _cached_qualified_import_roots(language_key: str) -> frozenset[str]:
    if not language_key or not _CONFIG_PATH.is_file():
        return frozenset()
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return frozenset()
    roots = data.get(language_key)
    if not isinstance(roots, list):
        return frozenset()
    return frozenset(x.strip() for x in roots if isinstance(x, str) and x.strip())


def clear_qualified_import_roots_cache() -> None:
    """Invalidate cache after tests or hot reload of YAML."""
    _cached_qualified_import_roots.cache_clear()
