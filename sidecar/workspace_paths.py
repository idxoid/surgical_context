"""Resolve filesystem paths under a workspace's registered project root."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WorkspaceRootNotRegisteredError(ValueError):
    """No project_path registered for this workspace (index the project first)."""


class PathOutsideWorkspaceError(ValueError):
    """Resolved path escapes the workspace project root."""


def registered_workspace_root(db: Any, workspace_id: str) -> Path | None:
    """Return the absolute project root from the workspace index manifest, if any."""
    get_manifest = getattr(db, "get_index_manifest", None)
    if not callable(get_manifest):
        return None
    try:
        manifest = get_manifest(workspace_id=workspace_id)
    except TypeError:
        manifest = get_manifest(workspace_id)
    if not isinstance(manifest, dict):
        return None
    project_path = manifest.get("project_path")
    if not project_path:
        return None
    return Path(str(project_path)).expanduser().resolve()


def resolve_project_root(raw_path: str) -> Path:
    """Resolve and validate a directory used as workspace project root."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_dir():
        raise FileNotFoundError(f"Path not found: {raw_path}")
    return candidate


def is_path_within_root(path: Path, root: Path) -> bool:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def resolve_path_under_workspace_root(
    raw_path: str,
    *,
    workspace_id: str,
    db: Any,
    workspace_root: Path | None = None,
) -> Path:
    """Normalize *raw_path* to an absolute path under the workspace root."""
    root = workspace_root
    if root is None:
        root = registered_workspace_root(db, workspace_id)
    if root is None:
        raise WorkspaceRootNotRegisteredError(
            f"Workspace '{workspace_id}' has no registered project root; "
            "POST /index with project_path first."
        )
    root = root.resolve()

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if not is_path_within_root(candidate, root):
        raise PathOutsideWorkspaceError(
            f"Path '{candidate}' is outside workspace root '{root}'"
        )
    return candidate


def resolve_graph_file_path(
    raw_path: str,
    *,
    workspace_root: Path | None,
) -> str | None:
    """Return a filesystem path safe to open for graph-resolved reads, or None to skip.

    When *workspace_root* is set (index manifest registered), paths outside the root
    are rejected so stale or corrupted graph nodes cannot escape the sandbox.
    When no root is registered yet, behavior matches the legacy open-any-path path.
    """
    if not raw_path or raw_path == "<unknown>":
        return None
    path = Path(raw_path).expanduser()
    if workspace_root is None:
        try:
            return str(path.resolve())
        except OSError:
            return None
    root = workspace_root.resolve()
    try:
        if not path.is_absolute():
            path = (root / path).resolve()
        else:
            path = path.resolve()
    except OSError:
        return None
    if not is_path_within_root(path, root):
        return None
    return str(path)


def prune_graph_paths_outside_root(
    db: Any,
    *,
    workspace_id: str,
    project_root: Path,
) -> list[str]:
    """Delete workspace File nodes (and symbols) whose paths fall outside *project_root*."""
    list_paths = getattr(db, "list_file_paths", None)
    delete_file = getattr(db, "delete_symbols_for_file", None)
    if not callable(list_paths) or not callable(delete_file):
        return []
    root = project_root.resolve()
    removed: list[str] = []
    for path in list_paths(workspace_id=workspace_id):
        if resolve_graph_file_path(path, workspace_root=root) is None:
            delete_file(path, workspace_id=workspace_id)
            removed.append(path)
    return removed
