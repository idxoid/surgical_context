"""CodeResolver — file I/O and overlay merging."""

from __future__ import annotations

from pathlib import Path

from sidecar.workspace_paths import resolve_graph_file_path


class CodeResolver:
    """Resolves code from filesystem or in-memory overlay."""

    def __init__(
        self,
        overlay=None,
        workspace_id: str = "local/surgical_context@main",
        user_id: str = "anonymous",
        *,
        workspace_root: Path | str | None = None,
    ):
        self.overlay = overlay
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None

    def resolve(self, file_path: str, start_line: int, end_line: int) -> tuple[str, bool]:
        """Return (code, is_dirty). Checks overlay first, falls back to FS."""
        safe_path = resolve_graph_file_path(file_path, workspace_root=self.workspace_root)
        if safe_path is None:
            return "", False

        is_dirty = bool(
            self.overlay
            and self.overlay.has(safe_path, workspace_id=self.workspace_id, user_id=self.user_id)
        )
        if is_dirty:
            code = self.overlay.read_lines(
                safe_path,
                start_line,
                end_line,
                workspace_id=self.workspace_id,
                user_id=self.user_id,
            )
            return code, True

        try:
            with open(safe_path, encoding="utf-8") as f:
                lines = f.readlines()
            code = "".join(lines[start_line - 1 : end_line])
            return code, False
        except (OSError, FileNotFoundError):
            return "", False
