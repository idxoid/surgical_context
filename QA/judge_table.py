#!/usr/bin/env python3
"""Flatten qa_benchmark JSON judge matrix to TSV."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

COLUMNS = [
    "question_id",
    "provider",
    "tier",
    "model",
    "verdict",
    "correctness",
    "grounding",
    "completeness",
    "context_sufficient",
    "unsupported_claims",
    "missing_evidence",
    "notes",
]


def _cell_notes(cell: dict) -> str:
    if cell.get("error"):
        return str(cell["error"])[:200]
    notes = (cell.get("notes") or "").strip()
    if notes and notes.lower() != "none":
        return notes[:200]
    return ""


def rows_from_report(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for result in data.get("results", []):
        qid = result.get("id", "")
        judge = result.get("judge") or {}
        matrix = judge.get("matrix") or {}
        for tier, providers in matrix.items():
            for provider, cell in providers.items():
                if not isinstance(cell, dict):
                    continue
                rows.append(
                    {
                        "question_id": qid,
                        "provider": provider,
                        "tier": tier,
                        "model": cell.get("model", ""),
                        "verdict": cell.get("verdict", ""),
                        "correctness": cell.get("correctness", cell.get("answer_quality", "")),
                        "grounding": cell.get("grounding", ""),
                        "completeness": cell.get("completeness", ""),
                        "context_sufficient": cell.get(
                            "context_sufficient",
                            "yes"
                            if cell.get("context_sufficiency") == "sufficient"
                            else "no",
                        ),
                        "unsupported_claims": cell.get("unsupported_claims", ""),
                        "missing_evidence": cell.get("missing_evidence", ""),
                        "notes": _cell_notes(cell),
                    }
                )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Export judge matrix TSV from benchmark JSON")
    parser.add_argument("reports", nargs="+", help="Benchmark JSON report paths")
    parser.add_argument("-o", "--output", help="Write TSV to file (default: stdout)")
    args = parser.parse_args()

    all_rows: list[dict[str, str]] = []
    for report in args.reports:
        all_rows.extend(rows_from_report(Path(report)))

    out = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(out, fieldnames=COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(all_rows)
    finally:
        if args.output:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
