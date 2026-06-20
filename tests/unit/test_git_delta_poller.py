"""Tests for the background git delta poller."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from context_engine.indexer.git_delta_poller import GitDeltaPoller, GitDeltaRegistry, GitDeltaTarget


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_registry_register_and_snapshot():
    registry = GitDeltaRegistry()
    registry.register("ws", "/tmp/repo", user_id="alice")
    [target] = registry.snapshot()
    assert target.workspace_id == "ws"
    assert target.project_path == "/tmp/repo"
    assert target.user_id == "alice"


def test_poller_disabled_when_interval_zero():
    registry = GitDeltaRegistry()
    calls: list[GitDeltaTarget] = []

    def _poll(target: GitDeltaTarget):
        calls.append(target)
        return None

    poller = GitDeltaPoller(registry, _poll, interval_seconds=0, auto_start=True)
    assert not poller.enabled
    poller.start()
    time.sleep(0.05)
    assert calls == []


def test_poller_tick_invokes_poll_fn_for_registered_git_repo(tmp_path):
    repo = _init_repo(tmp_path)

    registry = GitDeltaRegistry()
    registry.register("ws", str(repo), user_id="bob")
    seen: list[str] = []

    def _poll(target: GitDeltaTarget):
        seen.append(target.workspace_id)
        return {"indexed": ["x.py"], "queued": [], "tombstoned": [], "current_head": "abc"}

    poller = GitDeltaPoller(registry, _poll, interval_seconds=0, auto_start=False)
    poller._tick()
    assert seen == ["ws"]
    assert poller.snapshot()["syncs"] == 1
