"""Dependency-free shell bridge from MiniSWE containers to ``/ask/axis``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://host.docker.internal:8000"


def request_context(
    question: str,
    *,
    base_url: str,
    workspace: str,
    token_budget: int,
    timeout: float,
    bearer_token: str = "",
) -> dict[str, Any]:
    body = json.dumps(
        {
            "question": question,
            "with_context": True,
            "intent_budget": True,
            "token_budget": token_budget,
        }
    ).encode()
    headers = {"Content-Type": "application/json", "X-Workspace": workspace}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/ask/axis", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        value = json.loads(response.read().decode())
    if not isinstance(value, dict):
        raise ValueError("/ask/axis returned a non-object JSON response")
    return value


def to_mcp_event(response: dict[str, Any], instance_id: str) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    files: set[str] = set()
    for bundle in response.get("context_bundles", []):
        if not isinstance(bundle, dict):
            continue
        for item in [bundle.get("seed"), *bundle.get("related", [])]:
            if not isinstance(item, dict):
                continue
            path = item.get("file_path")
            if not isinstance(path, str) or not path:
                continue
            files.add(path)
            code = item.get("code")
            symbols.append(
                {
                    "uid": str(item.get("uid") or ""),
                    "name": str(item.get("name") or ""),
                    "file_path": path,
                    "role": item.get("role"),
                    "depth": int(item.get("distance_from_seed") or 0),
                    "expansion_step": item.get("expansion_step"),
                    "has_code": isinstance(code, str) and bool(code.strip()),
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                }
            )
    result = {
        "tool": "ask_code",
        "ok": True,
        "render": "full",
        "question": str(response.get("question") or ""),
        "workspace": response.get("workspace_id"),
        "files": sorted(files),
        "symbols": symbols,
    }
    return {"instance_id": instance_id, "tool": "ask_code", "result": result}


def render_context(response: dict[str, Any]) -> str:
    blocks = ["# Surgical Context retrieval"]
    seen: set[tuple[str, int | None, int | None]] = set()
    for bundle in response.get("context_bundles", []):
        if not isinstance(bundle, dict):
            continue
        for item in [bundle.get("seed"), *bundle.get("related", [])]:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                continue
            code = item["code"].strip()
            path = item.get("file_path")
            if not code or not isinstance(path, str):
                continue
            start, end = item.get("start_line"), item.get("end_line")
            key = (path, start, end)
            if key in seen:
                continue
            seen.add(key)
            blocks.extend([f"\n## {path}:{start or '?'}-{end or '?'}", "```", code, "```"])
    if len(blocks) == 1:
        blocks.append("\nNo code context found.")
    return "\n".join(blocks)


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+", help="Natural-language retrieval question")
    parser.add_argument("--url", default=os.getenv("SURGICAL_CONTEXT_URL", DEFAULT_URL))
    parser.add_argument("--workspace", default=os.getenv("SURGICAL_CONTEXT_WORKSPACE", ""))
    parser.add_argument("--instance-id", default=os.getenv("CONTEXTBENCH_INSTANCE_ID", ""))
    parser.add_argument("--log", type=Path, default=os.getenv("CONTEXTBENCH_EVENT_LOG"))
    parser.add_argument("--token-budget", type=int, default=6000)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.workspace:
        print("error: --workspace or SURGICAL_CONTEXT_WORKSPACE is required", file=sys.stderr)
        return 2
    question = " ".join(args.question)
    try:
        response = request_context(
            question,
            base_url=args.url,
            workspace=args.workspace,
            token_budget=max(400, min(args.token_budget, 32_000)),
            timeout=args.timeout,
            bearer_token=os.getenv("SURGICAL_CONTEXT_BEARER_TOKEN", ""),
        )
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        print(f"Surgical Context request failed: {exc}", file=sys.stderr)
        return 1
    if args.log and args.instance_id:
        append_event(args.log, to_mcp_event(response, args.instance_id))
    print(render_context(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
