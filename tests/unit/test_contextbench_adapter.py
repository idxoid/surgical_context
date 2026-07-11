from __future__ import annotations

import json

import pytest

from QA.contextbench_adapter import convert_file, convert_instance, extract_event_steps


def test_convert_ask_and_read_symbol_to_contextbench_trajectory(tmp_path):
    root = tmp_path / "repo"
    record = {
        "instance_id": "owner__repo-1",
        "events": [
            {
                "tool": "ask_code",
                "result": {
                    "structuredContent": {
                        "tool": "ask_code",
                        "ok": True,
                        "render": "full",
                        "files": [str(root / "src/a.py")],
                        "symbols": [
                            {
                                "name": "alpha",
                                "file_path": str(root / "src/a.py"),
                                "has_code": True,
                                "start_line": 10,
                                "end_line": 20,
                            }
                        ],
                    }
                },
            },
            {
                "tool": "read_symbol",
                "result": {
                    "ok": True,
                    "name": "beta",
                    "file_path": str(root / "src/a.py"),
                    "start_line": 19,
                    "end_line": 30,
                    "code": "def beta(): pass",
                },
            },
        ],
        "model_patch": "diff --git a/src/a.py b/src/a.py",
    }

    result = convert_instance(record, root)

    assert result["traj_data"]["pred_files"] == ["src/a.py"]
    assert len(result["traj_data"]["pred_steps"]) == 2
    assert result["traj_data"]["pred_spans"] == {
        "src/a.py": [{"type": "line", "start": 10, "end": 30}]
    }
    assert result["traj_data"]["pred_symbols"] == {"src/a.py": ["alpha", "beta"]}
    assert result["model_patch"].startswith("diff --git")


def test_names_only_and_metadata_tools_do_not_claim_source_spans():
    record = {
        "instance_id": "owner__repo-2",
        "events": [
            {
                "tool": "ask_code",
                "result": {
                    "ok": True,
                    "render": "names",
                    "files": ["src/a.py"],
                    "symbols": [
                        {
                            "name": "alpha",
                            "file_path": "src/a.py",
                            "has_code": True,
                            "start_line": 10,
                            "end_line": 20,
                        }
                    ],
                },
            },
            {
                "tool": "find_definition",
                "result": {
                    "ok": True,
                    "definitions": [{"file_path": "src/b.py", "start_line": 4}],
                },
            },
        ],
    }

    result = convert_instance(record)

    assert result["traj_data"]["pred_files"] == ["src/a.py", "src/b.py"]
    assert result["traj_data"]["pred_spans"] == {}


def test_batch_expands_subtools_and_ignores_failed_or_non_context_results():
    event = {
        "tool": "batch",
        "result": {
            "structuredContent": {
                "results": [
                    {
                        "tool": "read_symbol",
                        "result": {
                            "ok": True,
                            "name": "alpha",
                            "file_path": "src/a.py",
                            "start_line": 1,
                            "end_line": 3,
                            "code": "def alpha(): pass",
                        },
                    },
                    {"tool": "read_symbol", "result": {"ok": False}},
                    {"tool": "list_workspaces", "result": {"ok": True}},
                ]
            }
        },
    }

    steps = extract_event_steps(event)

    assert len(steps) == 1
    assert steps[0]["spans"]["src/a.py"][0]["end"] == 3


def test_absolute_path_outside_repo_is_dropped(tmp_path):
    record = {
        "instance_id": "owner__repo-3",
        "events": [
            {
                "tool": "read_symbol",
                "result": {
                    "ok": True,
                    "name": "secret",
                    "file_path": "/other/secret.py",
                    "start_line": 1,
                    "end_line": 2,
                    "code": "secret",
                },
            }
        ],
    }

    assert convert_instance(record, tmp_path / "repo")["traj_data"]["pred_steps"] == []


def test_convert_jsonl_event_log_groups_instances(tmp_path):
    source = tmp_path / "events.jsonl"
    target = tmp_path / "predictions.jsonl"
    rows = [
        {
            "instance_id": "owner__repo-4",
            "tool": "file_outline",
            "result": {"ok": True, "file_path": "src/a.py"},
        },
        {"instance_id": "owner__repo-4", "model_patch": "patch"},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    assert convert_file(source, target) == 1
    output = json.loads(target.read_text(encoding="utf-8"))
    assert output["instance_id"] == "owner__repo-4"
    assert output["traj_data"]["pred_files"] == ["src/a.py"]
    assert output["model_patch"] == "patch"


def test_missing_instance_id_is_rejected():
    with pytest.raises(ValueError, match="instance_id"):
        convert_instance({"events": []})
