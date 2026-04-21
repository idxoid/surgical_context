"""Privacy-conscious feedback snapshots and events.

The first learning-loop slice deliberately stores metadata, hashes, and ranked
candidate identities only. Raw prompts, code bodies, answers, and free-text
comments stay out of the append-only logs until a redaction pipeline exists.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FEEDBACK_KINDS = {
    "implicit_accept",
    "implicit_reject",
    "explicit_accept",
    "explicit_reject",
}

_ACCEPT_REJECT = {
    "implicit_accept": "accept",
    "explicit_accept": "accept",
    "implicit_reject": "reject",
    "explicit_reject": "reject",
}


@dataclass
class RetrievalSnapshot:
    feedback_token: str
    workspace_id: str
    user_id: str
    trace_id: str
    symbol: str
    intent: str
    mode: str
    question_hash: str
    question_tokens: int
    resolver_version: str
    selected_candidates: list[dict[str, Any]]
    documentation: list[dict[str, Any]]
    context_metadata: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeedbackEvent:
    feedback_token: str
    kind: str
    workspace_id: str
    user_id: str
    trace_id: str
    details: dict[str, Any] = field(default_factory=dict)
    client_timestamp: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def outcome(self) -> str:
        return _ACCEPT_REJECT.get(self.kind, "unknown")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"outcome": self.outcome}


class FeedbackStore:
    """Append-only JSONL store for retrieval snapshots and feedback events."""

    def __init__(
        self,
        snapshot_file: str | None = None,
        feedback_file: str | None = None,
    ):
        self.snapshot_file = (
            snapshot_file
            or os.getenv("FEEDBACK_SNAPSHOT_PATH")
            or ".surgical_context/retrieval_snapshots.jsonl"
        )
        self.feedback_file = (
            feedback_file or os.getenv("FEEDBACK_LOG_PATH") or ".surgical_context/feedback.jsonl"
        )
        Path(self.snapshot_file).parent.mkdir(parents=True, exist_ok=True)
        Path(self.feedback_file).parent.mkdir(parents=True, exist_ok=True)

    def issue_token(self) -> str:
        return f"fbk_{secrets.token_urlsafe(18)}"

    def record_snapshot(self, snapshot: RetrievalSnapshot) -> None:
        self._append_jsonl(self.snapshot_file, snapshot.to_dict())

    def get_snapshot(self, feedback_token: str) -> RetrievalSnapshot | None:
        for row in reversed(self._read_jsonl(self.snapshot_file)):
            if row.get("feedback_token") == feedback_token:
                return RetrievalSnapshot(**row)
        return None

    def record_feedback(self, event: FeedbackEvent) -> None:
        if event.kind not in FEEDBACK_KINDS:
            raise ValueError(f"Unsupported feedback kind: {event.kind}")
        clean_event = FeedbackEvent(
            feedback_token=event.feedback_token,
            kind=event.kind,
            workspace_id=event.workspace_id,
            user_id=event.user_id,
            trace_id=event.trace_id,
            details=sanitize_feedback_details(event.details),
            client_timestamp=event.client_timestamp,
            timestamp=event.timestamp,
        )
        self._append_jsonl(self.feedback_file, clean_event.to_dict())

    def recent_feedback(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._read_jsonl(self.feedback_file)
        return rows[-limit:]

    def _append_jsonl(self, path: str, row: dict[str, Any]) -> None:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            logger.exception("Failed to write feedback log: %s", path)
            raise

    def _read_jsonl(self, path: str) -> list[dict[str, Any]]:
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        return rows


def sanitize_feedback_details(details: dict[str, Any]) -> dict[str, Any]:
    """Keep structural labels; redact free text and unknown fields."""
    sanitized: dict[str, Any] = {}

    for key in ("missing_symbols", "wrong_symbols"):
        values = details.get(key)
        if isinstance(values, list):
            sanitized[key] = [str(value)[:200] for value in values[:25]]

    correct_intent = details.get("correct_intent")
    if isinstance(correct_intent, str):
        sanitized["correct_intent"] = correct_intent[:80]

    comment = details.get("comment")
    if isinstance(comment, str) and comment:
        sanitized["comment_present"] = True
        sanitized["comment_length"] = len(comment)

    redacted_keys = sorted(
        key
        for key in details
        if key
        not in {
            "missing_symbols",
            "wrong_symbols",
            "correct_intent",
            "comment",
        }
    )
    if redacted_keys:
        sanitized["redacted_keys"] = redacted_keys

    return sanitized
