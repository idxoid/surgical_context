"""Workspace identity helpers for branch/workspace isolation."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_WORKSPACE_ID = os.getenv("DEFAULT_WORKSPACE_ID", "local/surgical_context@main")
_WORKSPACE_RE = re.compile(
    r"^(?P<tenant>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)@(?P<ref>[^\s]+)$"
)


@dataclass(frozen=True)
class Workspace:
    id: str
    tenant: str
    repo: str
    ref: str
    ref_kind: str = "branch"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class WorkspaceResolver:
    """Resolve workspace headers and derive safe defaults for local development."""

    def from_header(self, value: str | None) -> Workspace:
        if not value:
            value = DEFAULT_WORKSPACE_ID
        match = _WORKSPACE_RE.match(value.strip())
        if not match:
            raise ValueError("Workspace must look like tenant/repo@ref")
        groups = match.groupdict()
        ref = groups["ref"]
        return Workspace(
            id=value.strip(),
            tenant=groups["tenant"],
            repo=groups["repo"],
            ref=ref,
            ref_kind=self._ref_kind(ref),
        )

    def from_project_path(self, project_path: str, value: str | None = None) -> Workspace:
        if value:
            return self.from_header(value)
        path = Path(project_path).resolve()
        tenant = os.getenv("WORKSPACE_TENANT", "local")
        repo = path.name or "repo"
        ref = current_git_ref(str(path)) or "main"
        return self.from_header(f"{tenant}/{repo}@{ref}")

    @staticmethod
    def _ref_kind(ref: str) -> str:
        if re.fullmatch(r"[0-9a-f]{7,40}", ref):
            return "commit"
        if ref.startswith("refs/tags/"):
            return "tag"
        return "branch"


def current_git_ref(project_path: str) -> str | None:
    """Return the active Git branch or short commit SHA for a project path."""
    try:
        branch = subprocess.run(
            ["git", "-C", project_path, "branch", "--show-current"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if branch:
            return branch
        sha = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return sha or None
    except OSError:
        return None
