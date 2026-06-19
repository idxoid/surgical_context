"""Unit tests for request-scoped database sessions."""

import pytest

from context_engine.database import session
from context_engine.database.provider import (
    close_database_provider,
    get_database_provider,
    reset_database_provider_for_tests,
)


class FakeDriver:
    def __init__(self) -> None:
        self.closed = False
        self.sessions = 0

    def close(self) -> None:
        self.closed = True

    def session(self):
        self.sessions += 1
        return self


class FakeAuraClient:
    def __init__(self, *, user_id: str, driver: FakeDriver, owns_driver: bool):
        self.user_id = user_id
        self.driver = driver
        self._owns_driver = owns_driver
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self._owns_driver:
            self.driver.close()

    @classmethod
    def from_driver(cls, driver: FakeDriver, *, user_id: str, **_flags):
        return cls(user_id=user_id, driver=driver, owns_driver=False)


class FakeProvider:
    def __init__(self, driver: FakeDriver):
        self.driver = driver
        self.client_calls: list[str] = []

    def client_for(self, user_id: str = "anonymous") -> FakeAuraClient:
        self.client_calls.append(user_id)
        return FakeAuraClient.from_driver(self.driver, user_id=user_id)


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_database_provider_for_tests()
    yield
    reset_database_provider_for_tests()


class TestDatabaseSession:
    def test_db_session_reuses_shared_driver_across_requests(self, monkeypatch):
        driver = FakeDriver()
        provider = FakeProvider(driver)
        monkeypatch.setattr(session, "get_database_provider", lambda: provider)

        with session.db_session(user_id="alice") as first:
            assert first.user_id == "alice"
            assert first.driver is driver
            assert first.closed is False

        with session.db_session(user_id="bob") as second:
            assert second.user_id == "bob"
            assert second.driver is driver
            assert second.closed is False

        assert first is not second
        assert provider.client_calls == ["alice", "bob"]
        assert first.closed is True
        assert second.closed is True
        assert driver.closed is False

    def test_db_session_does_not_close_shared_driver_on_exception(self, monkeypatch):
        driver = FakeDriver()
        provider = FakeProvider(driver)
        monkeypatch.setattr(session, "get_database_provider", lambda: provider)

        with pytest.raises(RuntimeError, match="boom"):
            with session.db_session(user_id="alice"):
                raise RuntimeError("boom")

        assert driver.closed is False
        assert provider.client_calls == ["alice"]

    def test_provider_closes_driver_on_shutdown(self, monkeypatch):
        driver = FakeDriver()

        def fake_connect():
            return driver, False, False

        monkeypatch.setattr(
            "context_engine.database.provider.connect_neo4j_driver",
            fake_connect,
        )

        provider = get_database_provider()
        client = provider.client_for("alice")
        assert client.driver is driver

        close_database_provider()

        assert driver.closed is True

        with pytest.raises(RuntimeError, match="DatabaseProvider is closed"):
            provider.client_for("bob")
