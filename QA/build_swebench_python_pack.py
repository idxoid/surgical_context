#!/usr/bin/env python3
"""Build an axis-benchmark question pack from SWE-bench (Lite/Verified).

Question text = GitHub issue ``problem_statement``.
Ground truth   = gold ``patch`` (the PR that closes the issue):
  - ``expected_files`` ← paths touched by the patch
  - ``expected_spans`` / ``expected_edit_spans`` ← precise old-side edit loci
  - ``expected_hunk_spans`` ← full old-side diff hunk ranges for diagnostics

Usage:
  .venv/bin/python -m QA.build_swebench_python_pack \\
      --parquet /tmp/swebench/lite_test.parquet \\
      --output QA/fixtures/questions_swebench_python.yaml

  # balanced smoke subset (default when --limit is set):
  .venv/bin/python -m QA.build_swebench_python_pack \\
      --parquet /tmp/swebench/lite_test.parquet \\
      --output QA/fixtures/questions_swebench_python_smoke.yaml \\
      --limit 30 --prefer-repos django/django,pallets/flask \\
      --repos django/django,pallets/flask
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

# Unified-diff hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@
_HUNK = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
_GIT_DIFF = re.compile(r"^diff --git a/(.+?) b/(.+)$")

# Map SWE-bench ``owner/name`` → short pack id (matches questions_python.yaml where possible).
_REPO_ID: dict[str, str] = {
    "django/django": "django",
    "pallets/flask": "flask",
    "sympy/sympy": "sympy",
    "matplotlib/matplotlib": "matplotlib",
    "scikit-learn/scikit-learn": "scikit_learn",
    "pytest-dev/pytest": "pytest",
    "sphinx-doc/sphinx": "sphinx",
    "astropy/astropy": "astropy",
    "psf/requests": "requests",
    "pylint-dev/pylint": "pylint",
    "pydata/xarray": "xarray",
    "mwaskom/seaborn": "seaborn",
}

_REPO_META: dict[str, dict[str, str]] = {
    "django": {
        "name": "Django",
        "clone_url": "https://github.com/django/django.git",
        "repo_url": "https://github.com/django/django",
        "docs_url": "https://docs.djangoproject.com/",
        "rationale": "SWE-bench Lite — largest Python issue/PR slice; ORM, forms, middleware.",
    },
    "flask": {
        "name": "Flask",
        "clone_url": "https://github.com/pallets/flask.git",
        "repo_url": "https://github.com/pallets/flask",
        "docs_url": "https://flask.palletsprojects.com/",
        "rationale": "SWE-bench Lite — small Flask slice; app/context/routing bugs.",
    },
    "sympy": {
        "name": "SymPy",
        "clone_url": "https://github.com/sympy/sympy.git",
        "repo_url": "https://github.com/sympy/sympy",
        "docs_url": "https://docs.sympy.org/",
        "rationale": "SWE-bench Lite — symbolic math library, large issue volume.",
    },
    "matplotlib": {
        "name": "Matplotlib",
        "clone_url": "https://github.com/matplotlib/matplotlib.git",
        "repo_url": "https://github.com/matplotlib/matplotlib",
        "docs_url": "https://matplotlib.org/",
        "rationale": "SWE-bench Lite — plotting library.",
    },
    "scikit_learn": {
        "name": "scikit-learn",
        "clone_url": "https://github.com/scikit-learn/scikit-learn.git",
        "repo_url": "https://github.com/scikit-learn/scikit-learn",
        "docs_url": "https://scikit-learn.org/",
        "rationale": "SWE-bench Lite — ML toolkit.",
    },
    "pytest": {
        "name": "pytest",
        "clone_url": "https://github.com/pytest-dev/pytest.git",
        "repo_url": "https://github.com/pytest-dev/pytest",
        "docs_url": "https://docs.pytest.org/",
        "rationale": "SWE-bench Lite — test runner.",
    },
    "sphinx": {
        "name": "Sphinx",
        "clone_url": "https://github.com/sphinx-doc/sphinx.git",
        "repo_url": "https://github.com/sphinx-doc/sphinx",
        "docs_url": "https://www.sphinx-doc.org/",
        "rationale": "SWE-bench Lite — documentation generator.",
    },
    "astropy": {
        "name": "Astropy",
        "clone_url": "https://github.com/astropy/astropy.git",
        "repo_url": "https://github.com/astropy/astropy",
        "docs_url": "https://docs.astropy.org/",
        "rationale": "SWE-bench Lite — astronomy toolkit.",
    },
    "requests": {
        "name": "Requests",
        "clone_url": "https://github.com/psf/requests.git",
        "repo_url": "https://github.com/psf/requests",
        "docs_url": "https://requests.readthedocs.io/",
        "rationale": "SWE-bench Lite — HTTP client.",
    },
    "pylint": {
        "name": "Pylint",
        "clone_url": "https://github.com/pylint-dev/pylint.git",
        "repo_url": "https://github.com/pylint-dev/pylint",
        "docs_url": "https://pylint.readthedocs.io/",
        "rationale": "SWE-bench Lite — static analysis.",
    },
    "xarray": {
        "name": "xarray",
        "clone_url": "https://github.com/pydata/xarray.git",
        "repo_url": "https://github.com/pydata/xarray",
        "docs_url": "https://docs.xarray.dev/",
        "rationale": "SWE-bench Lite — N-D labeled arrays.",
    },
    "seaborn": {
        "name": "seaborn",
        "clone_url": "https://github.com/mwaskom/seaborn.git",
        "repo_url": "https://github.com/mwaskom/seaborn",
        "docs_url": "https://seaborn.pydata.org/",
        "rationale": "SWE-bench Lite — statistical visualization.",
    },
}


def parse_patch_gold(
    patch: str,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract files, full hunks, and precise old-side edit loci.

    Deletions/replacements map to their deleted old-side lines. Pure additions
    pin the old-side insertion anchor. Both coordinate systems therefore match
    an index built at ``base_commit`` without treating unchanged hunk context
    as equally relevant line gold.
    """
    files: list[str] = []
    hunk_spans: list[dict[str, Any]] = []
    edit_spans: list[dict[str, Any]] = []
    current_file: str | None = None
    current_hunk: tuple[int, int] | None = None
    hunk_lines: list[str] = []

    def add_span(target: list[dict[str, Any]], start: int, end: int) -> None:
        if not current_file:
            return
        target.append(
            {
                "file_path": current_file,
                "symbol": "",
                "start_line": max(1, start),
                "end_line": max(1, end),
            }
        )

    def flush_hunk() -> None:
        nonlocal current_hunk, hunk_lines
        if current_hunk is None or not current_file:
            current_hunk = None
            hunk_lines = []
            return
        old_start, old_count = current_hunk
        if old_count <= 0:
            add_span(hunk_spans, old_start, old_start)
        else:
            add_span(hunk_spans, old_start, old_start + old_count - 1)

        old_line = old_start
        deleted_lines: list[int] = []
        insertion_anchor: int | None = None

        def flush_change() -> None:
            nonlocal deleted_lines, insertion_anchor
            if deleted_lines:
                add_span(edit_spans, min(deleted_lines), max(deleted_lines))
            elif insertion_anchor is not None:
                add_span(edit_spans, insertion_anchor, insertion_anchor)
            deleted_lines = []
            insertion_anchor = None

        for line in hunk_lines:
            if line.startswith("-"):
                if insertion_anchor is None:
                    insertion_anchor = old_line
                deleted_lines.append(old_line)
                old_line += 1
            elif line.startswith("+"):
                if insertion_anchor is None:
                    insertion_anchor = old_line
            elif line.startswith(" "):
                flush_change()
                old_line += 1
            elif line.startswith("\\"):
                continue
            else:
                flush_change()
        flush_change()
        current_hunk = None
        hunk_lines = []

    for raw_line in (patch or "").splitlines():
        git_match = _GIT_DIFF.match(raw_line)
        if git_match:
            flush_hunk()
            current_file = git_match.group(2)
            if current_file not in files:
                files.append(current_file)
            continue

        hunk = _HUNK.match(raw_line)
        if hunk and current_file:
            flush_hunk()
            current_hunk = (
                int(hunk.group(1)),
                int(hunk.group(2) if hunk.group(2) is not None else 1),
            )
            continue
        if current_hunk is not None:
            hunk_lines.append(raw_line)

    flush_hunk()
    return files, _merge_spans(hunk_spans), _merge_spans(edit_spans)


def parse_patch_spans(patch: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Backward-compatible full old-side hunk extraction."""
    files, hunk_spans, _edit_spans = parse_patch_gold(patch)
    return files, hunk_spans


def _merge_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping/adjacent spans within the same file."""
    by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for span in spans:
        by_file[span["file_path"]].append((span["start_line"], span["end_line"]))

    merged: list[dict[str, Any]] = []
    for file_path, intervals in by_file.items():
        intervals.sort()
        cur_s, cur_e = intervals[0]
        for start, end in intervals[1:]:
            if start <= cur_e + 1:
                cur_e = max(cur_e, end)
            else:
                merged.append(
                    {
                        "file_path": file_path,
                        "symbol": "",
                        "start_line": cur_s,
                        "end_line": cur_e,
                    }
                )
                cur_s, cur_e = start, end
        merged.append(
            {
                "file_path": file_path,
                "symbol": "",
                "start_line": cur_s,
                "end_line": cur_e,
            }
        )
    return merged


def _repo_id(repo: str) -> str:
    if repo in _REPO_ID:
        return _REPO_ID[repo]
    return repo.rsplit("/", 1)[-1].replace("-", "_")


def _difficulty_from_patch(files: list[str], spans: list[dict[str, Any]]) -> str:
    total_lines = sum(s["end_line"] - s["start_line"] + 1 for s in spans)
    if len(files) <= 1 and len(spans) <= 1 and total_lines <= 20:
        return "easy"
    if len(files) <= 1 and len(spans) <= 3 and total_lines <= 80:
        return "medium"
    return "hard"


def row_to_question(row: dict[str, Any]) -> dict[str, Any] | None:
    problem = (row.get("problem_statement") or "").strip()
    patch = row.get("patch") or ""
    if not problem or not patch:
        return None
    files, hunk_spans, edit_spans = parse_patch_gold(patch)
    if not files or not hunk_spans or not edit_spans:
        return None

    repo_full = str(row["repo"])
    repo = _repo_id(repo_full)
    instance_id = str(row["instance_id"])
    return {
        "id": instance_id.replace("__", "_").replace("-", "_"),
        "instance_id": instance_id,
        "repo": repo,
        "repo_full": repo_full,
        "base_commit": str(row["base_commit"]),
        "version": str(row.get("version") or ""),
        "question": problem,
        "difficulty": _difficulty_from_patch(files, edit_spans),
        "intent": "bug_fix",
        "expected_mode": "workspace",
        "mechanism": f"swebench_{repo}",
        "expected_files": files,
        "expected_spans": [dict(span) for span in edit_spans],
        "expected_edit_spans": edit_spans,
        "expected_hunk_spans": hunk_spans,
        "source": "SWE-bench_Lite",
        "fail_to_pass": row.get("FAIL_TO_PASS") or [],
    }


def select_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int | None,
    prefer_repos: list[str],
    repos_filter: list[str] | None,
) -> list[dict[str, Any]]:
    if repos_filter:
        allowed = set(repos_filter)
        rows = [r for r in rows if r["repo"] in allowed]
    if not limit or limit >= len(rows):
        return rows

    prefer_set = set(prefer_repos)
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)

    selected: list[dict[str, Any]] = []
    # 1) Guarantee at least one instance from each preferred repo that exists.
    for repo in prefer_repos:
        if len(selected) >= limit:
            break
        if by_repo.get(repo):
            selected.append(by_repo[repo].pop(0))

    # 2) Round-robin: preferred repos first in the rotation, then the rest.
    repo_order = list(prefer_repos) + sorted(
        (r for r in by_repo if r not in prefer_set),
        key=lambda r: (-len(by_repo[r]), r),
    )
    while len(selected) < limit and any(by_repo.values()):
        progressed = False
        for repo in repo_order:
            if len(selected) >= limit:
                break
            if by_repo.get(repo):
                selected.append(by_repo[repo].pop(0))
                progressed = True
        if not progressed:
            break
    return selected


def build_pack(rows: list[dict[str, Any]]) -> dict[str, Any]:
    questions: list[dict[str, Any]] = []
    repo_counts: Counter[str] = Counter()
    for row in rows:
        q = row_to_question(row)
        if q is None:
            continue
        questions.append(q)
        repo_counts[q["repo"]] += 1

    repositories = []
    for repo_id, count in sorted(repo_counts.items(), key=lambda x: (-x[1], x[0])):
        meta = _REPO_META.get(repo_id, {})
        # Find a full name from a question
        full = next(q["repo_full"] for q in questions if q["repo"] == repo_id)
        repositories.append(
            {
                "id": repo_id,
                "name": meta.get("name") or repo_id,
                "clone_url": meta.get("clone_url") or f"https://github.com/{full}.git",
                "repo_url": meta.get("repo_url") or f"https://github.com/{full}",
                "docs_url": meta.get("docs_url") or "",
                "language": "python",
                "rationale": meta.get("rationale")
                or f"SWE-bench Lite — {count} issue/PR instances.",
                "swebench_instances": count,
            }
        )

    return {
        "meta": {
            "source": "SWE-bench/SWE-bench_Lite",
            "split": "test",
            "question_field": "problem_statement",
            "gold_field": "patch",
            "span_coordinate_system": "base_commit_old_side_edit_locus",
            "description": (
                "Issue text is the question; gold files/edit loci and diagnostic full hunks "
                "are derived from the PR patch (pre-patch / base_commit line numbers)."
            ),
        },
        "repositories": repositories,
        "questions": questions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parquet",
        type=Path,
        default=Path("/tmp/swebench/lite_test.parquet"),
        help="SWE-bench parquet with problem_statement + patch columns",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("QA/fixtures/questions_swebench_python.yaml"),
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap question count")
    parser.add_argument(
        "--prefer-repos",
        type=str,
        default="django/django,pallets/flask",
        help="Comma-separated owner/name repos to prefer when --limit is set",
    )
    parser.add_argument(
        "--repos",
        type=str,
        default="",
        help="Optional comma-separated owner/name allowlist",
    )
    args = parser.parse_args()

    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("pyarrow required (use project .venv)") from exc

    rows = pq.read_table(args.parquet).to_pylist()
    prefer = [r.strip() for r in args.prefer_repos.split(",") if r.strip()]
    allow = [r.strip() for r in args.repos.split(",") if r.strip()] or None
    selected = select_rows(rows, limit=args.limit, prefer_repos=prefer, repos_filter=allow)
    pack = build_pack(selected)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        yaml.dump(
            pack,
            handle,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )

    n_q = len(pack["questions"])
    n_span = sum(len(q["expected_spans"]) for q in pack["questions"])
    print(f"Wrote {args.output}")
    print(f"  questions: {n_q}")
    print(f"  repositories: {len(pack['repositories'])}")
    print(f"  span entries: {n_span}")
    print("  by repo:", dict(Counter(q["repo"] for q in pack["questions"])))


if __name__ == "__main__":
    main()
