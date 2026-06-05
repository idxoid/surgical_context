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
                "decorator_owner_name": "celery.app",
                "decorator_owner_qualified_name": "celery.app",
            }
        ],
        "local/test@main",
    )

    assert tx.run.call_count == 2
    query = tx.run.call_args_list[0][0][0]
    assert "DECORATED_BY" in query
    assert "HANDLES" in query
    assert "MERGE (deco)-[h:HANDLES" in query
    owner_query = tx.run.call_args_list[1][0][0]
    assert "decorator_owner_qualified_name" in owner_query
    assert "MERGE (owner)-[h:HANDLES" in owner_query
    assert "decorator-owner-v1" in owner_query
    assert "owner.kind" in owner_query
