"""Pass 1: derive per-repository roles from call-graph topology via L1/L2 cascade.

Replaces flat k-means clustering with discriminator-first assignment
(``context_engine.indexer.role_cascade``). Output:

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
from typing import cast

from context_engine.indexer.external_boundary import EXTERNAL_INTEGRATION_PLUMBING_ROOTS
from context_engine.indexer.role_cascade import (
    FanProfile,
    SymbolRoleAssignment,
    assign_all,
    detect_present_roles,
    role_catalog_roles,
)
from context_engine.indexer.signal_constants import NOISE_PATH_PATTERNS

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
    "RESOLVES_ATTR",
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
    "RESOLVES_ATTR": 1.0,
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
    proxy_context_bind_fan_out: float = 0.0
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
    assignments = assign_all(cast(list[FanProfile], assign_rows))
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


_SYMBOL_FLAG_FIELDS = (
    "inherits_builtin_exception",
    "returns_function_expression",
    "returns_mapping",
    "returns_sequence",
    "returns_constructed_type",
    "iterates_attr_call",
    "assembles_mapping_in_loop",
)


def _symbol_flags_from_tuple(sym: tuple[str, ...]) -> dict[str, bool]:
    return {
        field: bool(sym[index]) if len(sym) > index else False
        for index, field in enumerate(_SYMBOL_FLAG_FIELDS, start=3)
    }


def _symbol_info_from_raw(symbols: Sequence[tuple[str, ...]]) -> dict[str, dict]:
    info: dict[str, dict] = {}
    for sym in symbols:
        uid, kind, file_path = sym[0], sym[1], sym[2]
        info[uid] = {
            "uid": uid,
            "kind": kind or "",
            "file_path": file_path or "",
            "package": os.path.dirname(file_path or ""),
            **_symbol_flags_from_tuple(sym),
        }
    return info


@dataclass
class _EdgeFanAccumulators:
    call_out: dict[str, set[str]]
    call_in: dict[str, set[str]]
    call_fan_in: dict[str, float]
    call_fan_out: dict[str, float]
    type_fan_in: dict[str, float]
    type_fan_out: dict[str, float]
    type_fan_in_param: dict[str, float]
    type_fan_in_isinstance: dict[str, float]
    type_fan_in_return: dict[str, float]
    type_fan_out_return: dict[str, float]
    api_fan_in: dict[str, float]
    api_fan_out: dict[str, float]
    inject_fan_in: dict[str, float]
    depend_fan_in: dict[str, float]
    depend_fan_out: dict[str, float]
    handle_fan_in: dict[str, float]
    handle_fan_out: dict[str, float]
    decorated_in: dict[str, float]
    decorated_out: dict[str, float]
    construct_fan_out: dict[str, float]
    proxy_context_bind_fan_out: dict[str, float]
    decorator_arg_ref_count: dict[str, int]
    attr_reads_fan_out: dict[str, float]
    attr_writes_fan_out: dict[str, float]
    attr_writes_subscript_fan_out: dict[str, float]

    @classmethod
    def for_uids(cls, uids: set[str]) -> _EdgeFanAccumulators:
        return cls(
            call_out={uid: set() for uid in uids},
            call_in={uid: set() for uid in uids},
            call_fan_in=defaultdict(float),
            call_fan_out=defaultdict(float),
            type_fan_in=defaultdict(float),
            type_fan_out=defaultdict(float),
            type_fan_in_param=defaultdict(float),
            type_fan_in_isinstance=defaultdict(float),
            type_fan_in_return=defaultdict(float),
            type_fan_out_return=defaultdict(float),
            api_fan_in=defaultdict(float),
            api_fan_out=defaultdict(float),
            inject_fan_in=defaultdict(float),
            depend_fan_in=defaultdict(float),
            depend_fan_out=defaultdict(float),
            handle_fan_in=defaultdict(float),
            handle_fan_out=defaultdict(float),
            decorated_in=defaultdict(float),
            decorated_out=defaultdict(float),
            construct_fan_out=defaultdict(float),
            proxy_context_bind_fan_out=defaultdict(float),
            decorator_arg_ref_count=defaultdict(int),
            attr_reads_fan_out=defaultdict(float),
            attr_writes_fan_out=defaultdict(float),
            attr_writes_subscript_fan_out=defaultdict(float),
        )

    def _accumulate_decorated_by(
        self,
        caller_in: bool,
        callee_in: bool,
        caller: str,
        callee: str,
        conf: float,
    ) -> None:
        if caller_in:
            self.decorated_out[caller] += conf
        if callee_in:
            self.decorated_in[callee] += conf

    def _accumulate_injects(self, callee_in: bool, callee: str, conf: float) -> None:
        if callee_in:
            self.inject_fan_in[callee] += conf

    def _accumulate_instantiates(self, caller_in: bool, caller: str, conf: float) -> None:
        if caller_in:
            self.construct_fan_out[caller] += conf

    def _accumulate_resolves_attr(self, caller_in: bool, caller: str, conf: float) -> None:
        if caller_in:
            self.proxy_context_bind_fan_out[caller] += conf

    def _accumulate_composes(self, caller_in: bool, caller: str) -> None:
        if caller_in:
            self.decorator_arg_ref_count[caller] += 1

    def _accumulate_reads_attr(self, caller_in: bool, caller: str, conf: float) -> None:
        if caller_in:
            self.attr_reads_fan_out[caller] += conf

    def _accumulate_writes_attr(
        self,
        caller_in: bool,
        caller: str,
        conf: float,
        kind: str,
    ) -> None:
        if not caller_in:
            return
        self.attr_writes_fan_out[caller] += conf
        if kind in ("write_subscript", "write_subscript_local"):
            self.attr_writes_subscript_fan_out[caller] += conf

    def accumulate(
        self,
        caller: str,
        callee: str,
        rel_type: str,
        conf: float,
        kind: str,
        info: dict[str, dict],
    ) -> None:
        caller_in = caller in info
        callee_in = callee in info
        if not caller_in and not callee_in:
            return
        if caller == callee:
            return
        if rel_type in CALL_REL_TYPES:
            self._accumulate_call(caller_in, callee_in, caller, callee, conf)
            return
        if rel_type == "USES_TYPE":
            self._accumulate_uses_type(caller_in, callee_in, caller, callee, conf, kind)
        elif rel_type in {"HAS_API", "INHERITED_API"}:
            self._accumulate_api(caller_in, callee_in, caller, callee, conf)
        elif rel_type == "INJECTS":
            self._accumulate_injects(callee_in, callee, conf)
        elif rel_type == "DEPENDS_ON":
            self._accumulate_depends_on(caller_in, callee_in, caller, callee, conf)
        elif rel_type == "HANDLES":
            self._accumulate_handles(caller_in, callee_in, caller, callee, conf)
        elif rel_type == "DECORATED_BY":
            self._accumulate_decorated_by(caller_in, callee_in, caller, callee, conf)
        elif rel_type == "INSTANTIATES":
            self._accumulate_instantiates(caller_in, caller, conf)
        elif rel_type == "RESOLVES_ATTR":
            self._accumulate_resolves_attr(caller_in, caller, conf)
        elif rel_type == "COMPOSES":
            self._accumulate_composes(caller_in, caller)
        elif rel_type == "READS_ATTR":
            self._accumulate_reads_attr(caller_in, caller, conf)
        elif rel_type == "WRITES_ATTR":
            self._accumulate_writes_attr(caller_in, caller, conf, kind)

    def _accumulate_call(
        self,
        caller_in: bool,
        callee_in: bool,
        caller: str,
        callee: str,
        conf: float,
    ) -> None:
        if caller_in:
            self.call_out[caller].add(callee)
            self.call_fan_out[caller] += conf
        if callee_in:
            self.call_in[callee].add(caller)
            self.call_fan_in[callee] += conf

    def _accumulate_uses_type(
        self,
        caller_in: bool,
        callee_in: bool,
        caller: str,
        callee: str,
        conf: float,
        kind: str,
    ) -> None:
        if caller_in:
            self.type_fan_out[caller] += conf
            if kind == "return":
                self.type_fan_out_return[caller] += conf
        if callee_in:
            self.type_fan_in[callee] += conf
            if kind in {"param", "annotation"}:
                self.type_fan_in_param[callee] += conf
            elif kind == "isinstance":
                self.type_fan_in_isinstance[callee] += conf
            elif kind == "return":
                self.type_fan_in_return[callee] += conf

    def _accumulate_api(
        self,
        caller_in: bool,
        callee_in: bool,
        caller: str,
        callee: str,
        conf: float,
    ) -> None:
        if caller_in:
            self.api_fan_out[caller] += conf
        if callee_in:
            self.api_fan_in[callee] += conf

    def _accumulate_depends_on(
        self,
        caller_in: bool,
        callee_in: bool,
        caller: str,
        callee: str,
        conf: float,
    ) -> None:
        if caller_in:
            self.depend_fan_out[caller] += conf
        if callee_in:
            self.depend_fan_in[callee] += conf

    def _accumulate_handles(
        self,
        caller_in: bool,
        callee_in: bool,
        caller: str,
        callee: str,
        conf: float,
    ) -> None:
        if caller_in:
            self.handle_fan_out[caller] += conf
        if callee_in:
            self.handle_fan_in[callee] += conf


def _compute_handler_call_fan_out(
    call_edges: Sequence[tuple[str, ...]],
    info: dict[str, dict],
    handle_fan_in: dict[str, float],
) -> dict[str, float]:
    handler_call_fan_out: dict[str, float] = defaultdict(float)
    for caller, callee, rel_type, conf, _kind in _iter_structural_edges(call_edges):
        if rel_type not in CALL_REL_TYPES or caller not in info or callee not in info:
            continue
        if handle_fan_in[callee] > _EPS:
            handler_call_fan_out[caller] += conf
    return handler_call_fan_out


def _compute_fluent_self_return_counts(
    call_edges: Sequence[tuple[str, ...]],
    info: dict[str, dict],
) -> dict[str, int]:
    api_owner_of: dict[str, set[str]] = defaultdict(set)
    method_return_type: dict[str, set[str]] = defaultdict(set)
    for caller, callee, rel_type, _conf, kind in _iter_structural_edges(call_edges):
        if rel_type in {"HAS_API", "INHERITED_API"} and caller in info and callee in info:
            api_owner_of[callee].add(caller)
        elif rel_type == "USES_TYPE" and kind == "return" and caller in info and callee in info:
            method_return_type[caller].add(callee)
    fluent_self_return_count: dict[str, int] = defaultdict(int)
    for method_uid, return_types in method_return_type.items():
        for owner_uid in api_owner_of.get(method_uid, set()) & return_types:
            fluent_self_return_count[owner_uid] += 1
    return fluent_self_return_count


@dataclass(frozen=True)
class _ExternalFanMetrics:
    call_fan_out_per_uid: dict[str, float]
    root_count_per_uid: dict[str, int]
    import_fan_out_by_file: dict[str, float]
    integration_call_fan_out_per_uid: dict[str, float]
    integration_root_count_per_uid: dict[str, int]
    integration_import_fan_out_by_file: dict[str, float]


def _symbol_row_from_meta(
    uid: str,
    meta: dict,
    *,
    fans: _EdgeFanAccumulators,
    depth_by_uid: dict[str, int],
    info: dict[str, dict],
    doc_counts: dict[str, int],
    doc_signal_by_uid: dict[str, dict[str, float]],
    import_in_per_uid: dict[str, int],
    reexport_in_per_uid: dict[str, int],
    proxy_uids: set[str],
    external_fans: _ExternalFanMetrics,
    handler_call_fan_out: dict[str, float],
    fluent_self_return_count: dict[str, int],
) -> SymbolRow:
    callers = fans.call_in[uid]
    callees = fans.call_out[uid]
    my_pkg = meta["package"]
    cross_in = sum(1 for c in callers if c in info and info[c]["package"] != my_pkg)
    cross_out = sum(1 for c in callees if c in info and info[c]["package"] != my_pkg)
    doc_signal = doc_signal_by_uid.get(uid, {})
    return SymbolRow(
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
        call_fan_in=fans.call_fan_in[uid],
        call_fan_out=fans.call_fan_out[uid],
        type_fan_in=fans.type_fan_in[uid],
        type_fan_out=fans.type_fan_out[uid],
        type_fan_in_param=fans.type_fan_in_param[uid],
        type_fan_in_isinstance=fans.type_fan_in_isinstance[uid],
        type_fan_in_return=fans.type_fan_in_return[uid],
        type_fan_out_return=fans.type_fan_out_return[uid],
        api_fan_in=fans.api_fan_in[uid],
        api_fan_out=fans.api_fan_out[uid],
        inject_fan_in=fans.inject_fan_in[uid],
        depend_fan_in=fans.depend_fan_in[uid],
        depend_fan_out=fans.depend_fan_out[uid],
        handle_fan_in=fans.handle_fan_in[uid],
        handle_fan_out=fans.handle_fan_out[uid],
        handler_call_fan_out=handler_call_fan_out[uid],
        decorated_in=fans.decorated_in[uid],
        decorated_out=fans.decorated_out[uid],
        construct_fan_out=fans.construct_fan_out[uid],
        proxy_context_bind_fan_out=fans.proxy_context_bind_fan_out[uid],
        fluent_self_return_count=fluent_self_return_count.get(uid, 0),
        decorator_arg_ref_count=fans.decorator_arg_ref_count.get(uid, 0),
        attr_reads_fan_out=fans.attr_reads_fan_out[uid],
        attr_writes_fan_out=fans.attr_writes_fan_out[uid],
        attr_writes_subscript_fan_out=fans.attr_writes_subscript_fan_out[uid],
        reexport_in=int(reexport_in_per_uid.get(uid, 0)),
        is_proxy_binding=uid in proxy_uids,
        external_call_fan_out=float(external_fans.call_fan_out_per_uid.get(uid, 0.0)),
        external_import_fan_out=float(
            external_fans.import_fan_out_by_file.get(meta["file_path"], 0.0)
        ),
        external_root_count=int(external_fans.root_count_per_uid.get(uid, 0)),
        external_integration_call_fan_out=float(
            external_fans.integration_call_fan_out_per_uid.get(uid, 0.0)
        ),
        external_integration_import_fan_out=float(
            external_fans.integration_import_fan_out_by_file.get(meta["file_path"], 0.0)
        ),
        external_integration_root_count=int(
            external_fans.integration_root_count_per_uid.get(uid, 0)
        ),
        inherits_builtin_exception=bool(meta.get("inherits_builtin_exception")),
        returns_function_expression=bool(meta.get("returns_function_expression")),
        returns_mapping=bool(meta.get("returns_mapping")),
        returns_sequence=bool(meta.get("returns_sequence")),
        returns_constructed_type=bool(meta.get("returns_constructed_type")),
        iterates_attr_call=bool(meta.get("iterates_attr_call")),
        assembles_mapping_in_loop=bool(meta.get("assembles_mapping_in_loop")),
    )


def assemble_symbol_rows(
    symbols: Sequence[tuple[str, ...]],
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

    info = _symbol_info_from_raw(symbols)
    fans = _EdgeFanAccumulators.for_uids(set(info))
    for caller, callee, rel_type, conf, kind in _iter_structural_edges(call_edges):
        fans.accumulate(caller, callee, rel_type, conf, kind, info)

    handler_call_fan_out = _compute_handler_call_fan_out(call_edges, info, fans.handle_fan_in)
    fluent_self_return_count = _compute_fluent_self_return_counts(call_edges, info)
    depth_by_uid = _depth_from_public_full_graph(call_edges, set(info))
    external_fans = _ExternalFanMetrics(
        call_fan_out_per_uid=external_call_fan_out_per_uid,
        root_count_per_uid=external_root_count_per_uid,
        import_fan_out_by_file=external_import_fan_out_by_file,
        integration_call_fan_out_per_uid=external_integration_call_fan_out_per_uid,
        integration_root_count_per_uid=external_integration_root_count_per_uid,
        integration_import_fan_out_by_file=external_integration_import_fan_out_by_file,
    )

    return [
        _symbol_row_from_meta(
            uid,
            meta,
            fans=fans,
            depth_by_uid=depth_by_uid,
            info=info,
            doc_counts=doc_counts,
            doc_signal_by_uid=doc_signal_by_uid,
            import_in_per_uid=import_in_per_uid,
            reexport_in_per_uid=reexport_in_per_uid,
            proxy_uids=proxy_uids,
            external_fans=external_fans,
            handler_call_fan_out=handler_call_fan_out,
            fluent_self_return_count=fluent_self_return_count,
        )
        for uid, meta in info.items()
    ]


def _bfs_depths(
    out_edges: dict[str, set[str]],
    sources: set[str],
) -> dict[str, int]:
    if not sources:
        return {}
    depths: dict[str, int] = dict.fromkeys(sources, 0)
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
        uid for uid in graph_nodes if full_call_fan_in[uid] <= _EPS and full_call_out[uid]
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
        cast(Sequence[tuple[str, ...]], symbols),
        cast(Sequence[tuple[str, ...]], edges),
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


def _query_symbols(
    db, workspace_id: str
) -> list[tuple[str, str, str, bool, bool, bool, bool, bool, bool, bool]]:
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
