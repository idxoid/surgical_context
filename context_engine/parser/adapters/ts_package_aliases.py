"""Monorepo workspace package-name → directory resolution for TS/JS imports.

In a monorepo, a file in one package imports a sibling package by its published
name (``import { CATCH_WATERMARK } from '@nestjs/common/constants'``) while the
declaring file imports the same module by a relative path
(``'../../constants'``). Without a package-name → directory map the two resolve
to different qualified names (``@nestjs.common.constants`` vs the in-repo
module), so every cross-package edge — calls, types, and the reflect-metadata
bridge — silently fails to connect. The map is read structurally from each
workspace ``package.json``'s ``name`` field; no name patterns, no hardcoding.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# Common workspace layouts. A package is any directory under one of these globs
# (or the repo root) carrying a ``package.json`` with a ``name``.
_WORKSPACE_GLOBS = ("packages/*", "apps/*", "libs/*", "modules/*", "*")


@lru_cache(maxsize=64)
def _alias_map(project_root: str) -> tuple[tuple[str, str], ...]:
    """``[(package_name, package_dir_abspath)]`` sorted longest-name-first."""
    root = Path(project_root)
    out: dict[str, str] = {}
    for glob in _WORKSPACE_GLOBS:
        for pkg_json in root.glob(f"{glob}/package.json"):
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = data.get("name") if isinstance(data, dict) else None
            if isinstance(name, str) and name:
                out.setdefault(name, str(pkg_json.parent))
    return tuple(sorted(out.items(), key=lambda kv: -len(kv[0])))


def resolve_package_subpath(project_root: str, source: str) -> str | None:
    """Map ``@scope/pkg/sub`` → ``<pkg_dir>/sub`` absolute path (no suffix), or None.

    Returns None for relative sources, true externals (``react``, ``rxjs``), and
    when no workspace package matches — callers keep their external fallback.
    """
    if not project_root or not source or source.startswith("."):
        return None
    for name, pkg_dir in _alias_map(project_root):
        if source == name:
            return pkg_dir
        if source.startswith(name + "/"):
            return str(Path(pkg_dir) / source[len(name) + 1 :])
    return None
