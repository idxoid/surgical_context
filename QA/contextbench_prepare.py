"""Plan or execute exact-commit checkout/index preparation for ContextBench."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")


def load_gold(path: Path) -> dict[str, dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required; use the project .venv") from exc
    table = pq.read_table(
        path,
        columns=[
            "instance_id",
            "original_inst_id",
            "repo",
            "repo_url",
            "language",
            "base_commit",
            "problem_statement",
        ],
    )
    output: dict[str, dict[str, Any]] = {}
    for row in table.to_pylist():
        output[str(row["instance_id"])] = row
        output[str(row["original_inst_id"])] = row
    return output


def load_subset(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def build_plan(
    subset: list[dict[str, str]], gold: dict[str, dict[str, Any]], work_root: Path
) -> list[dict[str, Any]]:
    plans = []
    for selected in subset:
        key = selected.get("instance_id") or selected.get("original_inst_id") or ""
        row = gold.get(key) or gold.get(selected.get("original_inst_id", ""))
        if row is None:
            raise ValueError(f"gold row not found for {key}")
        repo = str(row.get("repo") or "")
        repo_name = repo.rsplit("/", 1)[-1]
        url = str(row.get("repo_url") or "")
        commit = str(row.get("base_commit") or "")
        if not repo_name or not _GITHUB_URL.fullmatch(url) or not _SHA.fullmatch(commit):
            raise ValueError(f"unsafe or incomplete gold checkout metadata for {key}")
        instance_dir = (work_root / key).resolve()
        plans.append(
            {
                "instance_id": str(row["instance_id"]),
                "original_inst_id": str(row["original_inst_id"]),
                "repo": repo,
                "repo_url": url,
                "base_commit": commit,
                "language": str(row.get("language") or ""),
                "problem_statement": str(row.get("problem_statement") or ""),
                "checkout": str(instance_dir / repo_name),
                "workspace": f"contextbench/{repo_name}@{commit[:12]}",
                "event_log": str(instance_dir / "treatment.events.jsonl"),
            }
        )
    return plans


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def execute_plan(plan: list[dict[str, Any]], python: Path) -> None:
    for task in plan:
        checkout = Path(task["checkout"])
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if not (checkout / ".git").exists():
            _run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    "--filter=blob:none",
                    task["repo_url"],
                    str(checkout),
                ]
            )
        _run(["git", "fetch", "--depth", "1", "origin", task["base_commit"]], cwd=checkout)
        _run(["git", "checkout", "--detach", task["base_commit"]], cwd=checkout)
        _run(
            [
                str(python),
                "-m",
                "context_engine.indexer.fast",
                str(checkout),
                "--workspace",
                task["workspace"],
                "--index-profile",
                "axis_python_v1",
                "--fresh",
            ],
            cwd=ROOT,
        )


def write_manifest(plan: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema_version": 1, "tasks": plan}, indent=2) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=Path("/tmp/contextbench/worktrees"))
    parser.add_argument("--manifest", type=Path, default=Path("/tmp/contextbench/prepare.json"))
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan = build_plan(load_subset(args.subset), load_gold(args.gold), args.work_root.resolve())
    write_manifest(plan, args.manifest)
    print(f"planned {len(plan)} task(s); manifest: {args.manifest}")
    if args.execute:
        # Keep a virtualenv launcher path intact. Resolving its ``python``
        # symlink selects the system interpreter and loses venv site-packages.
        execute_plan(plan, args.python.expanduser().absolute())
        print(f"prepared and indexed {len(plan)} task(s)")
    else:
        print("plan only; pass --execute to clone, checkout, and index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
