#!/usr/bin/env python3
"""Build an axis-benchmark question pack from RepoBench-R (Python).

RepoBench-R items are next-line completion with a candidate snippet pool.
Gold for retrieval is ``golden_snippet_index`` into ``context`` (not a PR patch).

Important coverage note
-----------------------
RepoBench does **not** contain our FAQ cores (django/django, pallets/flask,
fastapi, pydantic, sqlalchemy/sqlalchemy, celery/celery). Exact hit among the
FAQ set is only ``pallets/click``. This builder therefore samples:

* **exact** — pallets/click
* **stack_adjacent** — werkzeug / jinja (Flask stack), starlette (FastAPI stack),
  mako (SQLAlchemy org)
* **ecosystem_proxy** — selected django-* / flask-* packages (not the cores)

Usage:
  .venv/bin/python -m QA.build_repobench_python_pack \\
      --cff /tmp/repobench/python_cff.gz \\
      --cfr /tmp/repobench/python_cfr.gz \\
      --output QA/fixtures/questions_repobench_python.yaml \\
      --per-repo 20
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import pickle
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

# (github owner/name) → pack repo id, overlap class, optional FAQ proxy tag
_TARGETS: list[dict[str, str]] = [
    {
        "repo_full": "pallets/click",
        "id": "click",
        "name": "Click",
        "overlap": "exact",
        "proxies_for": "click",
        "rationale": "Exact FAQ match — only FAQ core present in RepoBench-R.",
    },
    {
        "repo_full": "pallets/werkzeug",
        "id": "werkzeug",
        "name": "Werkzeug",
        "overlap": "stack_adjacent",
        "proxies_for": "flask",
        "rationale": "Flask WSGI stack (pallets); Flask itself absent from RepoBench.",
    },
    {
        "repo_full": "pallets/jinja",
        "id": "jinja",
        "name": "Jinja2",
        "overlap": "stack_adjacent",
        "proxies_for": "flask",
        "rationale": "Flask template engine (pallets); stack-adjacent to FAQ flask.",
    },
    {
        "repo_full": "encode/starlette",
        "id": "starlette",
        "name": "Starlette",
        "overlap": "stack_adjacent",
        "proxies_for": "fastapi",
        "rationale": "FastAPI ASGI core dependency; FastAPI itself absent from RepoBench.",
    },
    {
        "repo_full": "sqlalchemy/mako",
        "id": "mako",
        "name": "Mako",
        "overlap": "stack_adjacent",
        "proxies_for": "sqlalchemy",
        "rationale": "sqlalchemy org template engine; SQLAlchemy core absent from RepoBench.",
    },
    {
        "repo_full": "erikvw/django-crypto-fields",
        "id": "django_crypto_fields",
        "name": "django-crypto-fields",
        "overlap": "ecosystem_proxy",
        "proxies_for": "django",
        "rationale": "Django ecosystem (test split); django/django not in RepoBench.",
    },
    {
        "repo_full": "ozgurgunes/django-manifest",
        "id": "django_manifest",
        "name": "django-manifest",
        "overlap": "ecosystem_proxy",
        "proxies_for": "django",
        "rationale": "Django ecosystem (test split); proxy for FAQ django coverage gap.",
    },
    {
        "repo_full": "Jaza/flask-editablesite",
        "id": "flask_editablesite",
        "name": "flask-editablesite",
        "overlap": "ecosystem_proxy",
        "proxies_for": "flask",
        "rationale": "Flask ecosystem plugin; pallets/flask not in RepoBench.",
    },
]

_MISSING_FAQ = [
    "django/django",
    "pallets/flask",
    "tiangolo/fastapi",
    "fastapi/fastapi",
    "pydantic/pydantic",
    "sqlalchemy/sqlalchemy",
    "celery/celery",
]

_SETTING_PREF = {"cff": 0, "cfr": 1}
_SPLIT_PREF = {"test": 0, "train": 1}
_DIFF_PREF = {"hard": 0, "easy": 1}


def load_repobench(cff: Path, cfr: Path | None) -> dict[str, list[dict[str, Any]]]:
    """Load CFF (+ optional CFR) into ``repo_full → list of enriched rows``."""
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    paths = [("cff", cff)]
    if cfr is not None and cfr.exists():
        paths.append(("cfr", cfr))

    for setting, path in paths:
        with gzip.open(path, "rb") as handle:
            data = pickle.load(handle)
        for split, difficulties in data.items():
            for difficulty, rows in difficulties.items():
                for idx, row in enumerate(rows):
                    repo = row.get("repo_name") or ""
                    if not repo:
                        continue
                    by_repo[repo].append(
                        {
                            **row,
                            "_setting": setting,
                            "_split": split,
                            "_difficulty": difficulty,
                            "_row_idx": idx,
                        }
                    )
    return by_repo


def _code_tail(code: str, max_lines: int = 35) -> str:
    lines = (code or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def _snippet_preview(snippet: str, max_chars: int = 240) -> str:
    text = (snippet or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def make_question_text(file_path: str, code: str, next_line: str) -> str:
    tail = _code_tail(code)
    return (
        f"In `{file_path}`, the following code is incomplete. "
        f"Retrieve the cross-file definition needed so the next line can be written.\n\n"
        f"```python\n{tail}\n```\n\n"
        f"Next line to complete: `{next_line}`"
    )


def _dedupe_key(row: dict[str, Any]) -> str:
    raw = f"{row.get('file_path')}|{row.get('next_line')}|{row.get('golden_snippet_index')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _rank_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        _SPLIT_PREF.get(row["_split"], 9),
        _SETTING_PREF.get(row["_setting"], 9),
        _DIFF_PREF.get(row["_difficulty"], 9),
        -len(row.get("context") or []),
    )


def select_rows(
    rows: list[dict[str, Any]],
    *,
    per_repo: int,
    max_per_file: int,
) -> list[dict[str, Any]]:
    """Prefer test > train, cff > cfr, hard > easy; diversify file_path."""
    ranked = sorted(rows, key=_rank_key)
    seen: set[str] = set()
    per_file: dict[str, int] = defaultdict(int)
    selected: list[dict[str, Any]] = []
    for row in ranked:
        if len(selected) >= per_repo:
            break
        key = _dedupe_key(row)
        if key in seen:
            continue
        fp = str(row.get("file_path") or "")
        if per_file[fp] >= max_per_file:
            continue
        seen.add(key)
        per_file[fp] += 1
        selected.append(row)
    return selected


def _safe_id(repo_id: str, setting: str, split: str, difficulty: str, row_idx: int) -> str:
    return f"rb_{repo_id}_{setting}_{split}_{difficulty}_{row_idx}"


def row_to_question(row: dict[str, Any], target: dict[str, str]) -> dict[str, Any] | None:
    file_path = (row.get("file_path") or "").strip()
    next_line = (row.get("next_line") or "").rstrip("\n")
    code = row.get("code") or ""
    context = row.get("context") or []
    gold_idx = row.get("golden_snippet_index")
    if gold_idx is None:
        gold_idx = row.get("gold_snippet_index")
    if not file_path or next_line is None or gold_idx is None:
        return None
    try:
        gold_idx_i = int(gold_idx)
    except (TypeError, ValueError):
        return None
    if not context or gold_idx_i < 0 or gold_idx_i >= len(context):
        return None

    gold_snippet = context[gold_idx_i]
    repo_id = target["id"]
    setting = row["_setting"]
    split = row["_split"]
    difficulty = row["_difficulty"]
    qid = _safe_id(repo_id, setting, split, difficulty, int(row["_row_idx"]))

    # Infer a rough symbol hint from the gold snippet head (def/class).
    symbol = ""
    m = re.search(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", gold_snippet, re.M)
    if m:
        symbol = m.group(1)

    return {
        "id": qid,
        "repo": repo_id,
        "repo_full": target["repo_full"],
        "overlap": target["overlap"],
        "proxies_for": target["proxies_for"],
        "question": make_question_text(file_path, code, next_line),
        "difficulty": difficulty if difficulty in {"easy", "hard"} else "medium",
        "intent": "complete_line",
        "expected_mode": "workspace",
        "mechanism": f"repobench_{repo_id}",
        # Incomplete file is the locus; gold *snippet* has no stable path in RepoBench.
        "expected_files": [file_path],
        "expected_symbols": [symbol] if symbol else [],
        "source": "RepoBench-R",
        "repobench": {
            "setting": setting,
            "split": split,
            "difficulty": difficulty,
            "row_idx": int(row["_row_idx"]),
            "file_path": file_path,
            "next_line": next_line,
            "gold_snippet_index": gold_idx_i,
            "n_candidates": len(context),
            "gold_snippet_preview": _snippet_preview(gold_snippet),
            "gold_symbol_hint": symbol,
        },
    }


def build_pack(
    by_repo: dict[str, list[dict[str, Any]]],
    *,
    per_repo: int,
    max_per_file: int,
) -> dict[str, Any]:
    questions: list[dict[str, Any]] = []
    repositories: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []

    for target in _TARGETS:
        full = target["repo_full"]
        rows = by_repo.get(full, [])
        available = len(rows)
        selected = select_rows(rows, per_repo=per_repo, max_per_file=max_per_file)
        for row in selected:
            q = row_to_question(row, target)
            if q is not None:
                questions.append(q)

        taken = sum(1 for q in questions if q["repo"] == target["id"])
        repositories.append(
            {
                "id": target["id"],
                "name": target["name"],
                "clone_url": f"https://github.com/{full}.git",
                "repo_url": f"https://github.com/{full}",
                "language": "python",
                "overlap": target["overlap"],
                "proxies_for": target["proxies_for"],
                "rationale": target["rationale"],
                "repobench_available": available,
                "repobench_sampled": taken,
            }
        )
        coverage.append(
            {
                "faq_proxy": target["proxies_for"],
                "repo_full": full,
                "overlap": target["overlap"],
                "available": available,
                "sampled": taken,
            }
        )

    return {
        "meta": {
            "source": "tianyang/repobench-r (Python CFF+CFR)",
            "task": "RepoBench-R → retrieval + next-line (question text)",
            "gold_field": "golden_snippet_index / gold snippet text",
            "span_coordinate_system": "none",
            "description": (
                "Questions are incomplete-file + next-line prompts. Retrieval gold is the "
                "indexed candidate snippet (no file path / line spans). "
                "FAQ cores django/flask/fastapi/pydantic/sqlalchemy/celery are absent; "
                "pack uses exact click + stack-adjacent + django/flask ecosystem proxies."
            ),
            "missing_faq_cores": _MISSING_FAQ,
            "coverage": coverage,
            "per_repo_cap": per_repo,
        },
        "repositories": repositories,
        "questions": questions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cff",
        type=Path,
        default=Path("/tmp/repobench/python_cff.gz"),
        help="RepoBench-R python_cff.gz (cross-file-first)",
    )
    parser.add_argument(
        "--cfr",
        type=Path,
        default=Path("/tmp/repobench/python_cfr.gz"),
        help="RepoBench-R python_cfr.gz (cross-file-random); optional",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("QA/fixtures/questions_repobench_python.yaml"),
    )
    parser.add_argument(
        "--per-repo",
        type=int,
        default=20,
        help="Max questions per target repo (10–30 recommended)",
    )
    parser.add_argument(
        "--max-per-file",
        type=int,
        default=4,
        help="Cap samples sharing the same file_path",
    )
    args = parser.parse_args()

    if not args.cff.exists():
        raise SystemExit(f"Missing CFF archive: {args.cff}")

    by_repo = load_repobench(args.cff, args.cfr)
    pack = build_pack(by_repo, per_repo=args.per_repo, max_per_file=args.max_per_file)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            pack,
            handle,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        )

    n = len(pack["questions"])
    by: dict[str, int] = defaultdict(int)
    for q in pack["questions"]:
        by[q["repo"]] += 1
    print(f"Wrote {n} questions → {args.output}")
    for repo, count in sorted(by.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {repo}: {count}")


if __name__ == "__main__":
    main()
