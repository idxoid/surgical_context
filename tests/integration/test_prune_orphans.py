"""Live round-trips for the prune owner-count fix + orphan sweep.

The historical owner-check ``OPTIONAL MATCH (:File)-[:CONTAINS]->(sym)
WITH sym, count(*) AS owners WHERE owners = 0`` never fired (count(*) counts
the null row), so replaced symbols were unlinked but the nodes survived as
file-less orphans holding stale semantic edges. These tests pin the fixed
behavior against a real Neo4j: pruned symbols are actually deleted, deleted
files take their symbols with them, and ``prune_orphan_symbols`` sweeps
pre-existing orphans without touching file-linked nodes.
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
    ws = f"test/prune_orphans@{uuid.uuid4().hex[:8]}"
    yield ws
    with db.driver.session() as session:
        session.run(
            "MATCH (n {workspace_id: $ws}) DETACH DELETE n",
            ws=ws,
        )
        session.run("MATCH (w:Workspace {id: $ws}) DETACH DELETE w", ws=ws)


def _seed_file_with_symbols(db: Neo4jClient, ws: str, path: str, uids: list[str]) -> None:
    with db.driver.session() as session:
        session.run(
            """
            MERGE (w:Workspace {id: $ws})
            MERGE (f:File {path: $path, workspace_id: $ws})
            WITH f
            UNWIND $uids AS uid
            MERGE (s:Symbol {uid: uid})
            SET s.workspace_id = $ws,
                s.name = uid,
                s.qualified_name = 'mod.' + uid,
                s.kind = 'function'
            MERGE (f)-[:CONTAINS {workspace_id: $ws}]->(s)
            """,
            ws=ws,
            path=path,
            uids=uids,
        )


def _symbol_uids(db: Neo4jClient, ws: str) -> set[str]:
    with db.driver.session() as session:
        rows = session.run(
            "MATCH (s:Symbol {workspace_id: $ws}) RETURN s.uid AS uid",
            ws=ws,
        )
        return {str(row["uid"]) for row in rows}


@pytest.mark.integration
def test_prune_symbols_for_file_deletes_replaced_symbols(db, workspace_id):
    _seed_file_with_symbols(db, workspace_id, "/tmp/a.py", ["keep_1", "drop_1"])
    # Non-whitelisted semantic edge into the doomed symbol — the DETACH DELETE
    # must remove it even though the prune whitelist does not cover USES_TYPE.
    with db.driver.session() as session:
        session.run(
            """
            MATCH (keep:Symbol {uid: 'keep_1'}), (drop:Symbol {uid: 'drop_1'})
            MERGE (keep)-[:USES_TYPE {workspace_id: $ws}]->(drop)
            """,
            ws=workspace_id,
        )

    db.prune_symbols_for_file("/tmp/a.py", keep_uids=["keep_1"], workspace_id=workspace_id)

    assert _symbol_uids(db, workspace_id) == {"keep_1"}


@pytest.mark.integration
def test_delete_symbols_for_file_deletes_its_symbols(db, workspace_id):
    _seed_file_with_symbols(db, workspace_id, "/tmp/b.py", ["b_1", "b_2"])

    db.delete_symbols_for_file("/tmp/b.py", workspace_id=workspace_id)

    assert _symbol_uids(db, workspace_id) == set()
    with db.driver.session() as session:
        remaining = session.run(
            "MATCH (f:File {workspace_id: $ws}) RETURN count(f) AS n",
            ws=workspace_id,
        ).single()["n"]
    assert remaining == 0


@pytest.mark.integration
def test_prune_orphan_symbols_sweeps_only_file_less_nodes(db, workspace_id):
    _seed_file_with_symbols(db, workspace_id, "/tmp/c.py", ["live_1"])
    with db.driver.session() as session:
        session.run(
            """
            MERGE (o:Symbol {uid: 'orphan_1'})
            SET o.workspace_id = $ws, o.name = 'orphan_1', o.kind = 'function'
            WITH o
            MATCH (live:Symbol {uid: 'live_1'})
            MERGE (live)-[:USES_TYPE {workspace_id: $ws}]->(o)
            """,
            ws=workspace_id,
        )

    pruned = db.prune_orphan_symbols(workspace_id=workspace_id)

    assert pruned == 1
    assert _symbol_uids(db, workspace_id) == {"live_1"}
    # Idempotent when clean.
    assert db.prune_orphan_symbols(workspace_id=workspace_id) == 0
