from __future__ import annotations

import pytest

from QA.contextbench_prepare import build_plan, main


def _gold():
    row = {
        "instance_id": "opaque-1",
        "original_inst_id": "django__django-1",
        "repo": "django/django",
        "repo_url": "https://github.com/django/django.git",
        "language": "python",
        "base_commit": "a" * 40,
        "problem_statement": "fix it",
    }
    return {"opaque-1": row, "django__django-1": row}


def test_build_plan_uses_exact_commit_and_isolates_workspace(tmp_path):
    plan = build_plan([{"instance_id": "opaque-1"}], _gold(), tmp_path)
    assert plan[0]["checkout"] == str(tmp_path / "opaque-1" / "django")
    assert plan[0]["workspace"] == "contextbench/django@aaaaaaaaaaaa"
    assert plan[0]["base_commit"] == "a" * 40
    assert plan[0]["event_log"].endswith("opaque-1/treatment.events.jsonl")


def test_build_plan_rejects_non_github_checkout(tmp_path):
    gold = _gold()
    gold["opaque-1"]["repo_url"] = "file:///etc"
    with pytest.raises(ValueError, match="unsafe"):
        build_plan([{"instance_id": "opaque-1"}], gold, tmp_path)


def test_main_preserves_virtualenv_python_path(tmp_path, monkeypatch):
    subset = tmp_path / "subset.csv"
    subset.write_text("instance_id\nopaque-1\n", encoding="utf-8")
    monkeypatch.setattr("QA.contextbench_prepare.load_gold", lambda _path: _gold())
    captured = []
    monkeypatch.setattr(
        "QA.contextbench_prepare.execute_plan",
        lambda _plan, python: captured.append(python),
    )
    venv_python = tmp_path / ".venv" / "bin" / "python"

    assert (
        main(
            [
                "--subset",
                str(subset),
                "--gold",
                str(tmp_path / "gold.parquet"),
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--python",
                str(venv_python),
                "--execute",
            ]
        )
        == 0
    )
    assert captured == [venv_python.absolute()]
