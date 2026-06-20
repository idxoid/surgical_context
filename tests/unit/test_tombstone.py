"""Tests for stale-file tombstone helpers."""

from __future__ import annotations

from context_engine.workspace_paths import tombstone_indexed_file, tombstone_stale_indexed_files


class _FakeLance:
    def __init__(self):
        self.deleted: list[list[str]] = []

    def delete_symbol_embeddings(self, uids, workspace_id=None):
        self.deleted.append(list(uids))


class _FakeDb:
    def __init__(self, paths: list[str], uids_by_path: dict[str, list[str]] | None = None):
        self.paths = list(paths)
        self.uids_by_path = uids_by_path or {}
        self.deleted: list[str] = []

    def list_file_paths(self, workspace_id=None):
        return list(self.paths)

    def get_symbol_index_for_file(self, path, workspace_id=None):
        return {uid: {} for uid in self.uids_by_path.get(path, [])}

    def delete_symbols_for_file(self, path, workspace_id=None):
        self.deleted.append(path)
        if path in self.paths:
            self.paths.remove(path)


def test_tombstone_indexed_file_noop_when_not_indexed(tmp_path):
    db = _FakeDb([])
    lance = _FakeLance()
    path = str(tmp_path / "missing.py")
    assert tombstone_indexed_file(db, lance, path, workspace_id="ws") is None
    assert db.deleted == []


def test_tombstone_indexed_file_removes_graph_and_embeddings(tmp_path):
    path = str(tmp_path / "app.py")
    db = _FakeDb([path], {path: ["u:1", "u:2"]})
    lance = _FakeLance()
    removed = tombstone_indexed_file(db, lance, path, workspace_id="ws")
    assert removed == ["u:1", "u:2"]
    assert db.deleted == [path]
    assert lance.deleted == [["u:1", "u:2"]]


def test_tombstone_stale_indexed_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    kept = root / "kept.py"
    stale = root / "stale.py"
    kept.write_text("a = 1\n", encoding="utf-8")
    db = _FakeDb([str(kept), str(stale)], {str(stale): ["u:old"]})
    lance = _FakeLance()
    removed_paths, removed_uids = tombstone_stale_indexed_files(
        db,
        lance,
        workspace_id="ws",
        project_root=root,
        active_paths=[str(kept)],
    )
    assert removed_paths == [str(stale)]
    assert removed_uids == ["u:old"]
    assert str(stale) not in db.paths
