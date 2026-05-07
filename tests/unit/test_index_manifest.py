"""Unit tests for index manifest build + disk persistence."""

import json

from sidecar.indexer.repository_profile import build_empty_repository_profile
from sidecar.retrieval.manifest import (
    INDEX_MANIFEST_SCHEMA_VERSION,
    build_index_manifest,
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
    assert m["created_at"]


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
        "repository_profile": build_empty_repository_profile(
            str(project), "ws-x", reason="test"
        ),
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
