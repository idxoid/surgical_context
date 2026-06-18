"""Unit tests for request-scoped database sessions."""

import pytest

from context_engine.database import session


class FakeDb:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.closed = False

    def close(self):
        self.closed = True


class TestDatabaseSession:
    def test_db_session_creates_fresh_client_per_request(self, monkeypatch):
        created = []

        def fake_create_db(user_id="anonymous"):
            db = FakeDb(user_id)
            created.append(db)
            return db

        monkeypatch.setattr(session, "create_db", fake_create_db)

        with session.db_session(user_id="alice") as first:
            assert first.user_id == "alice"
            assert first.closed is False

        with session.db_session(user_id="bob") as second:
            assert second.user_id == "bob"
            assert second.closed is False

        assert first is not second
        assert [db.user_id for db in created] == ["alice", "bob"]
        assert all(db.closed for db in created)

    def test_db_session_closes_client_on_exception(self, monkeypatch):
        created = []

        def fake_create_db(user_id="anonymous"):
            db = FakeDb(user_id)
            created.append(db)
            return db

        monkeypatch.setattr(session, "create_db", fake_create_db)

        with pytest.raises(RuntimeError, match="boom"):
            with session.db_session(user_id="alice"):
                raise RuntimeError("boom")

        assert len(created) == 1
        assert created[0].closed is True
