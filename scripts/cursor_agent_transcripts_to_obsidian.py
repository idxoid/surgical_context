#!/usr/bin/env python3
"""
Export Cursor agent transcripts (JSONL) into the surgical_context Obsidian vault.

Reads ~/.cursor/projects/*/agent-transcripts/*/*.jsonl (Composer/agent runs),
mirroring the flow used for Codex (~/.codex/sessions) and Claude Code exports.

Output: ~/vault/surgical_context/chats/cursor/*.md with YAML frontmatter suitable
for Obsidian and for a later `graphify` pass on that folder.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

KEYWORD_TAG_MAP = {
    "python": "python",
    "fastapi": "fastapi",
    "typescript": "typescript",
    "react": "react",
    "neo4j": "neo4j",
    "lancedb": "lancedb",
    "deploy": "deploy",
    "bug": "debugging",
    "refactor": "refactoring",
    "api": "api",
    "database": "database",
    "test": "testing",
    "performance": "performance",
    "security": "security",
    "graphify": "graphify",
}

VAULT_NOTES: dict[str, Path] = {}


def load_vault_notes(vault_dir: Path) -> None:
    global VAULT_NOTES
    VAULT_NOTES = {}
    vault_path = vault_dir / "surgical_context"
    if not vault_path.is_dir():
        return
    for md_file in vault_path.rglob("*.md"):
        VAULT_NOTES[md_file.stem.lower()] = md_file.relative_to(vault_path)


def extract_tags_from_content(content: str) -> list[str]:
    tags: set[str] = set()
    lower = content.lower()
    for keyword, tag in KEYWORD_TAG_MAP.items():
        if keyword in lower:
            tags.add(tag)
    return sorted(tags)


def insert_wikilinks(content: str) -> str:
    for note_name in VAULT_NOTES.keys():
        pattern = rf"\b{re.escape(note_name)}\b"
        if re.search(pattern, content, re.IGNORECASE):
            replacement = f"[[{note_name}]]"
            content = re.sub(pattern, replacement, content, count=1, flags=re.IGNORECASE)
    return content


_TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>\s*", re.DOTALL)


def _clean_user_text(text: str) -> str:
    text = _TIMESTAMP_RE.sub("", text)
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    text = re.sub(r"\s*\[REDACTED\]\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _summarize_tool(block: dict[str, Any]) -> str | None:
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return f"`{name}`"
    if name in ("Read", "ReadLints", "Delete", "Glob", "Grep"):
        p = inp.get("path") or inp.get("target_notebook") or inp.get("glob_pattern")
        if p:
            return f"`{name}` — `{p}`"
    if name == "StrReplace":
        path = inp.get("path", "")
        return f"`{name}` — `{path}`" if path else f"`{name}`"
    if name == "Shell":
        cmd = (inp.get("command") or "").strip().replace("\n", " ")
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        return f"`{name}` — `{cmd}`" if cmd else f"`{name}`"
    if name == "SemanticSearch":
        q = (inp.get("query") or "").strip()
        if len(q) > 80:
            q = q[:77] + "..."
        return f"`{name}` — {q!r}" if q else f"`{name}`"
    keys = ", ".join(sorted(inp.keys())[:5])
    return f"`{name}` ({keys})" if keys else f"`{name}`"


def _blocks_to_turn(content: list[dict[str, Any]] | None) -> tuple[str, list[str]]:
    if not content:
        return "", []
    text_parts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            raw = block.get("text") or ""
            text_parts.append(raw)
        elif kind == "tool_use":
            s = _summarize_tool(block)
            if s:
                tools.append(s)
    body_text = "\n".join(text_parts)
    body_text = re.sub(r"\s*\[REDACTED\]\s*", "\n", body_text)
    body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
    return body_text, tools


def _parse_first_timestamp(text: str) -> str | None:
    m = re.search(r"<timestamp>\s*([^<]+)\s*</timestamp>", text)
    raw = m.group(1).strip() if m else text.strip()

    m_date = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),\s*(\d{4})\b",
        raw,
    )
    if m_date:
        try:
            dt = datetime.strptime(
                f"{m_date.group(1)} {int(m_date.group(2))}, {m_date.group(3)}",
                "%B %d, %Y",
            )
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    if not m:
        return None

    cleaned_tz = re.sub(r"\(UTC[-+]\d+(?::\d+)?\)\s*$", "", raw).strip()
    for fmt in ("%A, %B %d, %Y, %I:%M %p", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(cleaned_tz, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_cursor_jsonl(jsonl_path: Path) -> dict[str, Any] | None:
    """Return session dict or None if nothing worth writing."""
    grouped: list[dict[str, Any]] = []
    created_date: str | None = None
    all_plain: list[str] = []

    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = data.get("role")
            msg = data.get("message") or {}
            content = msg.get("content")
            if role not in ("user", "assistant"):
                continue
            body_text, tools = _blocks_to_turn(content if isinstance(content, list) else None)

            if role == "user":
                body_text = _clean_user_text(body_text)

            extra_lines: list[str] = []
            if tools:
                extra_lines.append("")
                extra_lines.append("*Tools:* " + " · ".join(tools))

            combined = (body_text + "\n".join(extra_lines)).strip()

            if created_date is None and role == "user":
                raw_probe = ""
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict) and first.get("type") == "text":
                        raw_probe = first.get("text") or ""
                ts = _parse_first_timestamp(raw_probe) or _parse_first_timestamp(body_text)
                if ts:
                    created_date = ts

            if len(combined) < 12 and not tools:
                continue

            if grouped and grouped[-1]["role"] == role:
                grouped[-1]["text"] += "\n\n---\n\n" + combined
            else:
                grouped.append({"role": role, "text": combined})

            if body_text:
                all_plain.append(body_text)

    if not grouped:
        return None

    combined_content = "\n".join(all_plain)
    tags = extract_tags_from_content(combined_content)
    tags.extend(["chat", "chat-cursor"])

    session_stem = jsonl_path.parent.name
    short = session_stem[:8] if len(session_stem) >= 8 else session_stem

    title_hint = ""
    for g in grouped:
        if g["role"] == "user":
            first_line = g["text"].split("\n", 1)[0].strip("# ").strip()
            if len(first_line) > 5:
                title_hint = first_line[:80]
                break

    title = title_hint or f"Cursor session {short}"

    created = created_date or datetime.fromtimestamp(jsonl_path.stat().st_mtime).strftime(
        "%Y-%m-%d"
    )

    return {
        "title": title,
        "messages": grouped,
        "created": created,
        "tags": tags,
        "session_id": session_stem,
        "short_id": short,
    }


def format_conversation(session: dict[str, Any]) -> str:
    parts: list[str] = []
    for msg in session["messages"]:
        role = msg["role"]
        text = insert_wikilinks(msg["text"])
        if role == "user":
            header = "## 👤 User"
        elif role == "assistant":
            header = "## 🤖 Cursor Agent"
        else:
            header = f"## {role}"
        if parts:
            parts.append("\n---\n")
        parts.append(f"{header}\n\n{text}\n")
    return "".join(parts)


def _yaml_escape(s: str) -> str:
    if re.search(r'[:#\[\]{}"\'\n]', s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dumps_markdown(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, val in meta.items():
        if key == "tags" and isinstance(val, list):
            lines.append("tags:")
            for t in val:
                lines.append(f"- {t}")
        elif isinstance(val, (str, int)):
            lines.append(f"{key}: {_yaml_escape(str(val))}")
        else:
            lines.append(f"{key}: {_yaml_escape(json.dumps(val))}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def discover_jsonl(cursor_home: Path, project_filter: str | None) -> list[Path]:
    projects = cursor_home / "projects"
    if not projects.is_dir():
        return []
    found: list[Path] = []
    for agent_root in projects.glob("*/agent-transcripts"):
        if not agent_root.is_dir():
            continue
        slug = agent_root.parent.name
        if project_filter and project_filter not in slug:
            continue
        found.extend(agent_root.rglob("*.jsonl"))
    return sorted(found)


def process_file(jsonl_path: Path, vault_base: Path, dry_run: bool) -> Path | None:
    session = parse_cursor_jsonl(jsonl_path)
    if not session:
        return None

    body_md = format_conversation(session)
    header_note = (
        f"# Cursor Agent Transcript\n\n"
        f"Session ID: `{session['session_id']}`\n"
        f"Source file: `{jsonl_path}`\n\n---\n\n"
    )
    full_body = header_note + body_md

    meta = {
        "title": session["title"][:200],
        "tags": session["tags"],
        "created": session["created"],
        "updated": datetime.now().strftime("%Y-%m-%d"),
        "status": "active",
        "type": "chat",
        "source": "cursor",
        "messages": len(session["messages"]),
        "cursor_session_id": session["session_id"],
        "cursor_project_slug": jsonl_path.parent.parent.parent.name,
    }

    out_dir = vault_base / "surgical_context" / "chats" / "cursor"
    safe_day = session["created"]
    fname = f"cursor-conversation-{safe_day}-{session['short_id']}.md"
    out_path = out_dir / fname

    if dry_run:
        print(f"Would write: {out_path} ({meta['messages']} turns)")
        return out_path

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dumps_markdown(meta, full_body), encoding="utf-8")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Cursor agent transcripts (JSONL) into Obsidian vault"
    )
    parser.add_argument(
        "--cursor-home",
        default=os.path.expanduser("~/.cursor"),
        help="Cursor config dir (default: ~/.cursor)",
    )
    parser.add_argument(
        "--vault-dir",
        default=os.path.expanduser("~/vault"),
        help="Obsidian vault root containing surgical_context/",
    )
    parser.add_argument(
        "--project-substring",
        default=None,
        help="Only transcripts under projects/* matching this substring (e.g. surgical-context)",
    )
    parser.add_argument(
        "--jsonl",
        action="append",
        default=[],
        help="Explicit JSONL file(s) to process (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print targets only")
    args = parser.parse_args()

    cursor_home = Path(args.cursor_home).expanduser()
    vault_dir = Path(args.vault_dir).expanduser()

    load_vault_notes(vault_dir)

    paths: list[Path] = [Path(p).expanduser() for p in args.jsonl]
    paths.extend(discover_jsonl(cursor_home, args.project_substring))

    processed = 0
    for jp in sorted(set(paths)):
        if not jp.is_file():
            print(f"⚠️  Skip (missing): {jp}")
            continue
        out = process_file(jp, vault_dir, args.dry_run)
        if out:
            print(f"✅ {jp.name} → {out.relative_to(vault_dir)}")
            processed += 1

    print(f"\nDone. Exported {processed} session(s).")


if __name__ == "__main__":
    main()
