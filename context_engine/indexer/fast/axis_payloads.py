"""Axis profile extraction and symbol-doc payload compilation."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, cast

from context_engine.indexer.fast.extractor import ExtractedFile
from context_engine.indexer.file_tier import classify_file_tier, is_pure_reexport_source

if TYPE_CHECKING:
    from context_engine.axis.container_kind import GraphContextProbe
    from context_engine.axis.schema import AxisProfile


def build_symbol_docs_for_extracted(
    ex: ExtractedFile,
    *,
    changed_uids: set[str],
    workspace_id: str,
    project_path: str = "",
    graph_probe: GraphContextProbe | None = None,
    include_axis_facts: bool = False,
) -> list[dict]:
    """Build Lance symbol rows for changed symbols in one extracted file."""
    source_lines = ex.source.splitlines()
    # File tier is a per-file structural property: path topology +
    # whether the module body is a pure re-export. Computed once per
    # file and stamped on every symbol row so the axis ranker can
    # demote noise tiers in seed retrieval (see file_tier.py).
    # Classify on the path RELATIVE to the indexed project root — the
    # absolute path carries infra segments (e.g. ``QA/repos/...``) that
    # would otherwise be mistaken for tier markers.
    tier_path = os.path.relpath(ex.path, project_path) if project_path else ex.path
    file_tier_value = classify_file_tier(
        tier_path, pure_reexport=is_pure_reexport_source(ex.source)
    )
    axis_payloads = (
        _axis_payloads_for_extracted_file(
            ex,
            project_path=project_path,
            graph_probe=graph_probe,
        )
        if include_axis_facts and changed_uids
        else {}
    )
    symbol_docs: list[dict] = []
    for sym in ex.symbols:
        if sym.uid not in changed_uids:
            continue
        code = "\n".join(source_lines[sym.start_line - 1 : sym.end_line])
        if not include_axis_facts:
            symbol_docs.append(
                {
                    "uid": sym.uid,
                    "name": sym.name,
                    "file_path": sym.file_path,
                    "workspace_id": workspace_id,
                    "code": code,
                    "start_line": sym.start_line,
                    "end_line": sym.end_line,
                }
            )
            continue
        row = {
            "uid": sym.uid,
            "name": sym.name,
            "symbol_kind": sym.kind,
            "qualified_name": sym.qualified_name or "",
            "file_path": sym.file_path,
            "workspace_id": workspace_id,
            "code": code,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            "file_tier": file_tier_value,
        }
        row.update(axis_payloads.get(sym.uid) or axis_payloads.get(sym.qualified_name) or {})
        symbol_docs.append(row)
    return symbol_docs


class _PeerAwarePeerProbe:
    """Wrap a ``GraphContextProbe`` with a per-file peer-kind lookup.

    Delegates every probe method to the wrapped base probe (or to a
    ``NullGraphProbe``-style return when no base probe is present) and only
    overrides ``peer_container_kinds_for`` to consult the supplied
    ``peer_kinds_by_qn`` map. The map carries the container kinds of
    every non-class profile in the same file, keyed by qualified_name.
    """

    def __init__(
        self,
        base: GraphContextProbe | None,
        peer_kinds_by_qn: dict[str, set[str]],
    ) -> None:
        self._base = base
        self._peer_kinds_by_qn = peer_kinds_by_qn

    def peer_container_kinds_for(self, qualified_name_prefix: str) -> set[str]:
        collected: set[str] = set()
        for qn, kinds in self._peer_kinds_by_qn.items():
            if qn.startswith(qualified_name_prefix):
                collected |= kinds
        return collected

    def outgoing_kind_edges(self, symbol_uid, kinds):
        if self._base is None:
            return 0
        return self._base.outgoing_kind_edges(symbol_uid, kinds)

    def library_marker_kinds(self, symbol_uid):
        if self._base is None:
            return set()
        return self._base.library_marker_kinds(symbol_uid)

    def caller_package_dispersion(self, symbol_uid):
        if self._base is None:
            return 0.0
        return self._base.caller_package_dispersion(symbol_uid)

    def is_cfg_driver(self, symbol_uid):
        if self._base is None:
            return False
        return self._base.is_cfg_driver(symbol_uid)

    def is_event_signal(self, symbol_uid):
        if self._base is None:
            return False
        return self._base.is_event_signal(symbol_uid)

    def outgoing_handles_count(self, symbol_uid):
        if self._base is None:
            return 0
        return self._base.outgoing_handles_count(symbol_uid)

    def outgoing_injects_count(self, symbol_uid):
        if self._base is None:
            return 0
        return self._base.outgoing_injects_count(symbol_uid)

    def metadata_bridge_keys(self, symbol_uid):
        if self._base is None:
            return ()
        fn = getattr(self._base, "metadata_bridge_keys", None)
        if not callable(fn):
            return ()
        return fn(symbol_uid)


def _load_axis_extraction_for_file(
    ex: ExtractedFile,
    *,
    project_path: str = "",
):
    from context_engine.axis.schema import AxisExtraction
    from context_engine.parser.registry import REGISTRY

    if ex.axis_facts is not None:
        facts = ex.axis_facts
    else:
        try:
            adapter = REGISTRY.get_adapter(REGISTRY.detect_language(ex.path))
        except ValueError:
            facts = []
        else:
            facts = adapter.extract_axis_facts(
                ex.source,
                ex.path,
                symbols=ex.symbols,
                project_root=project_path or None,
            )
    return AxisExtraction(file_path=ex.path, facts=facts)


def _merge_profile_fact(target, fact, target_uid):
    from context_engine.axis.schema import AxisFact

    if fact.symbol_uid == target_uid:
        target.add_fact(fact)
        return
    target.add_fact(
        AxisFact(
            symbol_uid=target_uid,
            qualified_name=fact.qualified_name,
            symbol_kind=fact.symbol_kind,
            axis=fact.axis,
            bit=fact.bit,
            line=fact.line,
            evidence=fact.evidence,
            ast_kind=fact.ast_kind,
            payload=dict(fact.payload),
        )
    )


def _variable_stub_profiles(
    ex: ExtractedFile,
    profiles_by_uid: dict[str, AxisProfile],
    *,
    graph_probe: GraphContextProbe | None,
) -> None:
    from context_engine.axis.schema import AxisFact, AxisProfile

    for sym in ex.symbols:
        if sym.kind != "variable" or sym.uid in profiles_by_uid:
            continue
        stub = AxisProfile(
            symbol_uid=sym.uid,
            qualified_name=sym.qualified_name,
            symbol_kind="variable",
        )
        handles_count = (
            graph_probe.outgoing_handles_count(sym.uid) if graph_probe is not None else 0
        )
        if handles_count > 0:
            stub.add_fact(
                AxisFact(
                    symbol_uid=sym.uid,
                    qualified_name=sym.qualified_name,
                    symbol_kind="variable",
                    axis="dfg",
                    bit="registered_callable",
                    line=sym.start_line,
                    evidence=f"<handles:{handles_count}>",
                    ast_kind="GraphProbe",
                    payload={"count": handles_count},
                )
            )
        profiles_by_uid[sym.uid] = stub


def _merge_extraction_profiles(
    extraction,
    ex: ExtractedFile,
    *,
    graph_probe: GraphContextProbe | None,
) -> dict[str, AxisProfile]:
    from context_engine.axis.schema import AxisProfile

    parser_uid_by_qn = {s.qualified_name: s.uid for s in ex.symbols if s.qualified_name}
    profiles_by_uid: dict[str, AxisProfile] = {}
    for profile in extraction.profiles.values():
        target_uid = parser_uid_by_qn.get(profile.qualified_name, profile.symbol_uid)
        target = profiles_by_uid.get(target_uid)
        if target is None:
            target = AxisProfile(
                symbol_uid=target_uid,
                qualified_name=profile.qualified_name,
                symbol_kind=profile.symbol_kind,
            )
            profiles_by_uid[target_uid] = target
        for fact in profile.facts:
            _merge_profile_fact(target, fact, target_uid)
    _variable_stub_profiles(ex, profiles_by_uid, graph_probe=graph_probe)
    return profiles_by_uid


def _add_injection_probe_facts(
    profiles_by_uid: dict[str, AxisProfile],
    graph_probe: GraphContextProbe | None,
) -> None:
    from context_engine.axis.schema import AxisFact

    if graph_probe is None:
        return
    for uid, profile in profiles_by_uid.items():
        if profile.symbol_kind not in {"function", "method"}:
            continue
        injects_count = graph_probe.outgoing_injects_count(uid)
        if injects_count <= 0:
            continue
        profile.add_fact(
            AxisFact(
                symbol_uid=uid,
                qualified_name=profile.qualified_name,
                symbol_kind=profile.symbol_kind,
                axis="dfg",
                bit="injected_dependency",
                line=0,
                evidence=f"<injects:{injects_count}>",
                ast_kind="GraphProbe",
                payload={"count": injects_count},
            )
        )


def _classify_profiles_by_uid(
    profiles_by_uid: dict[str, AxisProfile],
    base_probe: GraphContextProbe | None,
) -> dict[str, list]:
    from context_engine.axis.container_kind import ContainerKindClassifier, GraphContextProbe

    classifier = (
        ContainerKindClassifier(probe=base_probe)
        if base_probe is not None
        else ContainerKindClassifier()
    )
    container_kinds_by_uid: dict[str, list] = {}
    for uid, profile in profiles_by_uid.items():
        if profile.symbol_kind == "class":
            continue
        container_kinds_by_uid[uid] = classifier.classify(profile)

    peer_kinds_by_qn: dict[str, set[str]] = {}
    for uid, matches in container_kinds_by_uid.items():
        prof = profiles_by_uid[uid]
        if not matches:
            continue
        peer_kinds_by_qn.setdefault(prof.qualified_name, set()).update(
            match.kind for match in matches
        )

    class_probe = cast(GraphContextProbe, _PeerAwarePeerProbe(base_probe, peer_kinds_by_qn))
    class_classifier = ContainerKindClassifier(probe=class_probe)
    for uid, profile in profiles_by_uid.items():
        if profile.symbol_kind != "class":
            continue
        container_kinds_by_uid[uid] = class_classifier.classify(profile)
    return container_kinds_by_uid


def _compile_axis_payloads(
    profiles_by_uid: dict[str, AxisProfile],
    container_kinds_by_uid: dict[str, list],
    contract_compiler,
) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for profile in profiles_by_uid.values():
        container_kinds = container_kinds_by_uid.get(profile.symbol_uid, [])
        contracts = contract_compiler.compile(profile, container_kinds)
        payload = {
            "ast_kind_bits": sorted({fact.ast_kind for fact in profile.facts}),
            "cfg_bits": sorted(profile.cfg_bits),
            "dfg_bits": sorted(profile.dfg_bits),
            "struct_bits": sorted(profile.struct_bits),
            "container_kinds": sorted({match.kind for match in container_kinds}),
            "axis_evidence_json": json.dumps(
                [fact.to_dict() for fact in profile.facts],
                sort_keys=True,
            ),
            "axis_container_kinds_json": json.dumps(
                [match.to_dict() for match in container_kinds],
                sort_keys=True,
            ),
            "axis_contracts_json": json.dumps(
                [match.to_dict() for match in contracts],
                sort_keys=True,
            ),
        }
        payloads[profile.symbol_uid] = payload
        payloads[profile.qualified_name] = payload
    return payloads


def _axis_payloads_for_extracted_file(
    ex: ExtractedFile,
    *,
    project_path: str = "",
    graph_probe: GraphContextProbe | None = None,
) -> dict[str, dict]:
    """Return per-symbol axis payloads keyed by uid and qualified name.

    UID generation should line up with parser symbols, but this first isolated
    index keeps a qualified-name fallback so signature-normalization drift does
    not silently drop physical AST facts.
    """
    from context_engine.axis.contract_compiler import AxisContractCompiler

    extraction = _load_axis_extraction_for_file(ex, project_path=project_path)
    base_probe = graph_probe if graph_probe is not None else None
    contract_compiler = AxisContractCompiler()
    profiles_by_uid = _merge_extraction_profiles(
        extraction,
        ex,
        graph_probe=graph_probe,
    )
    _add_injection_probe_facts(profiles_by_uid, graph_probe)
    container_kinds_by_uid = _classify_profiles_by_uid(profiles_by_uid, base_probe)
    return _compile_axis_payloads(profiles_by_uid, container_kinds_by_uid, contract_compiler)
