"""Report persisted axis container kinds and compiled structural contracts.

This is a QA/reporting tool. It reads rows that the axis index already wrote
and runs the L3 compiler over them; it does not author graph edges, roles, or
runtime decisions.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
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


def _contract_names_from_json(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return ()
    elif isinstance(raw, list):
        data = raw
    else:
        return ()
    names = {
        str(item.get("contract") or "")
        for item in data
        if isinstance(item, dict) and item.get("contract")
    }
    return tuple(sorted(names))


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
    diagnostics: tuple[dict[str, object], ...]
    persisted_contracts: tuple[str, ...]
    contract_drift: bool
    plans: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "container_kinds": list(self.container_kinds),
            "contracts": [contract.to_dict() for contract in self.contracts],
            "diagnostics": list(self.diagnostics),
            "persisted_contracts": list(self.persisted_contracts),
            "contract_drift": self.contract_drift,
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
    diagnostics = tuple(diagnostic.to_dict() for diagnostic in compiler.diagnose(profile, matches))
    persisted_contracts = _contract_names_from_json(row.get("axis_contracts_json"))
    compiled_contracts = tuple(sorted(contract.contract for contract in contracts))
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
        diagnostics=diagnostics,
        persisted_contracts=persisted_contracts,
        contract_drift=bool(persisted_contracts and persisted_contracts != compiled_contracts),
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


def _sorted_counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def summarize_axis_contract_report(rows: list[AxisContractReportRow]) -> dict[str, object]:
    container_kinds: Counter[str] = Counter()
    contracts: Counter[str] = Counter()
    diagnostics: Counter[str] = Counter()
    persisted_contracts: Counter[str] = Counter()
    traversal_modes: Counter[str] = Counter()
    drift: Counter[str] = Counter()

    for row in rows:
        container_kinds.update(row.container_kinds)
        persisted_contracts.update(row.persisted_contracts)
        drift["yes" if row.contract_drift else "no"] += 1
        for contract in row.contracts:
            contracts[contract.contract] += 1
            if contract.traversal_mode:
                traversal_modes[str(contract.traversal_mode)] += 1
        for diagnostic in row.diagnostics:
            contract = str(diagnostic.get("contract") or "")
            if contract:
                diagnostics[contract] += 1

    return {
        "rows": len(rows),
        "container_kinds": _sorted_counter_dict(container_kinds),
        "contracts": _sorted_counter_dict(contracts),
        "persisted_contracts": _sorted_counter_dict(persisted_contracts),
        "diagnostics": _sorted_counter_dict(diagnostics),
        "contract_drift": _sorted_counter_dict(drift),
        "traversal_modes": _sorted_counter_dict(traversal_modes),
    }


def _markdown_table(rows: list[AxisContractReportRow]) -> str:
    summary = summarize_axis_contract_report(rows)
    lines = [
        "# Axis Contract Report",
        "",
        "## Summary",
        "",
        f"- rows: {summary['rows']}",
        f"- container kinds: {json.dumps(summary['container_kinds'], sort_keys=True)}",
        f"- contracts: {json.dumps(summary['contracts'], sort_keys=True)}",
        f"- persisted contracts: {json.dumps(summary['persisted_contracts'], sort_keys=True)}",
        f"- diagnostics: {json.dumps(summary['diagnostics'], sort_keys=True)}",
        f"- contract drift: {json.dumps(summary['contract_drift'], sort_keys=True)}",
        f"- traversal modes: {json.dumps(summary['traversal_modes'], sort_keys=True)}",
        "",
        "## Rows",
        "",
        "| uid | file | container kinds | contracts | diagnostics | persisted | drift | traversal plans |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        containers = ", ".join(row.container_kinds) or "-"
        contracts = ", ".join(contract.contract for contract in row.contracts) or "-"
        diagnostics = ", ".join(
            str(item.get("contract") or "") for item in row.diagnostics
        ) or "-"
        persisted = ", ".join(row.persisted_contracts) or "-"
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
                    diagnostics,
                    persisted,
                    "yes" if row.contract_drift else "no",
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
    summary_path = out_dir / "axis_contract_summary.json"
    jsonl_path.write_text(
        "".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    md_path.write_text(_markdown_table(rows), encoding="utf-8")
    summary_path.write_text(
        json.dumps(summarize_axis_contract_report(rows), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
            "axis_contracts_json",
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
    summary = summarize_axis_contract_report(rows)
    print(
        "rows={rows} drift={drift} contracts={contracts} diagnostics={diagnostics} out={out}".format(
            rows=summary["rows"],
            drift=json.dumps(summary["contract_drift"], sort_keys=True),
            contracts=json.dumps(summary["contracts"], sort_keys=True),
            diagnostics=json.dumps(summary["diagnostics"], sort_keys=True),
            out=args.out,
        )
    )


if __name__ == "__main__":
    main()
