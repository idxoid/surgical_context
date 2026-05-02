#!/usr/bin/env python3
"""Inspect benchmark snapshot rows from QA/benchmark_runs.jsonl.

``QA/qa_benchmark.py`` writes full reports to JSON files and appends a compact
pointer row here. This helper keeps the recent benchmark trail readable without
digging through /tmp by hand.

Examples:
    python QA/benchmark_runs.py --last 10
    python QA/benchmark_runs.py --repo fastapi --core12 --compare-last 2
    python QA/benchmark_runs.py --repo redux_toolkit --audit-pruned
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_DEFAULT_RUNS = Path(__file__).parent / "benchmark_runs.jsonl"

_METRICS = [
    ("pass_rate", "%", 1, True),
    ("precision_at_5", "", 3, True),
    ("file_recall", "", 3, True),
    ("role_recall", "", 3, True),
    ("tokens_surgical", "t", 0, False),
    ("reduction_ratio", "%", 1, True),
    ("assembly_ms_avg", "ms", 1, False),
]


def load_runs(path: Path) -> list[dict[str, Any]]:
    """Read snapshot rows, skipping blank/comment/malformed lines."""
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return entries


def filter_runs(
    entries: list[dict[str, Any]],
    *,
    repo: str | None = None,
    core12: bool | None = None,
) -> list[dict[str, Any]]:
    rows = entries
    if repo:
        rows = [row for row in rows if row.get("repo") == repo]
    if core12 is not None:
        rows = [row for row in rows if bool(row.get("core12_only")) is core12]
    return rows


def select_last(entries: list[dict[str, Any]], last: int) -> list[dict[str, Any]]:
    if last and last > 0:
        return entries[-last:]
    return entries


def _fmt(value: Any, unit: str, precision: int) -> str:
    if value is None:
        return "-"
    if unit == "%":
        return f"{float(value) * 100:.{precision}f}%"
    if precision == 0:
        return f"{int(value):,}{unit}"
    return f"{float(value):.{precision}f}{unit}"


def _delta(curr: Any, prev: Any, unit: str, precision: int, higher_is_better: bool) -> str:
    if curr is None or prev is None:
        return "-"
    diff = float(curr) - float(prev)
    improved = diff > 0 if higher_is_better else diff < 0
    marker = "+" if improved else "-" if diff else "="
    if unit == "%":
        return f"{marker}{diff * 100:.{precision}f}%"
    if precision == 0:
        return f"{marker}{int(diff):+,d}"
    return f"{marker}{diff:+.{precision}f}"


def print_runs(entries: list[dict[str, Any]]) -> None:
    if not entries:
        print("No benchmark snapshot rows found.")
        return

    print(f"\nBenchmark snapshots ({len(entries)} entries, oldest -> newest)")
    print("=" * 132)
    print(
        f"{'#':>3} {'repo':<15} {'core':<5} {'pass':>7} {'prec':>7} "
        f"{'file':>7} {'role':>7} {'tokens':>9} {'reduc':>8} {'ms':>8} report"
    )
    print("-" * 132)
    for idx, row in enumerate(entries, 1):
        print(
            f"{idx:>3} "
            f"{str(row.get('repo') or '-'):15.15} "
            f"{str(bool(row.get('core12_only'))):<5} "
            f"{_fmt(row.get('pass_rate'), '%', 1):>7} "
            f"{_fmt(row.get('precision_at_5'), '', 3):>7} "
            f"{_fmt(row.get('file_recall'), '', 3):>7} "
            f"{_fmt(row.get('role_recall'), '', 3):>7} "
            f"{_fmt(row.get('tokens_surgical'), 't', 0):>9} "
            f"{_fmt(row.get('reduction_ratio'), '%', 1):>8} "
            f"{_fmt(row.get('assembly_ms_avg'), 'ms', 1):>8} "
            f"{row.get('report_path') or '-'}"
        )
    print("=" * 132)


def print_comparison(entries: list[dict[str, Any]]) -> None:
    if len(entries) < 2:
        print("Need at least two rows to compare.")
        return
    prev, curr = entries[-2], entries[-1]
    print("\nLast-run comparison")
    print("=" * 72)
    print(f"previous: {prev.get('report_path')}")
    print(f"current:  {curr.get('report_path')}")
    print("-" * 72)
    for key, unit, precision, higher_is_better in _METRICS:
        print(
            f"{key:<18} "
            f"{_fmt(prev.get(key), unit, precision):>12} -> "
            f"{_fmt(curr.get(key), unit, precision):>12} "
            f"{_delta(curr.get(key), prev.get(key), unit, precision, higher_is_better):>12}"
        )
    print("=" * 72)


def _load_report(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    report = Path(path)
    if not report.exists():
        return None
    try:
        return json.loads(report.read_text())
    except json.JSONDecodeError:
        return None


def audit_pruned(entries: list[dict[str, Any]], *, top: int = 10) -> dict[str, Any]:
    """Aggregate pruned[] reasons from the referenced full reports."""
    reason_counts: Counter[str] = Counter()
    expected_symbol_hits: Counter[str] = Counter()
    report_count = 0
    question_count = 0
    pruned_count = 0

    for row in entries:
        report = _load_report(row.get("report_path"))
        if not report:
            continue
        report_count += 1
        for result in report.get("results", []):
            question_count += 1
            expected_symbols = {str(s).lower() for s in result.get("expected_symbols", [])}
            contract = (result.get("ready_context") or {}).get("contract") or {}
            for item in contract.get("pruned", []):
                pruned_count += 1
                reason_counts[str(item.get("reason") or "unknown")] += 1
                name = str(item.get("name") or item.get("uid") or "").lower()
                if name and name in expected_symbols:
                    expected_symbol_hits[name] += 1

    return {
        "reports_read": report_count,
        "questions_seen": question_count,
        "pruned_items_seen": pruned_count,
        "top_reasons": reason_counts.most_common(top),
        "expected_symbols_in_pruned": expected_symbol_hits.most_common(top),
    }


def print_pruned_audit(audit: dict[str, Any]) -> None:
    print("\nPruned audit")
    print("=" * 72)
    print(f"reports read:      {audit['reports_read']}")
    print(f"questions seen:    {audit['questions_seen']}")
    print(f"pruned items seen: {audit['pruned_items_seen']}")
    print("-" * 72)
    print("top reasons:")
    for reason, count in audit["top_reasons"]:
        print(f"  {reason:<42} {count:>6}")
    if audit["expected_symbols_in_pruned"]:
        print("-" * 72)
        print("expected symbols found in pruned:")
        for symbol, count in audit["expected_symbols_in_pruned"]:
            print(f"  {symbol:<42} {count:>6}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect QA/benchmark_runs.jsonl benchmark snapshots"
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=_DEFAULT_RUNS,
        help=f"Snapshot manifest path (default: {_DEFAULT_RUNS})",
    )
    parser.add_argument("--repo", help="Filter to one repo id", default=None)
    parser.add_argument(
        "--core12",
        action="store_true",
        help="Filter to core12 rows only",
    )
    parser.add_argument(
        "--all-questions",
        action="store_true",
        help="Filter to non-core12 rows only",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=10,
        help="Show the last N rows after filtering (0 = all). Default: 10",
    )
    parser.add_argument(
        "--compare-last",
        type=int,
        default=0,
        help="Compare the last N rows after filtering; currently prints last two from that window",
    )
    parser.add_argument(
        "--audit-pruned",
        action="store_true",
        help="Read referenced reports and summarize ready_context.contract.pruned reasons",
    )
    parser.add_argument("--top", type=int, default=10, help="Top N audit rows. Default: 10")
    args = parser.parse_args()

    if args.core12 and args.all_questions:
        print("--core12 and --all-questions are mutually exclusive", file=sys.stderr)
        return 2

    core12_filter = True if args.core12 else False if args.all_questions else None
    entries = filter_runs(load_runs(args.file), repo=args.repo, core12=core12_filter)
    entries = select_last(entries, args.compare_last or args.last)
    if not entries:
        print(f"No benchmark snapshot rows in {args.file}", file=sys.stderr)
        return 1

    print_runs(entries)
    if args.compare_last:
        print_comparison(entries)
    if args.audit_pruned:
        print_pruned_audit(audit_pruned(entries, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
