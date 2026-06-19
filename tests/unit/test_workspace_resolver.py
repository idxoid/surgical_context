import pytest

from context_engine.workspace import (
    WorkspaceResolver,
    assert_workspace_repo_matches_project_root,
)


def test_from_project_path_uses_directory_basename(tmp_path):
    project = tmp_path / "axis"
    project.mkdir()
    ws = WorkspaceResolver().from_project_path(str(project))
    assert ws.repo == "axis"
    assert ws.tenant == "local"
    assert ws.id.startswith("local/axis@")


def test_from_project_path_honors_explicit_workspace_id(tmp_path):
    project = tmp_path / "axis"
    project.mkdir()
    ws = WorkspaceResolver().from_project_path(
        str(project),
        value="local/surgical_context@main",
    )
    assert ws.id == "local/surgical_context@main"


def test_assert_workspace_repo_matches_project_root(tmp_path):
    project = tmp_path / "axis"
    project.mkdir()
    assert_workspace_repo_matches_project_root(project, "local/axis@main")
    with pytest.raises(ValueError, match="does not match workspace repo"):
        assert_workspace_repo_matches_project_root(project, "local/surgical_context@main")
