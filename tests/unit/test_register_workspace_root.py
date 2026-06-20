"""Tests for early workspace root registration on queued POST /index."""

from context_engine.retrieval.manifest import register_workspace_project_root


class FakeDb:
    def __init__(self):
        self.saved = None

    def get_workspace_graph_version(self, workspace_id=None):
        return 1

    def save_index_manifest(self, manifest, workspace_id=None):
        self.saved = manifest


def test_register_workspace_project_root_persists_project_path(tmp_path):
    db = FakeDb()
    project = tmp_path / "myrepo"
    project.mkdir()

    manifest = register_workspace_project_root(
        db=db,
        workspace_id="local/test@main",
        project_path=str(project),
        file_count=3,
    )

    assert manifest is not None
    assert manifest["project_path"] == str(project.resolve())
    assert manifest["indexing_outcome"] == "queued"
    assert manifest["indexing_pipeline"] == "queued_batch"
    assert db.saved is not None
    assert db.saved["project_path"] == str(project.resolve())
