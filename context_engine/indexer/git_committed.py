"""Git helpers — index only committed (HEAD) file snapshots."""

from __future__ import annotations

import subprocess
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


def is_tracked(project_root: Path, file_path: Path) -> bool:
    rel = _rel_path(project_root, file_path)
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--error-unmatch", "--", rel],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return out.returncode == 0


def matches_head(project_root: Path, file_path: Path) -> bool:
    """True when the working-tree file matches ``HEAD`` (safe to index)."""
    if not is_tracked(project_root, file_path):
        return False
    rel = _rel_path(project_root, file_path)
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "diff", "HEAD", "--quiet", "--", rel],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return out.returncode == 0


def should_index_file(file_path: str | Path, *, project_root: str | Path | None = None) -> bool:
    """Index gate: only tracked files whose on-disk content equals ``HEAD``."""
    path = Path(file_path).resolve()
    root = Path(project_root).resolve() if project_root else git_root_for(path)
    if root is None:
        return True
    try:
        path.relative_to(root)
    except ValueError:
        return True
    return matches_head(root, path)


def filter_indexable_paths(files: list[str], project_path: str) -> list[str]:
    """Drop untracked / uncommitted paths from a collector walk."""
    root = git_root_for(project_path)
    if root is None:
        return files
    kept: list[str] = []
    for raw in files:
        path = Path(raw).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            kept.append(raw)
            continue
        if matches_head(root, path):
            kept.append(str(path))
    return kept


__all__ = [
    "filter_indexable_paths",
    "git_root_for",
    "is_tracked",
    "matches_head",
    "should_index_file",
]
