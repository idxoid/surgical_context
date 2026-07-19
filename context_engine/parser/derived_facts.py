"""Per-file derived graph facts extracted once with the shared AST."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DerivedFileFacts:
    """Optional one-shot extracts that phases used to re-parse for.

    ``computed=False`` means the pipeline should fall back to live adapter
    calls (test doubles / legacy extract_all). ``computed=True`` with empty
    lists means "looked, found nothing" — do not re-extract.
    """

    computed: bool = False
    proxy_bindings: list[dict] = field(default_factory=list)
    proxy_return_calls: list[dict] = field(default_factory=list)
    decorators: list[dict] = field(default_factory=list)
    decorator_compositions: list[dict] = field(default_factory=list)
    type_references: list[dict] = field(default_factory=list)
    injections: list[dict] = field(default_factory=list)
    attr_accesses: list[dict] = field(default_factory=list)
    instantiations: list[dict] = field(default_factory=list)
    flow_pairs: list[dict] = field(default_factory=list)
    hooks: list[dict] = field(default_factory=list)
    http_endpoints: list[dict] = field(default_factory=list)
    reexports: list[dict] = field(default_factory=list)
    metadata_bridges: list[dict] = field(default_factory=list)


# (ExtractedFile.derived attribute, LanguageAdapter method name)
_DERIVED_TREE_METHODS: tuple[tuple[str, str], ...] = (
    ("proxy_bindings", "extract_proxy_bindings"),
    ("proxy_return_calls", "extract_self_method_proxy_calls"),
    ("decorators", "extract_decorators"),
    ("decorator_compositions", "extract_decorator_compositions"),
    ("type_references", "extract_type_references"),
    ("injections", "extract_injections"),
    ("attr_accesses", "extract_attr_accesses"),
    ("instantiations", "extract_instantiations"),
    ("flow_pairs", "extract_flow_pairs"),
    ("hooks", "extract_hooks"),
    ("http_endpoints", "extract_http_endpoints"),
    ("reexports", "extract_reexports"),
    ("metadata_bridges", "extract_metadata_bridges"),
)


def extract_derived_file_facts(
    adapter, source_code: str, file_path: str, *, tree
) -> DerivedFileFacts:
    """Run every optional derived extractor against one shared tree."""
    facts = DerivedFileFacts(computed=True)
    for attr, method_name in _DERIVED_TREE_METHODS:
        fn = getattr(adapter, method_name, None)
        if not callable(fn):
            continue
        try:
            try:
                value = fn(source_code, file_path, tree=tree)
            except TypeError:
                # Some adapters (e.g. Python reexports) do not take tree= yet.
                value = fn(source_code, file_path)
            setattr(facts, attr, list(value or []))
        except Exception:
            setattr(facts, attr, [])
    return facts
