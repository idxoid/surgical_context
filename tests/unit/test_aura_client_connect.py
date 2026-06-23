"""Unit tests for Neo4j Aura vs local connection selection."""

from __future__ import annotations

import pytest

from context_engine.database.aura_client import connect_neo4j_driver


class FakeSession:
    def run(self, _query: str):
        return self

    def consume(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeDriver:
    def __init__(self, uri: str, auth: tuple[str, str]) -> None:
        self.uri = uri
        self.auth = auth
        self.closed = False

    def session(self):
        return FakeSession()

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clear_neo4j_env(monkeypatch):
    for key in (
        "NEO4JAURA_USERNAME",
        "NEO4JAURA_PASSWORD",
        "NEO4J_INSTANCENAME",
        "NEO4J_URI",
        "NEO4J_USER",
        "NEO4J_PASSWORD",
        "NEO4J_LOCAL_ONLY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_connect_skips_aura_when_local_only(monkeypatch):
    calls: list[tuple[str, tuple[str, str]]] = []

    def fake_driver(uri: str, auth: tuple[str, str]):
        calls.append((uri, auth))
        return FakeDriver(uri, auth)

    monkeypatch.setenv("NEO4JAURA_USERNAME", "aura-user")
    monkeypatch.setenv("NEO4JAURA_PASSWORD", "aura-pass")
    monkeypatch.setenv("NEO4J_INSTANCENAME", "Instance01")
    monkeypatch.setenv("NEO4J_LOCAL_ONLY", "1")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password")
    monkeypatch.setattr("context_engine.database.aura_client.GraphDatabase.driver", fake_driver)

    driver, use_aura, local_fallback = connect_neo4j_driver()

    assert use_aura is False
    assert local_fallback is False
    assert driver.uri == "bolt://localhost:7687"
    assert calls == [("bolt://localhost:7687", ("neo4j", "password"))]


def test_connect_tries_aura_before_local_fallback(monkeypatch):
    calls: list[str] = []

    def fake_driver(uri: str, auth: tuple[str, str]):
        calls.append(uri)
        if "databases.neo4j.io" in uri:
            raise OSError("Aura unreachable")
        return FakeDriver(uri, auth)

    monkeypatch.setenv("NEO4JAURA_USERNAME", "aura-user")
    monkeypatch.setenv("NEO4JAURA_PASSWORD", "aura-pass")
    monkeypatch.setenv("NEO4J_INSTANCENAME", "Instance01")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "password")
    monkeypatch.setattr("context_engine.database.aura_client.GraphDatabase.driver", fake_driver)

    driver, use_aura, local_fallback = connect_neo4j_driver()

    assert use_aura is False
    assert local_fallback is True
    assert driver.uri == "bolt://localhost:7687"
    assert "Instance01.databases.neo4j.io" in calls[0]
    assert calls[1] == "bolt://localhost:7687"
