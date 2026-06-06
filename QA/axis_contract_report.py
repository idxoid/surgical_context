"""Report persisted axis container kinds and compiled structural contracts.

This is a QA/reporting tool. It reads rows that the axis index already wrote
and runs the L3 compiler over them; it does not author graph edges, roles, or
runtime decisions.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidecar.axis.contract_compiler import (
    AxisContractCompiler,
    AxisContractMatch,
    container_kind_matches_from_json,
)
from sidecar.axis.query_plan import compile_axis_query
from sidecar.axis.schema import AxisFact, AxisProfile
from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
from sidecar.workspace import DEFAULT_WORKSPACE_ID


def _list_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _axis_facts_from_json(raw: Any) -> list[AxisFact]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    elif isinstance(raw, list):
        data = raw
    else:
        return []
    facts: list[AxisFact] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        axis = str(item.get("axis") or "")
        bit = str(item.get("bit") or "")
        if axis not in {"cfg", "dfg", "struct"} or not bit:
            continue
        payload = item.get("payload")
        facts.append(
            AxisFact(
                symbol_uid=str(item.get("symbol_uid") or ""),
                qualified_name=str(item.get("qualified_name") or ""),
                symbol_kind=str(item.get("symbol_kind") or "symbol"),
                axis=axis,  # type: ignore[arg-type]
                bit=bit,
                line=int(item.get("line") or 0),
                evidence=str(item.get("evidence") or ""),
                ast_kind=str(item.get("ast_kind") or ""),
                payload=payload if isinstance(payload, dict) else {},
            )
        )
    return facts


def axis_profile_from_lance_row(row: dict[str, Any]) -> AxisProfile:
    matches = container_kind_matches_from_json(
        str(row.get("axis_container_kinds_json") or "[]")
    )
    facts = _axis_facts_from_json(row.get("axis_evidence_json"))
    uid = str(row.get("uid") or "")
    qualified_name = (
        matches[0].qualified_name
        if matches and matches[0].qualified_name
        else str(row.get("name") or uid)
    )
    if facts:
        profile = AxisProfile(
            symbol_uid=uid,
            qualified_name=qualified_name,
            symbol_kind=str(row.get("symbol_kind") or facts[0].symbol_kind or "symbol"),
        )
        for fact in facts:
            profile.add_fact(
                AxisFact(
                    symbol_uid=uid or fact.symbol_uid,
                    qualified_name=qualified_name or fact.qualified_name,
                    symbol_kind=profile.symbol_kind,
                    axis=fact.axis,
                    bit=fact.bit,
                    line=fact.line,
                    evidence=fact.evidence,
                    ast_kind=fact.ast_kind,
                    payload=fact.payload,
                )
            )
        return profile
    return AxisProfile(
        symbol_uid=uid,
        qualified_name=qualified_name,
        symbol_kind=str(row.get("symbol_kind") or "symbol"),
        cfg_bits=set(_list_strings(row.get("cfg_bits"))),
        dfg_bits=set(_list_strings(row.get("dfg_bits"))),
        struct_bits=set(_list_strings(row.get("struct_bits"))),
    )


@dataclass(frozen=True)
class AxisContractReportRow:
    uid: str
    name: str
    file_path: str
    container_kinds: tuple[str, ...]
    contracts: tuple[AxisContractMatch, ...]
    plans: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "container_kinds": list(self.container_kinds),
            "contracts": [contract.to_dict() for contract in self.contracts],
            "plans": list(self.plans),
        }


def compile_contract_report_row(
    row: dict[str, Any],
    *,
    workspace_id: str,
    compiler: AxisContractCompiler | None = None,
) -> AxisContractReportRow:
    profile = axis_profile_from_lance_row(row)
    matches = container_kind_matches_from_json(
        str(row.get("axis_container_kinds_json") or "[]")
    )
    compiler = compiler or AxisContractCompiler()
    contracts = tuple(compiler.compile(profile, matches))
    plans: list[dict[str, object]] = []
    for contract in contracts:
        if contract.traversal_mode is None:
            continue
        request = contract.to_query_request()
        plans.append(compile_axis_query(request, workspace_id=workspace_id).to_dict())
    return AxisContractReportRow(
        uid=profile.symbol_uid,
        name=str(row.get("name") or profile.qualified_name),
        file_path=str(row.get("file_path") or ""),
        container_kinds=tuple(sorted({match.kind for match in matches})),
        contracts=contracts,
        plans=tuple(plans),
    )


def build_axis_contract_report(
    rows: list[dict[str, Any]],
    *,
    workspace_id: str,
) -> list[AxisContractReportRow]:
    report_rows = [
        compile_contract_report_row(row, workspace_id=workspace_id)
        for row in rows
    ]
    return [
        row
        for row in report_rows
        if row.container_kinds or row.contracts
    ]


def _markdown_table(rows: list[AxisContractReportRow]) -> str:
    lines = [
        "# Axis Contract Report",
        "",
        "| uid | file | container kinds | contracts | traversal plans |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        containers = ", ".join(row.container_kinds) or "-"
        contracts = ", ".join(contract.contract for contract in row.contracts) or "-"
        modes = ", ".join(
            str(plan.get("traversal_mode") or "-")
            for plan in row.plans
        ) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    row.uid,
                    row.file_path,
                    containers,
                    contracts,
                    modes,
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_axis_contract_report(
    rows: list[AxisContractReportRow],
    out_dir: Path,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "axis_contract_report.jsonl"
    md_path = out_dir / "axis_contract_report.md"
    jsonl_path.write_text(
        "".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    md_path.write_text(_markdown_table(rows), encoding="utf-8")
    return jsonl_path, md_path


def run_report(
    *,
    workspace_id: str,
    out_dir: Path,
    limit: int | None = None,
) -> list[AxisContractReportRow]:
    from sidecar.database.lancedb_client import LanceDBClient

    lance = LanceDBClient(index_profile=AXIS_PYTHON_V1_PROFILE)
    rows = lance.scan_symbols_workspace(
        workspace_id,
        columns=[
            "uid",
            "name",
            "file_path",
            "cfg_bits",
            "dfg_bits",
            "struct_bits",
            "axis_evidence_json",
            "axis_container_kinds_json",
        ],
    )
    report = build_axis_contract_report(rows, workspace_id=workspace_id)
    if limit is not None:
        report = report[: max(0, limit)]
    write_axis_contract_report(report, out_dir)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Report axis L2 kinds and L3 contracts")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE_ID)
    parser.add_argument("--out", default="/tmp/axis_contract_report", type=Path)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    rows = run_report(
        workspace_id=args.workspace,
        out_dir=args.out,
        limit=args.limit,
    )
    print(f"rows={len(rows)} out={args.out}")


if __name__ == "__main__":
    main()
