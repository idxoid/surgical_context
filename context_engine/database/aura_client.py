"""Multi-user Neo4j Aura client with fallback to local Neo4j."""

from __future__ import annotations

import logging
import os
from typing import Any

from neo4j import GraphDatabase

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.database.neo4j_env import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

logger = logging.getLogger(__name__)


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def connect_neo4j_driver(
    *,
    aura_username: str | None = None,
    aura_password: str | None = None,
    aura_instance_name: str | None = None,
    local_uri: str | None = None,
    local_user: str | None = None,
    local_password: str | None = None,
) -> tuple[Any, bool, bool]:
    """Connect once with Aura-first fallback. Returns (driver, use_aura, local_fallback)."""
    aura_username = aura_username or os.getenv("NEO4JAURA_USERNAME")
    aura_password = aura_password or os.getenv("NEO4JAURA_PASSWORD")
    aura_instance_name = aura_instance_name or os.getenv("NEO4J_INSTANCENAME")
    resolved_local_uri = local_uri if local_uri is not None else NEO4J_URI
    resolved_local_user = local_user if local_user is not None else NEO4J_USER
    resolved_local_password = local_password if local_password is not None else NEO4J_PASSWORD
    local_only = _env_truthy("NEO4J_LOCAL_ONLY")

    if not local_only and aura_username and aura_password and aura_instance_name:
        try:
            aura_uri = AuraClient._build_aura_uri(aura_instance_name)
            driver = GraphDatabase.driver(aura_uri, auth=(aura_username, aura_password))
            with driver.session() as session:
                session.run("RETURN 1").consume()
            logger.info(
                "Connected to Neo4j Aura (%s) as %s",
                aura_instance_name,
                aura_username,
            )
            return driver, True, False
        except Exception as exc:
            logger.warning("Aura connection failed: %s. Falling back to local Neo4j.", exc)
            try:
                driver = GraphDatabase.driver(
                    resolved_local_uri,
                    auth=(resolved_local_user, resolved_local_password),
                )
                with driver.session() as session:
                    session.run("RETURN 1").consume()
                logger.info("Connected to local Neo4j (%s) as fallback", resolved_local_uri)
                return driver, False, True
            except Exception:
                logger.exception("Both Aura and local Neo4j failed")
                raise

    driver = GraphDatabase.driver(
        resolved_local_uri,
        auth=(resolved_local_user, resolved_local_password),
    )
    with driver.session() as session:
        session.run("RETURN 1").consume()
    logger.info("Connected to local Neo4j (%s) as %s", resolved_local_uri, resolved_local_user)
    return driver, False, False


class AuraClient(Neo4jClient):
    """Neo4j Aura wrapper with cloud-first strategy and local fallback."""

    def __init__(
        self,
        aura_username: str | None = None,
        aura_password: str | None = None,
        aura_instance_name: str | None = None,
        local_uri: str | None = None,
        local_user: str | None = None,
        local_password: str | None = None,
        user_id: str | None = None,
        *,
        driver: Any | None = None,
        use_aura: bool | None = None,
        local_fallback: bool | None = None,
    ):
        self.user_id = user_id or os.getenv("USER_ID", "anonymous")

        if driver is not None:
            super().__init__(driver=driver)
            self.use_aura = bool(use_aura)
            self.local_fallback = bool(local_fallback)
            return

        connected_driver, self.use_aura, self.local_fallback = connect_neo4j_driver(
            aura_username=aura_username,
            aura_password=aura_password,
            aura_instance_name=aura_instance_name,
            local_uri=local_uri,
            local_user=local_user,
            local_password=local_password,
        )
        self.driver = connected_driver
        self._owns_driver = True

    @classmethod
    def from_driver(
        cls,
        driver: Any,
        *,
        user_id: str = "anonymous",
        use_aura: bool = False,
        local_fallback: bool = False,
    ) -> AuraClient:
        """Request-scoped view over a process-wide driver (does not close the driver)."""
        return cls(
            user_id=user_id,
            driver=driver,
            use_aura=use_aura,
            local_fallback=local_fallback,
        )

    @staticmethod
    def _build_aura_uri(instance_name: str) -> str:
        """Build Aura URI from instance name."""
        if "databases.neo4j.io" in instance_name:
            return f"neo4j+s://{instance_name}"
        return f"neo4j+s://{instance_name}.databases.neo4j.io"

    def add_user_metadata(self, node_type: str, node_id: str, metadata: dict):
        """Add user-specific metadata to a node (e.g., last_modified_by, modified_at)."""
        import time

        metadata["modified_by"] = self.user_id
        metadata["modified_at"] = int(time.time())
        metadata["source"] = "aura" if self.use_aura else "local"

    def is_cloud(self) -> bool:
        """Check if using Aura cloud."""
        return self.use_aura

    def is_fallback(self) -> bool:
        """Check if using local fallback."""
        return self.local_fallback

    def health_check(self) -> dict:
        """Health check: verify connection is alive."""
        try:
            with self.driver.session() as session:
                _result = session.run("RETURN 1")
                return {
                    "status": "healthy",
                    "mode": "aura" if self.use_aura else "local",
                    "user": self.user_id,
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "mode": "aura" if self.use_aura else "local",
            }
