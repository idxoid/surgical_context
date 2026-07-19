"""Git helpers — index only committed (HEAD) file snapshots."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def git_root_for(path: str | Path) -> Path | None:
    """Return the git repo root containing ``path``, or ``None``."""
    start = Path(path).resolve()
    if start.is_file():
        start = start.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    root = out.stdout.strip()
    return Path(root) if root else None


def _rel_path(project_root: Path, file_path: Path) -> str:
    return file_path.resolve().relative_to(project_root.resolve()).as_posix()


def _nul_split(payload: str) -> set[str]:
    return {part for part in payload.split("\0") if part}


@dataclass(frozen=True)
class GitIndexableSnapshot:
    """Bulk HEAD tracking state for one repo (avoids per-file git subprocesses)."""

    root: Path
    tracked: frozenset[str]
    dirty: frozenset[str]

    def is_indexable(self, file_path: str | Path) -> bool:
        path = Path(file_path).resolve()
        try:
            rel = _rel_path(self.root, path)
        except ValueError:
            return True
        return rel in self.tracked and rel not in self.dirty


def load_git_indexable_snapshot(project_root: str | Path) -> GitIndexableSnapshot | None:
    """One ``ls-files`` + one ``diff HEAD`` for the whole tree."""
    root = git_root_for(project_root)
    if root is None:
        return None
    try:
        tracked_proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=False,
            capture_output=True,
            text=True,
        )
        dirty_proc = subprocess.run(
            ["git", "-C", str(root), "diff", "HEAD", "--name-only", "-z"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if tracked_proc.returncode != 0:
        return None
    # ``git diff HEAD`` exits 1 when there are differences; still usable.
    # Missing HEAD (fresh repo, no commits) is treated as empty dirty set —
    # ``ls-files`` is empty too, so nothing is indexable.
    if dirty_proc.returncode in (0, 1):
        dirty = frozenset(_nul_split(dirty_proc.stdout))
    else:
        dirty = frozenset()
    return GitIndexableSnapshot(
        root=root,
        tracked=frozenset(_nul_split(tracked_proc.stdout)),
        dirty=dirty,
    )


def is_tracked(
    project_root: Path,
    file_path: Path,
    *,
    snapshot: GitIndexableSnapshot | None = None,
) -> bool:
    snap = snapshot if snapshot is not None else load_git_indexable_snapshot(project_root)
    if snap is None:
        return False
    try:
        rel = _rel_path(project_root, file_path)
    except ValueError:
        return False
    return rel in snap.tracked


def matches_head(
    project_root: Path,
    file_path: Path,
    *,
    snapshot: GitIndexableSnapshot | None = None,
) -> bool:
    """True when the working-tree file matches ``HEAD`` (safe to index)."""
    snap = snapshot if snapshot is not None else load_git_indexable_snapshot(project_root)
    if snap is None:
        return False
    return snap.is_indexable(file_path)


def should_index_file(
    file_path: str | Path,
    *,
    project_root: str | Path | None = None,
    snapshot: GitIndexableSnapshot | None = None,
) -> bool:
    """Index gate: only tracked files whose on-disk content equals ``HEAD``."""
    path = Path(file_path).resolve()
    root = (
        Path(project_root).resolve()
        if project_root
        else (snapshot.root if snapshot is not None else git_root_for(path))
    )
    if root is None:
        return True
    try:
        path.relative_to(root)
    except ValueError:
        return True
    snap = snapshot if snapshot is not None else load_git_indexable_snapshot(root)
    if snap is None:
        return True
    return snap.is_indexable(path)


def filter_indexable_paths(files: list[str], project_path: str) -> list[str]:
    """Drop untracked / uncommitted paths from a collector walk."""
    snapshot = load_git_indexable_snapshot(project_path)
    if snapshot is None:
        return files
    kept: list[str] = []
    for raw in files:
        path = Path(raw).resolve()
        try:
            path.relative_to(snapshot.root)
        except ValueError:
            kept.append(raw)
            continue
        if snapshot.is_indexable(path):
            kept.append(str(path))
    return kept


__all__ = [
    "GitIndexableSnapshot",
    "filter_indexable_paths",
    "git_root_for",
    "is_tracked",
    "load_git_indexable_snapshot",
    "matches_head",
    "should_index_file",
]
