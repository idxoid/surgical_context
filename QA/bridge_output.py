"""Normalize CLI bridge stdout into plain text (judge / benchmark bridges)."""

from __future__ import annotations

import json
import re


def normalize_bridge_stdout(raw: str, provider: str) -> str:
    text = (raw or "").strip()
    if not text:
        return text

    key = provider.strip().casefold()
    if key in {"claude", "claude-code", "claude-headless"}:
        parsed = _claude_payload(text)
        if parsed is not None:
            return parsed
    if key in {"codex", "codex-cli", "openai-codex"}:
        parsed = _codex_payload(text)
        if parsed is not None:
            return parsed

    return text


def _claude_payload(text: str) -> str | None:
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None

    structured = envelope.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    result = envelope.get("result")
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, str) and result.strip():
        inner = result.strip()
        if inner.startswith("{") or inner.startswith("["):
            return inner
        return inner

    return None


def _codex_payload(text: str) -> str | None:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            return stripped

    for pattern in (
        r"```json\s*\n(.*?)\n\s*```",
        r"```\s*\n(\{.*?\})\s*\n```",
    ):
        match = re.search(pattern, stripped, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            try:
                json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return candidate
    return None
