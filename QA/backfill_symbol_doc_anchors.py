#!/usr/bin/env python3
"""Backfill in-code docstring anchors for an already-indexed workspace.

Parses source files and calls ``ingest_symbol_docstrings`` without a full
graph reindex — useful for validating doc-anchor seed after the indexer
change lands on an existing checkout.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from context_engine.database.lancedb_client import LanceDBClient
from context_engine.database.neo4j_client import Neo4jClient
from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile
from context_engine.indexer.anchor import ingest_symbol_docstrings
from context_engine.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
from context_engine.parser.registry import REGISTRY


def _iter_source_files(root: Path, extensions: frozenset[str]) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", "node_modules", "dist", "build", "__pycache__", ".venv"}
        ]
        for name in filenames:
            if Path(name).suffix in extensions:
                out.append(Path(dirpath) / name)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill symbol doc-anchor rows")
    parser.add_argument("path", help="Project root or subdirectory to scan")
    parser.add_argument("--workspace", required=True, help="Workspace id (profile suffix ok)")
    parser.add_argument(
        "--index-profile",
        default=AXIS_PYTHON_V1_PROFILE,
        help="Index profile (default: axis_python_v1)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Delete existing doc-anchor rows and DocAnchor nodes for this workspace first",
    )
    args = parser.parse_args()

    project_path = str(Path(args.path).resolve())
    profile = resolve_index_profile(args.index_profile)
    workspace_id = profile.workspace_id(args.workspace)

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    lance = LanceDBClient(index_profile=profile)

    if args.fresh:
        rows = lance.scan_docs_workspace(workspace_id)
        doc_ids = [str(row["id"]) for row in rows if "::doc::" in str(row.get("id") or "")]
        if doc_ids:
            batch_size = 128
            for start in range(0, len(doc_ids), batch_size):
                batch = doc_ids[start : start + batch_size]
                predicate = " OR ".join(
                    (
                        f"(workspace_id = '{lance._quote_delete_value(workspace_id)}' "
                        f"AND id = '{lance._quote_delete_value(chunk_id)}')"
                    )
                    for chunk_id in batch
                )
                try:
                    lance._table.delete(predicate)
                except Exception:
                    pass
        with db.driver.session() as session:
            session.run(
                "MATCH (a:DocAnchor {workspace_id: $ws}) DETACH DELETE a",
                ws=workspace_id,
            )

    symbols = []
    file_tier_by_path: dict[str, str] = {}
    from context_engine.indexer.file_tier import classify_file_tier, is_pure_reexport_source

    files = _iter_source_files(
        Path(project_path),
        frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}),
    )
    print(f"scanning {len(files)} files under {project_path}")
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = os.path.relpath(path, project_path)
        file_tier_by_path[rel] = classify_file_tier(
            rel,
            pure_reexport=is_pure_reexport_source(source),
        )
        file_tier_by_path[str(path)] = file_tier_by_path[rel]
        try:
            language = REGISTRY.detect_language(str(path))
            adapter = REGISTRY.get_adapter(language)
        except ValueError:
            continue
        try:
            symbols.extend(adapter.extract_symbols(source, rel))
        except Exception:
            continue

    with_doc = [s for s in symbols if str(getattr(s, "docstring", "") or "").strip()]
    print(f"extracted {len(symbols)} symbols, {len(with_doc)} with docstrings")
    stats = ingest_symbol_docstrings(
        db,
        lance,
        symbols,
        workspace_id=workspace_id,
        allowed_prefixes=[project_path],
        file_tier_by_path=file_tier_by_path,
    )
    print(f"ingest stats: {stats}")


if __name__ == "__main__":
    main()
