#!/usr/bin/env python3
"""Adapt a SWE-bench question pack to the CURRENTLY indexed checkouts.

The generated packs carry gold in *base_commit old-side* coordinates, which
requires one indexed workspace per instance commit
(``contextbench/{repo}@{sha}``). This adapter remaps the gold onto the plain
``qa_repo/{repo}@main`` checkouts that the benchmark box actually indexes:

1. reconstruct the *post-patch* file at ``base_commit`` (the merged fix),
2. align it against the file in the current checkout (``difflib``),
3. map each change-run's NEW-side lines through the alignment.

Runs whose fix region was refactored away simply do not map — the question
keeps its file-level gold and drops to file-only. Questions whose repo is not
in the current index are dropped (recorded in ``meta``).

Usage:
  .venv/bin/python -m QA.adapt_swebench_pack_to_checkout \\
      --pack QA/fixtures/questions_swebench_python_smoke.yaml \\
      --output QA/fixtures/questions_swebench_python_smoke_indexed.yaml
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_DIFF = re.compile(r"^diff --git a/(.+?) b/(.+)$")

ROOT = Path(__file__).resolve().parent.parent
REPOS_DIR = ROOT / "QA" / "repos"


def _git(repo_dir: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def parse_patch_hunks(patch: str) -> dict[str, list[dict[str, Any]]]:
    """Per file: hunks with old_start/old_count and raw body lines."""
    hunks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_file: str | None = None
    for raw in (patch or "").splitlines():
        match = _GIT_DIFF.match(raw)
        if match:
            current_file = match.group(2)
            continue
        header = _HUNK.match(raw)
        if header and current_file:
            hunks[current_file].append(
                {
                    "old_start": int(header.group(1)),
                    "old_count": int(header.group(2)) if header.group(2) is not None else 1,
                    "lines": [],
                }
            )
            continue
        if current_file and hunks[current_file]:
            if raw.startswith(("diff ", "index ", "--- ", "+++ ")):
                continue
            hunks[current_file][-1]["lines"].append(raw)


    return hunks


def apply_hunks(base_lines: list[str], file_hunks: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Apply hunks to the base file; return patched lines + change runs.

    Each run records its NEW-side (post-patch) line numbers: added lines for
    insert/replace runs, a one-line context anchor for pure deletions.
    """
    patched: list[str] = []
    runs: list[dict[str, Any]] = []
    cursor = 0  # 0-based index into base_lines already consumed

    for hunk in sorted(file_hunks, key=lambda h: h["old_start"]):
        old_start = hunk["old_start"]
        old_count = hunk["old_count"]
        # For pure insertions (old_count == 0) the hunk applies AFTER old_start.
        copy_upto = old_start - 1 if old_count > 0 else old_start
        if copy_upto < cursor:
            raise ValueError("overlapping hunks")
        patched.extend(base_lines[cursor:copy_upto])
        cursor = copy_upto

        run_added: list[int] = []
        run_deleted = 0

        def flush_run() -> None:
            nonlocal run_added, run_deleted
            if run_added:
                runs.append({"new_lines": list(run_added)})
            elif run_deleted:
                # pure deletion: anchor at the next emitted new-side line
                runs.append({"new_lines": [], "anchor_new": len(patched) + 1})
            run_added = []
            run_deleted = 0

        for line in hunk["lines"]:
            if line.startswith("-"):
                expected = line[1:]
                if cursor >= len(base_lines) or base_lines[cursor] != expected:
                    raise ValueError(
                        f"old-side mismatch at base line {cursor + 1}"
                    )
                run_deleted += 1
                cursor += 1
            elif line.startswith("+"):
                patched.append(line[1:])
                run_added.append(len(patched))
            elif line.startswith(" "):
                flush_run()
                if cursor >= len(base_lines) or base_lines[cursor] != line[1:]:
                    raise ValueError(
                        f"context mismatch at base line {cursor + 1}"
                    )
                patched.append(base_lines[cursor])
                cursor += 1
            elif line.startswith("\\"):
                continue
            else:
                flush_run()
        flush_run()

    patched.extend(base_lines[cursor:])
    return patched, runs


def map_new_lines_to_current(
    patched: list[str],
    current: list[str],
) -> dict[int, int]:
    """1-based new-side -> current line map over equal alignment blocks."""
    matcher = difflib.SequenceMatcher(a=patched, b=current, autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset + 1] = block.b + offset + 1
    return mapping


def merge_intervals(numbers: list[int]) -> list[tuple[int, int]]:
    if not numbers:
        return []
    ordered = sorted(set(numbers))
    merged = [[ordered[0], ordered[0]]]
    for value in ordered[1:]:
        if value <= merged[-1][1] + 1:
            merged[-1][1] = value
        else:
            merged.append([value, value])
    return [(start, end) for start, end in merged]


def adapt_question(
    question: dict[str, Any],
    patch: str,
    repo_dir: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    stats = {"runs": 0, "runs_mapped": 0, "files_dropped": [], "error": ""}
    hunks_by_file = parse_patch_hunks(patch)

    expected_files: list[str] = []
    spans: list[dict[str, Any]] = []
    for file_path in question["expected_files"]:
        current_path = repo_dir / file_path
        if not current_path.is_file():
            stats["files_dropped"].append(file_path)
            continue
        base_rc, base_text = _git(
            repo_dir, "show", f"{question['base_commit']}:{file_path}"
        )
        if base_rc != 0:
            stats["error"] = f"base blob missing for {file_path}"
            continue
        try:
            patched, runs = apply_hunks(
                base_text.splitlines(), hunks_by_file.get(file_path, [])
            )
        except ValueError as exc:
            stats["error"] = f"{file_path}: {exc}"
            continue
        current_lines = current_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        line_map = map_new_lines_to_current(patched, current_lines)

        expected_files.append(file_path)
        mapped_here: list[int] = []
        for run in runs:
            stats["runs"] += 1
            targets = run["new_lines"] or [run.get("anchor_new")]
            mapped = [line_map[n] for n in targets if n in line_map]
            if mapped:
                stats["runs_mapped"] += 1
                mapped_here.extend(mapped)
        for start, end in merge_intervals(mapped_here):
            spans.append(
                {
                    "file_path": file_path,
                    "symbol": "",
                    "start_line": start,
                    "end_line": end,
                }
            )

    if not expected_files:
        return None, stats

    adapted = {
        key: question[key]
        for key in (
            "id",
            "instance_id",
            "repo",
            "repo_full",
            "version",
            "question",
            "difficulty",
            "intent",
            "expected_mode",
            "mechanism",
        )
        if key in question
    }
    # provenance only — MUST NOT be ``base_commit`` or the benchmark routes the
    # question to a contextbench per-commit workspace again.
    adapted["source_base_commit"] = question["base_commit"]
    adapted["expected_files"] = expected_files
    if spans:
        adapted["expected_spans"] = spans
    adapted["span_mapping"] = {
        "runs": stats["runs"],
        "runs_mapped": stats["runs_mapped"],
    }
    adapted["source"] = question.get("source", "")
    if question.get("fail_to_pass"):
        adapted["fail_to_pass"] = question["fail_to_pass"]
    return adapted, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--parquet",
        type=Path,
        default=Path("/tmp/swebench/lite_test.parquet"),
    )
    parser.add_argument(
        "--repos",
        type=str,
        default="",
        help="Comma-separated pack repo ids to keep (default: those with a QA/repos clone)",
    )
    args = parser.parse_args()

    import pyarrow.parquet as pq

    patches = {
        str(row["instance_id"]): row["patch"] or ""
        for row in pq.read_table(args.parquet).to_pylist()
    }
    pack = yaml.safe_load(args.pack.open())

    keep_repos = {r.strip() for r in args.repos.split(",") if r.strip()} or {
        entry.name
        for entry in REPOS_DIR.iterdir()
        if (entry / ".git").exists()
    }

    checkouts: dict[str, str] = {}
    questions: list[dict[str, Any]] = []
    dropped_repo: dict[str, int] = defaultdict(int)
    dropped_files: list[str] = []
    errors: list[str] = []
    total_runs = total_mapped = span_full = span_partial = span_none = 0

    for question in pack["questions"]:
        repo = question["repo"]
        if repo not in keep_repos:
            dropped_repo[repo] += 1
            continue
        repo_dir = REPOS_DIR / repo
        if repo not in checkouts:
            _, head = _git(repo_dir, "rev-parse", "HEAD")
            checkouts[repo] = head.strip()
        patch = patches.get(question["instance_id"], "")
        adapted, stats = adapt_question(question, patch, repo_dir)
        if stats["error"]:
            errors.append(f"{question['id']}: {stats['error']}")
        dropped_files.extend(
            f"{question['id']}:{path}" for path in stats["files_dropped"]
        )
        if adapted is None:
            dropped_repo[repo] += 0  # question dropped for missing files
            errors.append(f"{question['id']}: all gold files missing in checkout")
            continue
        total_runs += stats["runs"]
        total_mapped += stats["runs_mapped"]
        if stats["runs"] and stats["runs_mapped"] == stats["runs"]:
            span_full += 1
        elif stats["runs_mapped"]:
            span_partial += 1
        else:
            span_none += 1
        questions.append(adapted)

    repo_ids = {q["repo"] for q in questions}
    repositories = [
        entry
        for entry in pack.get("repositories", [])
        if entry["id"] in repo_ids
    ]
    for entry in repositories:
        entry["swebench_instances"] = sum(
            1 for q in questions if q["repo"] == entry["id"]
        )

    out = {
        "meta": {
            **pack.get("meta", {}),
            "adapted_from": str(args.pack),
            "span_coordinate_system": "indexed_checkout",
            "checkouts": checkouts,
            "dropped_repos": dict(dropped_repo),
            "description": (
                "SWE-bench gold remapped onto the currently indexed checkouts: "
                "post-patch (merged fix) lines aligned to the checkout via difflib; "
                "unmapped runs degrade to file-level gold."
            ),
        },
        "repositories": repositories,
        "questions": questions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        yaml.dump(
            out,
            handle,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )

    print(f"Wrote {args.output}")
    print(f"  questions kept: {len(questions)} (dropped by repo: {dict(dropped_repo)})")
    print(f"  change runs mapped: {total_mapped}/{total_runs}")
    print(f"  span gold: full={span_full} partial={span_partial} file-only={span_none}")
    if dropped_files:
        print(f"  gold files missing in checkout: {len(dropped_files)} {dropped_files[:5]}")
    if errors:
        print(f"  errors: {len(errors)}")
        for line in errors[:10]:
            print(f"    - {line}")


if __name__ == "__main__":
    main()
