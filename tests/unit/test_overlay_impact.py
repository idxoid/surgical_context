"""Tier-1 overlay impact: degraded callers parsed from dirty editor buffers.

Impact's committed surface needs the indexed dependents graph. These tests
pin the additive, overlay-only augmentation: callers of a target that were
typed into open buffers but not yet indexed. Bounded to dirty buffers,
name-resolved, always flagged degraded.
"""

from __future__ import annotations

from context_engine.axis.overlay_impact import build_overlay_impact_callers
from context_engine.observability.metrics import MetricsRegistry
from context_engine.overlay import InMemoryOverlay

WS = "acme/repo@main"
INDEX_WS = "acme/repo@main+axis_python_v1"
USER = "u"


def _overlay() -> InMemoryOverlay:
    return InMemoryOverlay(max_entries=0, ttl_seconds=0, metrics=MetricsRegistry())


def test_finds_dirty_caller_of_brand_new_symbol():
    ov = _overlay()
    ov.update("caller.py", "def uses():\n    return brand_new(1)\n", workspace_id=WS, user_id=USER)
    rows = build_overlay_impact_callers(ov, symbol_name="brand_new", workspace_id=WS, user_id=USER)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "uses"
    assert row["file_path"] == "caller.py"
    assert row["kind"] == "overlay_caller"
    assert row["degraded"] is True
    assert row["start_line"] == 1


def test_saved_buffer_is_not_scanned():
    ov = _overlay()
    ov.update(
        "caller.py",
        "def uses():\n    return brand_new(1)\n",
        workspace_id=WS,
        user_id=USER,
        dirty=False,
    )
    assert (
        build_overlay_impact_callers(ov, symbol_name="brand_new", workspace_id=WS, user_id=USER)
        == []
    )


def test_recursive_self_call_is_skipped():
    ov = _overlay()
    ov.update(
        "m.py",
        "def brand_new(n):\n    return brand_new(n - 1)\n",
        workspace_id=WS,
        user_id=USER,
    )
    assert (
        build_overlay_impact_callers(ov, symbol_name="brand_new", workspace_id=WS, user_id=USER)
        == []
    )


def test_no_matching_calls_returns_empty():
    ov = _overlay()
    ov.update("m.py", "def other():\n    return 1\n", workspace_id=WS, user_id=USER)
    assert (
        build_overlay_impact_callers(ov, symbol_name="brand_new", workspace_id=WS, user_id=USER)
        == []
    )


def test_resolves_under_index_suffixed_workspace():
    # Axis/impact query under the profile-suffixed index id; buffers stored
    # under base must still be found (Phase A normalization).
    ov = _overlay()
    ov.update("caller.py", "def uses():\n    return brand_new(1)\n", workspace_id=WS, user_id=USER)
    rows = build_overlay_impact_callers(
        ov, symbol_name="brand_new", workspace_id=INDEX_WS, user_id=USER
    )
    assert [r["name"] for r in rows] == ["uses"]


def test_multiple_callers_dedup_per_enclosing_symbol():
    ov = _overlay()
    src = "def a():\n    return brand_new(1)\n\ndef b():\n    brand_new(2)\n    brand_new(3)\n"
    ov.update("c.py", src, workspace_id=WS, user_id=USER)
    rows = build_overlay_impact_callers(ov, symbol_name="brand_new", workspace_id=WS, user_id=USER)
    # b calls the target twice but collapses to one row; a is the other caller.
    assert sorted(r["name"] for r in rows) == ["a", "b"]


def test_none_overlay_returns_empty():
    assert build_overlay_impact_callers(None, symbol_name="x", workspace_id=WS, user_id=USER) == []
