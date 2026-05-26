"""Unit tests for workspace path sandboxing."""

from pathlib import Path

import pytest

from sidecar.workspace_paths import (
    PathOutsideWorkspaceError,
    WorkspaceRootNotRegisteredError,
    is_path_within_root,
    registered_workspace_root,
    resolve_path_under_workspace_root,
    resolve_project_root,
)


class FakeDb:
    def __init__(self, manifest: dict | None):
        self._manifest = manifest

    def get_index_manifest(self, workspace_id=None):
        return self._manifest


def test_registered_workspace_root_from_manifest():
    db = FakeDb({"project_path": "/tmp/proj"})
    root = registered_workspace_root(db, "ws-1")
    assert root == Path("/tmp/proj").resolve()


def test_resolve_relative_path_under_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    src = root / "src" / "main.py"
    src.parent.mkdir()
    src.write_text("x", encoding="utf-8")
    db = FakeDb({"project_path": str(root)})
    resolved = resolve_path_under_workspace_root(
        "src/main.py", workspace_id="ws", db=db
    )
    assert resolved == src.resolve()


def test_reject_path_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    db = FakeDb({"project_path": str(root)})
    with pytest.raises(PathOutsideWorkspaceError):
        resolve_path_under_workspace_root(str(outside), workspace_id="ws", db=db)


def test_reject_unregistered_workspace():
    db = FakeDb(None)
    with pytest.raises(WorkspaceRootNotRegisteredError):
        resolve_path_under_workspace_root("/any", workspace_id="ws", db=db)


def test_resolve_project_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert resolve_project_root(str(root)) == root.resolve()


def test_is_path_within_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    inner = root / "a.py"
    inner.write_text("", encoding="utf-8")
    assert is_path_within_root(inner, root)
    assert not is_path_within_root(tmp_path / "other.py", root)
