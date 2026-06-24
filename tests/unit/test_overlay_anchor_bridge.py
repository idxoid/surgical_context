"""Overlay -> anchor bridge: ask a brand-new symbol before it is indexed.

Commit/index is the usual way a symbol earns a ``uid``. These tests pin the
bridge that lets the axis pipeline anchor an ask on a symbol that only exists
in the live editor buffer, and the overlay-only render that skips every graph
walk (a synthetic anchor has no Neo4j node and no edges).
"""

from __future__ import annotations

import pytest

from context_engine.axis import pipeline as axis_pipeline
from context_engine.axis.pipeline import (
    _is_overlay_anchor,
    _overlay_anchor_candidate,
    _pin_anchor_symbol,
)
from context_engine.observability.metrics import MetricsRegistry
from context_engine.overlay import InMemoryOverlay

WS = "ws"
USER = "anonymous"
PATH = "pkg/new_mod.py"
SOURCE = "def brand_new(x):\n    return x + 1\n"


class _FakeLance:
    def _embed(self, texts):  # noqa: D401 - stub
        return [[0.0] * 4]


class _IndexedDb:
    """Index hit: resolves the anchor to a real uid (overlay must lose)."""

    def get_symbol_uid_by_name(self, name, *, workspace_id):
        return "real-uid"

    def get_file_path_for_symbol(self, uid, *, workspace_id):
        return PATH


@pytest.fixture
def overlay():
    ov = InMemoryOverlay(max_entries=0, ttl_seconds=0, metrics=MetricsRegistry())
    ov.update(PATH, SOURCE, workspace_id=WS, user_id=USER)
    return ov


def test_overlay_anchor_candidate_resolves_from_buffer(overlay):
    cand = _overlay_anchor_candidate(
        overlay, name="brand_new", file_path=PATH, workspace_id=WS, user_id=USER
    )
    assert cand is not None
    assert _is_overlay_anchor(cand)
    assert cand.name == "brand_new"
    assert cand.file_path == PATH
    assert cand.uid.startswith("overlay::")


def test_overlay_anchor_candidate_none_when_symbol_absent(overlay):
    assert (
        _overlay_anchor_candidate(
            overlay, name="missing", file_path=PATH, workspace_id=WS, user_id=USER
        )
        is None
    )


def test_overlay_anchor_candidate_none_without_overlay():
    assert (
        _overlay_anchor_candidate(
            None, name="brand_new", file_path=PATH, workspace_id=WS, user_id=USER
        )
        is None
    )


def test_pin_falls_through_to_overlay_on_index_miss(overlay):
    # Bare object() exposes no resolver methods -> index miss -> overlay rung.
    pinned = _pin_anchor_symbol(
        [],
        anchor_symbol="brand_new",
        anchor_path=PATH,
        workspace_id=WS,
        db=object(),
        scanned=None,
        overlay=overlay,
        user_id=USER,
    )
    assert len(pinned) == 1
    assert _is_overlay_anchor(pinned[0])


def test_pin_prefers_index_over_overlay(overlay):
    # The symbol is in BOTH the index and the overlay; the real uid must win.
    pinned = _pin_anchor_symbol(
        [],
        anchor_symbol="brand_new",
        anchor_path=PATH,
        workspace_id=WS,
        db=_IndexedDb(),
        scanned=None,
        overlay=overlay,
        user_id=USER,
    )
    assert len(pinned) == 1
    assert pinned[0].uid == "real-uid"
    assert not _is_overlay_anchor(pinned[0])


def test_run_axis_retrieval_overlay_only_mode(overlay):
    result = axis_pipeline.run_axis_retrieval(
        "what does brand_new do",
        workspace_id=WS,
        db=object(),
        lance=_FakeLance(),
        anchor_symbol="brand_new",
        anchor_path=PATH,
        overlay=overlay,
        user_id=USER,
    )
    # Overlay-only path returns before intent classification and any walk.
    assert result.intent == []
    assert result.raw_by_role == {}
    assert [c.uid for c in result.candidates_for_context] == [result.candidates_for_context[0].uid]
    assert _is_overlay_anchor(result.candidates_for_context[0])

    assert len(result.bundles) == 1
    seed = result.bundles[0].seed
    assert seed.role == "overlay_anchor"
    assert seed.code is not None
    assert "brand_new" in seed.code


def test_overlay_only_skips_graph_walk(overlay, monkeypatch):
    # If the overlay-only branch ever reached the walk it would explode here.
    import context_engine.axis.graph_walk_inproc as gw

    def _boom(*a, **k):
        raise AssertionError("overlay-only mode must not load adjacency")

    monkeypatch.setattr(gw, "load_adjacency", _boom)

    result = axis_pipeline.run_axis_retrieval(
        "what does brand_new do",
        workspace_id=WS,
        db=object(),
        lance=_FakeLance(),
        anchor_symbol="brand_new",
        anchor_path=PATH,
        overlay=overlay,
        user_id=USER,
    )
    assert _is_overlay_anchor(result.candidates_for_context[0])
