"""Derive ``alias_qn → canonical_qn`` map from indexed library workspaces.

For every package whose root ``__init__.py`` is indexed in a given workspace,
walk the ``RE_EXPORTS`` edges leaving that file to surfaced symbols, and emit
one alias row per re-export:

    flask.Flask        → flask.app.Flask
    flask.Blueprint    → flask.blueprints.Blueprint
    fastapi.FastAPI    → fastapi.applications.FastAPI
    fastapi.APIRouter  → fastapi.routing.APIRouter

These alias rows replace the ``flask.Flask`` / ``fastapi.FastAPI`` style
literal entries the catalogue used to carry by hand. The alias map is the
structural answer: ``__init__.py`` re-exports are an AST fact, not a human
guess. The catalogue still names the *canonical* QN's container kind
(``flask.app.Flask → web_route_register``); only re-export plumbing moves
into this generator.

The output is a JSON map merged into the catalogue at probe-lookup time
through :mod:`context_engine.axis.library_marker_aliases`.

Usage::

    python -m QA.build_library_marker_aliases \
        --workspace qa_repo/flask@axis-v4+axis_python_v1 \
        --workspace qa_repo/fastapi@axis-v4+axis_python_v1 \
        --workspace qa_repo/celery@axis-v4+axis_python_v1 \
        --out context_engine/axis/library_marker_aliases.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from context_engine.database.neo4j_client import Neo4jClient
from context_engine.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER


def derive_aliases_for_workspace(db: Neo4jClient, workspace_id: str) -> dict[str, list[str]]:
    """Return ``{alias_qn: [canonical_qn, ...]}`` for one indexed library.

    A re-export materialized by the indexer takes the shape
    ``(initfile:File)-[:RE_EXPORTS]->(sym:Symbol)``. The package name comes
    from the directory the ``__init__.py`` sits in (always a structural
    fact on the File path); the ``sym.qualified_name`` already lives at
    module qualifier level within that package. Together they yield both
    halves of the alias mapping without consulting a name list.
    """

    aliases: dict[str, list[str]] = defaultdict(list)
    with db.driver.session() as session:
        rows = session.run(
            """
            MATCH (init:File {workspace_id: $workspace_id})-[r:RE_EXPORTS]->(sym:Symbol)
            WHERE init.path ENDS WITH '/__init__.py'
              AND coalesce(r.workspace_id, $workspace_id) = $workspace_id
              AND sym.name IS NOT NULL AND sym.qualified_name IS NOT NULL
            RETURN init.path AS init_path,
                   sym.name AS short_name,
                   sym.qualified_name AS canonical_qn,
                   sym.kind AS sym_kind
            """,
            workspace_id=workspace_id,
        ).data()

    for row in rows:
        init_path = row.get("init_path") or ""
        short_name = row.get("short_name") or ""
        canonical_local_qn = row.get("canonical_qn") or ""
        kind = row.get("sym_kind") or ""
        if not init_path or not short_name or not canonical_local_qn:
            continue
        # Re-exports of methods (``ClassName.method``) are noise — the alias
        # belongs at top level on the package surface.
        if "." in short_name:
            continue
        if kind not in {"class", "function", "variable", "module"}:
            # Skip unknown / null kinds — leave the alias unproven.
            continue
        package = Path(init_path).parent.name
        if not package:
            continue
        alias_qn = f"{package}.{short_name}"
        canonical_qn = f"{package}.{canonical_local_qn}"
        if canonical_qn == alias_qn:
            # ``__init__.py`` re-exports a top-level name from itself
            # (unusual but possible) — nothing to alias.
            continue
        if canonical_qn in aliases[alias_qn]:
            continue
        aliases[alias_qn].append(canonical_qn)
    return dict(aliases)


def _flatten_alias_map(
    raw: dict[str, dict[str, list[str]]],
) -> dict[str, str]:
    """Collapse ``{workspace: {alias: [canonical, ...]}}`` to ``{alias: canonical}``.

    When the same alias resolves to multiple canonical QNs across or within
    workspaces, prefer:

      1. The shortest canonical QN (closer to the package root → more likely
         the primary definition rather than a re-export from a deeper module).
      2. Lexicographically smallest as a deterministic tiebreaker so the
         generated JSON is stable.
    """

    flat: dict[str, list[str]] = defaultdict(list)
    for ws_aliases in raw.values():
        for alias, candidates in ws_aliases.items():
            for canonical in candidates:
                if canonical not in flat[alias]:
                    flat[alias].append(canonical)
    out: dict[str, str] = {}
    for alias in sorted(flat):
        candidates = sorted(flat[alias], key=lambda qn: (len(qn), qn))
        out[alias] = candidates[0]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build alias_qn → canonical_qn map from indexed library workspaces",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        required=True,
        help="Indexed library workspace id (repeatable)",
    )
    parser.add_argument(
        "--out",
        default="context_engine/axis/library_marker_aliases.json",
        help="Output JSON path",
    )
    args = parser.parse_args()

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    per_workspace: dict[str, dict[str, list[str]]] = {}
    for ws in args.workspace:
        per_workspace[ws] = derive_aliases_for_workspace(db, ws)
        print(f"  {ws}: {sum(len(v) for v in per_workspace[ws].values())} alias rows")

    flat = _flatten_alias_map(per_workspace)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_from_workspaces": sorted(per_workspace),
        "aliases": flat,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {len(flat)} unique aliases to {out_path}")


if __name__ == "__main__":
    main()
