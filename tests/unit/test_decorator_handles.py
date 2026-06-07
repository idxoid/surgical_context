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

    # Three statements: deco-symbol HANDLES, class/interface owner HANDLES,
    # variable owner HANDLES (gated by INSTANTIATES_EXTERNAL).
    assert tx.run.call_count == 3

    query = tx.run.call_args_list[0][0][0]
    assert "DECORATED_BY" in query
    assert "HANDLES" in query
    assert "MERGE (deco)-[h:HANDLES" in query

    class_owner_query = tx.run.call_args_list[1][0][0]
    assert "decorator_owner_qualified_name" in class_owner_query
    assert "MERGE (owner)-[h:HANDLES" in class_owner_query
    assert "decorator-owner-v1" in class_owner_query
    assert "owner.kind" in class_owner_query
    assert "['class', 'interface']" in class_owner_query

    var_owner_query = tx.run.call_args_list[2][0][0]
    assert "decorator-owner-v1-variable" in var_owner_query
    # The variable branch must guard against unrelated module-level vars
    # by requiring an outgoing INSTANTIATES_EXTERNAL edge.
    assert "INSTANTIATES_EXTERNAL" in var_owner_query
    assert "'variable'" in var_owner_query
