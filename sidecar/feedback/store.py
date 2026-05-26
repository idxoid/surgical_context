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

_DEFAULT_JSONL_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_JSONL_MAX_LINES = 10_000


def _env_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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
        max_jsonl_bytes: int | None = None,
        max_jsonl_lines: int | None = None,
    ):
        self.snapshot_file = (
            snapshot_file
            or os.getenv("FEEDBACK_SNAPSHOT_PATH")
            or ".surgical_context/retrieval_snapshots.jsonl"
        )
        self.feedback_file = (
            feedback_file or os.getenv("FEEDBACK_LOG_PATH") or ".surgical_context/feedback.jsonl"
        )
        self.max_jsonl_bytes = max_jsonl_bytes
        if self.max_jsonl_bytes is None:
            self.max_jsonl_bytes = _env_positive_int(
                "FEEDBACK_JSONL_MAX_BYTES", _DEFAULT_JSONL_MAX_BYTES
            )
        self.max_jsonl_lines = max_jsonl_lines
        if self.max_jsonl_lines is None:
            self.max_jsonl_lines = _env_positive_int(
                "FEEDBACK_JSONL_MAX_LINES", _DEFAULT_JSONL_MAX_LINES
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

    def feedback_examples(self, limit: int = 200) -> list[dict[str, Any]]:
        """Join feedback events with their retrieval snapshots.

        Returns a list of training examples, each with:
        - outcome: "accept" | "reject"
        - intent: str
        - symbol: str
        - selected_candidates: list of candidate dicts from the snapshot
        - workspace_id: str
        """
        snapshots: dict[str, dict[str, Any]] = {}
        for row in self._read_jsonl(self.snapshot_file):
            token = row.get("feedback_token")
            if token:
                snapshots[token] = row

        examples: list[dict[str, Any]] = []
        for event in self._read_jsonl(self.feedback_file)[-limit:]:
            token = event.get("feedback_token")
            outcome = _ACCEPT_REJECT.get(event.get("kind", ""), "unknown")
            if outcome == "unknown" or not token:
                continue
            snapshot = snapshots.get(token)
            if snapshot is None:
                continue
            examples.append(
                {
                    "outcome": outcome,
                    "intent": snapshot.get("intent", ""),
                    "symbol": snapshot.get("symbol", ""),
                    "selected_candidates": snapshot.get("selected_candidates", []),
                    "workspace_id": snapshot.get("workspace_id", ""),
                    "feedback_token": token,
                }
            )
        return examples

    def _append_jsonl(self, path: str, row: dict[str, Any]) -> None:
        try:
            self._maybe_rotate_jsonl(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            logger.exception("Failed to write feedback log: %s", path)
            raise

    def _maybe_rotate_jsonl(self, path: str) -> None:
        target = Path(path)
        if not target.exists():
            return

        needs_rotate = target.stat().st_size >= self.max_jsonl_bytes
        if not needs_rotate:
            line_count = 0
            with target.open(encoding="utf-8") as handle:
                for line_count, _ in enumerate(handle, start=1):
                    if line_count >= self.max_jsonl_lines:
                        needs_rotate = True
                        break

        if not needs_rotate:
            return

        rotated = Path(f"{path}.1")
        if rotated.exists():
            rotated.unlink()
        target.rename(rotated)
        logger.info(
            "Rotated feedback JSONL %s -> %s (max_bytes=%s max_lines=%s)",
            path,
            rotated,
            self.max_jsonl_bytes,
            self.max_jsonl_lines,
        )

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
