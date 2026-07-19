"""Live round-trips for FLOWS_INTO linking against a real Neo4j.

Covers the linker's three resolution tiers (uid / exact qn / workspace-unique
name), the drop of unresolvable endpoints, and the caller-scoped incremental
clear that a property-anchored edge needs (a deleted caller function cannot
take its pairs down via DETACH DELETE).
"""

from __future__ import annotations

import uuid

import pytest

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER


@pytest.fixture()
def db() -> Neo4jClient:
    try:
        client = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        with client.driver.session() as session:
            session.run("RETURN 1").single()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"Neo4j unavailable: {exc}")
    yield client
    client.close()


@pytest.fixture()
def workspace_id(db: Neo4jClient):
    ws = f"test/flow_pairs@{uuid.uuid4().hex[:8]}"
    yield ws
    with db.driver.session() as session:
        session.run("MATCH (n {workspace_id: $ws}) DETACH DELETE n", ws=ws)
        session.run("MATCH (w:Workspace {id: $ws}) DETACH DELETE w", ws=ws)


def _seed_symbols(db: Neo4jClient, ws: str, symbols: list[tuple[str, str]]) -> None:
    with db.driver.session() as session:
        session.run(
            """
            MERGE (w:Workspace {id: $ws})
            MERGE (f:File {path: '/tmp/flow.py', workspace_id: $ws})
            WITH f
            UNWIND $symbols AS sym
            MERGE (s:Symbol {uid: sym.uid})
            SET s.workspace_id = $ws,
                s.name = sym.name,
                s.qualified_name = sym.qn,
                s.kind = 'function'
            MERGE (f)-[:CONTAINS {workspace_id: $ws}]->(s)
            """,
            ws=ws,
            symbols=[{"uid": uid, "name": qn.rsplit(".", 1)[-1], "qn": qn} for uid, qn in symbols],
        )


def _flow_edges(db: Neo4jClient, ws: str) -> set[tuple[str, str]]:
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (a:Symbol)-[r:FLOWS_INTO {workspace_id: $ws}]->(b:Symbol)
            RETURN a.uid AS src, b.uid AS dst
            """,
            ws=ws,
        )
        return {(str(row["src"]), str(row["dst"])) for row in rows}


@pytest.mark.integration
def test_link_flow_pairs_resolves_uid_qn_and_unique_name(db, workspace_id):
    _seed_symbols(
        db,
        workspace_id,
        [
            ("u_prod", "pkg.lib.produce"),
            ("u_cons", "pkg.lib.consume"),
            ("u_boost", "pkg.lib.boost"),
        ],
    )
    linked = db.link_flow_pairs(
        [
            # uid -> exact qn
            {
                "caller_uid": "u_caller",
                "source_uid": "u_prod",
                "source_qualified_name": "",
                "source_name": "produce",
                "target_uid": "",
                "target_qualified_name": "pkg.lib.consume",
                "target_name": "consume",
                "line": 5,
            },
            # unique-name fallback target
            {
                "caller_uid": "u_caller",
                "source_uid": "u_prod",
                "source_qualified_name": "",
                "source_name": "produce",
                "target_uid": "",
                "target_qualified_name": "",
                "target_name": "boost",
                "line": 7,
            },
            # unresolvable target -> dropped
            {
                "caller_uid": "u_caller",
                "source_uid": "u_prod",
                "source_qualified_name": "",
                "source_name": "produce",
                "target_uid": "",
                "target_qualified_name": "",
                "target_name": "nowhere_to_be_found",
                "line": 9,
            },
        ],
        workspace_id=workspace_id,
    )
    assert linked == 2
    assert _flow_edges(db, workspace_id) == {("u_prod", "u_cons"), ("u_prod", "u_boost")}


@pytest.mark.integration
def test_delete_flow_pairs_for_callers_clears_only_their_pairs(db, workspace_id):
    _seed_symbols(
        db,
        workspace_id,
        [("u_a", "pkg.lib.a"), ("u_b", "pkg.lib.b"), ("u_c", "pkg.lib.c")],
    )
    rows = [
        {
            "caller_uid": caller,
            "source_uid": src,
            "source_qualified_name": "",
            "source_name": "",
            "target_uid": dst,
            "target_qualified_name": "",
            "target_name": "",
            "line": 1,
        }
        for caller, src, dst in [
            ("caller_one", "u_a", "u_b"),
            ("caller_two", "u_b", "u_c"),
        ]
    ]
    assert db.link_flow_pairs(rows, workspace_id=workspace_id) == 2

    db.delete_flow_pairs_for_callers(["caller_one"], workspace_id=workspace_id)

    assert _flow_edges(db, workspace_id) == {("u_b", "u_c")}
