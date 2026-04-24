from pathlib import Path
from unittest.mock import patch

import pytest

from QA.reset_databases import resolve_default_targets, resolve_reset_target
from sidecar.workspace import DEFAULT_WORKSPACE_ID


def test_resolve_reset_target_fixture_uses_sample_project():
    workspace_id, project_path, docs_path = resolve_reset_target(
        fixture=True,
        repo=None,
        questions_path="unused.yaml",
        project_path=None,
        docs_path=None,
        workspace_id=None,
        repos_root=None,
    )

    project_root = Path(__file__).resolve().parents[2]
    assert workspace_id == DEFAULT_WORKSPACE_ID
    assert project_path == str(
        (project_root / "tests" / "fixtures" / "sample_project").resolve()
    )
    assert docs_path == str((project_root / "docs").resolve())


def test_resolve_reset_target_prefers_explicit_project_and_workspace(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    workspace_id, project_path, docs_path = resolve_reset_target(
        fixture=False,
        repo="fastapi",
        questions_path=str(
            Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"
        ),
        project_path=str(tmp_path),
        docs_path=None,
        workspace_id="local/test_repo@main",
        repos_root=None,
    )

    assert workspace_id == "local/test_repo@main"
    assert project_path == str(tmp_path.resolve())
    assert docs_path == str(docs_dir.resolve())


def test_resolve_reset_target_requires_existing_checkout():
    with pytest.raises(FileNotFoundError):
        resolve_reset_target(
            fixture=False,
            repo=None,
            questions_path="unused.yaml",
            project_path="/tmp/definitely-missing-surgical-context-path",
            docs_path=None,
            workspace_id="local/missing@main",
            repos_root=None,
        )


def test_resolve_default_targets_includes_fixture_and_existing_checkouts(tmp_path):
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"
    repos_root = tmp_path / "repos"
    fastapi_checkout = repos_root / "fastapi"
    fastapi_docs = fastapi_checkout / "docs"
    fastapi_docs.mkdir(parents=True)

    with patch(
        "QA.reset_databases.default_repo_checkout_path",
        side_effect=lambda repo, repos_root=None: Path(repos_root) / repo,
    ):
        targets = resolve_default_targets(
            questions_path=str(pack_path),
            repos_root=str(repos_root),
        )

    assert len(targets) == 2
    fixture_workspace, fixture_project, _fixture_docs = targets[0]
    fastapi_workspace, fastapi_project, fastapi_docs_path = targets[1]

    assert fixture_workspace == DEFAULT_WORKSPACE_ID
    assert fixture_project.endswith("tests/fixtures/sample_project")
    assert fastapi_project == str(fastapi_checkout.resolve())
    assert fastapi_docs_path == str(fastapi_docs.resolve())
    assert fastapi_workspace.startswith("local/fastapi@")
