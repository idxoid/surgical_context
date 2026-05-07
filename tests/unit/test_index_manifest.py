"""Unit tests for index manifest build + disk persistence."""

import json
from unittest.mock import patch

from sidecar.indexer.repository_profile import build_empty_repository_profile
from sidecar.retrieval.manifest import (
    INDEX_MANIFEST_SCHEMA_VERSION,
    build_index_manifest,
    compute_manifest_id,
    manifest_file_path,
    persist_index_manifest,
    read_manifest_from_disk,
    write_manifest_to_disk,
)


def test_build_index_manifest_shape():
    profile = build_empty_repository_profile("/tmp/foo", "ws-1", reason="test")
    stats = {
        "collected": 1,
        "changed": 0,
        "parsed": 0,
        "symbols_encoded": 0,
        "symbols_removed": 0,
        "affects_rebuilt": 0,
        "framework_hints_applied": 0,
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "timings_sec": {"hash": 0.1},
        "repository_profile": profile,
        "repository_profile_store": "neo4j_workspace",
    }
    m = build_index_manifest(
        workspace_id="ws-1",
        project_path="/tmp/foo",
        stats=stats,
        graph_version=7,
        outcome="noop_unchanged",
    )
    assert m["manifest_schema_version"] == INDEX_MANIFEST_SCHEMA_VERSION
    assert m["workspace_id"] == "ws-1"
    assert m["indexing_outcome"] == "noop_unchanged"
    assert m["graph_version"] == 7
    assert m["manifest_id"]
    assert len(m["manifest_id"]) == 32
    assert m["created_at"]


def test_noop_manifest_id_is_deterministic(monkeypatch):
    """Same workspace, graph, git, embed, outcome -> same id (not a random uuid)."""
    profile = build_empty_repository_profile("/tmp/foo", "ws-1", reason="test")
    stats = {
        "collected": 1,
        "changed": 0,
        "parsed": 0,
        "repository_profile": profile,
    }
    git = {"commit": "abc123", "branch": "main"}
    a = compute_manifest_id(
        workspace_id="ws-1",
        project_path="/tmp/foo",
        stats=stats,
        graph_version=3,
        outcome="noop_unchanged",
        git=git,
    )
    b = compute_manifest_id(
        workspace_id="ws-1",
        project_path="/tmp/foo",
        stats=stats,
        graph_version=3,
        outcome="noop_unchanged",
        git=git,
    )
    assert a == b
    c = compute_manifest_id(
        workspace_id="ws-1",
        project_path="/tmp/foo",
        stats={**stats, "parsed": 5},
        graph_version=3,
        outcome="noop_unchanged",
        git=git,
    )
    assert c == a


def test_persist_index_manifest_disk_and_db(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    stored: dict = {}

    class _FakeDb:
        def get_workspace_graph_version(self, workspace_id: str = "x"):
            return 3

        def save_index_manifest(self, manifest: dict, workspace_id: str = "x"):
            stored["ws"] = workspace_id
            stored["manifest"] = manifest

    stats: dict = {
        "collected": 0,
        "changed": 0,
        "parsed": 0,
        "symbols_encoded": 0,
        "symbols_removed": 0,
        "affects_rebuilt": 0,
        "framework_hints_applied": 0,
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "timings_sec": {},
        "repository_profile": build_empty_repository_profile(str(project), "ws-x", reason="test"),
        "repository_profile_store": "",
    }
    m = persist_index_manifest(
        stats=stats,
        db=_FakeDb(),
        workspace_id="ws-x",
        project_path=str(project),
        outcome="no_indexable_files",
    )
    path = manifest_file_path(str(project))
    assert path.is_file()
    roundtrip = read_manifest_from_disk(str(project))
    assert roundtrip is not None
    assert roundtrip["manifest_id"] == m["manifest_id"]
    assert stored["ws"] == "ws-x"
    assert stored["manifest"]["workspace_id"] == "ws-x"
    assert stats.get("index_manifest") is m


def test_persist_records_warning_when_disk_unwritable(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    class _FakeDb:
        def get_workspace_graph_version(self, workspace_id: str = "x"):
            return 1

        def save_index_manifest(self, *args, **kwargs):
            pass

    stats: dict = {
        "collected": 0,
        "changed": 0,
        "parsed": 0,
        "symbols_encoded": 0,
        "symbols_removed": 0,
        "affects_rebuilt": 0,
        "framework_hints_applied": 0,
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "timings_sec": {},
        "repository_profile": build_empty_repository_profile(
            str(project), "ws-x", reason="test"
        ),
        "repository_profile_store": "",
    }
    with patch(
        "sidecar.retrieval.manifest.write_manifest_to_disk",
        side_effect=OSError("permission denied"),
    ):
        m = persist_index_manifest(
            stats=stats,
            db=_FakeDb(),
            workspace_id="ws-x",
            project_path=str(project),
            outcome="no_indexable_files",
        )
    assert m is not None
    assert stats.get("index_manifest_persist_warnings")
    assert stats["index_manifest_path"] == ""


def test_read_manifest_from_disk_missing():
    assert read_manifest_from_disk("/nonexistent/path/that/does/not/exist") is None


def test_write_manifest_atomic_replace(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    m1 = {"manifest_schema_version": 1, "manifest_id": "a", "x": 1}
    m2 = {"manifest_schema_version": 1, "manifest_id": "b", "x": 2}
    p1 = write_manifest_to_disk(str(d), m1)
    assert json.loads(p1.read_text())["manifest_id"] == "a"
    write_manifest_to_disk(str(d), m2)
    assert json.loads(p1.read_text())["manifest_id"] == "b"
