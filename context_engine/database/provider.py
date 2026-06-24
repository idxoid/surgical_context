"""Process-wide Neo4j driver holder for request-scoped Aura clients."""

from __future__ import annotations

import threading
from typing import Any

from context_engine.database.aura_client import AuraClient, connect_neo4j_driver


class DatabaseProvider:
    """Own one Neo4j driver for the context_engine process; mint lightweight clients per request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._driver: Any | None = None
        self.use_aura = False
        self.local_fallback = False
        self._closed = False

    def client_for(self, user_id: str = "anonymous") -> AuraClient:
        driver = self._driver_handle()
        return AuraClient.from_driver(
            driver,
            user_id=user_id,
            use_aura=self.use_aura,
            local_fallback=self.local_fallback,
        )

    def _driver_handle(self) -> Any:
        if self._closed:
            raise RuntimeError("DatabaseProvider is closed")
        if self._driver is not None:
            return self._driver
        with self._lock:
            if self._driver is not None:
                return self._driver
            driver, self.use_aura, self.local_fallback = connect_neo4j_driver()
            self._driver = driver
            return self._driver

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._driver is not None:
                self._driver.close()
                self._driver = None


_provider: DatabaseProvider | None = None
_provider_lock = threading.Lock()


def get_database_provider() -> DatabaseProvider:
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                _provider = DatabaseProvider()
    return _provider


def close_database_provider() -> None:
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.close()
            _provider = None


def reset_database_provider_for_tests() -> None:
    """Drop the singleton so the next request opens a fresh provider."""
    close_database_provider()
