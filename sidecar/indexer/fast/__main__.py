"""CLI entry point for the fast indexer.

Usage:
    python -m sidecar.indexer.fast [path] [--workspace ID]
                                   [--index-profile PROFILE]
                                   [--hash-workers N] [--parse-workers N]

Mirrors ``python -m sidecar.indexer.code`` but runs the alternative
parallel pipeline. The baseline indexer is untouched.
"""

import argparse
import os

from sidecar.indexer.fast.collector import ROOT
from sidecar.indexer.fast.pipeline import run_fast_indexing


def main():
    parser = argparse.ArgumentParser(
        description="Fast (parallel) indexer — alternative to sidecar.indexer.code"
    )
    parser.add_argument("path", nargs="?", default=ROOT, help="Project path to index")
    parser.add_argument("--workspace", default=None, help="Workspace id override")
    parser.add_argument(
        "--index-profile",
        default=None,
        help="Physical index profile, e.g. legacy or axis_python_v1",
    )
    parser.add_argument(
        "--hash-workers",
        type=int,
        default=None,
        help="Thread pool size for sha256 hashing",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=None,
        help="Thread pool size for tree-sitter parsing",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Clear the workspace's Symbol/File/FileHash nodes and Lance "
            "rows before indexing, forcing a full re-index (post-passes "
            "like error_model / registry_class propagation only run when "
            "files are seen as changed)."
        ),
    )

    args = parser.parse_args()
    project_path = os.path.abspath(args.path)

    if args.fresh:
        if not args.workspace:
            parser.error("--fresh requires --workspace")
        _wipe_workspace(args.workspace, args.index_profile)

    run_fast_indexing(
        project_path,
        workspace_id=args.workspace,
        index_profile=args.index_profile,
        hash_workers=args.hash_workers,
        parse_workers=args.parse_workers,
    )


def _wipe_workspace(workspace_id: str, index_profile: str | None) -> None:
    """Drop a workspace's graph nodes and Lance rows so the next index
    run treats every file as new. Profile defaults to axis_python_v1
    when not given (the only profile the post-passes target)."""
    from sidecar.database.lancedb_client import LanceDBClient
    from sidecar.database.neo4j_client import Neo4jClient
    from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
    from sidecar.indexer.fast.pipeline import (
        NEO4J_PASSWORD,
        NEO4J_URI,
        NEO4J_USER,
    )

    profile_name = index_profile or AXIS_PYTHON_V1_PROFILE
    lance = LanceDBClient(index_profile=profile_name)
    lance.delete_workspace(workspace_id)
    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    with db.driver.session() as session:
        # Every workspace-scoped label now carries workspace_id (symbols too,
        # since uids are workspace-scoped), so a per-label delete is exact — no
        # orphan leak the way the old Symbol-by-workspace_id no-op left behind.
        for label in ("Symbol", "File", "FileHash", "ExternalSymbol", "ExternalPkg", "DocAnchor"):
            session.run(
                f"MATCH (n:{label} {{workspace_id: $ws}}) DETACH DELETE n",
                ws=workspace_id,
            )
    print(f"🧹 wiped workspace {workspace_id}")


if __name__ == "__main__":
    main()
