"""Audit logging for multi-user tracking."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class AuditLog:
    """Simple audit log for tracking user actions."""

    def __init__(self, log_file: str | None = None):
        """
        Initialize audit log.

        Args:
            log_file: Path to audit log file (env: AUDIT_LOG_PATH or default)
        """
        self.log_file: str = (
            log_file or os.getenv("AUDIT_LOG_PATH") or ".surgical_context/audit.jsonl"
        )
        Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)

    def log_action(
        self,
        user_id: str,
        action: str,
        resource: str,
        details: dict[str, str | int] | None = None,
        status: str = "success",
    ):
        """
        Log a user action.

        Args:
            user_id: User ID
            action: Action type (index, query, edit, sync, etc.)
            resource: Resource affected (file, symbol, graph, etc.)
            details: Additional details as dict
            status: success | error
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "action": action,
            "resource": resource,
            "status": status,
            "details": details or {},
        }

        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
            logger.debug(f"Audit: {user_id} {action} {resource} {status}")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    def log_index(self, user_id: str, project_path: str, file_count: int, symbol_count: int):
        """Log indexing action."""
        self.log_action(
            user_id,
            "index",
            "project",
            {
                "project_path": project_path,
                "file_count": file_count,
                "symbol_count": symbol_count,
            },
        )

    def log_query(self, user_id: str, symbol: str, question: str, intent: str, mode: str):
        """Log query action."""
        self.log_action(
            user_id,
            "query",
            "symbol",
            {
                "symbol": symbol,
                "question": question,
                "intent": intent,
                "mode": mode,
            },
        )

    def log_overlay_sync(self, user_id: str, file_path: str, lines_changed: int):
        """Log overlay sync to cloud."""
        self.log_action(
            user_id,
            "sync",
            "overlay",
            {
                "file_path": file_path,
                "lines_changed": lines_changed,
            },
        )

    def log_error(self, user_id: str, action: str, error_msg: str):
        """Log error action."""
        self.log_action(user_id, action, "error", {"error": error_msg}, status="error")

    def get_recent_actions(
        self, user_id: str | None = None, limit: int = 100
    ) -> list[dict[str, str | int]]:
        """Get recent audit log entries."""
        entries = []
        try:
            if os.path.exists(self.log_file):
                with open(self.log_file) as f:
                    lines = f.readlines()
                    for line in lines[-limit:]:
                        entry = json.loads(line)
                        if user_id is None or entry.get("user_id") == user_id:
                            entries.append(entry)
        except Exception as e:
            logger.error(f"Failed to read audit log: {e}")

        return entries
