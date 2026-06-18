#!/usr/bin/env python3
"""Static scan: ProxySurface bindings + Tier-4.5 typed calls (no Neo4j)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from context_engine.parser.adapters.python_adapter import PythonAdapter  # noqa: E402
from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter  # noqa: E402

QA = Path(__file__).resolve().parent


def _scan_python_tree(root: Path) -> dict:
    adapter = PythonAdapter()
    bindings: list[dict] = []
    typed_calls = 0
    typed_samples: list[dict] = []
    phantom_risk: list[dict] = []
    files = 0

    for path in sorted(root.rglob("*.py")):
        if any(part in {".git", "__pycache__", ".tox", "node_modules"} for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        files += 1
        file_bindings = adapter.extract_proxy_bindings(source, rel)
        bindings.extend(file_bindings)
        calls = adapter.extract_calls_from_source(source, rel)
        for call in calls:
            if call.get("tier") != "typed":
                continue
            typed_calls += 1
            qn = call.get("callee_qualified_name")
            if qn and len(typed_samples) < 12:
                typed_samples.append(
                    {
                        "file": rel,
                        "line": call.get("call_site_line"),
                        "callee": qn,
                        "callee_name": call.get("callee_name"),
                    }
                )
            # Heuristic: typed edge to stdlib-ish or overly short type names
            if qn and (
                qn.startswith("builtins.")
                or qn.count(".") < 2
                or qn.split(".")[-2] in {"self", "cls"}
            ):
                phantom_risk.append({"file": rel, "callee": qn, "line": call.get("call_site_line")})

    return {
        "files_scanned": files,
        "proxy_bindings": len(bindings),
        "proxy_binding_samples": bindings[:8],
        "typed_calls_emitted": typed_calls,
        "typed_call_samples": typed_samples,
        "typed_phantom_risk": phantom_risk[:12],
    }


def _scan_typescript_tree(root: Path) -> dict:
    adapter = TypeScriptAdapter()
    files = 0
    calls_by_tier: dict[str, int] = {}
    samples: list[dict] = []

    for path in sorted(root.rglob("*")):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        if any(part in {".git", "node_modules", "dist", "build"} for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        files += 1
        for call in adapter.extract_calls_from_source(source, rel):
            tier = call.get("tier", "unknown")
            calls_by_tier[tier] = calls_by_tier.get(tier, 0) + 1
            if tier == "typed" and len(samples) < 8:
                samples.append(
                    {
                        "file": rel,
                        "callee": call.get("callee_qualified_name") or call.get("callee_name"),
                    }
                )

    return {
        "files_scanned": files,
        "calls_by_tier": calls_by_tier,
        "note": "ProxySurface is Python-only; express uses TS adapter tiers only.",
        "typed_samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, choices=["django", "pydantic", "express"])
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    root = QA / "repos" / args.repo
    if not root.is_dir():
        print(f"missing checkout: {root}", file=sys.stderr)
        return 1

    if args.repo == "express":
        payload = {"repo": args.repo, "scan": _scan_typescript_tree(root)}
    else:
        payload = {"repo": args.repo, "scan": _scan_python_tree(root)}

    out = args.report or f"/tmp/proxy_audit_static_{args.repo}.json"
    Path(out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
