"""Per-workspace report of L4 roles satisfied by L3 contracts.

For each indexed workspace, scans persisted ``axis_contracts_json`` on
every Symbol row, runs the L4 role resolver, and prints two views:

  - **role coverage**: how many symbols satisfy each role (across all
    contracts that can satisfy it).
  - **symbol → roles**: per-symbol list of satisfied roles + the
    contracts that fired (sampled).

This is a diagnostic / QA tool. It does not write anything back; the
roles are derived from contracts at read time.

Usage::

    python -m QA.axis_role_report \\
        --workspace qa_repo/flask_consumer@axis-v4+axis_python_v1 \\
        --workspace qa_repo/fastapi_consumer@axis-v4+axis_python_v1
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from typing import Any

import lancedb

from sidecar.axis.role_resolver import (
    ROLE_CONTRACT_MAP,
    resolve_roles_with_evidence,
)


def load_rows(workspace_id: str) -> list[dict[str, Any]]:
    table = lancedb.connect("./data/lancedb").open_table("symbols_axis_python_v1")
    return [
        r
        for r in table.to_lance()
        .to_table(
            columns=[
                "uid",
                "name",
                "file_path",
                "axis_contracts_json",
                "axis_container_kinds_json",
                "workspace_id",
            ],
        )
        .to_pylist()
        if r.get("workspace_id") == workspace_id
    ]


def report_workspace(workspace_id: str) -> dict[str, Any]:
    rows = load_rows(workspace_id)

    role_symbol_count: Counter[str] = Counter()
    role_contract_count: dict[str, Counter[str]] = defaultdict(Counter)
    symbol_role_sample: list[dict[str, Any]] = []
    no_role_count = 0
    total_with_contracts = 0

    for row in rows:
        try:
            contracts = json.loads(row.get("axis_contracts_json") or "[]")
        except json.JSONDecodeError:
            continue
        if not contracts:
            continue
        total_with_contracts += 1
        contract_names = [c.get("contract") or "" for c in contracts]
        resolutions = resolve_roles_with_evidence(contract_names)
        if not resolutions:
            no_role_count += 1
            continue
        for r in resolutions:
            role_symbol_count[r.role] += 1
            for c in r.satisfying_contracts:
                role_contract_count[r.role][c] += 1
        if len(symbol_role_sample) < 8:
            symbol_role_sample.append(
                {
                    "name": row.get("name"),
                    "file_path": row.get("file_path"),
                    "roles": [r.role for r in resolutions],
                    "contracts": contract_names,
                }
            )

    return {
        "workspace_id": workspace_id,
        "total_rows": len(rows),
        "rows_with_contracts": total_with_contracts,
        "rows_with_no_role": no_role_count,
        "role_symbol_count": dict(role_symbol_count),
        "role_contract_count": {role: dict(counts) for role, counts in role_contract_count.items()},
        "sample": symbol_role_sample,
    }


def print_workspace_report(report: dict[str, Any]) -> None:
    print(f"\n{'=' * 72}")
    print(report["workspace_id"])
    print("=" * 72)
    print(f"  total symbols:               {report['total_rows']}")
    print(f"  symbols with any contract:   {report['rows_with_contracts']}")
    print(f"  symbols with NO role:        {report['rows_with_no_role']}")

    print("\n  role → symbol count:")
    role_counts = report["role_symbol_count"]
    if not role_counts:
        print("    (no roles satisfied in this workspace)")
        for role in sorted(ROLE_CONTRACT_MAP):
            print(f"    {role:25s}  0  (contracts in scope: {sorted(ROLE_CONTRACT_MAP[role])})")
    for role in sorted(role_counts, key=lambda r: -role_counts[r]):
        evidence = report["role_contract_count"].get(role, {})
        evidence_str = ", ".join(f"{c}={n}" for c, n in sorted(evidence.items()))
        print(f"    {role:25s}  {role_counts[role]:4d}  via [{evidence_str}]")

    if report["sample"]:
        print("\n  sample symbols (≤8):")
        for s in report["sample"]:
            short_path = (s["file_path"] or "").split("/")[-1]
            print(f"    {s['name']:18s} ({short_path}) → roles={s['roles']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-workspace L4 role coverage report (axis pipeline)",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        required=True,
        help="Indexed workspace id (repeatable)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the text table",
    )
    args = parser.parse_args()

    reports = [report_workspace(ws) for ws in args.workspace]

    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
        return

    for report in reports:
        print_workspace_report(report)


if __name__ == "__main__":
    main()
