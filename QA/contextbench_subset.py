"""Build a deterministic, repository-balanced ContextBench smoke subset."""

from __future__ import annotations

import argparse
import csv
import hashlib
from collections import defaultdict, deque
from pathlib import Path


def _repo_name(row: dict[str, str]) -> str:
    original = row.get("original_inst_id", "")
    if "__" not in original:
        return ""
    tail = original.split("__", 1)[1]
    return tail.split("-", 1)[0]


def _stable_key(row: dict[str, str], seed: str) -> str:
    identity = row.get("instance_id") or row.get("original_inst_id", "")
    return hashlib.sha256(f"{seed}\0{identity}".encode()).hexdigest()


def select_rows(
    rows: list[dict[str, str]],
    *,
    limit: int,
    seed: str,
    benches: set[str],
    languages: set[str],
    repos: set[str] | None,
) -> list[dict[str, str]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    buckets: dict[str, deque[dict[str, str]]] = {}
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        repo = _repo_name(row)
        if row.get("bench") not in benches or row.get("language") not in languages:
            continue
        if repos is not None and repo not in repos:
            continue
        grouped[repo].append(row)
    for repo, values in grouped.items():
        buckets[repo] = deque(sorted(values, key=lambda row: _stable_key(row, seed)))

    selected: list[dict[str, str]] = []
    repo_order = sorted(buckets)
    while len(selected) < limit and repo_order:
        next_order = []
        for repo in repo_order:
            bucket = buckets[repo]
            if bucket and len(selected) < limit:
                selected.append(bucket.popleft())
            if bucket:
                next_order.append(repo)
        repo_order = next_order
    return selected


def build_subset(
    source: Path,
    output: Path,
    *,
    limit: int,
    seed: str,
    benches: set[str],
    languages: set[str],
    repos: set[str] | None,
) -> int:
    with source.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames
        if not fieldnames:
            raise ValueError("source CSV has no header")
        selected = select_rows(
            list(reader),
            limit=limit,
            seed=seed,
            benches=benches,
            languages=languages,
            repos=repos,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
    return len(selected)


def _items(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--seed", default="surgical-context-v1")
    parser.add_argument("--bench", default="Verified")
    parser.add_argument("--language", default="python")
    parser.add_argument("--repos", help="Optional comma-separated repository names")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    count = build_subset(
        args.source,
        args.output,
        limit=args.limit,
        seed=args.seed,
        benches=_items(args.bench),
        languages=_items(args.language),
        repos=_items(args.repos) if args.repos else None,
    )
    print(f"wrote {count} ContextBench task(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
