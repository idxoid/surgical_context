from __future__ import annotations

import pytest

from context_engine import main as context_engine_main
from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, INDEX_PROFILE_ENV


@pytest.fixture(autouse=True)
def axis_profile(monkeypatch):
    monkeypatch.setenv(INDEX_PROFILE_ENV, AXIS_PYTHON_V1_PROFILE)


def test_resolve_index_workspace_applies_profile_suffix(monkeypatch):
    monkeypatch.setattr(
        context_engine_main,
        "_resolve_workspace",
        lambda *a, **k: "local/repo@main",
    )
    assert context_engine_main._resolve_index_workspace() == "local/repo@main+axis_python_v1"


def test_resolve_ask_context_passes_suffixed_workspace_to_axis(monkeypatch):
    seen: dict[str, str] = {}

    def fake_axis(
        _question,
        *,
        base_workspace_id,
        index_workspace_id,
        db,
        token_budget=6000,
        anchor_path=None,
        anchor_symbol=None,
        trace_id="",
        user_id="anonymous",
    ):
        seen["base"] = base_workspace_id
        seen["index"] = index_workspace_id
        sentinel = object.__new__(type("Ctx", (), {}))
        sentinel.budget = {}
        return sentinel

    monkeypatch.setenv("ASK_AXIS_FIRST", "1")
    monkeypatch.setattr(context_engine_main, "_context_from_axis", fake_axis)

    context_engine_main._resolve_ask_context(
        req=context_engine_main.AskRequest(question="how does routing work"),
        user_id="u",
        workspace_id="local/repo@main",
        db=object(),
    )

    assert seen["base"] == "local/repo@main"
    assert seen["index"] == "local/repo@main+axis_python_v1"
