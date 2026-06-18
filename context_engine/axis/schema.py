"""Schema for physical axis facts.

This module intentionally does not define roles, buckets, or contracts. It is
the normalized layer between raw AST facts and later query planning.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

AxisName = Literal["cfg", "dfg", "struct"]


@dataclass(frozen=True)
class AxisFact:
    """One AST-derived fact attached to one symbol or module scope."""

    symbol_uid: str
    qualified_name: str
    symbol_kind: str
    axis: AxisName
    bit: str
    line: int
    evidence: str
    ast_kind: str
    payload: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol_uid": self.symbol_uid,
            "qualified_name": self.qualified_name,
            "symbol_kind": self.symbol_kind,
            "axis": self.axis,
            "bit": self.bit,
            "line": self.line,
            "evidence": self.evidence,
            "ast_kind": self.ast_kind,
            "payload": dict(self.payload),
        }


@dataclass
class AxisProfile:
    """Aggregated axis bitset for one symbol."""

    symbol_uid: str
    qualified_name: str
    symbol_kind: str
    cfg_bits: set[str] = field(default_factory=set)
    dfg_bits: set[str] = field(default_factory=set)
    struct_bits: set[str] = field(default_factory=set)
    facts: list[AxisFact] = field(default_factory=list)

    def add_fact(self, fact: AxisFact) -> None:
        self.facts.append(fact)
        if fact.axis == "cfg":
            self.cfg_bits.add(fact.bit)
        elif fact.axis == "dfg":
            self.dfg_bits.add(fact.bit)
        elif fact.axis == "struct":
            self.struct_bits.add(fact.bit)
        else:  # pragma: no cover - guarded by typing, defensive for runtime data
            raise ValueError(f"Unknown axis: {fact.axis}")

    def has(self, axis: AxisName, bit: str) -> bool:
        return bit in self.bits(axis)

    def bits(self, axis: AxisName) -> set[str]:
        if axis == "cfg":
            return self.cfg_bits
        if axis == "dfg":
            return self.dfg_bits
        if axis == "struct":
            return self.struct_bits
        raise ValueError(f"Unknown axis: {axis}")  # pragma: no cover

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol_uid": self.symbol_uid,
            "qualified_name": self.qualified_name,
            "symbol_kind": self.symbol_kind,
            "cfg_bits": sorted(self.cfg_bits),
            "dfg_bits": sorted(self.dfg_bits),
            "struct_bits": sorted(self.struct_bits),
            "facts": [fact.to_dict() for fact in self.facts],
        }


@dataclass
class AxisExtraction:
    """Facts and profiles extracted from one source file."""

    file_path: str
    facts: list[AxisFact]

    @property
    def profiles(self) -> dict[str, AxisProfile]:
        profiles: dict[str, AxisProfile] = {}
        for fact in self.facts:
            profile = profiles.get(fact.symbol_uid)
            if profile is None:
                profile = AxisProfile(
                    symbol_uid=fact.symbol_uid,
                    qualified_name=fact.qualified_name,
                    symbol_kind=fact.symbol_kind,
                )
                profiles[fact.symbol_uid] = profile
            profile.add_fact(fact)
        return profiles

    @property
    def profiles_by_qualified_name(self) -> dict[str, AxisProfile]:
        return {profile.qualified_name: profile for profile in self.profiles.values()}

    def facts_by_axis(self) -> dict[AxisName, list[AxisFact]]:
        grouped: dict[AxisName, list[AxisFact]] = defaultdict(list)
        for fact in self.facts:
            grouped[fact.axis].append(fact)
        return grouped
