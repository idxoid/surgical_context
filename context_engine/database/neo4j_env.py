"""Neo4j connection settings resolved from environment (and repo ``.env``)."""

from __future__ import annotations

import os

from context_engine.env_loader import load_repo_dotenv

load_repo_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
