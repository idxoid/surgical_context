"""Unit tests for feedback JSONL rotation."""

import json

from context_engine.feedback.store import FeedbackStore, RetrievalSnapshot


def _snapshot(token: str) -> RetrievalSnapshot:
    return RetrievalSnapshot(
        feedback_token=token,
        workspace_id="ws1",
        user_id="alice",
        trace_id="trace-1",
        symbol="process",
        intent="exploration",
        mode="surgical_full",
        question_hash="hash-q",
        question_tokens=12,
        context_pipeline_version="test",
        selected_candidates=[],
        documentation=[],
        context_metadata={},
    )


def test_feedback_store_rotates_when_line_cap_reached(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    store = FeedbackStore(snapshot_file=str(path), max_jsonl_bytes=1_000_000, max_jsonl_lines=3)

    for idx in range(3):
        store.record_snapshot(_snapshot(f"fbk_{idx}"))

    assert path.exists()
    assert not path.with_name(path.name + ".1").exists()

    store.record_snapshot(_snapshot("fbk_new"))

    rotated = path.with_name(path.name + ".1")
    assert rotated.exists()
    assert path.read_text(encoding="utf-8").count("\n") == 1
    assert json.loads(path.read_text(encoding="utf-8").strip())["feedback_token"] == "fbk_new"
    assert rotated.read_text(encoding="utf-8").count("\n") == 3


def test_feedback_store_rotates_when_byte_cap_reached(tmp_path):
    path = tmp_path / "snapshots.jsonl"
    store = FeedbackStore(
        snapshot_file=str(path),
        max_jsonl_bytes=120,
        max_jsonl_lines=10_000,
    )

    store.record_snapshot(_snapshot("fbk_a"))
    store.record_snapshot(_snapshot("fbk_b"))

    rotated = path.with_name(path.name + ".1")
    assert rotated.exists()
    assert path.read_text(encoding="utf-8").count("\n") == 1
