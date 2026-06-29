"""Impact resolves symbols across index-profile namespaces."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from context_engine.api.routes import impact as impact_routes


def test_impact_falls_back_to_axis_namespace_without_index_profile_env(monkeypatch):
    monkeypatch.delenv("INDEX_PROFILE", raising=False)

    seen_workspaces: list[str] = []

    class FakeDb:
        def resolve_impact_symbol_uid(self, name, workspace_id="ws", *, file_path=None):
            del name, file_path
            seen_workspaces.append(workspace_id)
            if workspace_id.endswith("+axis_python_v1"):
                return "uid-axis"
            return None

        def get_file_path_for_symbol(self, uid, workspace_id="ws"):
            del uid, workspace_id
            return "/repo/app.py"

    @contextmanager
    def db_session(user_id="anonymous"):
        del user_id
        yield FakeDb()

    class FakeMain:
        overlay = None

        @staticmethod
        def _resolve_request_user(*_a, **_k):
            return "alice"

        @staticmethod
        def _resolve_workspace(*_a, **_k):
            return "qa_repo/surgical_context@main"

        @staticmethod
        def effective_index_workspace_id(base):
            from context_engine.index_profile import effective_index_workspace_id

            return effective_index_workspace_id(base)

        @staticmethod
        def db_session(user_id="anonymous"):
            return db_session(user_id)

        @staticmethod
        def _sandbox_path(raw_path, *, workspace_id, db):
            del workspace_id, db
            return raw_path

    monkeypatch.setattr(impact_routes, "require_services", lambda _request=None: FakeMain())
    monkeypatch.setattr(
        "context_engine.axis.impact_surface.build_impact_surface",
        lambda **kwargs: {
            "affected_symbols": [],
            "affected_files": [],
            "max_depth": kwargs["max_depth"],
        },
    )

    body = impact_routes.impact(
        symbol="_resolve_committed_uid",
        max_depth=3,
        file_path="/repo/context_engine/api/routes/impact.py",
        request=None,
    )

    assert body["symbol_uid"] == "uid-axis"
    assert "qa_repo/surgical_context@main" in seen_workspaces
    assert "qa_repo/surgical_context@main+axis_python_v1" in seen_workspaces


def test_impact_local_file_anchor_avoids_404_for_unindexed_symbol(monkeypatch, tmp_path):
    monkeypatch.delenv("INDEX_PROFILE", raising=False)
    source = tmp_path / "app.py"
    source.write_text("def brand_new():\n    return 1\n", encoding="utf-8")

    class FakeDb:
        def resolve_impact_symbol_uid(self, *_a, **_k):
            return None

        def get_symbol_uid_by_name_in_file(self, *_a, **_k):
            return None

        def get_symbol_uid_by_name(self, *_a, **_k):
            return None

    @contextmanager
    def db_session(user_id="anonymous"):
        del user_id
        yield FakeDb()

    class FakeOverlay:
        def has(self, *_a, **_k):
            return False

    class FakeMain:
        overlay = FakeOverlay()

        @staticmethod
        def _resolve_request_user(*_a, **_k):
            return "alice"

        @staticmethod
        def _resolve_workspace(*_a, **_k):
            return "local/repo@main"

        @staticmethod
        def effective_index_workspace_id(base):
            from context_engine.index_profile import effective_index_workspace_id

            return effective_index_workspace_id(base)

        @staticmethod
        def db_session(user_id="anonymous"):
            return db_session(user_id)

        @staticmethod
        def _sandbox_path(raw_path, *, workspace_id, db):
            del workspace_id, db
            return str(source)

    monkeypatch.setattr(impact_routes, "require_services", lambda _request=None: FakeMain())
    monkeypatch.setattr(
        "context_engine.axis.overlay_impact.build_overlay_impact_callers",
        lambda *_a, **_k: [],
    )

    body = impact_routes.impact(
        symbol="brand_new",
        max_depth=3,
        file_path=str(source),
        request=None,
    )

    assert body["degraded"] is True
    assert body["symbol"] == "brand_new"
    assert body["affected_count"] == 0


def test_impact_still_404_when_symbol_missing_everywhere(monkeypatch):
    monkeypatch.delenv("INDEX_PROFILE", raising=False)

    class FakeDb:
        def resolve_impact_symbol_uid(self, *_a, **_k):
            return None

        def get_symbol_uid_by_name_in_file(self, *_a, **_k):
            return None

        def get_symbol_uid_by_name(self, *_a, **_k):
            return None

    @contextmanager
    def db_session(user_id="anonymous"):
        del user_id
        yield FakeDb()

    class FakeMain:
        overlay = None

        @staticmethod
        def _resolve_request_user(*_a, **_k):
            return "alice"

        @staticmethod
        def _resolve_workspace(*_a, **_k):
            return "local/repo@main"

        @staticmethod
        def effective_index_workspace_id(base):
            from context_engine.index_profile import effective_index_workspace_id

            return effective_index_workspace_id(base)

        @staticmethod
        def db_session(user_id="anonymous"):
            return db_session(user_id)

        @staticmethod
        def _sandbox_path(raw_path, *, workspace_id, db):
            del workspace_id, db
            return raw_path

    monkeypatch.setattr(impact_routes, "require_services", lambda _request=None: FakeMain())

    with pytest.raises(HTTPException) as exc_info:
        impact_routes.impact(
            symbol="missing_symbol",
            max_depth=3,
            file_path="/repo/nope.py",
            request=None,
        )

    assert exc_info.value.status_code == 404
