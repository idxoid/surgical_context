from pathlib import Path

from context_engine.indexer.repository_profile import (
    RepositoryProfileInputs,
    build_repository_profile,
    summarize_repository_profile,
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


def test_repository_profile_reports_indexability_and_shallow_impact(tmp_path: Path):
    app = tmp_path / "app.py"
    app.write_text(
        "registry = {}\n\n"
        "class Field:\n"
        "    pass\n\n"
        "class BookModel:\n"
        "    title = Field()\n\n"
        "def register_route(path, handler):\n"
        "    registry[path] = handler\n",
        encoding="utf-8",
    )

    profile = build_repository_profile(
        RepositoryProfileInputs(
            project_path=str(tmp_path),
            workspace_id="local/repo@main",
            collected_files=[str(app)],
            parsed_files=1,
            symbols_indexed=2,
            calls_indexed=2,
            imports_indexed=2,
            inheritance_indexed=1,
            affects_rebuilt=2,
        )
    )

    assert profile["indexability"] == "high"
    assert profile["languages"]["supported"]["python"] == 1
    assert profile["mechanism_profile"]["framework_signals"] == []
    assert profile["strategy_profile"]["selected_strategy"] == "generic_symbol_context"
    assert profile["strategy_profile"]["role_plan"] == ["docs_or_concept"]
    assert "mechanism_archetypes" not in profile["strategy_profile"]
    assert profile["capabilities"]["impact_analysis"] == "shallow"
    assert "reachability-based impact candidates" in profile["reasoning_contract"]["allowed"]
    summary = summarize_repository_profile(profile)
    assert "strategy=generic_symbol_context" in summary
    assert "impact=shallow" in summary


def test_repository_profile_is_plain_db_storable_payload(tmp_path: Path):
    source = tmp_path / "app.py"
    source.write_text("def handler():\n    return 1\n", encoding="utf-8")

    profile = build_repository_profile(
        RepositoryProfileInputs(
            project_path=str(tmp_path),
            workspace_id="local/repo@main",
            collected_files=[str(source)],
            parsed_files=1,
            symbols_indexed=1,
            calls_indexed=0,
        )
    )

    assert profile["workspace_id"] == "local/repo@main"
    assert "profile_path" not in profile
    assert profile["schema_version"] == 1


def test_repository_profile_does_not_infer_from_workspace_or_directory_name(tmp_path: Path):
    project = tmp_path / "django"
    project.mkdir()
    source = project / "plain.py"
    source.write_text("def handler():\n    return 1\n", encoding="utf-8")

    profile = build_repository_profile(
        RepositoryProfileInputs(
            project_path=str(project),
            workspace_id="local/django@main",
            collected_files=[str(source)],
            parsed_files=1,
            symbols_indexed=1,
        )
    )

    assert profile["mechanism_profile"]["framework_signals"] == []
    assert profile["strategy_profile"]["selected_strategy"] == "generic_symbol_context"
