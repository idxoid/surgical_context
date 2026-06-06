"""Project boundary + external root classification for materialized externals (C1).

External targets are truncated to **top-level module roots** (``httpx``, ``sqlalchemy``).
In-project imports whose root is inside the boundary set are never ``ExternalPkg`` —
unresolved links are parser/index debt (BrokenLink), not external boundary signals.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from functools import lru_cache
from pathlib import Path

from sidecar.parser.uid import module_name_from_path


def external_root_from_qualified_name(qualified_name: str) -> str:
    """Return the root module segment from a dotted qualified name."""
    qn = (qualified_name or "").strip()
    if not qn:
        return ""
    return qn.split(".", 1)[0]


def external_pkg_uid(workspace_id: str, root: str) -> str:
    """Stable uid for an ``(:ExternalPkg)`` node (workspace-scoped, root-truncated)."""
    import hashlib

    payload = f"external_pkg:{workspace_id}:{root}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def external_symbol_uid(workspace_id: str, qualified_name: str) -> str:
    """Stable uid for an ``(:ExternalSymbol)`` node.

    Unlike ``external_pkg_uid``, this captures the *named* import target rather
    than the package root: ``starlette.routing.Router``, not ``starlette``. The
    catalogue lookup is keyed by ``qualified_name`` (workspace-independent), so
    the same external symbol uses a different uid per workspace but resolves to
    the same catalogue entry.
    """
    import hashlib

    payload = f"external_symbol:{workspace_id}:{qualified_name}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def installed_top_level_packages() -> frozenset[str]:
    try:
        return frozenset(importlib.metadata.packages_distributions().keys())
    except Exception:
        return frozenset()


def is_published_external_root(root: str) -> bool:
    """True when ``root`` is stdlib or an installed distribution top-level name."""
    if not root:
        return False
    if root in sys.stdlib_module_names:
        return True
    return root in installed_top_level_packages()


def package_manifest_external_roots(project_root: str | Path) -> frozenset[str]:
    """Dependency roots declared by JS package manifests in the indexed project."""
    root = Path(project_root)
    manifest = root / "package.json"
    if not manifest.exists():
        return frozenset()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    roots: set[str] = set()
    for section in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name in deps:
            if isinstance(name, str) and name:
                roots.add(name)
    return frozenset(roots)


def _layout_roots(project_root: Path) -> set[str]:
    roots: set[str] = set()
    if not project_root.is_dir():
        return roots
    for child in project_root.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir() and ((child / "__init__.py").exists() or any(child.glob("*.py"))):
            roots.add(child.name)
        elif child.suffix == ".py" and child.stem != "__init__":
            roots.add(child.stem)
    src = project_root / "src"
    if src.is_dir():
        for child in src.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() and ((child / "__init__.py").exists() or any(child.glob("*.py"))):
                roots.add(child.name)
            elif child.suffix == ".py" and child.stem != "__init__":
                roots.add(child.stem)
    return roots


def build_project_boundary(
    project_root: str | Path,
    *,
    file_paths: tuple[str, ...] | list[str] = (),
) -> frozenset[str]:
    """Module roots that belong to the indexed project (not external)."""
    root = Path(project_root).resolve()
    roots = _layout_roots(root)
    for file_path in file_paths:
        if not file_path:
            continue
        try:
            rel = Path(file_path).resolve().relative_to(root)
        except (OSError, ValueError):
            mod = module_name_from_path(file_path, str(root))
        else:
            mod = module_name_from_path(str(rel))
        if mod:
            roots.add(mod.split(".", 1)[0])
    return frozenset(roots)


# Stdlib/typing plumbing and test/doc tooling — materialized as ExternalPkg (C1) but
# excluded from integration-boundary cascade signals (C2).
EXTERNAL_INTEGRATION_PLUMBING_ROOTS: frozenset[str] = frozenset(
    {
        "abc",
        "asyncio",
        "builtins",
        "collections",
        "contextlib",
        "copy",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "operator",
        "os",
        "pathlib",
        "re",
        "subprocess",
        "sys",
        "threading",
        "types",
        "typing",
        "typing_extensions",
        "warnings",
        "weakref",
        "annotated_doc",
        "doctest",
        "pytest",
        "rich",
        "typer",
        "unittest",
    }
)


def is_integration_external_root(root: str) -> bool:
    """True when ``root`` counts toward gateway/integration topology (not plumbing)."""
    return bool(root) and root not in EXTERNAL_INTEGRATION_PLUMBING_ROOTS


def classify_external_root(
    root: str,
    project_boundary: frozenset[str],
    project_external_roots: frozenset[str] = frozenset(),
) -> str:
    """Classify a module root for external materialization.

    Returns:
        ``external`` — materialize ``ExternalPkg`` + ``*_EXTERNAL`` edges.
        ``internal`` — in-project; never external (link failure = BrokenLink).
        ``skip``     — unknown/local-outside-boundary; do not score as external.
    """
    if not root:
        return "skip"
    if root in project_boundary:
        return "internal"
    if root in project_external_roots:
        return "external"
    if is_published_external_root(root):
        return "external"
    return "skip"
