"""Resolve filesystem paths under a workspace's registered project root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class WorkspaceRootNotRegisteredError(ValueError):
    """No project_path registered for this workspace (index the project first)."""


class PathOutsideWorkspaceError(ValueError):
    """Resolved path escapes the workspace project root."""


class WorkspaceRootMismatchError(ValueError):
    """project_path conflicts with the workspace identity or an existing registration."""


class WorkspaceRootNotAllowedError(ValueError):
    """project_path is outside WORKSPACE_TRUSTED_ROOTS."""


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
    candidate = Path(raw_path).expanduser().resolve()
    if not candidate.is_dir():
        raise FileNotFoundError(f"Path not found: {raw_path}")
    return candidate


def resolve_cli_directory(raw_path: str) -> Path:
    """Resolve and authorize a CLI directory path before filesystem access."""
    root = resolve_project_root(raw_path)
    trusted = trusted_workspace_roots()
    if trusted and not any(is_path_within_root(root, base) for base in trusted):
        raise WorkspaceRootNotAllowedError(f"Path '{root}' is not under WORKSPACE_TRUSTED_ROOTS")
    return root


def trusted_workspace_roots() -> list[Path]:
    """Optional allowlist from WORKSPACE_TRUSTED_ROOTS (os.pathsep-separated).

    When unset, no extra root restriction is applied beyond workspace identity
    checks. Set to parent directories that may host registered project roots
    (e.g. ``$HOME`` or ``/home/user/projects``) when the context_engine is reachable
    beyond strict localhost trust.
    """
    raw = os.getenv("WORKSPACE_TRUSTED_ROOTS", "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for entry in raw.split(os.pathsep):
        text = entry.strip()
        if not text:
            continue
        roots.append(Path(text).expanduser().resolve())
    return roots


def validate_workspace_project_root(
    project_root: Path,
    *,
    workspace_repo: str,
    existing_root: Path | None = None,
) -> None:
    """Authorize a directory as the workspace sandbox root.

    Rules:
    - An existing registration is sticky: the same resolved path only.
    - First registration must use a directory whose basename matches
      ``workspace.repo`` (the VS Code extension derives both from the folder).
    - When WORKSPACE_TRUSTED_ROOTS is set, the root must lie under one entry.
    """
    resolved = project_root.resolve()
    if existing_root is not None:
        if resolved != existing_root.resolve():
            raise WorkspaceRootMismatchError(
                f"Workspace already registered with project root '{existing_root}'; "
                f"refusing to re-register '{resolved}'"
            )
        return

    if resolved.name != workspace_repo:
        raise WorkspaceRootMismatchError(
            f"Project directory name '{resolved.name}' does not match "
            f"workspace repo '{workspace_repo}'"
        )

    trusted = trusted_workspace_roots()
    if trusted and not any(is_path_within_root(resolved, base) for base in trusted):
        raise WorkspaceRootNotAllowedError(
            f"Project root '{resolved}' is not under WORKSPACE_TRUSTED_ROOTS"
        )


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
        raise PathOutsideWorkspaceError(f"Path '{candidate}' is outside workspace root '{root}'")
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


def _normalize_path(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return path


def tombstone_indexed_file(
    db: Any,
    lance: Any,
    file_path: str,
    *,
    workspace_id: str,
) -> list[str] | None:
    """Remove one indexed file from Neo4j + Lance.

    Returns removed symbol uids, or ``None`` when the path was not indexed.
    """
    delete_file = getattr(db, "delete_symbols_for_file", None)
    if not callable(delete_file):
        return []
    list_paths = getattr(db, "list_file_paths", None)
    indexed_path = file_path
    if callable(list_paths):
        known = list_paths(workspace_id=workspace_id)
        normalized = _normalize_path(file_path)
        matches = [path for path in known if _normalize_path(path) == normalized]
        if not matches:
            return None
        indexed_path = matches[0]
    get_idx = getattr(db, "get_symbol_index_for_file", None)
    uids: list[str] = []
    if callable(get_idx):
        uids = list(get_idx(indexed_path, workspace_id=workspace_id))
    delete_file(indexed_path, workspace_id=workspace_id)
    delete_embeddings = getattr(lance, "delete_symbol_embeddings", None)
    if uids and callable(delete_embeddings):
        try:
            delete_embeddings(uids, workspace_id=workspace_id)
        except TypeError:
            delete_embeddings(uids)
    return uids


def tombstone_stale_indexed_files(
    db: Any,
    lance: Any,
    *,
    workspace_id: str,
    project_root: Path,
    active_paths: list[str],
) -> tuple[list[str], list[str]]:
    """Drop indexed files under *project_root* that are absent from *active_paths*."""
    list_paths = getattr(db, "list_file_paths", None)
    if not callable(list_paths):
        return [], []
    root = project_root.resolve()
    active = {_normalize_path(path) for path in active_paths}
    removed_paths: list[str] = []
    removed_uids: list[str] = []
    for path in list_paths(workspace_id=workspace_id):
        if resolve_graph_file_path(path, workspace_root=root) is None:
            continue
        if _normalize_path(path) in active:
            continue
        uids = tombstone_indexed_file(db, lance, path, workspace_id=workspace_id)
        if uids is None:
            continue
        removed_paths.append(path)
        removed_uids.extend(uids)
    return removed_paths, removed_uids
