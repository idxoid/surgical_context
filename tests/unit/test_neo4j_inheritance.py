from unittest.mock import MagicMock

from sidecar.database.neo4j_client import Neo4jClient
from sidecar.parser.protocol import ClassApiEdge, InheritanceEdge


def test_builtin_exception_propagation_uses_direct_steps_not_variable_path():
    tx = MagicMock()
    tx.run.return_value.single.return_value = {"updated": 0}

    Neo4jClient._create_inheritance_relations(
        tx,
        [InheritanceEdge("error-u", "Exception", False)],
        "local/test@main",
    )

    queries = [call.args[0] for call in tx.run.call_args_list]
    assert not any("DEPENDS_ON*1..6" in query for query in queries)
    assert any("-[r:DEPENDS_ON]->" in query for query in queries)


def test_link_class_api_writes_edges_in_batches():
    execute_sizes: list[int] = []
    run_queries: list[str] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute_write(self, callback, edges, workspace_id):
            execute_sizes.append(len(edges))

        def run(self, query, **params):
            run_queries.append(query)

    class FakeDriver:
        def session(self):
            return FakeSession()

    client = Neo4jClient.__new__(Neo4jClient)
    client.driver = FakeDriver()
    edges = [
        ClassApiEdge(class_uid=f"c-{idx}", method_uid=f"m-{idx}", edge_type="HAS_API")
        for idx in range(1001)
    ]

    client.link_class_api(edges, workspace_id="local/test@main")

    assert execute_sizes == [1000, 1]
    assert any("graph_version" in query for query in run_queries)


def test_clear_class_api_edges_deletes_in_limited_batches():
    run_calls: list[tuple[str, dict]] = []
    delete_counts = [5000, 17]

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def single(self):
            return self.row

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query, **params):
            run_calls.append((query, params))
            if "RETURN count(*) AS deleted_edges" in query:
                return FakeResult({"deleted_edges": delete_counts.pop(0)})
            return FakeResult({})

    class FakeDriver:
        def session(self):
            return FakeSession()

    client = Neo4jClient.__new__(Neo4jClient)
    client.driver = FakeDriver()

    client.clear_class_api_edges(workspace_id="local/test@main")

    delete_queries = [
        query for query, _ in run_calls if "RETURN count(*) AS deleted_edges" in query
    ]
    assert len(delete_queries) == 2
    assert all("collect(r)" not in query for query in delete_queries)
    assert all("LIMIT $limit" in query for query in delete_queries)
    assert any("graph_version" in query for query, _ in run_calls)
