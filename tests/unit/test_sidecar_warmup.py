"""Sidecar startup warmup."""

from __future__ import annotations

from context_engine.api import warmup as warmup_mod
from context_engine.api.state import SidecarState


class _FakeLance:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def warmup(self, *, workspace_id: str | None = None) -> None:
        self.calls.append(workspace_id)


class _FakeAskContextBuilder:
    def __init__(self, default: _FakeLance, axis: _FakeLance | None = None) -> None:
        self.vector_db = default
        self._axis = axis

    def lance_for_index_workspace(self, index_workspace_id: str) -> _FakeLance:
        del index_workspace_id
        return self._axis or self.vector_db


class _FakeNeo4j:
    def health_check(self) -> dict:
        return {"ok": True}


class _FakeProvider:
    def client_for(self, user_id: str = "anonymous") -> _FakeNeo4j:
        del user_id
        return _FakeNeo4j()


def _fake_state(*, axis: _FakeLance | None = None) -> SidecarState:
    default = _FakeLance()
    state = object.__new__(SidecarState)
    state.vector_db = default  # type: ignore[attr-defined]
    state.ask_context_builder = _FakeAskContextBuilder(default, axis)  # type: ignore[attr-defined]
    return state


def test_warm_sidecar_runs_default_and_axis_clients(monkeypatch):
    axis = _FakeLance()
    state = _fake_state(axis=axis)
    monkeypatch.setenv("SIDECAR_WARMUP_ENABLED", "true")
    monkeypatch.setattr(
        "context_engine.database.provider.get_database_provider",
        lambda: _FakeProvider(),
    )

    warmup_mod.warm_sidecar(state)

    assert len(state.vector_db.calls) == 1  # type: ignore[attr-defined]
    assert len(axis.calls) == 1


def test_warm_sidecar_skipped_when_disabled(monkeypatch):
    state = _fake_state()
    monkeypatch.setenv("SIDECAR_WARMUP_ENABLED", "false")

    warmup_mod.warm_sidecar(state)

    assert state.vector_db.calls == []  # type: ignore[attr-defined]
