"""Evidence gate for ``sidecar/axis/library_marker_catalogue.py``.

Every canonical catalogue entry — ``flask.app.Flask → web_route_register``
and the rest — is a human assertion: *this external class is structurally
the kind we say it is*. Until L2 has a registry_class predicate that can
derive the assertion structurally, the catalogue is a transition shim
(see ``project_library_marker_catalogue_replacement.md``).

This tool surfaces, per entry, whether the indexed library shows
*method-level* registry evidence on the canonical class. A class whose
methods carry the ``metadata_key_roundtrip`` or
``callable_container_dispatch`` contract is structurally a registry — its
catalogue claim is **earned**. A class whose methods do not is an
**unproven** assertion. A class absent from every indexed workspace is
**not yet verifiable** — index the library first.

Status taxonomy:

  - ``structurally_backed`` — class found in an indexed workspace, ≥1 of
    its methods carries a registry-shape contract.
  - ``unproven``           — class found but no method shows the
    registry-shape contract. Catalogue claim is currently unbacked.
  - ``absent``             — class not present in any provided
    workspace; cannot evaluate. Run the indexer on that library.

This is a diagnostic, not a runtime fallback. The runtime catalogue still
returns the kind; this report tells maintainers which entries are debt.

Usage::

    python -m QA.library_marker_evidence \
        --workspace qa_repo/flask@axis-v4+axis_python_v1 \
        --workspace qa_repo/fastapi@axis-v4+axis_python_v1 \
        --workspace qa_repo/celery@axis-v4+axis_python_v1 \
        --out /tmp/library_marker_evidence.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sidecar.axis.library_marker_catalogue import LIBRARY_MARKER_CATALOGUE
from sidecar.database.neo4j_client import Neo4jClient
from sidecar.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

# Method-level contracts that prove a class participates in a registry
# pattern. A class whose methods carry any of these is structurally
# registry-like — the canonical catalogue claim is earned regardless of
# subtype (web / task / signal / error).
_REGISTRY_METHOD_CONTRACTS = frozenset(
    {
        "metadata_key_roundtrip",
        "callable_container_dispatch",
    }
)


@dataclass(frozen=True)
class CatalogueEvidence:
    canonical_qn: str
    declared_kind: str
    status: str  # structurally_backed | unproven | absent
    workspace: str | None
    method_evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    class_registry_evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_qn": self.canonical_qn,
            "declared_kind": self.declared_kind,
            "status": self.status,
            "workspace": self.workspace,
            "method_evidence": list(self.method_evidence),
            "class_registry_evidence": self.class_registry_evidence,
        }


def _package_name_for_workspace(db: Neo4jClient, workspace_id: str) -> str | None:
    """Top-level package name = directory holding the shortest ``__init__.py``."""
    with db.driver.session() as session:
        rec = session.run(
            """
            MATCH (f:File {workspace_id: $ws})
            WHERE f.path ENDS WITH '/__init__.py'
            RETURN f.path AS path ORDER BY size(f.path) ASC LIMIT 1
            """,
            ws=workspace_id,
        ).single()
    if rec is None:
        return None
    return Path(rec["path"]).parent.name or None


def _find_class_with_evidence(
    db: Neo4jClient,
    workspace_id: str,
    package: str,
    canonical_qn: str,
) -> tuple[bool, list[dict[str, Any]], str | None] | None:
    """Resolve the canonical class in the workspace and collect both proof
    channels:

      - **method contracts** — walk ``DEPENDS_ON`` ancestry + ``HAS_API`` to
        the class's methods (own and inherited), look up their persisted
        ``axis_contracts_json`` in Lance, and keep the registry-shape
        contracts (``metadata_key_roundtrip`` / ``callable_container_dispatch``).
      - **class registry_class kind** — the L2 ``registry_class`` predicate
        runs at index time and records the class itself as registry when its
        per-file peer methods carry registry kinds; the gate trusts this
        verdict as another structural channel.

    Returns ``None`` when the canonical QN doesn't belong to this workspace's
    package. Otherwise returns ``(class_found, method_evidence,
    class_registry_class_evidence)``.
    """
    prefix = f"{package}."
    if not canonical_qn.startswith(prefix):
        return None
    local_qn = canonical_qn[len(prefix) :]
    with db.driver.session() as session:
        rec = session.run(
            """
            MATCH (c:Symbol {qualified_name: $qn, kind: 'class'})
            WHERE EXISTS((:File {workspace_id: $ws})-[:CONTAINS]->(c))
            // Walk inheritance chain up to 6 hops; each step must stay in
            // this workspace.
            OPTIONAL MATCH path = (c)-[:DEPENDS_ON*0..6 {workspace_id: $ws}]->(ancestor:Symbol)
            WHERE ancestor.kind = 'class'
            WITH c, collect(DISTINCT ancestor) AS ancestors
            UNWIND (ancestors + [c]) AS cls
            OPTIONAL MATCH (cls)-[:HAS_API]->(m:Symbol)
            WITH c, collect(DISTINCT m.uid) AS method_uids
            RETURN c.uid AS class_uid, method_uids
            """,
            ws=workspace_id,
            qn=local_qn,
        ).single()
    if rec is None or rec.get("class_uid") is None:
        return (False, [], None)
    method_uids = [u for u in (rec.get("method_uids") or []) if u]
    class_uid = str(rec["class_uid"])
    method_evidence = (
        _collect_method_contract_evidence(db, workspace_id, method_uids) if method_uids else []
    )
    class_kind_evidence = _check_class_registry_kind(workspace_id, class_uid)
    return (True, method_evidence, class_kind_evidence)


def _check_class_registry_kind(workspace_id: str, class_uid: str) -> str | None:
    """Return the ``peer_method_kinds`` evidence string from the class's
    ``registry_class`` ContainerKindMatch, or ``None`` if the class wasn't
    classified as such.
    """
    import lancedb

    table = lancedb.connect("./data/lancedb").open_table("symbols_axis_python_v1")
    rows = (
        table.to_lance()
        .to_table(
            columns=["uid", "axis_container_kinds_json", "workspace_id"],
        )
        .to_pylist()
    )
    for row in rows:
        if row.get("workspace_id") != workspace_id or row.get("uid") != class_uid:
            continue
        try:
            kinds_json = json.loads(row.get("axis_container_kinds_json") or "[]")
        except json.JSONDecodeError:
            return None
        for match in kinds_json:
            if match.get("kind") == "registry_class":
                evidence_probes = match.get("evidence_probes") or []
                return ", ".join(str(p) for p in evidence_probes) or "registry_class"
        return None
    return None


def _collect_method_contract_evidence(
    db: Neo4jClient,
    workspace_id: str,
    method_uids: list[str],
) -> list[dict[str, Any]]:
    """Pull ``axis_contracts_json`` for each method from Lance, augment with
    Neo4j-resolved qualified_name (Lance doesn't store qualified_name on
    symbol rows), and keep only methods that carry a registry-shape contract.
    """
    import lancedb

    table = lancedb.connect("./data/lancedb").open_table("symbols_axis_python_v1")
    lance_rows = (
        table.to_lance()
        .to_table(
            columns=["uid", "name", "axis_contracts_json", "workspace_id"],
        )
        .to_pylist()
    )
    by_uid = {
        r["uid"]: r
        for r in lance_rows
        if r.get("workspace_id") == workspace_id and r.get("uid") in method_uids
    }
    qn_by_uid: dict[str, str] = {}
    with db.driver.session() as session:
        for rec in session.run(
            """
            UNWIND $uids AS u
            MATCH (s:Symbol {uid: u})
            RETURN s.uid AS uid, coalesce(s.qualified_name, s.name) AS qn
            """,
            uids=method_uids,
        ):
            qn_by_uid[str(rec["uid"])] = str(rec.get("qn") or "")

    evidence: list[dict[str, Any]] = []
    for uid in method_uids:
        row = by_uid.get(uid)
        if not row:
            continue
        try:
            contracts = json.loads(row.get("axis_contracts_json") or "[]")
        except json.JSONDecodeError:
            continue
        registry_contracts = sorted(
            {
                str(c.get("contract") or "")
                for c in contracts
                if str(c.get("contract") or "") in _REGISTRY_METHOD_CONTRACTS
            }
        )
        if registry_contracts:
            evidence.append(
                {
                    "method_uid": uid,
                    "method_qualified_name": qn_by_uid.get(uid) or row.get("name"),
                    "registry_contracts": registry_contracts,
                }
            )
    return evidence


def evaluate_catalogue(
    db: Neo4jClient,
    workspaces: list[str],
) -> list[CatalogueEvidence]:
    """Return a CatalogueEvidence per canonical entry in the catalogue."""
    package_for_ws: dict[str, str | None] = {
        ws: _package_name_for_workspace(db, ws) for ws in workspaces
    }
    out: list[CatalogueEvidence] = []
    for canonical_qn in sorted(LIBRARY_MARKER_CATALOGUE):
        declared_kind = LIBRARY_MARKER_CATALOGUE[canonical_qn]
        matched_workspace: str | None = None
        method_evidence: list[dict[str, Any]] = []
        class_registry_evidence: str | None = None
        found_class = False
        for ws in workspaces:
            package = package_for_ws.get(ws)
            if not package:
                continue
            result = _find_class_with_evidence(db, ws, package, canonical_qn)
            if result is None:
                continue
            found_class, method_evidence, class_registry_evidence = result
            matched_workspace = ws
            break

        if matched_workspace is None or not found_class:
            status = "absent"
            ws_label: str | None = None
        elif method_evidence or class_registry_evidence:
            status = "structurally_backed"
            ws_label = matched_workspace
        else:
            status = "unproven"
            ws_label = matched_workspace

        out.append(
            CatalogueEvidence(
                canonical_qn=canonical_qn,
                declared_kind=declared_kind,
                status=status,
                workspace=ws_label,
                method_evidence=tuple(method_evidence),
                class_registry_evidence=class_registry_evidence,
            )
        )
    return out


def summarize(rows: list[CatalogueEvidence]) -> dict[str, Any]:
    status_counter: Counter[str] = Counter()
    by_kind: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        status_counter[row.status] += 1
        by_kind[row.declared_kind][row.status] += 1
    return {
        "total": len(rows),
        "by_status": dict(status_counter),
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
    }


def _render_markdown(rows: list[CatalogueEvidence], summary: dict[str, Any]) -> str:
    lines = [
        "# Library marker catalogue — structural evidence report",
        "",
        f"- total entries: {summary['total']}",
        f"- by status: `{json.dumps(summary['by_status'], sort_keys=True)}`",
        "",
        "## By kind",
        "",
    ]
    for kind, statuses in summary["by_kind"].items():
        lines.append(f"- **{kind}**: `{json.dumps(statuses, sort_keys=True)}`")
    lines.extend(
        [
            "",
            "## Entries",
            "",
            "| canonical_qn | kind | status | workspace | method evidence | registry_class |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in rows:
        evidence_brief = (
            ", ".join(
                f"{m['method_qualified_name'].split('.')[-1]}({'+'.join(m['registry_contracts'])})"
                for m in row.method_evidence[:4]
            )
            or "-"
        )
        ws = row.workspace or "-"
        rc = row.class_registry_evidence or "-"
        lines.append(
            f"| `{row.canonical_qn}` | {row.declared_kind} | **{row.status}** | {ws} | {evidence_brief} | {rc} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evidence gate for library_marker_catalogue canonical entries",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        required=True,
        help="Indexed library workspace id (repeatable)",
    )
    parser.add_argument("--out", default="/tmp/library_marker_evidence")
    args = parser.parse_args()

    db = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    rows = evaluate_catalogue(db, args.workspace)
    summary = summarize(rows)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "evidence.jsonl"
    md_path = out_dir / "evidence.md"
    summary_path = out_dir / "summary.json"

    jsonl_path.write_text(
        "".join(json.dumps(r.to_dict(), sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(rows, summary), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nfull report → {out_dir}/")


if __name__ == "__main__":
    main()
