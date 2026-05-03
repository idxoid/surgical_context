import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from QA.qa_benchmark import (
    _compute_role_recall,
    _empty_indexing_summary,
    _normalize_cleanup_prefixes,
    _path_matches_prefix,
    append_snapshot_manifest,
    build_snapshot_manifest_row,
    default_repo_checkout_path,
    default_report_output_path,
    default_snapshot_manifest_path,
    ensure_repo_checkout,
    load_question_pack,
    load_questions,
    load_repository_meta,
    main,
    resolve_questions_path,
    resolve_repo_docs_path,
    run_benchmark,
    setup_fixture_db,
    write_metrics_report,
)


def test_compute_role_recall_normalizes_legacy_roles_to_canonical_taxonomy():
    recall = _compute_role_recall(
        ["public_entrypoint", "schema_generator", "schema_module"],
        ["api_surface", "representation_surface"],
    )

    assert recall == pytest.approx(1 / 3)


def test_load_question_pack_reads_real_repo_metadata():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    pack = load_question_pack(str(pack_path))

    assert pack["kind"] == "real_repo"
    repo_ids = {repo["id"] for repo in pack["repositories"]}
    assert {"fastapi", "pydantic", "redux_toolkit"}.issubset(repo_ids)
    assert len(pack["repositories"]) >= 3
    assert len(pack["questions"]) >= 24


def test_load_questions_filters_real_repo_core12_subset():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    questions = load_questions(str(pack_path), repo="fastapi", core12_only=True)

    assert len(questions) == 4
    assert all(question["repo"] == "fastapi" for question in questions)
    assert all(question["core12"] is True for question in questions)


def test_resolve_questions_path_defaults_to_fixture_pack():
    resolved = resolve_questions_path(None)

    assert resolved.endswith("tests/fixtures/sample_project/questions.yaml")


def test_resolve_questions_path_defaults_to_real_repo_pack_for_repo_filters():
    resolved = resolve_questions_path(None, repo="fastapi")

    assert resolved.endswith("tests/fixtures/real_repo_question_pack.yaml")


def test_resolve_questions_path_defaults_to_real_repo_pack_for_project_paths():
    resolved = resolve_questions_path(None, project_path="/tmp/fastapi")

    assert resolved.endswith("tests/fixtures/real_repo_question_pack.yaml")


def test_run_benchmark_rejects_empty_question_selection():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    with pytest.raises(ValueError, match="No benchmark questions matched"):
        run_benchmark(
            questions_path=str(pack_path),
            repo="not_a_repo",
            no_index=True,
        )


def test_load_repository_meta_returns_selected_repo():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    repo = load_repository_meta(str(pack_path), "pydantic")

    assert repo is not None
    assert repo["id"] == "pydantic"
    assert repo["language"] == "python"


def test_normalize_cleanup_prefixes_deduplicates_and_resolves():
    project_root = Path(__file__).resolve().parents[2]

    prefixes = _normalize_cleanup_prefixes(
        str(project_root / "docs"),
        str(project_root / "docs" / ".." / "docs"),
        None,
    )

    assert prefixes == [str((project_root / "docs").resolve())]


def test_path_matches_prefix_accepts_nested_paths_only():
    project_root = Path(__file__).resolve().parents[2]
    docs_prefix = str((project_root / "docs").resolve())

    assert _path_matches_prefix(docs_prefix, [docs_prefix]) is True
    assert _path_matches_prefix(str(Path(docs_prefix) / "road_map.md"), [docs_prefix]) is True
    assert _path_matches_prefix(str(project_root / "docs-v2" / "road_map.md"), [docs_prefix]) is False


def test_default_repo_checkout_path_uses_qa_repos_when_root_missing():
    checkout_path = default_repo_checkout_path("fastapi")

    assert checkout_path == Path(__file__).resolve().parents[2] / "QA" / "repos" / "fastapi"


def test_resolve_repo_docs_path_prefers_english_locale(tmp_path):
    english_docs = tmp_path / "docs" / "en" / "docs"
    english_docs.mkdir(parents=True)
    (tmp_path / "docs" / "fr" / "docs").mkdir(parents=True)

    resolved = resolve_repo_docs_path(str(tmp_path))

    assert resolved == str(english_docs.resolve())


def test_resolve_repo_docs_path_falls_back_to_docs_root(tmp_path):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()

    resolved = resolve_repo_docs_path(str(tmp_path))

    assert resolved == str(docs_root.resolve())


def test_ensure_repo_checkout_uses_existing_default_checkout():
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"
    expected_path = default_repo_checkout_path("fastapi")

    with patch("QA.qa_benchmark.Path.exists", return_value=True):
        checkout_path = ensure_repo_checkout(str(pack_path), "fastapi")

    assert checkout_path == str(expected_path.resolve())


def test_ensure_repo_checkout_clones_when_missing(tmp_path):
    pack_path = Path(__file__).parent.parent / "fixtures" / "real_repo_question_pack.yaml"

    with patch("QA.qa_benchmark.subprocess.run") as run_mock:
        checkout_path = ensure_repo_checkout(
            str(pack_path),
            "fastapi",
            repos_root=str(tmp_path),
        )

    expected_path = tmp_path / "fastapi"
    assert checkout_path == str(expected_path.resolve())
    run_mock.assert_called_once_with(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/fastapi/fastapi.git",
            str(expected_path.resolve()),
        ],
        check=True,
        text=True,
    )


def test_empty_indexing_summary_marks_skipped_run():
    summary = _empty_indexing_summary(skipped=True)

    assert summary["performed"] is False
    assert summary["skipped"] is True
    assert summary["skip_affects"] is False
    assert summary["collected"] == 0
    assert summary["docs_files_indexed"] == 0
    assert summary["docs_chunks_indexed"] == 0
    assert summary["timings_sec"] == {}
    assert summary["docs_timings_sec"] == {}


def test_default_report_output_path_includes_repo_and_core12_suffix():
    report_path = default_report_output_path(
        repo="redux_toolkit",
        core12_only=True,
        now=1_777_486_905.0,
    )

    assert "qa_benchmark_redux_toolkit_core12_" in report_path
    assert report_path.endswith(".json")


def test_write_metrics_report_persists_json_and_report_path(tmp_path):
    report_path = tmp_path / "report.json"
    metrics = {"summary": {"pass_rate": 1.0}}

    written = write_metrics_report(metrics, str(report_path))

    assert written == str(report_path.resolve())
    payload = json.loads(report_path.read_text())
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["report_path"] == str(report_path.resolve())
    assert metrics["report_path"] == str(report_path.resolve())


def test_append_snapshot_manifest_writes_compact_report_pointer(tmp_path):
    metrics = {
        "timestamp": 123.0,
        "question_pack": {
            "path": "questions.yaml",
            "repo_filter": "fastapi",
            "core12_only": True,
            "workspace_id": "local/fastapi@main",
        },
        "indexing": {
            "skipped": True,
            "repository_profile_store": "neo4j_workspace",
            "repository_profile": {
                "indexability": "medium",
                "retrieval_readiness": "partial",
                "capabilities": {"impact_analysis": "shallow_partial"},
                "strategy_profile": {"selected_strategy": "registration_flow"},
            },
        },
        "summary": {
            "total_questions": 4,
            "pass_count": 4,
            "pass_rate": 1.0,
            "precision_at_5": 0.5,
            "file_recall": 1.0,
            "role_recall": 1.0,
            "tokens_surgical": 10418,
            "reduction_ratio": 0.936,
            "assembly_ms_avg": 275.1,
        },
    }
    report_path = tmp_path / "report.json"
    manifest_path = tmp_path / "benchmark_runs.jsonl"

    row = build_snapshot_manifest_row(
        metrics,
        str(report_path),
        git_commit="abc123",
        git_branch="topic",
    )
    written = append_snapshot_manifest(metrics, str(report_path), str(manifest_path))

    assert row["repo"] == "fastapi"
    assert row["core12_only"] is True
    assert row["git_commit"] == "abc123"
    assert row["repository_readiness"] == "partial"
    assert row["impact_readiness"] == "shallow_partial"
    assert row["repository_profile_store"] == "neo4j_workspace"
    assert row["selected_strategy"] == "registration_flow"
    assert written == str(manifest_path.resolve())
    payload = json.loads(manifest_path.read_text().splitlines()[0])
    assert payload["report_path"] == str(report_path.resolve())
    assert payload["pass_rate"] == 1.0
    assert payload["precision_at_5"] == 0.5


def test_main_auto_writes_report_and_prints_path_when_report_flag_omitted(tmp_path, capsys):
    fake_metrics = {"summary": {"pass_rate": 1.0}}
    auto_report = tmp_path / "auto-report.json"
    manifest_path = tmp_path / "benchmark_runs.jsonl"

    with (
        patch("QA.qa_benchmark.run_benchmark", return_value=fake_metrics),
        patch("QA.qa_benchmark.default_report_output_path", return_value=str(auto_report)),
        patch("QA.qa_benchmark.default_snapshot_manifest_path", return_value=str(manifest_path)),
        patch("sys.argv", ["qa_benchmark.py", "--no-index"]),
    ):
        exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Report JSON:     {auto_report.resolve()}" in captured.out
    assert f"Snapshot index:  {manifest_path.resolve()}" in captured.out
    payload = json.loads(auto_report.read_text())
    assert payload["report_path"] == str(auto_report.resolve())
    manifest_payload = json.loads(manifest_path.read_text().splitlines()[0])
    assert manifest_payload["report_path"] == str(auto_report.resolve())
    assert default_snapshot_manifest_path().endswith("QA/benchmark_runs.jsonl")


def test_setup_fixture_db_returns_indexing_stats():
    stats = {
        "performed": True,
        "skipped": False,
        "collected": 10,
        "changed": 3,
        "parsed": 3,
        "symbols_encoded": 7,
        "symbols_removed": 1,
        "affects_rebuilt": 7,
        "docs_files_indexed": 0,
        "docs_chunks_indexed": 0,
        "timings_sec": {"total": 1.23},
        "docs_timings_sec": {},
    }
    docs_stats = {
        "docs_path": "/tmp/docs",
        "files_indexed": 2,
        "chunks_indexed": 9,
        "timings_sec": {"chunking": 0.1, "upsert": 0.2, "link": 0.3, "total": 0.6},
    }

    with (
        patch("QA.qa_benchmark.reset_index_state"),
        patch("sidecar.indexer.fast.run_fast_indexing", return_value=dict(stats)) as run_mock,
        patch("sidecar.indexer.docs.index_docs", return_value=dict(docs_stats)) as docs_mock,
    ):
        workspace_id, result = setup_fixture_db(skip_affects=True)

    assert workspace_id == "local/surgical_context@main"
    assert result["collected"] == 10
    assert "docs_indexed_path" in result
    assert result["docs_files_indexed"] == 2
    assert result["docs_chunks_indexed"] == 9
    assert result["docs_timings_sec"] == docs_stats["timings_sec"]
    run_mock.assert_called_once()
    assert run_mock.call_args.kwargs["skip_affects"] is True
    assert run_mock.call_args.kwargs["workspace_id"] == "local/surgical_context@main"
    docs_mock.assert_called_once()


def test_run_benchmark_report_includes_precision_and_ready_context():
    question = {
        "id": "q1",
        "symbol": "Target",
        "question": "How does Target work?",
        "difficulty": "medium",
        "intent": "explain_behavior",
        "mechanism": "demo_mechanism",
        "required_roles": ["public_entrypoint"],
        "expected_symbols": ["Target", "Helper"],
        "expected_files": ["repo/target.py"],
    }

    class _FakeContext:
        def __init__(self):
            self.primary_source = SimpleNamespace(symbol="Target", file_path="/tmp/repo/target.py")
            self.graph_context = [SimpleNamespace(symbol="Helper", file_path="/tmp/repo/helper.py")]
            self.documentation = [SimpleNamespace(source_file="/tmp/repo/docs.md")]
            self.missing_roles = []
            self.stopped_reason = "pool_exhausted"

        def token_count(self):
            return 321

        def to_dict(self):
            return {"primary_source": {"symbol": "Target"}, "graph_context": [{"symbol": "Helper"}]}

        def to_system_prompt(self):
            return "--- TARGET SYMBOL: Target ---\ncode"

    class _FakeArbitrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_context_for_symbol(self, *_args, **_kwargs):
            return _FakeContext()

    class _FakeNeo4jClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    class _FakeLanceDBClient:
        def __init__(self, *_args, **_kwargs):
            pass

    with (
        patch("QA.qa_benchmark.load_question_pack", return_value={"kind": "real_repo", "repositories": [], "questions": [question]}),
        patch("QA.qa_benchmark.load_questions", return_value=[question]),
        patch("QA.qa_benchmark.compute_carpet_bomb_tokens", return_value=1000),
        patch("sidecar.database.neo4j_client.Neo4jClient", _FakeNeo4jClient),
        patch("sidecar.database.lancedb_client.LanceDBClient", _FakeLanceDBClient),
        patch("sidecar.context.arbitrator.ContextArbitrator", _FakeArbitrator),
    ):
        metrics = run_benchmark(
            questions_path="ignored.yaml",
            no_index=True,
        )

    result = metrics["results"][0]
    assert result["precision"] == pytest.approx(1.0)
    assert result["precision_at_k"] == pytest.approx(1.0)
    assert result["ready_context"]["token_count"] == 321
    assert result["ready_context"]["contract"]["primary_source"]["symbol"] == "Target"
    assert result["ready_context"]["system_prompt"].startswith("--- TARGET SYMBOL: Target ---")
    assert metrics["summary"]["precision"] == pytest.approx(1.0)
    assert metrics["summary"]["precision_at_5"] == pytest.approx(1.0)
