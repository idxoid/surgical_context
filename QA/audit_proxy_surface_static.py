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
from QA.output_paths import (  # noqa: E402
    default_report_basename,
    resolve_output_path,
    resolve_repo_checkout,
)

QA = Path(__file__).resolve().parent

_ALLOWED_REPOS = frozenset({"django", "pydantic", "express"})

_SKIP_PARTS = frozenset({".git", "__pycache__", ".tox", "node_modules"})


def _skip_path(path: Path) -> bool:
    return any(part in _SKIP_PARTS for part in path.parts)


def _read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _is_phantom_typed_callee(qn: str) -> bool:
    return qn.startswith("builtins.") or qn.count(".") < 2 or qn.split(".")[-2] in {"self", "cls"}


def _record_typed_call(
    call: dict,
    rel: str,
    typed_samples: list[dict],
    phantom_risk: list[dict],
) -> None:
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
    if qn and _is_phantom_typed_callee(qn):
        phantom_risk.append({"file": rel, "callee": qn, "line": call.get("call_site_line")})


def _scan_python_tree(root: Path) -> dict:
    adapter = PythonAdapter()
    bindings: list[dict] = []
    typed_calls = 0
    typed_samples: list[dict] = []
    phantom_risk: list[dict] = []
    files = 0

    for path in sorted(root.rglob("*.py")):
        if _skip_path(path):
            continue
        source = _read_source(path)
        if source is None:
            continue
        rel = str(path.relative_to(root))
        files += 1
        bindings.extend(adapter.extract_proxy_bindings(source, rel))
        for call in adapter.extract_calls_from_source(source, rel):
            if call.get("tier") != "typed":
                continue
            typed_calls += 1
            _record_typed_call(call, rel, typed_samples, phantom_risk)

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

    root = resolve_repo_checkout(QA, args.repo, _ALLOWED_REPOS)
    if not root.is_dir():
        print(f"missing checkout: {root}", file=sys.stderr)
        return 1

    if args.repo == "express":
        payload = {"repo": args.repo, "scan": _scan_typescript_tree(root)}
    else:
        payload = {"repo": args.repo, "scan": _scan_python_tree(root)}

    out = resolve_output_path(
        args.report or None,
        default_name=default_report_basename("proxy_audit_static", args.repo, _ALLOWED_REPOS),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
