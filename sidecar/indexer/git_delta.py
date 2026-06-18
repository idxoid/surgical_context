"""Incremental index sync after a git HEAD move (commit / merge / rebase)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sidecar.indexer.git_committed import should_index_file
from sidecar.indexer.git_sync import GitStateTracker
from sidecar.workspace_paths import tombstone_indexed_file


def apply_git_head_delta(
    project_path: str,
    *,
    db: Any,
    lance: Any,
    workspace_id: str,
    user_id: str,
    index_file_fn: Any,
    enqueue_file_fn: Any | None = None,
    queue: bool = True,
) -> dict[str, Any]:
    """Index only files touched between the previous and current ``HEAD``.

    No full-project walk — intended as a post-commit safety net. Deleted paths
    are tombstoned; added/modified paths at ``HEAD`` are passed to the per-file
    indexer when ``should_index_file`` allows.
    """
    project = Path(project_path).resolve()
    change_set = GitStateTracker(
        state_file=str(project / ".surgical_context/git_state.json")
    ).detect_changes(str(project))
    result: dict[str, Any] = {
        "previous_head": change_set.previous.head if change_set.previous else "",
        "current_head": change_set.current.head,
        "branch_changed": change_set.branch_changed,
        "indexed": [],
        "tombstoned": [],
        "skipped": [],
        "queued": [],
    }
    if not change_set.changed_files:
        return result

    for raw_path in change_set.changed_files:
        path = str(Path(raw_path).resolve())
        if os.path.isfile(path):
            if not should_index_file(path):
                result["skipped"].append(path)
                continue
            if queue and enqueue_file_fn is not None:
                enqueue_file_fn(path, workspace_id, user_id)
                result["queued"].append(path)
            else:
                index_file_fn(path, db, lance, workspace_id=workspace_id)
                result["indexed"].append(path)
            continue
        if tombstone_indexed_file(db, lance, path, workspace_id=workspace_id) is not None:
            result["tombstoned"].append(path)

    return result


__all__ = ["apply_git_head_delta"]
