"""Tests for committed-only indexing gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

from context_engine.indexer.git_committed import (
    filter_indexable_paths,
    matches_head,
    should_index_file,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_should_index_file_skips_untracked(tmp_path):
    repo = _init_repo(tmp_path)
    path = repo / "new.py"
    path.write_text("x = 1\n", encoding="utf-8")
    assert not should_index_file(path)


def test_should_index_file_allows_committed_match(tmp_path):
    repo = _init_repo(tmp_path)
    path = repo / "tracked.py"
    path.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")
    assert should_index_file(path)


def test_should_index_file_skips_modified_tracked(tmp_path):
    repo = _init_repo(tmp_path)
    path = repo / "tracked.py"
    path.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")
    path.write_text("x = 2\n", encoding="utf-8")
    assert not should_index_file(path)
    assert not matches_head(repo, path)


def test_filter_indexable_paths(tmp_path):
    repo = _init_repo(tmp_path)
    committed = repo / "ok.py"
    dirty = repo / "dirty.py"
    untracked = repo / "new.py"
    committed.write_text("a = 1\n", encoding="utf-8")
    dirty.write_text("b = 1\n", encoding="utf-8")
    untracked.write_text("c = 1\n", encoding="utf-8")
    _git(repo, "add", "ok.py", "dirty.py")
    _git(repo, "commit", "-m", "init")
    dirty.write_text("b = 2\n", encoding="utf-8")

    kept = filter_indexable_paths(
        [str(committed), str(dirty), str(untracked)],
        str(repo),
    )
    assert kept == [str(committed.resolve())]


def test_load_git_indexable_snapshot_is_bulk(tmp_path, monkeypatch):
    from context_engine.indexer import git_committed as gc

    repo = _init_repo(tmp_path)
    path = repo / "tracked.py"
    path.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "init")

    calls = {"n": 0}
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gc.subprocess, "run", counting_run)
    snapshot = gc.load_git_indexable_snapshot(repo)
    # rev-parse + ls-files + diff HEAD (3), not 2*N per file.
    assert snapshot is not None
    assert calls["n"] <= 3
    assert snapshot.is_indexable(path)
