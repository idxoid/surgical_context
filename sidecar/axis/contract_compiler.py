"""L3 structural contract compiler.

Contracts sit above axis bits and container kinds, but below roles and
intent. This module proves small structural patterns only; it does not know
benchmark labels, framework names, or question text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from sidecar.axis.container_kind import ContainerKindMatch
from sidecar.axis.query_plan import AxisQueryRequest, AxisRequirement, TraversalMode
from sidecar.axis.schema import AxisFact, AxisName, AxisProfile

ContractPredicate = Callable[
    [AxisProfile, tuple[ContainerKindMatch, ...]],
    "AxisContractMatch | None",
]

_PREDICATES: dict[str, ContractPredicate] = {}


@dataclass(frozen=True)
class _ContractSpec:
    """Declarative shape of one contract, used by both compile and diagnose.

    Keeping this separate from the predicate body lets ``diagnose()`` produce a
    per-bit diagnostic for *every* registered contract without each predicate
    re-implementing the same teardown logic. The predicate is still the source
    of truth for what is proven; the spec is the source of truth for what is
    required.
    """

    contract: str
    container_kinds: tuple[str, ...]
    required_bits: tuple["AxisRequirement", ...]
    payload_rule_name: str | None = None
    payload_rule: Callable[[AxisProfile], bool] | None = None


_SPECS: dict[str, _ContractSpec] = {}


def register_contract(contract: str) -> Callable[[ContractPredicate], ContractPredicate]:
    """Register one structural contract predicate."""

    def deco(fn: ContractPredicate) -> ContractPredicate:
        if contract in _PREDICATES:
            raise ValueError(f"Contract already registered: {contract}")
        _PREDICATES[contract] = fn
        return fn

    return deco


def _register_spec(spec: _ContractSpec) -> None:
    if spec.contract in _SPECS:
        raise ValueError(f"Contract spec already registered: {spec.contract}")
    _SPECS[spec.contract] = spec


def _req(axis: AxisName, bit: str) -> AxisRequirement:
    return AxisRequirement(axis, bit)


def _kind_match(
    matches: Sequence[ContainerKindMatch],
    *kinds: str,
) -> ContainerKindMatch | None:
    wanted = set(kinds)
    for match in matches:
        if match.kind in wanted:
            return match
    return None


def _bits_present(profile: AxisProfile, requirements: Iterable[AxisRequirement]) -> bool:
    return all(profile.has(req.axis, req.bit) for req in requirements)


def _facts(profile: AxisProfile, axis: AxisName, bit: str) -> list[AxisFact]:
    return [fact for fact in profile.facts if fact.axis == axis and fact.bit == bit]


def _shared_write_iteration_container(profile: AxisProfile) -> str | None:
    writes = {
        str(fact.payload.get("container") or "")
        for fact in _facts(profile, "dfg", "container_write_value")
        if fact.payload.get("container")
    }
    iterations = {
        str(fact.payload.get("iterable") or "")
        for fact in _facts(profile, "dfg", "iteration_source")
        if fact.payload.get("iterable")
    }
    shared = sorted(writes & iterations)
    return shared[0] if shared else None


def _requirements_from_pairs(
    pairs: Iterable[tuple[str, str]],
) -> tuple[AxisRequirement, ...]:
    out: list[AxisRequirement] = []
    for axis, bit in pairs:
        if axis in {"cfg", "dfg", "struct"} and bit:
            out.append(_req(axis, bit))  # type: ignore[arg-type]
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class AxisContractDiagnostic:
    """Why a plausible contract candidate was not proven."""

    contract: str
    symbol_uid: str
    qualified_name: str
    container_kind: str | None
    missing: tuple[str, ...]
    present_bits: tuple[AxisRequirement, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "symbol_uid": self.symbol_uid,
            "qualified_name": self.qualified_name,
            "container_kind": self.container_kind,
            "missing": list(self.missing),
            "present_bits": [
                {"axis": req.axis, "bit": req.bit} for req in self.present_bits
            ],
        }


@dataclass(frozen=True)
class AxisContractMatch:
    """One proven structural contract on a symbol."""

    contract: str
    symbol_uid: str
    qualified_name: str
    required_bits: tuple[AxisRequirement, ...]
    evidence_bits: tuple[AxisRequirement, ...]
    container_kind: str | None = None
    evidence_probes: tuple[str, ...] = ()
    traversal_mode: TraversalMode | None = None
    payload: dict[str, object] = field(default_factory=dict)

    def to_query_request(self, *, limit: int = 30) -> AxisQueryRequest:
        """Turn this contract into a storage query request.

        Static contracts, such as data shape declarations, deliberately have
        no traversal mode and cannot compile to a graph traversal request yet.
        """

        if self.traversal_mode is None:
            raise ValueError(f"Contract has no traversal mode: {self.contract}")
        container_kinds = (self.container_kind,) if self.container_kind else ()
        return AxisQueryRequest(
            traversal_mode=self.traversal_mode,
            required_bits=self.required_bits,
            container_kinds=container_kinds,
            limit=limit,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "symbol_uid": self.symbol_uid,
            "qualified_name": self.qualified_name,
            "required_bits": [
                {"axis": req.axis, "bit": req.bit} for req in self.required_bits
            ],
            "evidence_bits": [
                {"axis": req.axis, "bit": req.bit} for req in self.evidence_bits
            ],
            "container_kind": self.container_kind,
            "evidence_probes": list(self.evidence_probes),
            "traversal_mode": self.traversal_mode,
            "payload": dict(self.payload),
        }


def _match_from_kind(
    *,
    contract: str,
    profile: AxisProfile,
    kind: ContainerKindMatch,
    required_bits: tuple[AxisRequirement, ...] | None = None,
    traversal_mode: TraversalMode | None,
    payload: dict[str, object] | None = None,
) -> AxisContractMatch | None:
    requirements = required_bits or _requirements_from_pairs(kind.evidence_bits)
    if not _bits_present(profile, requirements):
        return None
    return AxisContractMatch(
        contract=contract,
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        required_bits=tuple(sorted(set(requirements))),
        evidence_bits=tuple(sorted(set(requirements))),
        container_kind=kind.kind,
        evidence_probes=kind.evidence_probes,
        traversal_mode=traversal_mode,
        payload={**kind.payload, **(payload or {})},
    )


_register_spec(_ContractSpec(
    contract="metadata_key_roundtrip",
    container_kinds=("metadata_carrier",),
    required_bits=(
        AxisRequirement("dfg", "keyed_write"),
        AxisRequirement("dfg", "keyed_read"),
        AxisRequirement("struct", "literal_key"),
    ),
))


@register_contract("metadata_key_roundtrip")
def _compile_metadata_key_roundtrip(
    profile: AxisProfile,
    matches: tuple[ContainerKindMatch, ...],
) -> AxisContractMatch | None:
    kind = _kind_match(matches, "metadata_carrier")
    if kind is None:
        return None
    return _match_from_kind(
        contract="metadata_key_roundtrip",
        profile=profile,
        kind=kind,
        required_bits=(
            _req("dfg", "keyed_write"),
            _req("dfg", "keyed_read"),
            _req("struct", "literal_key"),
        ),
        traversal_mode="deferred_binding_flow",
    )


_register_spec(_ContractSpec(
    contract="callable_container_dispatch",
    container_kinds=("middleware_chain", "signal_register"),
    required_bits=(
        AxisRequirement("dfg", "callable_value"),
        AxisRequirement("dfg", "container_write_value"),
        AxisRequirement("dfg", "iteration_source"),
        AxisRequirement("cfg", "value_call"),
    ),
    payload_rule_name=(
        "payload_identity:container_write_value.container"
        "==iteration_source.iterable"
    ),
    payload_rule=lambda profile: _shared_write_iteration_container(profile) is not None,
))


@register_contract("callable_container_dispatch")
def _compile_callable_container_dispatch(
    profile: AxisProfile,
    matches: tuple[ContainerKindMatch, ...],
) -> AxisContractMatch | None:
    kind = _kind_match(matches, "middleware_chain", "signal_register")
    if kind is None:
        return None
    shared_container = _shared_write_iteration_container(profile)
    if shared_container is None:
        return None
    return _match_from_kind(
        contract="callable_container_dispatch",
        profile=profile,
        kind=kind,
        required_bits=(
            _req("dfg", "callable_value"),
            _req("dfg", "container_write_value"),
            _req("dfg", "iteration_source"),
            _req("cfg", "value_call"),
        ),
        traversal_mode="deferred_binding_flow",
        payload={"container": shared_container},
    )


_register_spec(_ContractSpec(
    contract="provider_default_binding",
    container_kinds=("di_container",),
    required_bits=(
        AxisRequirement("struct", "parameter_default"),
        AxisRequirement("dfg", "parameter_default_value"),
        AxisRequirement("dfg", "callable_value"),
    ),
))


@register_contract("provider_default_binding")
def _compile_provider_default_binding(
    profile: AxisProfile,
    matches: tuple[ContainerKindMatch, ...],
) -> AxisContractMatch | None:
    kind = _kind_match(matches, "di_container")
    if kind is None:
        return None
    return _match_from_kind(
        contract="provider_default_binding",
        profile=profile,
        kind=kind,
        required_bits=(
            _req("struct", "parameter_default"),
            _req("dfg", "parameter_default_value"),
            _req("dfg", "callable_value"),
        ),
        traversal_mode="deferred_binding_flow",
    )


_register_spec(_ContractSpec(
    contract="proxy_indirection",
    container_kinds=("proxy_object",),
    required_bits=(),
))


@register_contract("proxy_indirection")
def _compile_proxy_indirection(
    profile: AxisProfile,
    matches: tuple[ContainerKindMatch, ...],
) -> AxisContractMatch | None:
    kind = _kind_match(matches, "proxy_object")
    if kind is None:
        return None
    return AxisContractMatch(
        contract="proxy_indirection",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        required_bits=(),
        evidence_bits=(),
        container_kind=kind.kind,
        evidence_probes=kind.evidence_probes,
        traversal_mode="deferred_binding_flow",
        payload=dict(kind.payload),
    )


_register_spec(_ContractSpec(
    contract="data_shape_declaration",
    container_kinds=("data_model",),
    required_bits=(),
))


@register_contract("data_shape_declaration")
def _compile_data_shape_declaration(
    profile: AxisProfile,
    matches: tuple[ContainerKindMatch, ...],
) -> AxisContractMatch | None:
    kind = _kind_match(matches, "data_model")
    if kind is None:
        return None
    return _match_from_kind(
        contract="data_shape_declaration",
        profile=profile,
        kind=kind,
        traversal_mode=None,
    )


_register_spec(_ContractSpec(
    contract="configuration_carrier",
    container_kinds=("config_carrier",),
    required_bits=(
        AxisRequirement("struct", "class_def"),
        AxisRequirement("struct", "class_attribute"),
        AxisRequirement("struct", "annotation"),
    ),
))


@register_contract("configuration_carrier")
def _compile_configuration_carrier(
    profile: AxisProfile,
    matches: tuple[ContainerKindMatch, ...],
) -> AxisContractMatch | None:
    kind = _kind_match(matches, "config_carrier")
    if kind is None:
        return None
    return _match_from_kind(
        contract="configuration_carrier",
        profile=profile,
        kind=kind,
        required_bits=(
            _req("struct", "class_def"),
            _req("struct", "class_attribute"),
            _req("struct", "annotation"),
        ),
        traversal_mode=None,
    )


class AxisContractCompiler:
    """Compile L2 container-kind matches into L3 structural contracts."""

    def compile(
        self,
        profile: AxisProfile,
        container_matches: Iterable[ContainerKindMatch],
    ) -> list[AxisContractMatch]:
        matches = tuple(container_matches)
        out: list[AxisContractMatch] = []
        for contract, predicate in _PREDICATES.items():
            result = predicate(profile, matches)
            if result is not None and result.contract == contract:
                out.append(result)
        return out

    def compile_many(
        self,
        profiles: Iterable[AxisProfile],
        matches_by_uid: dict[str, list[ContainerKindMatch]],
    ) -> dict[str, list[AxisContractMatch]]:
        out: dict[str, list[AxisContractMatch]] = {}
        for profile in profiles:
            contracts = self.compile(profile, matches_by_uid.get(profile.symbol_uid, ()))
            if contracts:
                out[profile.symbol_uid] = contracts
        return out

    def registered_contracts(self) -> list[str]:
        return sorted(_PREDICATES)

    def diagnose(
        self,
        profile: AxisProfile,
        container_matches: Iterable[ContainerKindMatch],
    ) -> list[AxisContractDiagnostic]:
        """Per-bit diagnostics for every contract whose container kind is
        matched but whose proof did not complete.

        Driven by ``_SPECS`` so adding a contract automatically adds its
        diagnostic. A contract with no required bits and no payload rule
        cannot produce a missing-bit diagnostic; if such a contract has its
        kind matched but did not compile, that is a predicate bug, and the
        diagnostic surfaces it as ``contract_predicate_returned_none:<name>``.
        """
        matches = tuple(container_matches)
        proven = {match.contract for match in self.compile(profile, matches)}
        diagnostics: list[AxisContractDiagnostic] = []
        for spec in _SPECS.values():
            if spec.contract in proven:
                continue
            kind = _kind_match(matches, *spec.container_kinds)
            if kind is None:
                continue
            missing: list[str] = [
                f"{req.axis}:{req.bit}"
                for req in spec.required_bits
                if not profile.has(req.axis, req.bit)
            ]
            if (
                spec.payload_rule is not None
                and spec.payload_rule_name
                and not spec.payload_rule(profile)
            ):
                missing.append(spec.payload_rule_name)
            if not missing:
                missing.append(f"contract_predicate_returned_none:{spec.contract}")
            present = tuple(
                req for req in spec.required_bits if profile.has(req.axis, req.bit)
            )
            diagnostics.append(
                AxisContractDiagnostic(
                    contract=spec.contract,
                    symbol_uid=profile.symbol_uid,
                    qualified_name=profile.qualified_name,
                    container_kind=kind.kind,
                    missing=tuple(missing),
                    present_bits=present,
                )
            )
        return diagnostics


def container_kind_matches_from_json(raw: str | list[dict[str, object]]) -> list[ContainerKindMatch]:
    """Parse persisted ``axis_container_kinds_json`` rows back into L2 matches."""

    if isinstance(raw, str):
        try:
            data = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    else:
        data = raw
    if not isinstance(data, list):
        return []
    out: list[ContainerKindMatch] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        symbol_uid = str(item.get("symbol_uid") or "")
        qualified_name = str(item.get("qualified_name") or "")
        if not kind or not symbol_uid:
            continue
        evidence_bits = []
        for pair in item.get("evidence_bits") or []:
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and str(pair[0]) in {"cfg", "dfg", "struct"}
                and str(pair[1])
            ):
                evidence_bits.append((str(pair[0]), str(pair[1])))
        probes = tuple(str(p) for p in item.get("evidence_probes") or [] if str(p))
        payload = item.get("payload")
        out.append(
            ContainerKindMatch(
                kind=kind,
                symbol_uid=symbol_uid,
                qualified_name=qualified_name,
                evidence_bits=tuple(evidence_bits),
                evidence_probes=probes,
                payload=payload if isinstance(payload, dict) else {},
            )
        )
    return out


__all__ = [
    "AxisContractCompiler",
    "AxisContractDiagnostic",
    "AxisContractMatch",
    "container_kind_matches_from_json",
    "register_contract",
]
