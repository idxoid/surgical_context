#!/usr/bin/env python3
"""Inspect Pass-1 discriminator cascade on an indexed benchmark workspace.

Runs the same extract → L1/L2 cascade → presence gate as the indexer, then
prints L1 distribution, present roles, target-symbol checks, and multi-label samples.

Usage:
    python QA/prototype_role_cascade.py --repo fastapi
    python QA/prototype_role_cascade.py --repos fastapi,flask
    python QA/prototype_role_cascade.py --workspace-id <uuid>
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(QA_DIR))

from qa_benchmark import default_repo_checkout_path  # noqa: E402
from sidecar.database.neo4j_client import Neo4jClient  # noqa: E402
from sidecar.indexer.role_cascade import L1_BUCKETS, SymbolRoleAssignment  # noqa: E402
from sidecar.indexer.role_clustering import (  # noqa: E402
    STRUCTURAL_REL_TYPES,
    assign_role_taxonomy,
    extract_symbol_rows,
)
from sidecar.workspace import WorkspaceResolver  # noqa: E402

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

TARGET_SYMBOLS = (
    "run_endpoint_function",
    "solve_dependencies",
    "get_dependant",
    "add_api_route",
    "jsonable_encoder",
    "Default",
    "Param",
    "FastAPI",
    "APIRoute",
)

QA_EXPECTED: dict[str, tuple[str, ...]] = {
    "run_endpoint_function": ("executor", "runtime_surface"),
    "solve_dependencies": ("orchestrator", "dependency_solver"),
    "add_api_route": ("factory_surface", "registration_step"),
    "Param": ("config_surface",),
    "APIRoute": ("representation_surface",),
    "FastAPI": ("api_surface",),
}


def _load_symbol_names(db, workspace_id: str) -> dict[str, tuple[str, str]]:
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (f:File {workspace_id: $ws})-[:CONTAINS]->(s:Symbol)
            RETURN s.uid AS uid, s.name AS name, f.path AS path
            """,
            ws=workspace_id,
        )
        return {r["uid"]: (r["name"], r["path"]) for r in rows if r["uid"]}


def _query_edge_inventory(db, workspace_id: str) -> Counter[str]:
    rel_union = "|".join(STRUCTURAL_REL_TYPES)
    with db.driver.session() as session:
        rows = session.run(
            f"""
            MATCH (:Symbol)-[r:{rel_union}]->(:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN type(r) AS rel_type, count(r) AS c
            """,
            workspace_id=workspace_id,
        )
        return Counter({r["rel_type"]: int(r["c"]) for r in rows})


def _roles_list(asn: SymbolRoleAssignment) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for role in (asn.primary, *asn.supporting):
        if role not in seen:
            seen.add(role)
            out.append(role)
    return out


def _print_l1_distribution(assignments: dict[str, SymbolRoleAssignment]) -> None:
    counts = Counter(a.l1 for a in assignments.values())
    print("\n=== L1 distribution ===")
    for bucket in L1_BUCKETS:
        if counts.get(bucket, 0):
            print(f"  {bucket:16s} {counts[bucket]}")


def _print_present_roles(present: dict[str, int]) -> None:
    print("\n=== Present roles (presence gate) ===")
    if not present:
        print("  (none)")
        return
    for role, count in present.items():
        print(f"  {role:24s} {count:5d}")


def _print_target_symbols(
    assignments: dict[str, SymbolRoleAssignment],
    names: dict[str, tuple[str, str]],
    *,
    prod_path_hint: str = "",
) -> None:
    print("\n=== Target symbols ===")
    by_name: dict[str, list[tuple[str, str, SymbolRoleAssignment]]] = defaultdict(list)
    for uid, asn in assignments.items():
        if uid not in names:
            continue
        name, path = names[uid]
        by_name[name].append((uid, path, asn))

    for sym in TARGET_SYMBOLS:
        entries = by_name.get(sym, [])
        if prod_path_hint:
            preferred = [e for e in entries if prod_path_hint in e[1]]
            entries = preferred or entries
        if not entries:
            print(f"  {sym:30s} NOT FOUND")
            continue
        _uid, path, asn = entries[0]
        roles = _roles_list(asn)
        expected = QA_EXPECTED.get(sym, ())
        missing = [r for r in expected if r not in roles] if expected else []
        qa = f" | qa_missing={missing}" if missing else (" | qa_ok" if expected else "")
        print(
            f"  {sym:30s} L1={asn.l1:14s} primary={asn.primary:22s} "
            f"supporting={list(asn.supporting)}{qa} | {Path(path).name}"
        )


def _print_multi_label_samples(
    assignments: dict[str, SymbolRoleAssignment],
    names: dict[str, tuple[str, str]],
    *,
    limit: int = 12,
) -> None:
    multi = [
        (uid, names[uid], asn)
        for uid, asn in assignments.items()
        if uid in names and asn.supporting
    ]
    multi.sort(key=lambda item: (-len(item[2].supporting), item[1][0]))
    print(f"\n=== Multi-label samples (top {limit}) ===")
    for _uid, (name, path), asn in multi[:limit]:
        print(
            f"  {name:30s} L1={asn.l1:14s} roles={_roles_list(asn)} | {Path(path).name}"
        )


def run_cascade_for_workspace(workspace_id: str, *, repo_label: str = "") -> None:
    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    label = repo_label or workspace_id
    names = _load_symbol_names(db, workspace_id)

    print(f"\n{'=' * 72}")
    print(f"REPO / WORKSPACE: {label}")
    print(f"workspace_id={workspace_id}")

    edge_counts = _query_edge_inventory(db, workspace_id)
    print("\n=== Edge inventory ===")
    for rel, count in edge_counts.most_common():
        print(f"  {rel}: {count}")

    rows = extract_symbol_rows(db, workspace_id)
    summary, assignments, present = assign_role_taxonomy(rows)
    print(
        f"\n  symbols={summary.sample_size} connected={summary.filtered_sample_size} "
        f"present_roles={len(present)}"
    )

    _print_l1_distribution(assignments)
    _print_present_roles(present)

    prod_hint = ""
    if repo_label == "fastapi":
        prod_hint = "/fastapi/fastapi/"
    elif repo_label == "flask":
        prod_hint = "/flask/flask/"
    _print_target_symbols(assignments, names, prod_path_hint=prod_hint)
    _print_multi_label_samples(assignments, names)
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Pass-1 role cascade on indexed repo")
    parser.add_argument("--repo", default="", help="Single QA repo id (e.g. fastapi)")
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated repo ids (e.g. fastapi,flask). Overrides --repo.",
    )
    parser.add_argument("--workspace-id", default="", help="Override workspace uuid")
    args = parser.parse_args()

    if args.workspace_id:
        run_cascade_for_workspace(args.workspace_id)
        return

    repo_ids: list[str] = []
    if args.repos:
        repo_ids = [r.strip() for r in args.repos.split(",") if r.strip()]
    elif args.repo:
        repo_ids = [args.repo]
    else:
        repo_ids = ["fastapi"]

    resolver = WorkspaceResolver()
    for repo in repo_ids:
        project_path = default_repo_checkout_path(repo)
        ws = resolver.from_project_path(str(project_path)).id
        run_cascade_for_workspace(ws, repo_label=repo)


if __name__ == "__main__":
    main()
