#!/usr/bin/env python3
"""Summarize Cursor agent transcripts as an observable retrieval baseline.

Cursor's internal index and hidden retrieval tokens are not exposed. This script
therefore reports only what is visible in exported JSONL transcripts: user
queries, assistant text, tool calls, mentioned/inspected files, and approximate
visible token burn. It can match transcripts to a QA question pack by explicit
question id or by question text.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import tiktoken
except Exception:  # pragma: no cover - dependency is present in normal runs.
    tiktoken = None

try:
    from QA.qa_benchmark import _expected_file_matches, load_question_pack
except ImportError:  # pragma: no cover - direct script invocation fallback.
    from qa_benchmark import _expected_file_matches, load_question_pack


_ENCODER = None
_QUESTION_ID_RE = re.compile(
    r"\b(?:question[_ -]?id|qid|id)\s*[:=]\s*([a-zA-Z0-9_.-]+)\b|\b([a-z]+_q\d{2})\b",
    re.IGNORECASE,
)
_ABS_PATH_RE = re.compile(r"/(?:home|tmp|workspace|Users|var)/[^\s)'\"`>,]+")
_REL_PATH_RE = re.compile(
    r"\b(?:QA/repos/)?[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+"
    r"\.(?:py|pyi|ts|tsx|js|jsx|md|rst|yaml|yml|json|toml|ini|cfg|txt)\b"
)


@dataclass
class TranscriptSummary:
    transcript_path: str
    project_slug: str
    session_id: str
    question_id: str = ""
    matched_by: str = ""
    user_queries: list[str] | None = None
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_names: dict[str, int] | None = None
    mentioned_files: list[str] | None = None
    visible_tokens: int = 0
    user_tokens: int = 0
    assistant_tokens: int = 0
    tool_input_tokens: int = 0
    expected_files: list[str] | None = None
    expected_file_recall: float = 0.0
    missing_expected_files: list[str] | None = None


def estimate_tokens(text: str) -> int:
    global _ENCODER
    if tiktoken is None:
        return max(1, len(text or "") // 4)
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return len(_ENCODER.encode(text or ""))


def discover_transcripts(root: Path, project_substring: str = "") -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for transcript_dir in root.glob("*/agent-transcripts"):
        slug = transcript_dir.parent.name
        if project_substring and project_substring not in slug:
            continue
        paths.extend(transcript_dir.rglob("*.jsonl"))
    return sorted(paths)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return rows


def _content_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    message = row.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return [part for part in content if isinstance(part, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _part_text(part: dict[str, Any]) -> str:
    if part.get("type") == "text":
        return str(part.get("text") or "")
    if part.get("type") == "tool_use":
        return json.dumps(part.get("input") or {}, ensure_ascii=False)
    return ""


def _extract_user_query(text: str) -> str:
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _extract_paths_from_value(value: Any) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in {"path", "file", "file_path", "target_file", "target_path"}:
                if isinstance(nested, str):
                    paths.add(nested)
            paths.update(_extract_paths_from_value(nested))
        return paths
    if isinstance(value, list):
        for nested in value:
            paths.update(_extract_paths_from_value(nested))
        return paths
    if not isinstance(value, str):
        return paths
    paths.update(match.rstrip(".,:;") for match in _ABS_PATH_RE.findall(value))
    paths.update(match.rstrip(".,:;") for match in _REL_PATH_RE.findall(value))
    return paths


def _project_slug(path: Path) -> str:
    parts = path.parts
    if "projects" in parts:
        index = parts.index("projects")
        if index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _session_id(path: Path) -> str:
    return path.stem


def _question_by_id(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(question.get("id")): question for question in questions if question.get("id")}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _explicit_question_id(text: str, known_ids: set[str]) -> str:
    for match in _QUESTION_ID_RE.finditer(text):
        value = (match.group(1) or match.group(2) or "").strip()
        if value in known_ids:
            return value
    return ""


def _match_question(
    text: str,
    questions: list[dict[str, Any]],
) -> tuple[str, str]:
    known_ids = {str(question.get("id")) for question in questions if question.get("id")}
    explicit = _explicit_question_id(text, known_ids)
    if explicit:
        return explicit, "explicit_id"
    haystack = _normalize(text)
    for question in questions:
        qid = str(question.get("id") or "")
        qtext = _normalize(str(question.get("question") or ""))
        if len(qtext) >= 40 and qtext in haystack:
            return qid, "question_text"
    return "", ""


def _file_recall(expected_files: list[str], mentioned_files: set[str]) -> tuple[float, list[str]]:
    if not expected_files:
        return 0.0, []
    normalized_mentioned = {
        path.strip().strip("/").replace("\\", "/") for path in mentioned_files if path
    }

    def _matches(expected: str) -> bool:
        expected_norm = expected.strip().strip("/").replace("\\", "/")
        if expected_norm in normalized_mentioned:
            return True
        if any(path.endswith("/" + expected_norm) for path in normalized_mentioned):
            return True
        return _expected_file_matches(expected_norm, mentioned_files)

    missing = [
        expected for expected in expected_files if not _matches(expected)
    ]
    return (len(expected_files) - len(missing)) / len(expected_files), missing


def summarize_transcript(
    path: Path,
    *,
    questions: list[dict[str, Any]],
) -> TranscriptSummary:
    rows = _load_jsonl(path)
    user_queries: list[str] = []
    tool_names: dict[str, int] = {}
    mentioned_files: set[str] = set()
    user_tokens = 0
    assistant_tokens = 0
    tool_input_tokens = 0
    assistant_messages = 0

    for row in rows:
        role = str(row.get("role") or "")
        parts = _content_parts(row)
        if role == "assistant":
            assistant_messages += 1
        for part in parts:
            text = _part_text(part)
            mentioned_files.update(_extract_paths_from_value(text))
            if part.get("type") == "tool_use":
                name = str(part.get("name") or "tool")
                tool_names[name] = tool_names.get(name, 0) + 1
                tool_input = part.get("input") or {}
                tool_input_tokens += estimate_tokens(json.dumps(tool_input, ensure_ascii=False))
                mentioned_files.update(_extract_paths_from_value(tool_input))
            elif role == "user":
                query = _extract_user_query(text)
                if query:
                    user_queries.append(query)
                user_tokens += estimate_tokens(text)
            elif role == "assistant":
                assistant_tokens += estimate_tokens(text)

    full_text = "\n".join([*user_queries])
    for row in rows:
        for part in _content_parts(row):
            full_text += "\n" + _part_text(part)

    question_id, matched_by = _match_question(full_text, questions)
    question_by_id = _question_by_id(questions)
    question = question_by_id.get(question_id, {})
    expected_files = list(question.get("expected_files") or [])
    recall, missing = _file_recall(expected_files, mentioned_files)

    return TranscriptSummary(
        transcript_path=str(path),
        project_slug=_project_slug(path),
        session_id=_session_id(path),
        question_id=question_id,
        matched_by=matched_by,
        user_queries=user_queries,
        assistant_messages=assistant_messages,
        tool_calls=sum(tool_names.values()),
        tool_names=tool_names,
        mentioned_files=sorted(mentioned_files),
        visible_tokens=user_tokens + assistant_tokens + tool_input_tokens,
        user_tokens=user_tokens,
        assistant_tokens=assistant_tokens,
        tool_input_tokens=tool_input_tokens,
        expected_files=expected_files,
        expected_file_recall=recall,
        missing_expected_files=missing,
    )


def _load_questions(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    return list(load_question_pack(path).get("questions") or [])


def _write_tsv(rows: list[TranscriptSummary], output: Path) -> None:
    columns = [
        "question_id",
        "matched_by",
        "project_slug",
        "session_id",
        "visible_tokens",
        "user_tokens",
        "assistant_tokens",
        "tool_input_tokens",
        "tool_calls",
        "expected_file_recall",
        "missing_expected_files",
        "transcript_path",
    ]
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            payload["missing_expected_files"] = ",".join(row.missing_expected_files or [])
            writer.writerow({column: payload.get(column, "") for column in columns})


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.cursor_root).expanduser()
    paths = [Path(path) for path in args.transcript] if args.transcript else discover_transcripts(
        root, args.project_substring
    )
    questions = _load_questions(args.questions)
    rows = [summarize_transcript(path, questions=questions) for path in paths]
    if args.only_matched:
        rows = [row for row in rows if row.question_id]
    if args.tsv:
        _write_tsv(rows, Path(args.tsv).resolve())
    return {
        "protocol": "cursor_visible_transcript_baseline",
        "limitation": (
            "Only visible transcript tokens and tool calls are counted. Cursor hidden index "
            "retrieval and internal ranking tokens are not observable from JSONL exports."
        ),
        "cursor_root": str(root),
        "project_substring": args.project_substring,
        "summary": {
            "transcripts": len(rows),
            "matched_questions": sum(1 for row in rows if row.question_id),
            "visible_tokens": sum(row.visible_tokens for row in rows),
            "tool_calls": sum(row.tool_calls for row in rows),
        },
        "results": [asdict(row) for row in rows],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Cursor agent JSONL transcripts as a black-box baseline."
    )
    parser.add_argument(
        "--cursor-root",
        default="~/.cursor/projects",
        help="Cursor projects root (default: ~/.cursor/projects)",
    )
    parser.add_argument(
        "--project-substring",
        default="",
        help="Only read Cursor project slugs containing this substring",
    )
    parser.add_argument(
        "--transcript",
        action="append",
        default=[],
        help="Specific transcript JSONL path; repeatable. Overrides discovery.",
    )
    parser.add_argument("--questions", help="QA question pack for matching and file recall")
    parser.add_argument("--only-matched", action="store_true", help="Drop unmatched transcripts")
    parser.add_argument("--tsv", help="Optional TSV output path")
    parser.add_argument("-o", "--output", help="Write JSON report here")
    args = parser.parse_args()

    payload = build_report(args)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"Cursor baseline report: {output}")
        if args.tsv:
            print(f"Cursor baseline TSV:    {Path(args.tsv).resolve()}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
