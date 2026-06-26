"""Tests for post-commit git delta indexing."""

from __future__ import annotations

import subprocess
from pathlib import Path

from context_engine.indexer.git_delta import apply_git_head_delta
from context_engine.indexer.git_sync import GitStateTracker


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


class _FakeLance:
    def delete_symbol_embeddings(self, uids, workspace_id=None):
        # No-op stub: git-delta tests do not assert on embedding tombstones.
        pass


class _FakeDb:
    pass


def test_apply_git_head_delta_indexes_changed_files(tmp_path):
    repo = _init_repo(tmp_path)
    path = repo / "app.py"
    path.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "init")

    tracker = GitStateTracker(state_file=str(repo / ".surgical_context/git_state.json"))
    tracker.detect_changes(str(repo))

    path.write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "change")

    indexed: list[str] = []

    def _index_one(file_path, db, lance, *, workspace_id):
        indexed.append(file_path)
        return []

    result = apply_git_head_delta(
        str(repo),
        db=_FakeDb(),
        lance=_FakeLance(),
        workspace_id="ws",
        user_id="alice",
        index_file_fn=_index_one,
        queue=False,
    )
    assert str(path.resolve()) in indexed
    assert result["indexed"]


def test_apply_git_head_delta_tombstones_deleted_files(tmp_path):
    repo = _init_repo(tmp_path)
    path = repo / "app.py"
    path.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "init")

    tracker = GitStateTracker(state_file=str(repo / ".surgical_context/git_state.json"))
    tracker.detect_changes(str(repo))

    class Db:
        def __init__(self):
            self.paths = [str(path.resolve())]

        def list_file_paths(self, workspace_id=None):
            return list(self.paths)

        def get_symbol_index_for_file(self, path, workspace_id=None):
            return {"u:1": {}}

        def delete_symbols_for_file(self, path, workspace_id=None):
            self.paths = [p for p in self.paths if p != path]

    db = Db()
    _git(repo, "rm", "app.py")
    _git(repo, "commit", "-m", "delete")

    result = apply_git_head_delta(
        str(repo),
        db=db,
        lance=_FakeLance(),
        workspace_id="ws",
        user_id="alice",
        index_file_fn=lambda *a, **k: [],
        queue=False,
    )
    assert result["tombstoned"]
    assert db.paths == []
