#!/usr/bin/env python3
"""Reset graph/vector state for benchmark repositories."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from QA.qa_benchmark import (
    default_repo_checkout_path,
    load_repository_meta,
    reset_index_state,
)
from sidecar.workspace import DEFAULT_WORKSPACE_ID, WorkspaceResolver

_DEFAULT_REAL_REPO_PACK = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "real_repo_question_pack.yaml"
)


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
        fixture_path = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_project"
        repo_docs_path = Path(__file__).parent.parent / "docs"
        return (
            workspace_id or DEFAULT_WORKSPACE_ID,
            str(fixture_path.resolve()),
            str(repo_docs_path.resolve()) if repo_docs_path.exists() else None,
        )

    if not repo and not project_path:
        raise ValueError("Pass --repo, --project-path, or --fixture.")

    if repo:
        repo_meta = load_repository_meta(questions_path, repo)
        if repo_meta is None:
            raise ValueError(f"Repository '{repo}' is not defined in {questions_path}")

    resolved_project_path = (
        Path(project_path).resolve()
        if project_path
        else default_repo_checkout_path(repo, repos_root=repos_root).resolve()
    )
    if not resolved_project_path.exists():
        raise FileNotFoundError(
            f"Checkout not found at {resolved_project_path}. "
            "Run the benchmark with --repo first or pass --project-path explicitly."
        )

    resolved_docs_path: str | None = None
    if docs_path:
        resolved_docs_path = str(Path(docs_path).resolve())
    else:
        candidate = resolved_project_path / "docs"
        if candidate.exists():
            resolved_docs_path = str(candidate.resolve())

    resolved_workspace_id = WorkspaceResolver().from_project_path(
        str(resolved_project_path),
        value=workspace_id,
    ).id
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

    import yaml

    with open(pack) as f:
        payload = yaml.safe_load(f) or {}

    for repo_meta in payload.get("repositories", []):
        repo_id = repo_meta.get("id")
        if not repo_id:
            continue
        checkout = default_repo_checkout_path(repo_id, repos_root=repos_root).resolve()
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
        description="Reset Neo4j + LanceDB rows for benchmark runs",
    )
    parser.add_argument(
        "--questions",
        help="Path to real-repo question pack",
        default=str(_DEFAULT_REAL_REPO_PACK),
    )
    parser.add_argument(
        "--repo",
        help="Repository id from the real-repo question pack",
        default=None,
    )
    parser.add_argument(
        "--project-path",
        help="Explicit path to the repository checkout",
        default=None,
    )
    parser.add_argument(
        "--docs-path",
        help="Optional docs path override",
        default=None,
    )
    parser.add_argument(
        "--workspace-id",
        help="Optional explicit workspace id override",
        default=None,
    )
    parser.add_argument(
        "--repos-root",
        help="Directory used for auto-cloned benchmark repositories",
        default=None,
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Reset the sample_project fixture workspace instead of a real repo checkout",
    )
    args = parser.parse_args()

    has_explicit_target = bool(args.fixture or args.repo or args.project_path)
    targets = (
        [
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
        if has_explicit_target
        else resolve_default_targets(
            questions_path=args.questions,
            repos_root=args.repos_root,
        )
    )

    if not has_explicit_target:
        print("[reset] no target passed, cleaning fixture and any existing QA/repos checkouts")

    for resolved_workspace_id, resolved_project_path, resolved_docs_path in targets:
        print(f"[reset] workspace: {resolved_workspace_id}")
        print(f"[reset] project:   {resolved_project_path}")
        if resolved_docs_path:
            print(f"[reset] docs:      {resolved_docs_path}")
        else:
            print("[reset] docs:      <none>")

        reset_index_state(
            workspace_id=resolved_workspace_id,
            project_path=resolved_project_path,
            docs_path=resolved_docs_path,
        )

    print("[reset] complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
