"""Git branch invalidation helpers for differential workspace sync."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitState:
    ref: str
    head: str


@dataclass(frozen=True)
class GitChangeSet:
    previous: GitState | None
    current: GitState
    changed_files: list[str]
    branch_changed: bool


class GitStateTracker:
    """Tracks Git ref/head changes and returns files needing re-index."""

    def __init__(self, state_file: str | None = None):
        self.state_file = Path(state_file or ".surgical_context/git_state.json")

    def detect_changes(self, project_path: str) -> GitChangeSet:
        project = Path(project_path).resolve()
        previous = self._load(project)
        current = GitState(ref=_git(project, "branch", "--show-current") or "DETACHED", head=_git(project, "rev-parse", "HEAD"))
        changed_files = self._changed_files(project, previous, current)
        change_set = GitChangeSet(
            previous=previous,
            current=current,
            changed_files=changed_files,
            branch_changed=bool(previous and previous.ref != current.ref),
        )
        self._save(project, current)
        return change_set

    def _changed_files(self, project: Path, previous: GitState | None, current: GitState) -> list[str]:
        if previous is None or not previous.head:
            return []
        if previous.head == current.head:
            return []
        output = _git(project, "diff", "--name-only", previous.head, current.head)
        return [str(project / line) for line in output.splitlines() if line.strip()]

    def _load(self, project: Path) -> GitState | None:
        state_path = project / self.state_file
        if not state_path.exists():
            return None
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            return GitState(ref=data["ref"], head=data["head"])
        except (OSError, KeyError, json.JSONDecodeError):
            return None

    def _save(self, project: Path, state: GitState) -> None:
        state_path = project / self.state_file
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(asdict(state), sort_keys=True), encoding="utf-8")


def _git(project: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
