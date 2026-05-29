from unittest.mock import MagicMock

from sidecar.database.neo4j_client import Neo4jClient


def test_create_decorator_relations_materializes_handles_inverse():
    tx = MagicMock()
    Neo4jClient._create_decorator_relations(
        tx,
        [
            {
                "decorated_uid": "handler-u",
                "decorator_name": "task",
                "decorator_qualified_name": "celery.app.task",
            }
        ],
        "local/test@main",
    )

    tx.run.assert_called_once()
    query = tx.run.call_args[0][0]
    assert "DECORATED_BY" in query
    assert "HANDLES" in query
    assert "MERGE (deco)-[h:HANDLES" in query
