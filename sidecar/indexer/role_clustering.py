"""Pass 1: derive per-repository roles from call-graph topology via L1/L2 cascade.

Replaces flat k-means clustering with discriminator-first assignment
(``sidecar.indexer.role_cascade``). Output:

- per-symbol primary + supporting roles (persisted on Symbol nodes)
- workspace ``RoleCatalog`` with presence-gated ``present_roles``
- workspace ``RoleAssignmentSummary`` metadata
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from sidecar.context.mechanism_registry import merge_preloaded_mechanisms_into_role_catalog
from sidecar.context.ranker.signal_constants import NOISE_PATH_PATTERNS
from sidecar.indexer.external_boundary import EXTERNAL_INTEGRATION_PLUMBING_ROOTS
from sidecar.indexer.role_cascade import (
    SymbolRoleAssignment,
    assign_all,
    detect_present_roles,
    role_catalog_roles,
)

ROLE_TAXONOMY_SCHEMA_VERSION = 3
ROLE_CATALOG_SCHEMA_VERSION = 3

CALL_REL_TYPES = (
    "CALLS",
    "CALLS_DIRECT",
    "CALLS_SCOPED",
    "CALLS_IMPORTED",
    "CALLS_DYNAMIC",
    "CALLS_INFERRED",
    "CALLS_GUESS",
)

STRUCTURAL_REL_TYPES = (
    *CALL_REL_TYPES,
    "DEPENDS_ON",
    "HAS_API",
    "INHERITED_API",
    "USES_TYPE",
    "INJECTS",
    "HANDLES",
    "DECORATED_BY",
    "INSTANTIATES",
    "COMPOSES",
    "READS_ATTR",
    "WRITES_ATTR",
)

DEFAULT_EDGE_CONFIDENCE: dict[str, float] = {
    "CALLS_DIRECT": 1.0,
    "CALLS_SCOPED": 0.9,
    "CALLS_IMPORTED": 0.85,
    "CALLS_DYNAMIC": 0.7,
    "CALLS_INFERRED": 0.7,
    "CALLS_GUESS": 0.4,
    "CALLS": 0.85,
    "DEPENDS_ON": 0.9,
    "HAS_API": 0.95,
    "INHERITED_API": 0.9,
    "INJECTS": 0.85,
    "HANDLES": 1.0,
    "USES_TYPE": 1.0,
    "DECORATED_BY": 1.0,
    "INSTANTIATES": 1.0,
    "COMPOSES": 1.0,
    "READS_ATTR": 1.0,
    "WRITES_ATTR": 1.0,
}

USES_TYPE_KIND_WEIGHT: dict[str, float] = {
    "param": 1.0,
    "annotation": 0.8,
    "return": 0.6,
    "isinstance": 0.5,
}

_EPS = 0.05


@dataclass(frozen=True)
class SymbolRow:
    """Structural facts about one symbol for cascade predicates."""

    uid: str
    kind: str
    fan_in: int
    fan_out: int
    cross_package_in: int
    cross_package_out: int
    depth_from_public: int
    doc_anchor_count: int
    import_in: int = 0
    doc_definition_weight: float = 0.0
    doc_reference_weight: float = 0.0
    doc_example_weight: float = 0.0
    call_fan_in: float = 0.0
    call_fan_out: float = 0.0
    type_fan_in: float = 0.0
    type_fan_out: float = 0.0
    type_fan_in_param: float = 0.0
    type_fan_in_isinstance: float = 0.0
    type_fan_in_return: float = 0.0
    type_fan_out_return: float = 0.0
    api_fan_in: float = 0.0
    api_fan_out: float = 0.0
    inject_fan_in: float = 0.0
    depend_fan_in: float = 0.0
    depend_fan_out: float = 0.0
    handle_fan_in: float = 0.0
    handle_fan_out: float = 0.0
    handler_call_fan_out: float = 0.0
    decorated_in: float = 0.0
    decorated_out: float = 0.0
    construct_fan_out: float = 0.0
    fluent_self_return_count: int = 0
    decorator_arg_ref_count: int = 0
    # Attribute-access fans — outgoing edges count how many distinct
    # attribute symbols the function reads / writes. ``subscript`` is the
    # binding-surface signal: a function that writes into the *contents* of
    # an attribute (``self.cache[k] = v``) is shaping a mapping/sequence.
    attr_reads_fan_out: float = 0.0
    attr_writes_fan_out: float = 0.0
    attr_writes_subscript_fan_out: float = 0.0
    reexport_in: int = 0
    is_proxy_binding: bool = False
    external_call_fan_out: float = 0.0
    external_import_fan_out: float = 0.0
    external_root_count: int = 0
    external_integration_call_fan_out: float = 0.0
    external_integration_import_fan_out: float = 0.0
    external_integration_root_count: int = 0
    inherits_builtin_exception: bool = False
    returns_function_expression: bool = False
    returns_mapping: bool = False
    returns_sequence: bool = False
    returns_constructed_type: bool = False
    iterates_attr_call: bool = False
    assembles_mapping_in_loop: bool = False

    @property
    def external_call_out_ratio(self) -> float:
        denom = self.call_fan_out + self.external_call_fan_out + _EPS
        return self.external_call_fan_out / denom

    @property
    def external_integration_out_ratio(self) -> float:
        ext = self.external_integration_call_fan_out
        denom = self.call_fan_out + ext + _EPS
        return ext / denom

    @property
    def cross_package_call_in(self) -> float:
        return float(self.cross_package_in)

    @property
    def cross_package_call_out(self) -> float:
        return float(self.cross_package_out)

    @property
    def is_class(self) -> bool:
        return self.kind in {"class", "interface"}

    @property
    def is_function(self) -> bool:
        return self.kind in {"function", "method"}

    @property
    def has_documentation(self) -> bool:
        return self.doc_anchor_count > 0 or self.doc_definition_weight > 0

    @property
    def call_leaf(self) -> bool:
        return self.call_fan_out <= _EPS

    @property
    def zero_in_degree(self) -> bool:
        return all(
            v <= _EPS
            for v in (
                self.call_fan_in,
                self.type_fan_in,
                self.api_fan_in,
                self.inject_fan_in,
                self.depend_fan_in,
                self.handle_fan_in,
                self.decorated_in,
            )
        )

    @property
    def structurally_connected(self) -> bool:
        # A proxy_binding's only edge is PROXY_OF, which is not aggregated into any
        # fan metric; without this exemption it would be dropped from Pass-1 even
        # though it is a fully-defined structural node and the proxy_mechanism L2
        # predicate is keyed on is_proxy_binding.
        if self.is_proxy_binding:
            return True
        return any(
            v > _EPS
            for v in (
                self.call_fan_in,
                self.call_fan_out,
                self.type_fan_in,
                self.type_fan_out,
                self.api_fan_in,
                self.api_fan_out,
                self.inject_fan_in,
                self.depend_fan_in,
                self.depend_fan_out,
                self.handle_fan_in,
                self.handle_fan_out,
                self.decorated_in,
                self.decorated_out,
                self.construct_fan_out,
            )
        )

    def effective_call_fan_in(self) -> float:
        return self.call_fan_in if self.call_fan_in > 0.0 else float(self.fan_in)

    def effective_call_fan_out(self) -> float:
        return self.call_fan_out if self.call_fan_out > 0.0 else float(self.fan_out)


@dataclass(frozen=True)
class RoleAssignmentSummary:
    method: str
    sample_size: int
    filtered_sample_size: int
    present_roles: dict[str, int]
    l1_distribution: dict[str, int]
    schema_version: int = ROLE_TAXONOMY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "method": self.method,
            "sample_size": self.sample_size,
            "filtered_sample_size": self.filtered_sample_size,
            "present_roles": dict(self.present_roles),
            "l1_distribution": dict(self.l1_distribution),
        }


@dataclass(frozen=True)
class RoleCatalog:
    present_roles: dict[str, int]
    schema_version: int = ROLE_CATALOG_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "present_roles": dict(self.present_roles),
        }


def filter_clustering_rows(rows: Sequence[SymbolRow]) -> list[SymbolRow]:
    """Drop symbols with no position in any structural edge family."""
    return [row for row in rows if row.structurally_connected]


def assign_role_taxonomy(
    rows: Sequence[SymbolRow],
    *,
    min_support: int | None = None,
) -> tuple[RoleAssignmentSummary, dict[str, SymbolRoleAssignment], dict[str, int]]:
    """Run discriminator-first Pass 1 on structural rows."""
    assign_rows = filter_clustering_rows(rows)
    assignments = assign_all(assign_rows)
    kwargs = {} if min_support is None else {"min_support": min_support}
    present = detect_present_roles(assignments, **kwargs)
    l1_counts = Counter(asn.l1 for asn in assignments.values())
    summary = RoleAssignmentSummary(
        method="discriminator_cascade",
        sample_size=len(rows),
        filtered_sample_size=len(assign_rows),
        present_roles=present,
        l1_distribution=dict(sorted(l1_counts.items())),
    )
    return summary, assignments, present


def build_role_catalog(present_roles: dict[str, int]) -> RoleCatalog:
    """Build workspace catalog from presence-gated roles only."""
    return RoleCatalog(present_roles=dict(present_roles))


def _edge_confidence(rel_type: str, stored: float | None, kind: str = "") -> float:
    if rel_type == "USES_TYPE":
        return USES_TYPE_KIND_WEIGHT.get(kind or "", DEFAULT_EDGE_CONFIDENCE["USES_TYPE"])
    if stored is not None:
        return float(stored)
    return DEFAULT_EDGE_CONFIDENCE.get(rel_type, 1.0)


def _iter_structural_edges(
    edges: Sequence[tuple[str, ...]],
) -> list[tuple[str, str, str, float, str]]:
    normalized: list[tuple[str, str, str, float, str]] = []
    for edge in edges:
        if len(edge) == 2:
            normalized.append((edge[0], edge[1], "CALLS_DIRECT", 1.0, ""))
        elif len(edge) == 4:
            normalized.append((edge[0], edge[1], edge[2], float(edge[3]), ""))
        elif len(edge) >= 5:
            normalized.append((edge[0], edge[1], edge[2], float(edge[3]), edge[4] or ""))
    return normalized


def assemble_symbol_rows(
    symbols: Sequence[tuple[str, str, str]],
    call_edges: Sequence[tuple[str, ...]],
    doc_counts: dict[str, int],
    import_in_per_uid: dict[str, int] | None = None,
    doc_signal_by_uid: dict[str, dict[str, float]] | None = None,
    proxy_uids: set[str] | None = None,
    reexport_in_per_uid: dict[str, int] | None = None,
    external_call_fan_out_per_uid: dict[str, float] | None = None,
    external_root_count_per_uid: dict[str, int] | None = None,
    external_import_fan_out_by_file: dict[str, float] | None = None,
    external_integration_call_fan_out_per_uid: dict[str, float] | None = None,
    external_integration_root_count_per_uid: dict[str, int] | None = None,
    external_integration_import_fan_out_by_file: dict[str, float] | None = None,
) -> list[SymbolRow]:
    """Combine raw graph extracts into ``SymbolRow``s with cascade features."""
    if not symbols:
        return []

    import_in_per_uid = import_in_per_uid or {}
    doc_signal_by_uid = doc_signal_by_uid or {}
    proxy_uids = proxy_uids or set()
    reexport_in_per_uid = reexport_in_per_uid or {}
    external_call_fan_out_per_uid = external_call_fan_out_per_uid or {}
    external_root_count_per_uid = external_root_count_per_uid or {}
    external_import_fan_out_by_file = external_import_fan_out_by_file or {}
    external_integration_call_fan_out_per_uid = external_integration_call_fan_out_per_uid or {}
    external_integration_root_count_per_uid = external_integration_root_count_per_uid or {}
    external_integration_import_fan_out_by_file = external_integration_import_fan_out_by_file or {}

    info: dict[str, dict] = {}
    for sym in symbols:
        uid, kind, file_path = sym[0], sym[1], sym[2]
        inherits_exc = bool(sym[3]) if len(sym) > 3 else False
        returns_fn_expr = bool(sym[4]) if len(sym) > 4 else False
        returns_map = bool(sym[5]) if len(sym) > 5 else False
        returns_seq = bool(sym[6]) if len(sym) > 6 else False
        returns_ct = bool(sym[7]) if len(sym) > 7 else False
        iter_call = bool(sym[8]) if len(sym) > 8 else False
        assembles = bool(sym[9]) if len(sym) > 9 else False
        info[uid] = {
            "uid": uid,
            "kind": kind or "",
            "file_path": file_path or "",
            "package": os.path.dirname(file_path or ""),
            "inherits_builtin_exception": inherits_exc,
            "returns_function_expression": returns_fn_expr,
            "returns_mapping": returns_map,
            "returns_sequence": returns_seq,
            "returns_constructed_type": returns_ct,
            "iterates_attr_call": iter_call,
            "assembles_mapping_in_loop": assembles,
        }

    call_out: dict[str, set[str]] = {uid: set() for uid in info}
    call_in: dict[str, set[str]] = {uid: set() for uid in info}
    call_fan_in: dict[str, float] = defaultdict(float)
    call_fan_out: dict[str, float] = defaultdict(float)
    type_fan_in: dict[str, float] = defaultdict(float)
    type_fan_out: dict[str, float] = defaultdict(float)
    type_fan_in_param: dict[str, float] = defaultdict(float)
    type_fan_in_isinstance: dict[str, float] = defaultdict(float)
    type_fan_in_return: dict[str, float] = defaultdict(float)
    type_fan_out_return: dict[str, float] = defaultdict(float)
    api_fan_in: dict[str, float] = defaultdict(float)
    api_fan_out: dict[str, float] = defaultdict(float)
    inject_fan_in: dict[str, float] = defaultdict(float)
    depend_fan_in: dict[str, float] = defaultdict(float)
    depend_fan_out: dict[str, float] = defaultdict(float)
    handle_fan_in: dict[str, float] = defaultdict(float)
    handle_fan_out: dict[str, float] = defaultdict(float)
    decorated_in: dict[str, float] = defaultdict(float)
    decorated_out: dict[str, float] = defaultdict(float)
    construct_fan_out: dict[str, float] = defaultdict(float)
    decorator_arg_ref_count: dict[str, int] = defaultdict(int)
    attr_reads_fan_out: dict[str, float] = defaultdict(float)
    attr_writes_fan_out: dict[str, float] = defaultdict(float)
    attr_writes_subscript_fan_out: dict[str, float] = defaultdict(float)

    for caller, callee, rel_type, conf, kind in _iter_structural_edges(call_edges):
        caller_in = caller in info
        callee_in = callee in info
        # F13: credit Pass-1 endpoints from full-graph edges outside the
        # clustered symbol set (tests/docs user code calling framework APIs).
        if not caller_in and not callee_in:
            continue
        if caller == callee:
            continue
        if rel_type in CALL_REL_TYPES:
            if caller_in:
                call_out[caller].add(callee)
                call_fan_out[caller] += conf
            if callee_in:
                call_in[callee].add(caller)
                call_fan_in[callee] += conf
        elif rel_type == "USES_TYPE":
            if caller_in:
                type_fan_out[caller] += conf
                if kind == "return":
                    type_fan_out_return[caller] += conf
            if callee_in:
                type_fan_in[callee] += conf
                if kind in {"param", "annotation"}:
                    type_fan_in_param[callee] += conf
                elif kind == "isinstance":
                    type_fan_in_isinstance[callee] += conf
                elif kind == "return":
                    type_fan_in_return[callee] += conf
        elif rel_type in {"HAS_API", "INHERITED_API"}:
            if caller_in:
                api_fan_out[caller] += conf
            if callee_in:
                api_fan_in[callee] += conf
        elif rel_type == "INJECTS":
            if callee_in:
                inject_fan_in[callee] += conf
        elif rel_type == "DEPENDS_ON":
            if caller_in:
                depend_fan_out[caller] += conf
            if callee_in:
                depend_fan_in[callee] += conf
        elif rel_type == "HANDLES":
            if caller_in:
                handle_fan_out[caller] += conf
            if callee_in:
                handle_fan_in[callee] += conf
        elif rel_type == "DECORATED_BY":
            if caller_in:
                decorated_out[caller] += conf
            if callee_in:
                decorated_in[callee] += conf
        elif rel_type == "INSTANTIATES":
            if caller_in:
                construct_fan_out[caller] += conf
        elif rel_type == "COMPOSES":
            # Subtype 2: decorator-arg references from a decorated class to the
            # symbols it composes (NestJS `@Module({imports:[X],...})`). Counted
            # per-class only; the receiving side is not credited (a composed
            # service does not gain a role from being composed).
            if caller_in:
                decorator_arg_ref_count[caller] += 1
        elif rel_type == "READS_ATTR":
            # Function reads an attribute — counted per-accessor only. The
            # attribute side is not credited (a frequently-read attribute is
            # not itself a binder).
            if caller_in:
                attr_reads_fan_out[caller] += conf
        elif rel_type == "WRITES_ATTR":
            # Functions writes an attribute. ``kind`` distinguishes a direct
            # write (``self.x = ...``) from a subscript write (``self.x[k]
            # = v``). The subscript form is the binding-surface signal —
            # function builds a mapping/sequence inside an attribute.
            if caller_in:
                attr_writes_fan_out[caller] += conf
                if kind in ("write_subscript", "write_subscript_local"):
                    attr_writes_subscript_fan_out[caller] += conf

    handler_call_fan_out: dict[str, float] = defaultdict(float)
    for caller, callee, rel_type, conf, _kind in _iter_structural_edges(call_edges):
        if rel_type not in CALL_REL_TYPES or caller not in info or callee not in info:
            continue
        if handle_fan_in[callee] > _EPS:
            handler_call_fan_out[caller] += conf

    # Fluent self-return: count methods M of class C whose return type is C
    # itself (a builder/fluent-chain shape — QuerySet.filter()→QuerySet,
    # Context.invoke()→Context). Cross-references HAS_API (C→M) with the
    # USES_TYPE(kind=return) edge (M→C) without any name-pattern; both are
    # AST-visible static facts already stored as edges.
    api_owner_of: dict[str, set[str]] = defaultdict(set)
    method_return_type: dict[str, set[str]] = defaultdict(set)
    for caller, callee, rel_type, _conf, kind in _iter_structural_edges(call_edges):
        if rel_type in {"HAS_API", "INHERITED_API"} and caller in info and callee in info:
            api_owner_of[callee].add(caller)
        elif (
            rel_type == "USES_TYPE"
            and kind == "return"
            and caller in info
            and callee in info
        ):
            method_return_type[caller].add(callee)
    fluent_self_return_count: dict[str, int] = defaultdict(int)
    for method_uid, return_types in method_return_type.items():
        owners = api_owner_of.get(method_uid, set())
        # Method belongs to (HAS_API'd by) class C AND returns C → fluent on C.
        for owner_uid in owners & return_types:
            fluent_self_return_count[owner_uid] += 1

    depth_by_uid = _depth_from_public_full_graph(call_edges, set(info))

    rows: list[SymbolRow] = []
    for uid, meta in info.items():
        callers = call_in[uid]
        callees = call_out[uid]
        my_pkg = meta["package"]
        cross_in = sum(1 for c in callers if c in info and info[c]["package"] != my_pkg)
        cross_out = sum(1 for c in callees if c in info and info[c]["package"] != my_pkg)
        doc_signal = doc_signal_by_uid.get(uid, {})
        rows.append(
            SymbolRow(
                uid=uid,
                kind=meta["kind"],
                fan_in=len(callers),
                fan_out=len(callees),
                cross_package_in=cross_in,
                cross_package_out=cross_out,
                depth_from_public=depth_by_uid[uid],
                doc_anchor_count=int(doc_counts.get(uid, 0)),
                import_in=int(import_in_per_uid.get(uid, 0)),
                doc_definition_weight=float(doc_signal.get("definition", 0.0)),
                doc_reference_weight=float(doc_signal.get("reference", 0.0)),
                doc_example_weight=float(doc_signal.get("example", 0.0)),
                call_fan_in=call_fan_in[uid],
                call_fan_out=call_fan_out[uid],
                type_fan_in=type_fan_in[uid],
                type_fan_out=type_fan_out[uid],
                type_fan_in_param=type_fan_in_param[uid],
                type_fan_in_isinstance=type_fan_in_isinstance[uid],
                type_fan_in_return=type_fan_in_return[uid],
                type_fan_out_return=type_fan_out_return[uid],
                api_fan_in=api_fan_in[uid],
                api_fan_out=api_fan_out[uid],
                inject_fan_in=inject_fan_in[uid],
                depend_fan_in=depend_fan_in[uid],
                depend_fan_out=depend_fan_out[uid],
                handle_fan_in=handle_fan_in[uid],
                handle_fan_out=handle_fan_out[uid],
                handler_call_fan_out=handler_call_fan_out[uid],
                decorated_in=decorated_in[uid],
                decorated_out=decorated_out[uid],
                construct_fan_out=construct_fan_out[uid],
                fluent_self_return_count=fluent_self_return_count.get(uid, 0),
                decorator_arg_ref_count=decorator_arg_ref_count.get(uid, 0),
                attr_reads_fan_out=attr_reads_fan_out[uid],
                attr_writes_fan_out=attr_writes_fan_out[uid],
                attr_writes_subscript_fan_out=attr_writes_subscript_fan_out[uid],
                reexport_in=int(reexport_in_per_uid.get(uid, 0)),
                is_proxy_binding=uid in proxy_uids,
                external_call_fan_out=float(external_call_fan_out_per_uid.get(uid, 0.0)),
                external_import_fan_out=float(
                    external_import_fan_out_by_file.get(meta["file_path"], 0.0)
                ),
                external_root_count=int(external_root_count_per_uid.get(uid, 0)),
                external_integration_call_fan_out=float(
                    external_integration_call_fan_out_per_uid.get(uid, 0.0)
                ),
                external_integration_import_fan_out=float(
                    external_integration_import_fan_out_by_file.get(meta["file_path"], 0.0)
                ),
                external_integration_root_count=int(
                    external_integration_root_count_per_uid.get(uid, 0)
                ),
                inherits_builtin_exception=bool(meta.get("inherits_builtin_exception")),
                returns_function_expression=bool(meta.get("returns_function_expression")),
                returns_mapping=bool(meta.get("returns_mapping")),
                returns_sequence=bool(meta.get("returns_sequence")),
                returns_constructed_type=bool(meta.get("returns_constructed_type")),
                iterates_attr_call=bool(meta.get("iterates_attr_call")),
                assembles_mapping_in_loop=bool(meta.get("assembles_mapping_in_loop")),
            )
        )
    return rows


def _bfs_depths(
    out_edges: dict[str, set[str]],
    sources: set[str],
) -> dict[str, int]:
    if not sources:
        return {}
    depths: dict[str, int] = {src: 0 for src in sources}
    queue: deque[str] = deque(sources)
    while queue:
        u = queue.popleft()
        for v in out_edges.get(u, ()):
            if v not in depths:
                depths[v] = depths[u] + 1
                queue.append(v)
    return depths


def _depth_from_public_full_graph(
    call_edges: Sequence[tuple[str, ...]],
    pass1_uids: set[str],
) -> dict[str, int]:
    """BFS distance from public call-graph roots on the full workspace graph (F13).

    Pass-1 keeps test/doc paths out of role assignment, but entrypoint reachability
    must traverse those callers so ``depth_from_public`` is not collapsed to zero.
    """
    full_call_out: dict[str, set[str]] = defaultdict(set)
    full_call_fan_in: dict[str, float] = defaultdict(float)
    for caller, callee, rel_type, conf, _kind in _iter_structural_edges(call_edges):
        if caller == callee or rel_type not in CALL_REL_TYPES:
            continue
        full_call_out[caller].add(callee)
        full_call_fan_in[callee] += conf

    graph_nodes = set(full_call_fan_in) | set(full_call_out)
    public_uids = {
        uid
        for uid in graph_nodes
        if full_call_fan_in[uid] <= _EPS and full_call_out[uid]
    }
    depths = _bfs_depths(full_call_out, public_uids)
    unreachable_depth = max(depths.values()) + 1 if depths else 0
    return {uid: depths.get(uid, unreachable_depth) for uid in pass1_uids}


def extract_symbol_rows(db, workspace_id: str) -> list[SymbolRow]:
    """Read every symbol's structural facts from Neo4j and assemble them."""
    symbols = _query_symbols(db, workspace_id)
    edges = _query_structural_edges(db, workspace_id)
    doc_counts = _query_doc_anchor_counts(db, workspace_id)
    doc_signals = _query_doc_anchor_signals(db, workspace_id)
    import_in = _query_file_import_in_counts(db, workspace_id)
    reexport_in = _query_reexport_in_counts(db, workspace_id)
    proxy_uids = _query_proxy_binding_uids(db, workspace_id)
    external_call_fan, external_root_count = _query_external_call_fan(db, workspace_id)
    external_import_by_file = _query_external_import_fan_by_file(db, workspace_id)
    (
        external_integration_call_fan,
        external_integration_root_count,
    ) = _query_external_integration_call_fan(db, workspace_id)
    external_integration_import_by_file = _query_external_integration_import_fan_by_file(
        db, workspace_id
    )
    return assemble_symbol_rows(
        symbols,
        edges,
        doc_counts,
        import_in,
        doc_signals,
        proxy_uids,
        reexport_in,
        external_call_fan,
        external_root_count,
        external_import_by_file,
        external_integration_call_fan,
        external_integration_root_count,
        external_integration_import_by_file,
    )


def _query_symbols(db, workspace_id: str) -> list[tuple[str, str, str, bool, bool, bool, bool, bool]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            WHERE NOT any(noise IN $noise_patterns WHERE f.path CONTAINS noise)
            RETURN s.uid AS uid,
                   coalesce(s.kind, '') AS kind,
                   coalesce(f.path, '') AS file_path,
                   coalesce(s.inherits_builtin_exception, false) AS inherits_builtin_exception,
                   coalesce(s.returns_function_expression, false) AS returns_function_expression,
                   coalesce(s.returns_mapping, false) AS returns_mapping,
                   coalesce(s.returns_sequence, false) AS returns_sequence,
                   coalesce(s.returns_constructed_type, false) AS returns_constructed_type,
                   coalesce(s.iterates_attr_call, false) AS iterates_attr_call,
                   coalesce(s.assembles_mapping_in_loop, false) AS assembles_mapping_in_loop
            """,
            workspace_id=workspace_id,
            noise_patterns=list(NOISE_PATH_PATTERNS),
        )
        return [
            (
                r["uid"],
                r["kind"],
                r["file_path"],
                bool(r["inherits_builtin_exception"]),
                bool(r["returns_function_expression"]),
                bool(r["returns_mapping"]),
                bool(r["returns_sequence"]),
                bool(r["returns_constructed_type"]),
                bool(r["iterates_attr_call"]),
                bool(r["assembles_mapping_in_loop"]),
            )
            for r in result
            if r["uid"]
        ]


def _query_structural_edges(db, workspace_id: str) -> list[tuple[str, str, str, float, str]]:
    rel_union = "|".join(STRUCTURAL_REL_TYPES)
    with db.driver.session() as session:
        result = session.run(
            f"""
            MATCH (caller:Symbol)-[r:{rel_union}]->(callee:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN caller.uid AS caller_uid,
                   callee.uid AS callee_uid,
                   type(r) AS rel_type,
                   r.confidence AS confidence,
                   coalesce(r.kind, '') AS kind
            """,
            workspace_id=workspace_id,
        )
        rows: list[tuple[str, str, str, float, str]] = []
        for record in result:
            caller = record["caller_uid"]
            callee = record["callee_uid"]
            if not caller or not callee or caller == callee:
                continue
            rel_type = record["rel_type"]
            conf = _edge_confidence(rel_type, record["confidence"], record["kind"] or "")
            rows.append((caller, callee, rel_type, conf, record["kind"] or ""))
        return rows


def _query_call_edges(db, workspace_id: str) -> list[tuple[str, str]]:
    return [
        (caller, callee)
        for caller, callee, rel_type, _conf, _kind in _query_structural_edges(db, workspace_id)
        if rel_type in CALL_REL_TYPES
    ]


def _query_reexport_in_counts(db, workspace_id: str) -> dict[str, int]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (:File {workspace_id: $workspace_id})-[r:RE_EXPORTS]->(sym:Symbol)
            RETURN sym.uid AS uid, count(r) AS c
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"]: int(r["c"]) for r in result if r.get("uid")}


def _query_proxy_binding_uids(db, workspace_id: str) -> set[str]:
    with db.driver.session() as session:
        # Workspace is scoped via File-[:CONTAINS]->Symbol; Symbol nodes have no
        # workspace_id property of their own, so matching {workspace_id: ...} on
        # Symbol returned nothing — every proxy_binding lost its is_proxy_binding
        # flag and dropped out of the cascade. Match through the file.
        result = session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding'})
            RETURN p.uid AS uid
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"] for r in result if r.get("uid")}


def _query_external_call_fan(db, workspace_id: str) -> tuple[dict[str, float], dict[str, int]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (s:Symbol)-[r:CALLS_EXTERNAL]->(e:ExternalPkg)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN s.uid AS uid,
                   sum(coalesce(r.confidence, 1.0)) AS fan,
                   count(DISTINCT e) AS roots
            """,
            workspace_id=workspace_id,
        )
        fan: dict[str, float] = {}
        roots: dict[str, int] = {}
        for record in result:
            uid = record.get("uid")
            if not uid:
                continue
            fan[uid] = float(record.get("fan") or 0.0)
            roots[uid] = int(record.get("roots") or 0)
        return fan, roots


def _query_external_import_fan_by_file(db, workspace_id: str) -> dict[str, float]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})-[r:IMPORTS_EXTERNAL]->(:ExternalPkg)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN f.path AS path, count(DISTINCT r) AS fan
            """,
            workspace_id=workspace_id,
        )
        return {
            str(record["path"]): float(record.get("fan") or 0.0)
            for record in result
            if record.get("path")
        }


def _query_external_integration_call_fan(
    db, workspace_id: str
) -> tuple[dict[str, float], dict[str, int]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (s:Symbol)-[r:CALLS_EXTERNAL]->(e:ExternalPkg)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
              AND NOT e.root IN $plumbing
            RETURN s.uid AS uid,
                   sum(coalesce(r.confidence, 1.0)) AS fan,
                   count(DISTINCT e) AS roots
            """,
            workspace_id=workspace_id,
            plumbing=list(EXTERNAL_INTEGRATION_PLUMBING_ROOTS),
        )
        fan: dict[str, float] = {}
        roots: dict[str, int] = {}
        for record in result:
            uid = record.get("uid")
            if not uid:
                continue
            fan[uid] = float(record.get("fan") or 0.0)
            roots[uid] = int(record.get("roots") or 0)
        return fan, roots


def _query_external_integration_import_fan_by_file(db, workspace_id: str) -> dict[str, float]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (f:File {workspace_id: $workspace_id})-[r:IMPORTS_EXTERNAL]->(e:ExternalPkg)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
              AND NOT e.root IN $plumbing
            RETURN f.path AS path, count(DISTINCT e) AS fan
            """,
            workspace_id=workspace_id,
            plumbing=list(EXTERNAL_INTEGRATION_PLUMBING_ROOTS),
        )
        return {
            str(record["path"]): float(record.get("fan") or 0.0)
            for record in result
            if record.get("path")
        }


def _query_file_import_in_counts(db, workspace_id: str) -> dict[str, int]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (target:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
            OPTIONAL MATCH (importer:File)-[r:IMPORTS]->(target)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            WITH s, count(DISTINCT importer) AS imp_in
            RETURN s.uid AS uid, imp_in
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"]: int(r["imp_in"]) for r in result if r["uid"]}


def _query_doc_anchor_counts(db, workspace_id: str) -> dict[str, int]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (a:DocAnchor)-[r:COVERS]->(s:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            RETURN s.uid AS uid, count(r) AS doc_count
            """,
            workspace_id=workspace_id,
        )
        return {r["uid"]: int(r["doc_count"]) for r in result if r["uid"]}


def _query_doc_anchor_signals(db, workspace_id: str) -> dict[str, dict[str, float]]:
    with db.driver.session() as session:
        result = session.run(
            """
            MATCH (a:DocAnchor)-[r:COVERS]->(s:Symbol)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            WITH s.uid AS uid,
                 coalesce(r.anchor_type, 'reference') AS anchor_type,
                 coalesce(r.confidence, 0.6) * coalesce(r.primary_bias, 0.6) AS weight
            RETURN uid,
                   sum(CASE WHEN anchor_type IN ['definition', 'warning', 'deprecated'] THEN weight ELSE 0.0 END) AS definition_weight,
                   sum(CASE WHEN anchor_type = 'reference' THEN weight ELSE 0.0 END) AS reference_weight,
                   sum(CASE WHEN anchor_type = 'example' THEN weight ELSE 0.0 END) AS example_weight
            """,
            workspace_id=workspace_id,
        )
        return {
            r["uid"]: {
                "definition": float(r["definition_weight"] or 0.0),
                "reference": float(r["reference_weight"] or 0.0),
                "example": float(r["example_weight"] or 0.0),
            }
            for r in result
            if r["uid"]
        }


def persist_role_taxonomy(
    db,
    workspace_id: str,
    summary: RoleAssignmentSummary,
    assignments: dict[str, SymbolRoleAssignment],
    *,
    structural_rows: Sequence[SymbolRow] | None = None,
    present_roles: dict[str, int] | None = None,
    batch_size: int = 1000,
) -> None:
    """Save assignment summary + catalog on Workspace and roles on Symbol nodes."""
    payload = json.dumps(summary.to_dict(), sort_keys=True)
    catalog_dict = build_role_catalog(present_roles or summary.present_roles).to_dict()
    catalog_dict = merge_preloaded_mechanisms_into_role_catalog(catalog_dict)
    catalog_payload = json.dumps(catalog_dict, sort_keys=True)
    with db.driver.session() as session:
        session.run(
            """
            MERGE (w:Workspace {id: $workspace_id})
            SET w.role_taxonomy_json = $payload,
                w.role_taxonomy_schema_version = $schema_version,
                w.role_catalog_json = $catalog_payload,
                w.role_catalog_schema_version = $catalog_schema_version,
                w.role_taxonomy_updated_at = timestamp()
            """,
            workspace_id=workspace_id,
            payload=payload,
            catalog_payload=catalog_payload,
            schema_version=ROLE_TAXONOMY_SCHEMA_VERSION,
            catalog_schema_version=ROLE_CATALOG_SCHEMA_VERSION,
        )

        profile_items = []
        for row in structural_rows or ():
            asn = assignments.get(row.uid)
            supporting = list(asn.supporting) if asn else []
            profile_items.append(
                {
                    "uid": row.uid,
                    "primary": asn.primary if asn else "",
                    "supporting_json": json.dumps(supporting),
                    "call_fan_in": round(row.effective_call_fan_in(), 4),
                    "call_fan_out": round(row.effective_call_fan_out(), 4),
                    "type_fan_in": round(row.type_fan_in, 4),
                }
            )
        for offset in range(0, len(profile_items), batch_size):
            batch = profile_items[offset : offset + batch_size]
            session.run(
                """
                UNWIND $items AS item
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol {uid: item.uid})
                SET s.call_fan_in = item.call_fan_in,
                    s.call_fan_out = item.call_fan_out,
                    s.type_fan_in = item.type_fan_in,
                    s.derived_primary_role = item.primary,
                    s.derived_supporting_roles_json = item.supporting_json,
                    s.derived_role_id = null
                """,
                items=batch,
                workspace_id=workspace_id,
            )


def get_role_taxonomy(db, workspace_id: str) -> dict | None:
    with db.driver.session() as session:
        row = session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})
            RETURN w.role_taxonomy_json AS payload
            """,
            workspace_id=workspace_id,
        ).single()
    if not row or not row["payload"]:
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def get_role_catalog(db, workspace_id: str) -> dict | None:
    with db.driver.session() as session:
        row = session.run(
            """
            MATCH (w:Workspace {id: $workspace_id})
            RETURN w.role_catalog_json AS payload
            """,
            workspace_id=workspace_id,
        ).single()
    if not row or not row["payload"]:
        return None
    try:
        data = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def derive_and_persist_role_taxonomy(
    db,
    workspace_id: str,
    *,
    seed: int = 0,
) -> RoleAssignmentSummary:
    """Run Pass 1 end-to-end: extract → cascade assign → persist."""
    del seed  # deterministic cascade; kept for call-site compatibility
    all_rows = extract_symbol_rows(db, workspace_id)
    summary, assignments, present = assign_role_taxonomy(all_rows)
    persist_role_taxonomy(
        db,
        workspace_id,
        summary,
        assignments,
        structural_rows=all_rows,
        present_roles=present,
    )
    return summary


__all__ = [
    "ROLE_CATALOG_SCHEMA_VERSION",
    "ROLE_TAXONOMY_SCHEMA_VERSION",
    "RoleAssignmentSummary",
    "RoleCatalog",
    "SymbolRow",
    "assemble_symbol_rows",
    "assign_role_taxonomy",
    "build_role_catalog",
    "derive_and_persist_role_taxonomy",
    "extract_symbol_rows",
    "filter_clustering_rows",
    "get_role_catalog",
    "get_role_taxonomy",
    "persist_role_taxonomy",
    "role_catalog_roles",
]
