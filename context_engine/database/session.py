"""Request-scoped database session helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

from context_engine.database.aura_client import AuraClient


def create_db(user_id: str = "anonymous") -> AuraClient:
    """Create a fresh Aura/local Neo4j client for one request."""
    return AuraClient(user_id=user_id)


@contextmanager
def db_session(user_id: str = "anonymous") -> Iterator[AuraClient]:
    """Yield a database client and always close it after the request."""
    db = create_db(user_id=user_id)
    try:
        yield db
    finally:
        db.close()
