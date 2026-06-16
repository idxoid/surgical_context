#!/usr/bin/env python3
"""Reset graph/vector state for benchmark workspaces.

Re-pointed off the deleted legacy ``qa_benchmark`` harness (cascade cleanup,
2026-06-15): the wipe now goes through the indexer's own
``sidecar.indexer.fast.__main__._wipe_workspace`` (exact per-workspace delete —
every workspace-scoped label carries ``workspace_id``), and the tiny pack /
checkout helpers are inlined. No more ``QA.qa_benchmark`` import.

NB the axis benchmark keys its workspaces ``qa_repo/<repo>@axis-v4`` (a manual
base that ``WorkspaceResolver.from_project_path`` does not reproduce), so to wipe
an axis workspace pass it explicitly via ``--workspace-id`` (+ the matching
``--index-profile``).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from sidecar.index_profile import AXIS_PYTHON_V1_PROFILE
from sidecar.indexer.fast.__main__ import _wipe_workspace
from sidecar.workspace import DEFAULT_WORKSPACE_ID, WorkspaceResolver

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_REAL_REPO_PACK = _REPO_ROOT / "tests" / "fixtures" / "questions_python.yaml"


def _default_repo_checkout_path(repo: str, *, repos_root: str | None = None) -> Path:
    """Where a benchmark repo is checked out (mirror of the old helper)."""
    root = Path(repos_root) if repos_root else _REPO_ROOT / "QA" / "repos"
    return root / repo


def _repo_meta_from_pack(questions_path: str, repo: str) -> dict | None:
    """Return the ``repositories`` entry for ``repo`` from a question pack, or None."""
    pack = Path(questions_path)
    if not pack.exists():
        return None
    payload = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
    for meta in payload.get("repositories", []):
        if meta.get("id") == repo:
            return meta
    return None


def resolve_reset_target(
    *,
    fixture: bool,
    repo: str | None,
    questions_path: str,
    project_path: str | None,
    docs_path: str | None,
    workspace_id: str | None,
    repos_root: str | None,
) -> tuple[str, str, str | None]:
    """Resolve workspace/project/docs paths for a cleanup run."""
    if fixture:
        fixture_path = _REPO_ROOT / "tests" / "fixtures" / "sample_project"
        repo_docs_path = _REPO_ROOT / "docs"
        return (
            workspace_id or DEFAULT_WORKSPACE_ID,
            str(fixture_path.resolve()),
            str(repo_docs_path.resolve()) if repo_docs_path.exists() else None,
        )

    if not repo and not project_path:
        raise ValueError("Pass --repo, --project-path, or --fixture.")

    if repo and _repo_meta_from_pack(questions_path, repo) is None:
        raise ValueError(f"Repository '{repo}' is not defined in {questions_path}")

    if project_path:
        resolved_project_path = Path(project_path).resolve()
    else:
        # repo is set (guarded above); use its pack project_path or default checkout.
        meta = _repo_meta_from_pack(questions_path, repo) or {}
        resolved_project_path = Path(
            meta.get("project_path") or _default_repo_checkout_path(repo, repos_root=repos_root)
        ).resolve()
    if not resolved_project_path.exists():
        raise FileNotFoundError(
            f"Checkout not found at {resolved_project_path}. "
            "Index the repo first or pass --project-path explicitly."
        )

    resolved_docs_path: str | None = None
    if docs_path:
        resolved_docs_path = str(Path(docs_path).resolve())
    else:
        candidate = resolved_project_path / "docs"
        if candidate.exists():
            resolved_docs_path = str(candidate.resolve())

    resolved_workspace_id = (
        WorkspaceResolver().from_project_path(str(resolved_project_path), value=workspace_id).id
    )
    return resolved_workspace_id, str(resolved_project_path), resolved_docs_path


def resolve_default_targets(
    *,
    questions_path: str,
    repos_root: str | None,
) -> list[tuple[str, str, str | None]]:
    """Resolve the default cleanup set: fixture plus any existing cached repos."""
    targets = [
        resolve_reset_target(
            fixture=True,
            repo=None,
            questions_path=questions_path,
            project_path=None,
            docs_path=None,
            workspace_id=None,
            repos_root=repos_root,
        )
    ]

    pack = Path(questions_path)
    if not pack.exists():
        return targets

    payload = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
    for repo_meta in payload.get("repositories", []):
        repo_id = repo_meta.get("id")
        if not repo_id:
            continue
        meta_path = repo_meta.get("project_path")
        checkout = (
            Path(meta_path).resolve()
            if meta_path
            else _default_repo_checkout_path(repo_id, repos_root=repos_root).resolve()
        )
        if not checkout.exists():
            continue
        targets.append(
            resolve_reset_target(
                fixture=False,
                repo=repo_id,
                questions_path=questions_path,
                project_path=str(checkout),
                docs_path=None,
                workspace_id=None,
                repos_root=repos_root,
            )
        )
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset Neo4j + LanceDB rows for benchmark workspaces",
    )
    parser.add_argument(
        "--questions", default=str(_DEFAULT_REAL_REPO_PACK), help="Path to real-repo question pack"
    )
    parser.add_argument("--repo", default=None, help="Repository id from the question pack")
    parser.add_argument("--project-path", default=None, help="Explicit checkout path")
    parser.add_argument("--docs-path", default=None, help="Optional docs path override")
    parser.add_argument("--workspace-id", default=None, help="Explicit workspace id override")
    parser.add_argument("--repos-root", default=None, help="Directory for benchmark repo checkouts")
    parser.add_argument(
        "--index-profile",
        default=AXIS_PYTHON_V1_PROFILE,
        help="Index profile whose rows to wipe (default: axis_python_v1)",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Reset the sample_project fixture workspace instead of a real repo",
    )
    args = parser.parse_args()

    has_explicit_target = bool(args.fixture or args.repo or args.project_path or args.workspace_id)
    if args.workspace_id and not (args.fixture or args.repo or args.project_path):
        targets = [(args.workspace_id, "<explicit workspace-id>", None)]
    elif has_explicit_target:
        targets = [
            resolve_reset_target(
                fixture=args.fixture,
                repo=args.repo,
                questions_path=args.questions,
                project_path=args.project_path,
                docs_path=args.docs_path,
                workspace_id=args.workspace_id,
                repos_root=args.repos_root,
            )
        ]
    else:
        print("[reset] no target passed, cleaning fixture and any existing QA/repos checkouts")
        targets = resolve_default_targets(questions_path=args.questions, repos_root=args.repos_root)

    for resolved_workspace_id, resolved_project_path, _docs in targets:
        print(f"[reset] workspace: {resolved_workspace_id}  (profile={args.index_profile})")
        print(f"[reset] project:   {resolved_project_path}")
        _wipe_workspace(resolved_workspace_id, args.index_profile)

    print("[reset] complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
