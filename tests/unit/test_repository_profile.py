from pathlib import Path

from sidecar.indexer.repository_profile import (
    RepositoryProfileInputs,
    build_repository_profile,
    read_repository_profile,
    repository_profile_path,
    summarize_repository_profile,
    write_repository_profile,
)


def test_repository_profile_marks_unsupported_symbol_surface(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "execProcNode.c").write_text(
        "typedef struct PlannerInfo PlannerInfo;\n#define ExecProcNode(node) node\n",
        encoding="utf-8",
    )

    profile = build_repository_profile(
        RepositoryProfileInputs(
            project_path=str(tmp_path),
            workspace_id="local/postgres@master",
            collected_files=[],
            parsed_files=0,
            symbols_indexed=0,
        )
    )

    assert profile["indexability"] == "none"
    assert profile["retrieval_readiness"] == "unsupported_symbol_surface"
    assert profile["languages"]["unsupported_or_unparsed"]["c"] == 1
    assert "no_symbol_surface" in profile["warnings"]
    assert profile["capabilities"]["impact_analysis"] == "none"


def test_repository_profile_detects_framework_and_shallow_impact(tmp_path: Path):
    app = tmp_path / "app.py"
    app.write_text(
        "from django.db import models\n"
        "from django.urls import path\n\n"
        "urlpatterns = []\n\n"
        "class Book(models.Model):\n"
        "    title = models.CharField(max_length=100)\n",
        encoding="utf-8",
    )

    profile = build_repository_profile(
        RepositoryProfileInputs(
            project_path=str(tmp_path),
            workspace_id="local/django@main",
            collected_files=[str(app)],
            parsed_files=1,
            symbols_indexed=2,
            calls_indexed=2,
            imports_indexed=2,
            inheritance_indexed=1,
            affects_rebuilt=2,
            sample_texts=[app.read_text(encoding="utf-8")],
        )
    )

    assert profile["indexability"] == "high"
    assert profile["languages"]["supported"]["python"] == 1
    assert profile["mechanism_profile"]["framework_signals"][0]["name"] == "django"
    assert profile["capabilities"]["impact_analysis"] == "shallow"
    assert "reachability-based impact candidates" in profile["reasoning_contract"]["allowed"]
    assert "impact=shallow" in summarize_repository_profile(profile)


def test_repository_profile_persists_by_workspace_id(tmp_path: Path):
    profile = {
        "workspace_id": "local/repo@feature/test",
        "indexability": "medium",
        "retrieval_readiness": "partial",
        "languages": {"supported": {"python": 1}},
        "mechanism_profile": {"framework_signals": []},
        "capabilities": {"impact_analysis": "shallow_partial"},
    }

    written = write_repository_profile(profile, profile_dir=tmp_path)
    loaded = read_repository_profile("local/repo@feature/test", profile_dir=tmp_path)

    assert Path(written).exists()
    assert Path(written) == repository_profile_path(
        "local/repo@feature/test", profile_dir=tmp_path
    ).resolve()
    assert loaded is not None
    assert loaded["workspace_id"] == "local/repo@feature/test"
    assert loaded["profile_path"] == written
