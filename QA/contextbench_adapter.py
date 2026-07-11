"""Convert Surgical Context MCP tool logs to ContextBench unified trajectories.

The adapter deliberately depends only on the stable MCP structured-output
contract.  It can therefore run after any host agent (MiniSWE, Codex, Claude
Code, etc.) as long as the host records each tool name and structured result.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

Span = dict[str, int | str]


_CONTEXT_TOOLS = frozenset({"ask_code", "investigate"})
_FILE_DISCOVERY_TOOLS = frozenset(
    {"callers", "callees", "impact", "search_code", "find_definition", "file_outline"}
)


def _structured_result(value: Any) -> dict[str, Any]:
    """Unwrap common MCP client result envelopes."""
    if not isinstance(value, dict):
        return {}
    for key in ("structuredContent", "structured_content", "result"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return _structured_result(nested)
    return value


def _relative_path(raw: Any, repo_root: Path | None) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip().replace("\\", "/")
    path = Path(value)
    if repo_root is not None:
        try:
            path = path.resolve().relative_to(repo_root.resolve())
            value = path.as_posix()
        except (OSError, ValueError):
            root = repo_root.resolve().as_posix().rstrip("/")
            if value.startswith(root + "/"):
                value = value[len(root) + 1 :]
    value = str(PurePosixPath(value))
    if value.startswith("/") or value == ".." or value.startswith("../"):
        return None
    return value.lstrip("./") or None


def _add_file(files: set[str], raw: Any, repo_root: Path | None) -> str | None:
    path = _relative_path(raw, repo_root)
    if path:
        files.add(path)
    return path


def _add_span(
    spans: dict[str, list[Span]],
    file_path: str | None,
    start: Any,
    end: Any,
) -> None:
    if not file_path or type(start) is not int or type(end) is not int:
        return
    if start < 1 or end < start:
        return
    spans[file_path].append({"type": "line", "start": start, "end": end})


def _extract_one(tool: str, raw_result: Any, repo_root: Path | None) -> dict[str, Any]:
    result = _structured_result(raw_result)
    files: set[str] = set()
    spans: dict[str, list[Span]] = defaultdict(list)
    symbols: dict[str, list[str]] = defaultdict(list)

    if not result or result.get("ok") is False:
        return {"files": [], "spans": {}, "symbols": {}}

    if tool in _CONTEXT_TOOLS:
        for raw in result.get("files", []):
            _add_file(files, raw, repo_root)
        # names-only retrieval exposes locations but not source spans.
        include_spans = result.get("render") != "names" and result.get("depth") != "lean"
        for item in result.get("symbols", []):
            if not isinstance(item, dict):
                continue
            path = _add_file(files, item.get("file_path"), repo_root)
            name = item.get("name")
            if path and isinstance(name, str) and name:
                symbols[path].append(name)
            if include_spans and item.get("has_code", True):
                _add_span(spans, path, item.get("start_line"), item.get("end_line"))
    elif tool == "read_symbol":
        path = _add_file(files, result.get("file_path"), repo_root)
        name = result.get("name")
        if path and isinstance(name, str) and name:
            symbols[path].append(name)
        if result.get("code"):
            _add_span(spans, path, result.get("start_line"), result.get("end_line"))
    elif tool in {"callers", "callees"}:
        for item in result.get("neighbours", []):
            if isinstance(item, dict):
                _add_file(files, item.get("file_path"), repo_root)
    elif tool == "impact":
        _add_file(files, result.get("file_path"), repo_root)
        for raw in result.get("affected_files", []):
            _add_file(files, raw, repo_root)
        for item in result.get("affected_symbols", []):
            if isinstance(item, dict):
                _add_file(files, item.get("file_path"), repo_root)
    elif tool == "search_code":
        for item in result.get("hits", []):
            if isinstance(item, dict):
                _add_file(files, item.get("file_path"), repo_root)
    elif tool == "find_definition":
        for item in result.get("definitions", []):
            if isinstance(item, dict):
                _add_file(files, item.get("file_path"), repo_root)
    elif tool == "file_outline":
        _add_file(files, result.get("file_path"), repo_root)

    return {
        "files": sorted(files),
        "spans": {path: values for path, values in sorted(spans.items())},
        "symbols": {path: sorted(set(values)) for path, values in sorted(symbols.items())},
    }


def extract_event_steps(event: dict[str, Any], repo_root: Path | None = None) -> list[dict[str, Any]]:
    """Return zero or more ContextBench steps from one recorded MCP event."""
    tool = event.get("tool") or event.get("name")
    result = event.get("result", event.get("output", event.get("response", {})))
    structured = _structured_result(result)
    if tool == "batch":
        steps = []
        for row in structured.get("results", []):
            if isinstance(row, dict):
                steps.extend(
                    extract_event_steps(
                        {"tool": row.get("tool"), "result": row.get("result")}, repo_root
                    )
                )
        return steps
    if not isinstance(tool, str) or tool not in _CONTEXT_TOOLS | _FILE_DISCOVERY_TOOLS | {
        "read_symbol"
    }:
        return []
    step = _extract_one(tool, structured, repo_root)
    return [step] if step["files"] or step["spans"] else []


def _merge_spans(steps: Iterable[dict[str, Any]]) -> dict[str, list[Span]]:
    by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for step in steps:
        for path, values in step.get("spans", {}).items():
            for span in values:
                by_file[path].append((span["start"], span["end"]))
    merged: dict[str, list[Span]] = {}
    for path, intervals in sorted(by_file.items()):
        output: list[list[int]] = []
        for start, end in sorted(intervals):
            if output and start <= output[-1][1] + 1:
                output[-1][1] = max(output[-1][1], end)
            else:
                output.append([start, end])
        merged[path] = [
            {"type": "line", "start": start, "end": end} for start, end in output
        ]
    return merged


def convert_instance(record: dict[str, Any], repo_root: Path | None = None) -> dict[str, Any]:
    instance_id = record.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("trajectory record requires a non-empty instance_id")
    events = record.get("events", record.get("trajectory", []))
    if not isinstance(events, list):
        raise ValueError(f"{instance_id}: events must be a list")
    steps = [step for event in events if isinstance(event, dict) for step in extract_event_steps(event, repo_root)]
    final_files = sorted({path for step in steps for path in step["files"]})
    final_symbols: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for path, names in step.get("symbols", {}).items():
            final_symbols[path].extend(names)
    return {
        "instance_id": instance_id,
        "traj_data": {
            "pred_steps": steps,
            "pred_files": final_files,
            "pred_spans": _merge_spans(steps),
            "pred_symbols": {
                path: sorted(set(names)) for path, names in sorted(final_symbols.items())
            },
        },
        "model_patch": str(record.get("model_patch", record.get("patch", "")) or ""),
    }


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = value.get("records", [value])
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("input must be a JSON record/list or JSONL records")
    # Event-per-line logs are grouped by instance_id when they do not already
    # carry an events array.
    grouped: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for row in value:
        if "events" in row or "trajectory" in row:
            records.append(row)
            continue
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("every JSONL event requires instance_id")
        target = grouped.setdefault(instance_id, {"instance_id": instance_id, "events": []})
        if row.get("model_patch") is not None:
            target["model_patch"] = row["model_patch"]
        if row.get("tool") or row.get("name"):
            target["events"].append(row)
    records.extend(grouped.values())
    return records


def convert_file(input_path: Path, output_path: Path, repo_root: Path | None = None) -> int:
    converted = [convert_instance(record, repo_root) for record in load_records(input_path)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in converted),
        encoding="utf-8",
    )
    return len(converted)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Surgical Context JSON/JSONL log")
    parser.add_argument("--output", type=Path, required=True, help="ContextBench prediction JSONL")
    parser.add_argument("--repo-root", type=Path, help="Strip this checkout root from absolute paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    count = convert_file(args.input, args.output, args.repo_root)
    print(f"wrote {count} ContextBench trajectory record(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
