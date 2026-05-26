"""CodeResolver — file I/O and overlay merging."""


class CodeResolver:
    """Resolves code from filesystem or in-memory overlay."""

    def __init__(
        self,
        overlay=None,
        workspace_id: str = "local/surgical_context@main",
        user_id: str = "anonymous",
    ):
        self.overlay = overlay
        self.workspace_id = workspace_id
        self.user_id = user_id

    def resolve(self, file_path: str, start_line: int, end_line: int) -> tuple[str, bool]:
        """Return (code, is_dirty). Checks overlay first, falls back to FS."""
        if file_path == "<unknown>":
            return "", False

        is_dirty = bool(
            self.overlay
            and self.overlay.has(
                file_path, workspace_id=self.workspace_id, user_id=self.user_id
            )
        )
        if is_dirty:
            code = self.overlay.read_lines(
                file_path,
                start_line,
                end_line,
                workspace_id=self.workspace_id,
                user_id=self.user_id,
            )
            return code, True

        try:
            with open(file_path, encoding="utf-8") as f:
                lines = f.readlines()
            code = "".join(lines[start_line - 1 : end_line])
            return code, False
        except (OSError, FileNotFoundError):
            return "", False
