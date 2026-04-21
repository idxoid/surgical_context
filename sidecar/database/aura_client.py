"""Multi-user Neo4j Aura client with fallback to local Neo4j."""

import os
import logging
from typing import Optional

from sidecar.database.neo4j_client import Neo4jClient


logger = logging.getLogger(__name__)


class AuraClient(Neo4jClient):
    """Neo4j Aura wrapper with cloud-first strategy and local fallback."""

    def __init__(
        self,
        aura_username: Optional[str] = None,
        aura_password: Optional[str] = None,
        aura_instance_name: Optional[str] = None,
        local_uri: Optional[str] = None,
        local_user: Optional[str] = None,
        local_password: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        """
        Initialize Aura client with fallback to local Neo4j.

        Args:
            aura_username: Aura username (env: NEO4JAURA_USERNAME)
            aura_password: Aura password (env: NEO4JAURA_PASSWORD)
            aura_instance_name: Aura instance name (env: NEO4J_INSTANCENAME)
            local_uri: Local Neo4j URI (env: NEO4J_URI)
            local_user: Local Neo4j user (env: NEO4J_USER)
            local_password: Local Neo4j password (env: NEO4J_PASSWORD)
            user_id: User identifier for multi-user tracking (env: USER_ID or auto-detected)
        """
        # Load from env if not provided
        aura_username = aura_username or os.getenv("NEO4JAURA_USERNAME")
        aura_password = aura_password or os.getenv("NEO4JAURA_PASSWORD")
        aura_instance_name = aura_instance_name or os.getenv("NEO4J_INSTANCENAME")
        local_uri = local_uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        local_user = local_user or os.getenv("NEO4J_USER", "neo4j")
        local_password = local_password or os.getenv("NEO4J_PASSWORD", "password")
        self.user_id = user_id or os.getenv("USER_ID", "anonymous")

        self.use_aura = False
        self.local_fallback = False

        # Try Aura first
        if aura_username and aura_password and aura_instance_name:
            try:
                aura_uri = self._build_aura_uri(aura_instance_name)
                super().__init__(aura_uri, aura_username, aura_password)
                self.use_aura = True
                logger.info(f"✅ Connected to Neo4j Aura ({aura_instance_name}) as {aura_username}")
            except Exception as e:
                logger.warning(f"⚠️ Aura connection failed: {e}. Falling back to local Neo4j.")
                self.use_aura = False
                try:
                    super().__init__(local_uri, local_user, local_password)
                    self.local_fallback = True
                    logger.info(f"✅ Connected to local Neo4j ({local_uri}) as fallback")
                except Exception as e2:
                    logger.error(f"❌ Both Aura and local Neo4j failed: {e2}")
                    raise
        else:
            # No Aura config, use local Neo4j
            super().__init__(local_uri, local_user, local_password)
            logger.info(f"✅ Connected to local Neo4j ({local_uri}) as {local_user}")

    @staticmethod
    def _build_aura_uri(instance_name: str) -> str:
        """Build Aura URI from instance name."""
        # Aura URI format: neo4j+s://{instance-id}.databases.neo4j.io
        if "databases.neo4j.io" in instance_name:
            # Already a full URI-like string
            return f"neo4j+s://{instance_name}"
        else:
            # Just the instance ID
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
                result = session.run("RETURN 1")
                return {
                    "status": "healthy",
                    "mode": "aura" if self.use_aura else "local",
                    "user": self.user_id,
                }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e), "mode": "aura" if self.use_aura else "local"}
