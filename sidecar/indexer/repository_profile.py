"""Index-time repository readiness and reasoning capability profiling."""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from sidecar.parser.registry import REGISTRY

PROFILE_SCHEMA_VERSION = 1


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".parcel-cache",
    "dist",
    "build",
    "out",
    "target",
    ".gradle",
}

_EXTENSION_LANGUAGE_HINTS: Mapping[str, str] = {
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".vue": "vue_sfc",
    ".svelte": "svelte",
    ".sql": "sql",
    ".sgml": "sgml_docs",
}

_NEUTRAL_NON_CODE_EXTENSIONS = {
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".lock",
    ".csv",
    ".tsv",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".map",
    "<no_ext>",
}


@dataclass(frozen=True)
class RepositoryProfileInputs:
    project_path: str
    workspace_id: str
    collected_files: list[str]
    parsed_files: int
    symbols_indexed: int
    symbols_removed: int = 0
    calls_indexed: int = 0
    imports_indexed: int = 0
    inheritance_indexed: int = 0
    affects_rebuilt: int = 0
    skip_affects: bool = False
    sample_texts: list[str] | None = None


def build_repository_profile(inputs: RepositoryProfileInputs) -> dict:
    """Return an index-time contract for supported reasoning on a repository.

    The profile intentionally uses conservative signals. It should explain
    broad capability boundaries without pretending to understand a framework
    more deeply than the current graph can represent.
    """
    all_extensions = _scan_extensions(inputs.project_path)
    supported_exts = _supported_extensions()
    indexed_extensions = Counter(_extension(path) for path in inputs.collected_files)
    supported_languages = _supported_language_counts(indexed_extensions)
    neutral_extensions = Counter(
        {ext: count for ext, count in all_extensions.items() if ext in _NEUTRAL_NON_CODE_EXTENSIONS}
    )
    unsupported_extensions = Counter(
        {
            ext: count
            for ext, count in all_extensions.items()
            if ext not in supported_exts and ext not in _NEUTRAL_NON_CODE_EXTENSIONS
        }
    )
    unsupported_languages = _unsupported_language_counts(unsupported_extensions)

    indexed_file_total = sum(indexed_extensions.values())
    all_file_total = sum(all_extensions.values())
    code_file_total = indexed_file_total + sum(unsupported_extensions.values())
    supported_ratio = indexed_file_total / code_file_total if code_file_total else 0.0
    parse_coverage = inputs.parsed_files / indexed_file_total if indexed_file_total else 0.0
    symbol_density = (
        inputs.symbols_indexed / max(1, inputs.parsed_files) if inputs.parsed_files else 0.0
    )
    call_density = (
        inputs.calls_indexed / max(1, inputs.symbols_indexed) if inputs.symbols_indexed else 0.0
    )

    dynamic_surfaces = _detect_dynamic_surfaces(unsupported_extensions)
    strategy_profile = _build_strategy_profile(dynamic_surfaces)

    capability_flags = _capability_flags(
        supported_ratio=supported_ratio,
        parse_coverage=parse_coverage,
        symbol_density=symbol_density,
        call_density=call_density,
        inheritance_count=inputs.inheritance_indexed,
        import_count=inputs.imports_indexed,
        affects_rebuilt=inputs.affects_rebuilt,
        skip_affects=inputs.skip_affects,
        dynamic_surfaces=dynamic_surfaces,
        unsupported_languages=unsupported_languages,
    )
    readiness = _retrieval_readiness(
        supported_ratio=supported_ratio,
        parse_coverage=parse_coverage,
        symbols_indexed=inputs.symbols_indexed,
        unsupported_languages=unsupported_languages,
    )
    reasoning_contract = _reasoning_contract(capability_flags, readiness, dynamic_surfaces)

    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "generated_at": time.time(),
        "workspace_id": inputs.workspace_id,
        "project_path": inputs.project_path,
        "indexability": readiness["indexability"],
        "retrieval_readiness": readiness["retrieval_readiness"],
        "languages": {
            "supported": dict(supported_languages),
            "unsupported_or_unparsed": dict(unsupported_languages),
            "supported_file_ratio": round(supported_ratio, 3),
        },
        "file_surface": {
            "files_seen": all_file_total,
            "code_files_seen": code_file_total,
            "files_collected": indexed_file_total,
            "indexed_extensions": _top_counts(indexed_extensions),
            "unsupported_extensions": _top_counts(unsupported_extensions),
            "neutral_extensions": _top_counts(neutral_extensions),
        },
        "symbol_surface": {
            "files_parsed": inputs.parsed_files,
            "parse_coverage": round(parse_coverage, 3),
            "symbols_indexed": inputs.symbols_indexed,
            "symbols_removed": inputs.symbols_removed,
            "symbols_per_parsed_file": round(symbol_density, 3),
            "calls_indexed": inputs.calls_indexed,
            "imports_indexed": inputs.imports_indexed,
            "inheritance_indexed": inputs.inheritance_indexed,
            "calls_per_symbol": round(call_density, 3),
        },
        "mechanism_profile": {
            "framework_signals": [],
            "dynamic_surfaces": dynamic_surfaces,
        },
        "strategy_profile": strategy_profile,
        "capabilities": capability_flags,
        "reasoning_contract": reasoning_contract,
        "warnings": readiness["warnings"],
    }


def build_empty_repository_profile(
    project_path: str = "",
    workspace_id: str = "",
    *,
    reason: str = "not_indexed",
) -> dict:
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "generated_at": time.time(),
        "workspace_id": workspace_id,
        "project_path": project_path,
        "indexability": "unknown",
        "retrieval_readiness": reason,
        "languages": {
            "supported": {},
            "unsupported_or_unparsed": {},
            "supported_file_ratio": 0.0,
        },
        "file_surface": {
            "files_seen": 0,
            "code_files_seen": 0,
            "files_collected": 0,
            "indexed_extensions": {},
            "unsupported_extensions": {},
            "neutral_extensions": {},
        },
        "symbol_surface": {
            "files_parsed": 0,
            "parse_coverage": 0.0,
            "symbols_indexed": 0,
            "symbols_removed": 0,
            "symbols_per_parsed_file": 0.0,
            "calls_indexed": 0,
            "imports_indexed": 0,
            "inheritance_indexed": 0,
            "calls_per_symbol": 0.0,
        },
        "mechanism_profile": {
            "framework_signals": [],
            "dynamic_surfaces": [],
        },
        "strategy_profile": {
            "selected_strategy": "unprofiled",
            "role_plan": [],
            "fallbacks": ["direct_symbol"],
        },
        "capabilities": {
            "code_navigation": "unknown",
            "static_call_reasoning": "unknown",
            "inheritance_reasoning": "unknown",
            "doc_code_bridge": "unknown",
            "impact_analysis": "unknown",
        },
        "reasoning_contract": {
            "allowed": [],
            "risky": ["indexing did not produce a repository profile"],
        },
        "warnings": [reason],
    }


def summarize_repository_profile(profile: Mapping) -> str:
    """Compact single-line summary for benchmark/index logs."""
    languages = profile.get("languages", {})
    supported = languages.get("supported", {})
    top_langs = (
        ", ".join(f"{name}:{count}" for name, count in list(supported.items())[:3]) or "none"
    )
    capabilities = profile.get("capabilities", {})
    strategy = (profile.get("strategy_profile") or {}).get("selected_strategy", "unknown")
    return (
        f"{profile.get('retrieval_readiness', 'unknown')} "
        f"(indexability={profile.get('indexability', 'unknown')}, "
        f"langs={top_langs}, strategy={strategy}, "
        f"impact={capabilities.get('impact_analysis', 'unknown')})"
    )


def _scan_extensions(project_path: str) -> Counter:
    counts: Counter[str] = Counter()
    for _root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            counts[_extension(name)] += 1
    return counts


def _extension(path: str) -> str:
    _, ext = os.path.splitext(path)
    return ext.lower() or "<no_ext>"


def _supported_extensions() -> set[str]:
    return {ext for adapter in REGISTRY.supported_adapters() for ext in adapter.file_extensions}


def _supported_language_counts(indexed_extensions: Counter) -> Counter:
    counts: Counter[str] = Counter()
    for ext, count in indexed_extensions.items():
        try:
            lang = REGISTRY.detect_language(f"file{ext}")
        except ValueError:
            continue
        counts[lang] += count
    return counts


def _unsupported_language_counts(unsupported_extensions: Counter) -> Counter:
    counts: Counter[str] = Counter()
    for ext, count in unsupported_extensions.items():
        lang = _EXTENSION_LANGUAGE_HINTS.get(ext, "unknown")
        counts[lang] += count
    return counts


def _top_counts(counts: Counter, limit: int = 12) -> dict[str, int]:
    return dict(counts.most_common(limit))


def _detect_dynamic_surfaces(unsupported_extensions: Counter) -> list[str]:
    """Extension-only hints for unsupported static surfaces (no source-text keyword scan)."""
    surfaces: set[str] = set()
    if any(ext in unsupported_extensions for ext in (".vue", ".svelte")):
        surfaces.add("templates")
    if any(ext in unsupported_extensions for ext in (".c", ".h", ".cc", ".cpp", ".hpp")):
        surfaces.add("macros_or_c")
    return sorted(surfaces)


def _build_strategy_profile(dynamic_surfaces: list[str]) -> dict:
    fallbacks = ["direct_symbol", "semantic_docs"]
    if "macros_or_c" in dynamic_surfaces:
        fallbacks.append("docs_files_fallback")
    return {
        "selected_strategy": "generic_symbol_context",
        "role_plan": ["docs_or_concept"],
        "fallbacks": fallbacks,
    }


def _capability_flags(
    *,
    supported_ratio: float,
    parse_coverage: float,
    symbol_density: float,
    call_density: float,
    inheritance_count: int,
    import_count: int,
    affects_rebuilt: int,
    skip_affects: bool,
    dynamic_surfaces: list[str],
    unsupported_languages: Counter,
) -> dict[str, str]:
    code_navigation = _level(min(parse_coverage, 1.0), high=0.75, medium=0.35)
    if symbol_density < 0.5:
        code_navigation = _downgrade(code_navigation)

    static_calls = _level(call_density, high=0.6, medium=0.15)
    inheritance = "medium" if inheritance_count else "low"
    imports = "high" if import_count > 100 else "medium" if import_count else "low"
    doc_code_bridge = "medium" if supported_ratio >= 0.5 else "low"

    if skip_affects:
        impact = "disabled"
    elif affects_rebuilt <= 0:
        impact = "none"
    elif static_calls in {"high", "medium"} and not _has_major_unsupported_language(
        unsupported_languages
    ):
        impact = "shallow"
    else:
        impact = "shallow_partial"

    template_semantics = "low" if "templates" in dynamic_surfaces else "none"
    macro_semantics = "low" if "macros_or_c" in dynamic_surfaces else "none"

    return {
        "code_navigation": code_navigation,
        "static_call_reasoning": static_calls,
        "import_reasoning": imports,
        "inheritance_reasoning": inheritance,
        "template_semantics": template_semantics,
        "macro_semantics": macro_semantics,
        "doc_code_bridge": doc_code_bridge,
        "impact_analysis": impact,
    }


def _retrieval_readiness(
    *,
    supported_ratio: float,
    parse_coverage: float,
    symbols_indexed: int,
    unsupported_languages: Counter,
) -> dict:
    warnings: list[str] = []
    if symbols_indexed <= 0:
        warnings.append("no_symbol_surface")
        return {
            "indexability": "none",
            "retrieval_readiness": "unsupported_symbol_surface",
            "warnings": warnings,
        }

    if supported_ratio < 0.2 or _has_major_unsupported_language(unsupported_languages):
        warnings.append("large_unsupported_language_surface")
    if parse_coverage < 0.5:
        warnings.append("low_parse_coverage")

    if supported_ratio >= 0.75 and parse_coverage >= 0.75:
        indexability = "high"
    elif supported_ratio >= 0.35 and parse_coverage >= 0.35:
        indexability = "medium"
    else:
        indexability = "low"

    if indexability in {"high", "medium"}:
        retrieval = "partial"
    else:
        retrieval = "limited"

    return {
        "indexability": indexability,
        "retrieval_readiness": retrieval,
        "warnings": warnings,
    }


def _reasoning_contract(
    capabilities: Mapping[str, str],
    readiness: Mapping[str, object],
    dynamic_surfaces: list[str],
) -> dict[str, list[str]]:
    allowed = []
    risky = []

    if capabilities.get("code_navigation") in {"high", "medium"}:
        allowed.append("symbol/file navigation over indexed languages")
    else:
        risky.append("symbol navigation may miss large parts of the repository")

    if capabilities.get("static_call_reasoning") in {"high", "medium"}:
        allowed.append("local static call/import reasoning")
    else:
        risky.append("call-flow explanations may be incomplete")

    impact = capabilities.get("impact_analysis")
    if impact == "shallow":
        allowed.append("reachability-based impact candidates")
        risky.append("causal breakage claims require human/test validation")
    elif impact == "shallow_partial":
        allowed.append("limited reachability-based impact candidates")
        risky.append("impact is shallow and may miss dynamic/framework edges")
    else:
        risky.append("impact analysis is not supported by this index profile")

    if dynamic_surfaces:
        risky.append(
            "framework/runtime surfaces need mechanism validation: " + ", ".join(dynamic_surfaces)
        )

    if readiness.get("retrieval_readiness") == "unsupported_symbol_surface":
        risky.append("retrieval should fall back to docs/files or direct LLM context")

    return {"allowed": allowed, "risky": risky}


def _level(value: float, *, high: float, medium: float) -> str:
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    if value > 0:
        return "low"
    return "none"


def _downgrade(level: str) -> str:
    order = ["none", "low", "medium", "high"]
    idx = max(0, order.index(level) - 1)
    return order[idx]


def _has_major_unsupported_language(unsupported_languages: Counter) -> bool:
    return any(
        lang not in {"unknown", "sgml_docs"} and count >= 50
        for lang, count in unsupported_languages.items()
    )
