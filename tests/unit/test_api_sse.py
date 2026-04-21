"""Unit tests for server-sent event formatting."""

import json

from sidecar.api.sse import format_sse


class TestSSEFormatting:
    def test_format_sse_emits_named_json_event(self):
        frame = format_sse("chunk", {"type": "chunk", "content": "hello"})

        assert frame.startswith("event: chunk\n")
        assert frame.endswith("\n\n")

        data_line = frame.splitlines()[1]
        assert data_line.startswith("data: ")
        assert json.loads(data_line.removeprefix("data: ")) == {
            "type": "chunk",
            "content": "hello",
        }

    def test_format_sse_escapes_multiline_and_unicode_content(self):
        frame = format_sse(
            "chunk",
            {
                "type": "chunk",
                "content": "line one\nline two",
                "symbol": "café",
            },
        )

        lines = [line for line in frame.splitlines() if line]
        assert lines[0] == "event: chunk"
        assert len(lines) == 2

        payload = json.loads(lines[1].removeprefix("data: "))
        assert payload["content"] == "line one\nline two"
        assert payload["symbol"] == "café"

    def test_format_sse_context_payload_is_json_parseable(self):
        frame = format_sse(
            "context",
            {
                "type": "context",
                "context": {
                    "mode": "surgical_full",
                    "metadata": {"tiers_used": ["code", "docs"]},
                },
            },
        )

        payload = json.loads(frame.splitlines()[1].removeprefix("data: "))
        assert payload["type"] == "context"
        assert payload["context"]["metadata"]["tiers_used"] == ["code", "docs"]
