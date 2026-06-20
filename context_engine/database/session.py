"""Request-scoped database session helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

from context_engine.database.aura_client import AuraClient
from context_engine.database.provider import get_database_provider


def create_db(user_id: str = "anonymous") -> AuraClient:
    """Return a request-scoped client view over the process-wide Neo4j driver."""
    return get_database_provider().client_for(user_id)


@contextmanager
def db_session(user_id: str = "anonymous") -> Iterator[AuraClient]:
    """Yield a database client for one request without closing the shared driver."""
    db = create_db(user_id=user_id)
    try:
        yield db
    finally:
        db.close()
