from __future__ import annotations

import json

from QA.contextbench_http_bridge import append_event, render_context, to_mcp_event


def _response():
    return {
        "question": "where is routing handled",
        "workspace_id": "bench/django@base+axis_python_v1",
        "context_bundles": [
            {
                "seed": {
                    "uid": "u1",
                    "name": "resolve",
                    "file_path": "django/urls/resolvers.py",
                    "role": "routing",
                    "distance_from_seed": 0,
                    "expansion_step": "seed",
                    "code": "def resolve(path): pass",
                    "start_line": 10,
                    "end_line": 20,
                    "rendered_spans": [[10, 10], [18, 20]],
                },
                "related": [],
            }
        ],
    }


def test_response_maps_to_adapter_compatible_event():
    event = to_mcp_event(_response(), "django__django-1")
    assert event["tool"] == "ask_code"
    assert event["result"]["files"] == ["django/urls/resolvers.py"]
    assert event["result"]["symbols"][0]["has_code"] is True
    assert event["result"]["symbols"][0]["start_line"] == 10
    assert event["result"]["symbols"][0]["rendered_spans"] == [[10, 10], [18, 20]]


def test_render_context_prints_file_ranges_and_deduplicates():
    response = _response()
    response["context_bundles"].append(response["context_bundles"][0])
    rendered = render_context(response)
    assert rendered.count("django/urls/resolvers.py:10-20") == 1
    assert "def resolve(path)" in rendered


def test_append_event_writes_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    event = to_mcp_event(_response(), "django__django-1")
    append_event(path, event)
    assert json.loads(path.read_text(encoding="utf-8"))["instance_id"] == "django__django-1"
