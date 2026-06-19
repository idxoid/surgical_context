"""Workspace root registration and path sandboxing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from context_engine.workspace import Workspace


def require_workspace_root_dir(raw_project_path: str) -> Path:
    from context_engine.workspace_paths import resolve_project_root

    try:
        return resolve_project_root(raw_project_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def authorize_workspace_project_root(
    project_root: Path,
    *,
    workspace: Workspace,
    db: Any,
) -> None:
    from context_engine.workspace_paths import (
        WorkspaceRootMismatchError,
        WorkspaceRootNotAllowedError,
        registered_workspace_root,
        validate_workspace_project_root,
    )

    existing = registered_workspace_root(db, workspace.id)
    try:
        validate_workspace_project_root(
            project_root,
            workspace_repo=workspace.repo,
            existing_root=existing,
        )
    except WorkspaceRootMismatchError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkspaceRootNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def sandbox_path(
    raw_path: str,
    *,
    workspace_id: str,
    db: Any,
    workspace_root=None,
) -> str:
    from context_engine.workspace_paths import (
        PathOutsideWorkspaceError,
        WorkspaceRootNotRegisteredError,
        resolve_path_under_workspace_root,
    )

    try:
        return str(
            resolve_path_under_workspace_root(
                raw_path,
                workspace_id=workspace_id,
                db=db,
                workspace_root=workspace_root,
            )
        )
    except WorkspaceRootNotRegisteredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PathOutsideWorkspaceError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
