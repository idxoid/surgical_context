#!/usr/bin/env python3
"""Compare benchmark baselines from QA/baselines.jsonl.

Each ``QA/qa_benchmark.py --report ...`` run appends one summary line to
``baselines.jsonl``. This tool reads the last N entries and prints a
side-by-side table so you can see whether a weight tweak / code change
improved or regressed retrieval quality.

Examples:
    # Show the last 5 baseline entries.
    python QA/baselines_compare.py

    # Compare just the last two entries (current vs previous).
    python QA/baselines_compare.py --last 2

    # Show all entries from a specific log file.
    python QA/baselines_compare.py --file QA/baselines.jsonl --last 0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_BASELINES = Path(__file__).parent / "baselines.jsonl"


_METRICS = [
    ("pass_rate", "%", 4),
    ("recall_at_5", "", 4),
    ("precision_at_5", "", 4),
    ("file_recall", "", 4),
    ("reduction_ratio", "%", 4),
    ("tokens_surgical", "t", 0),
    ("tokens_carpet_bomb", "t", 0),
    ("assembly_ms_avg", "ms", 1),
]


def load_baselines(path: Path) -> list[dict]:
    """Read baselines.jsonl, skipping comment/blank lines."""
    if not path.exists():
        return []
    entries: list[dict] = []
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


def _fmt(value, unit: str, precision: int) -> str:
    if value is None:
        return "    -"
    if unit == "%":
        return f"{value * 100:>6.{precision}f}%"
    if precision == 0:
        return f"{int(value):>7,}{unit}"
    return f"{value:>7.{precision}f}{unit}"


def _delta(curr, prev, unit: str, precision: int) -> str:
    if curr is None or prev is None:
        return ""
    diff = curr - prev
    if abs(diff) < 10**-precision and precision > 0:
        return "  ="
    if unit == "%":
        return f" ({diff * 100:+.{precision}f}%)"
    if precision == 0:
        return f" ({int(diff):+,d})"
    return f" ({diff:+.{precision}f})"


def print_comparison(entries: list[dict]) -> None:
    if not entries:
        print("No baseline entries found.")
        return

    header_cols = [f"#{len(entries) - i}" for i in range(len(entries))]
    print(f"\nBaselines comparison ({len(entries)} entries, oldest → newest)")
    print("=" * (24 + 18 * len(entries)))

    metric_label_w = 22
    print(f"{'metric':<{metric_label_w}}", end="")
    for col in header_cols[::-1]:
        print(f"{col:>18}", end="")
    print()
    print("-" * (metric_label_w + 18 * len(entries)))

    for key, unit, precision in _METRICS:
        print(f"{key:<{metric_label_w}}", end="")
        prev_val = None
        for entry in entries:
            val = entry.get(key)
            cell = _fmt(val, unit, precision)
            cell += _delta(val, prev_val, unit, precision)
            print(f"{cell:>18}", end="")
            prev_val = val
        print()

    print("=" * (metric_label_w + 18 * len(entries)))
    print(
        "Δ shown vs the immediately older entry. Positive = improvement for "
        "recall/precision/pass_rate/reduction; for tokens/latency a negative "
        "Δ is the improvement."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare the last N entries from QA/baselines.jsonl"
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=_DEFAULT_BASELINES,
        help=f"Baselines log path (default: {_DEFAULT_BASELINES})",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=5,
        help="Show the last N entries (0 = all). Default: 5",
    )
    args = parser.parse_args()

    entries = load_baselines(args.file)
    if not entries:
        print(f"No baseline entries in {args.file}", file=sys.stderr)
        return 1

    if args.last and args.last > 0:
        entries = entries[-args.last :]

    print_comparison(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
