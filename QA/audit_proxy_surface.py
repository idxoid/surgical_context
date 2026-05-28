#!/usr/bin/env python3
"""Re-index a QA repo and audit ProxySurface + Tier-4.5 (return/typed) edge quality."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(QA_DIR))

from qa_benchmark import default_repo_checkout_path, setup_real_repo_db  # noqa: E402

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")


def _audit_workspace(workspace_id: str) -> dict:
    from sidecar.database.neo4j_client import Neo4jClient

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        with db.driver.session() as session:
            tier_rows = session.run(
                """
                MATCH ()-[r:CALLS_DYNAMIC {workspace_id: $ws}]->()
                RETURN coalesce(r.tier, '(null)') AS tier, count(*) AS n
                ORDER BY n DESC
                """,
                ws=workspace_id,
            ).data()

            proxy_bindings = session.run(
                """
                MATCH (:File {workspace_id: $ws})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding'})
                RETURN count(p) AS n
                """,
                ws=workspace_id,
            ).single()["n"]

            proxy_of = session.run(
                """
                MATCH (:Symbol {kind: 'proxy_binding'})-[r:PROXY_OF {workspace_id: $ws}]->()
                RETURN count(r) AS n
                """,
                ws=workspace_id,
            ).single()["n"]

            orphan_proxy_of = session.run(
                """
                MATCH (p:Symbol {kind: 'proxy_binding'})-[r:PROXY_OF {workspace_id: $ws}]->(t)
                WHERE NOT EXISTS {
                  MATCH (:File {workspace_id: $ws})-[:CONTAINS]->(t)
                }
                RETURN count(r) AS n
                """,
                ws=workspace_id,
            ).single()["n"]

            via_proxy = session.run(
                """
                MATCH ()-[r:CALLS_DYNAMIC {workspace_id: $ws}]->(callee)
                WHERE r.via_proxy IS NOT NULL
                RETURN count(r) AS n,
                       collect({
                         proxy: r.via_proxy,
                         callee: callee.qualified_name,
                         resolver: r.resolver
                       })[0..8] AS samples
                """,
                ws=workspace_id,
            ).single()

            typed_total = session.run(
                """
                MATCH ()-[r:CALLS_DYNAMIC {workspace_id: $ws}]->()
                WHERE r.tier = 'typed'
                RETURN count(r) AS n
                """,
                ws=workspace_id,
            ).single()["n"]

            typed_object_api = session.run(
                """
                MATCH ()-[r:CALLS_DYNAMIC {workspace_id: $ws}]->(callee:Symbol {kind: 'object_api'})
                WHERE r.tier = 'typed'
                RETURN count(r) AS n,
                       collect(callee.qualified_name)[0..8] AS samples
                """,
                ws=workspace_id,
            ).single()

            typed_top_callers = session.run(
                """
                MATCH (caller)-[r:CALLS_DYNAMIC {workspace_id: $ws}]->()
                WHERE r.tier = 'typed'
                WITH caller, count(r) AS edge_count
                ORDER BY edge_count DESC
                LIMIT 8
                RETURN caller.qualified_name AS caller, edge_count
                """,
                ws=workspace_id,
            ).data()

            guess_total = session.run(
                """
                MATCH ()-[r:CALLS_GUESS {workspace_id: $ws}]->()
                RETURN count(r) AS n
                """,
                ws=workspace_id,
            ).single()["n"]

        return {
            "workspace_id": workspace_id,
            "proxy_bindings": int(proxy_bindings),
            "proxy_of_edges": int(proxy_of),
            "orphan_proxy_of": int(orphan_proxy_of),
            "via_proxy_edges": int(via_proxy["n"]),
            "via_proxy_samples": via_proxy["samples"] or [],
            "calls_dynamic_by_tier": tier_rows,
            "typed_edges": int(typed_total),
            "typed_to_object_api": int(typed_object_api["n"]),
            "typed_object_api_samples": typed_object_api["samples"] or [],
            "typed_top_callers": typed_top_callers,
            "calls_guess_edges": int(guess_total),
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        choices=["django", "pydantic", "express"],
        help="QA/repos checkout id",
    )
    parser.add_argument(
        "--report",
        default="",
        help="Optional JSON output path (default: /tmp/proxy_audit_<repo>.json)",
    )
    args = parser.parse_args()

    project_path = str(default_repo_checkout_path(args.repo))
    workspace_id, index_stats = setup_real_repo_db(
        project_path,
        workspace_id=None,
        docs_path=None,
        skip_affects=False,
        skip_docs=True,
    )
    audit = _audit_workspace(workspace_id)
    payload = {
        "repo": args.repo,
        "project_path": project_path,
        "indexing": {
            "proxy_bindings": index_stats.get("proxy_bindings"),
            "proxy_calls_resolved": index_stats.get("proxy_calls_resolved"),
            "parsed": index_stats.get("parsed"),
            "timings_sec": index_stats.get("timings_sec"),
        },
        "audit": audit,
    }
    out = args.report or f"/tmp/proxy_audit_{args.repo}.json"
    Path(out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
