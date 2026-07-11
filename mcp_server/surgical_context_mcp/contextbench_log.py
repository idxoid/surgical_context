"""Opt-in JSONL capture of MCP results for ContextBench evaluation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LOG_PATH_ENV = "SURGICAL_CONTEXT_CONTEXTBENCH_LOG"
INSTANCE_ID_ENV = "SURGICAL_CONTEXT_CONTEXTBENCH_INSTANCE_ID"


def record_tool_result(payload: dict[str, Any]) -> None:
    """Append one compact event when both ContextBench env vars are set.

    Logging failures never break retrieval: benchmark capture is diagnostic and
    must not change the treatment agent's behavior.
    """
    raw_path = os.getenv(LOG_PATH_ENV, "").strip()
    instance_id = os.getenv(INSTANCE_ID_ENV, "").strip()
    tool = payload.get("tool")
    if not raw_path or not instance_id or not isinstance(tool, str) or tool == "batch":
        return
    compact = dict(payload)
    compact.pop("markdown", None)
    event = {"instance_id": instance_id, "tool": tool, "result": compact}
    try:
        path = Path(raw_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError):
        return
