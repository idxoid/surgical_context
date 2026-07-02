"""Workspace mapping coverage for axis_benchmark question packs."""

from QA.axis_benchmark import REPO_TO_WORKSPACE


def test_non_python_benchmark_repos_have_workspace_mapping():
    for repo in ("express", "nestjs", "redux_toolkit", "vue"):
        ws = REPO_TO_WORKSPACE.get(repo)
        assert ws is not None, f"missing workspace mapping for {repo!r}"
        assert repo in ws
        assert ws.endswith("+axis_python_v1")


def test_surgical_context_defaults_to_qa_repo_workspace():
    ws = REPO_TO_WORKSPACE.get("surgical_context")

    assert ws is not None
    assert ws.startswith("qa_repo/surgical_context@")
    assert ws.endswith("+axis_python_v1")
