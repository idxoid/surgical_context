"""Phase 1d: ASK_AXIS_FIRST routing in ``_resolve_ask_context``.

The flag is opt-in (default off). When set, the axis provider leads; on
nothing-renderable or any failure the cascade falls through to the
unchanged symbol -> file -> workspace -> direct tiers. These tests pin the
routing only (the axis provider itself is stubbed); the legacy tiers are
reached with req.symbol/file_path unset so the fall-through lands on the
deterministic ``direct`` tier (workspace stubbed to None).
"""

from __future__ import annotations

from context_engine import main as context_engine_main


def _req(question: str = "how does routing work") -> context_engine_main.AskRequest:
    return context_engine_main.AskRequest(question=question)


def _ask_level(ctx) -> str:
    return context_engine_main._context_budget(ctx)["ask_level"]


def test_flag_off_never_calls_axis(monkeypatch):
    monkeypatch.delenv("ASK_AXIS_FIRST", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("axis must not run when ASK_AXIS_FIRST is unset")

    monkeypatch.setattr(context_engine_main, "_context_from_axis", _boom)
    # Make the legacy cascade land deterministically on `direct`.
    monkeypatch.setattr(context_engine_main, "_context_from_workspace", lambda *_a, **_k: None)

    ctx = context_engine_main._resolve_ask_context(req=_req(), user_id="u", workspace_id="ws", db=object())
    assert _ask_level(ctx) == "direct_llm"


def test_flag_on_axis_wins(monkeypatch):
    monkeypatch.setenv("ASK_AXIS_FIRST", "1")

    sentinel = object.__new__(type("Ctx", (), {}))  # bare object with a __dict__
    monkeypatch.setattr(context_engine_main, "_context_from_axis", lambda _q, **_k: sentinel)

    ctx = context_engine_main._resolve_ask_context(req=_req(), user_id="u", workspace_id="ws", db=object())
    assert ctx is sentinel
    assert _ask_level(ctx) == "axis"


def test_flag_on_axis_none_falls_through(monkeypatch):
    monkeypatch.setenv("ASK_AXIS_FIRST", "true")
    monkeypatch.setattr(context_engine_main, "_context_from_axis", lambda _q, **_k: None)
    monkeypatch.setattr(context_engine_main, "_context_from_workspace", lambda *_a, **_k: None)

    ctx = context_engine_main._resolve_ask_context(req=_req(), user_id="u", workspace_id="ws", db=object())
    assert _ask_level(ctx) == "direct_llm"


def test_flag_on_axis_error_falls_through(monkeypatch):
    monkeypatch.setenv("ASK_AXIS_FIRST", "on")

    def _raise(_q, **_k):
        raise RuntimeError("axis index missing for workspace")

    monkeypatch.setattr(context_engine_main, "_context_from_axis", _raise)
    monkeypatch.setattr(context_engine_main, "_context_from_workspace", lambda *_a, **_k: None)

    # Must not raise — degrades to the legacy cascade.
    ctx = context_engine_main._resolve_ask_context(req=_req(), user_id="u", workspace_id="ws", db=object())
    assert _ask_level(ctx) == "direct_llm"
