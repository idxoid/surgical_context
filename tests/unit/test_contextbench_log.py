from __future__ import annotations

import json

from surgical_context_mcp.contextbench_log import (
    INSTANCE_ID_ENV,
    LOG_PATH_ENV,
    record_tool_result,
)


def test_record_tool_result_is_disabled_without_environment(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.delenv(LOG_PATH_ENV, raising=False)
    monkeypatch.delenv(INSTANCE_ID_ENV, raising=False)

    record_tool_result({"tool": "read_symbol", "ok": True})

    assert not path.exists()


def test_record_tool_result_writes_compact_event(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "events.jsonl"
    monkeypatch.setenv(LOG_PATH_ENV, str(path))
    monkeypatch.setenv(INSTANCE_ID_ENV, "owner__repo-1")

    record_tool_result(
        {"tool": "read_symbol", "ok": True, "markdown": "duplicate render", "code": "x"}
    )

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event == {
        "instance_id": "owner__repo-1",
        "tool": "read_symbol",
        "result": {"code": "x", "ok": True, "tool": "read_symbol"},
    }


def test_batch_envelope_is_not_logged_because_subtools_are_already_captured(
    tmp_path, monkeypatch
):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv(LOG_PATH_ENV, str(path))
    monkeypatch.setenv(INSTANCE_ID_ENV, "owner__repo-1")

    record_tool_result({"tool": "batch", "ok": True, "results": []})

    assert not path.exists()
