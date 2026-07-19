"""Context builder — RoleCandidate → expanded code bundle for an LLM.

The role-driven retrieval primitive returns ranked seed symbols. An
``/ask``-style consumer needs the *code* around those seeds — not just
the seed name and uid. This module is the bridge: walk every candidate's
structural neighbourhood via the shared ``graph_walk`` core (one batched
grouped walk per expansion step), dedupe + depth-rank the related
symbols per seed, and pull their ``code`` from Lance.

What "neighbourhood" means depends on the contract that satisfied the
role. ``deferred_binding_flow`` (the only mode any current contract
uses) walks ``DECORATED_BY | USES_TYPE | INJECTS | HANDLES | REFERENCES
| HAS_API | INHERITED_API`` first (the structural binding ring) then
``CALLS_*`` for runtime dispatch — exactly what a question about a
registry or dependency-binding pattern needs to surface.

The output is a ``ContextBundle`` per candidate, ready for prompt
assembly. We do not produce the final prompt: prompt shape is the
consumer's choice (chat format, tool-use schema, etc.).
"""

from __future__ import annotations

import heapq
import math
import os
import re
from collections import Counter, defaultdict, namedtuple
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from context_engine.axis.graph_walk import steps_for_mode, walk_neighbours_grouped
from context_engine.axis.query_node_ranking import semantic_noise_floor
from context_engine.axis.role_retrieval import (
    QueryScoringContext,
    RoleCandidate,
    retrieval_channel_families,
)
from context_engine.axis.test_file_filter import is_test_path
from context_engine.observability.metrics import estimate_text_tokens

_RENDER_LADDER: tuple[str, ...] = (
    "impact_surface",
    "signature_only",
    "fold_compact",
    "fold",
    "hybrid_compact",
    "hybrid",
    "full",
)

# Rank-decay needs a pressure guard at small envelopes, where coverage leaves
# too little room for a useful large body. Relax that guard as the absolute
# envelope grows: aligned 8k/10k/12k sweeps show that high-coverage requests at
# 10–12k can still afford (and benefit from) head bodies that are inaccessible
# at 8k. The configured threshold is therefore the 8k floor, not a fixed gate.
_RANK_DECAY_GUARD_START_TOKENS = 8_000
_RANK_DECAY_GUARD_END_TOKENS = 12_000
# A lexical/semantic source span in the ranked head is direct evidence that a
# body (rather than only a signature) can answer the request. Let those few
# candidates cross the generic density cutoff once. Naturally qualifying
# rungs do not consume that exception; after the first rejected-density rung
# is rescued, the cutoff controls the rest of the ladder normally.
_RANK_DECAY_SPAN_PROTECTED_HEAD = 5

_CLASS_DEF_PREFIX = "class "

# DECOUPLED-ALLOCATION experiment (exp/decoupled-allocation) — REFUTED, kept OFF.
# When ON: force a signature-only coverage floor, then order the token-credit
# UPGRADE phase by the seed's raw query↔node cosine (semantic-primary) instead of
# structural utility/cost density. An offline candidate-level sim
# (candidate_metrics.xlsx) predicted ~2.5–2.9× token_precision from this.
# RESULT: the benchmark A/B REFUTED it — token_precision got WORSE (fastapi
# 0.316→0.199, click 0.404→0.345; recall held 1.0). The sim was symbol-level and
# ignored neighbor-EXPANSION tokens, which dominate the real bundle-level render;
# seed ordering does not concentrate them, and the forced signature floor discards
# the profile's precision-tuned rich render + _freeze_cross_file_member_bodies.
# Left env-gated + OFF as a documented dead-end; do NOT enable. See memory
# project-ranker-ordering-gold-blind "REFUTED IN THE REAL AUCTION".
_AUCTION_SEMANTIC_PRIMARY = os.getenv("AXIS_AUCTION_SEMANTIC_PRIMARY", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


# One expansion hit: a neighbour reached from a seed, tagged with the
# step that found it. Semantic fields are request-local annotations; they are
# never persisted back to the graph or vector index.
@dataclass(frozen=True)
class _Hit:
    uid: str
    name: str
    file_path: str
    depth: int
    step: str
    query_similarity: float | None = None
    semantic_excess: float = 0.0
    tier_weight: float = 1.0
    structural_weight: float = 0.0
    relevance_score: float = 0.0


_PayloadRow = dict[str, Any]
SpanScoreFn = Callable[[list[str]], list[float]]


@dataclass(frozen=True)
class _SpanCandidate:
    """One within-symbol window competing for rendered body lines."""

    line_indices: tuple[int, ...]
    text: str
    lexical_score: float
    structural_score: float
    line_anchor_score: float = 0.0
    retrieval_anchor_score: float = 0.0
    anchored_line_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class LexicalSpanProbeTrace:
    candidate_count: int
    bounded_candidates: int
    payload_count: int
    matched_symbols: int
    span_count: int
    covered_lines: int
    fetch_failed: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "candidate_count": self.candidate_count,
            "bounded_candidates": self.bounded_candidates,
            "payload_count": self.payload_count,
            "matched_symbols": self.matched_symbols,
            "span_count": self.span_count,
            "covered_lines": self.covered_lines,
            "fetch_failed": self.fetch_failed,
        }


@dataclass(frozen=True)
class LexicalSpanEvidence:
    spans: tuple[tuple[int, int], ...]
    score: float
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class RenderedOwner:
    """One source symbol honestly represented inside an aggregate render."""

    uid: str
    name: str
    qualified_name: str
    file_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "uid": self.uid,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
        }


@dataclass(frozen=True)
class ContextSymbol:
    """One symbol in the assembled context: the seed (depth 0) or a
    related symbol reached through graph expansion."""

    uid: str
    name: str
    file_path: str
    role: str
    distance_from_seed: int
    expansion_step: str | None
    code: str | None
    qualified_name: str = ""
    kind: str = ""
    direction: str = "callee"
    edge_type: str = ""
    relevance_score: float = 0.0
    utility_score: float = 0.0
    query_similarity: float | None = None
    semantic_excess: float = 0.0
    tier_weight: float = 1.0
    structural_weight: float = 0.0
    start_line: int = 0
    end_line: int = 0
    # Exact source intervals represented by the current ``code`` render.
    # ``None`` means the untrimmed symbol body (``start_line..end_line``);
    # an empty tuple means synthetic text with no honest source attribution.
    rendered_spans: tuple[tuple[int, int], ...] | None = None
    # Retrieval hint only: source intervals returned by semantic chunks.  It
    # is not claimed as rendered until the line ranker actually selects it.
    retrieval_spans: tuple[tuple[int, int], ...] = ()
    # Direct retrieval provenance survives RoleCandidate -> ContextSymbol so
    # the body allocator can distinguish a neighbour with its own evidence
    # from a symbol present only because a graph edge reached it.
    retrieval_channels: tuple[str, ...] = ()
    exact_symbol_match: bool = False
    supporting_roles: tuple[str, ...] = ()
    lexical_span_score: float | None = None
    # Aggregate renders (currently class folds) represent several real source
    # symbols while retaining one primary uid for budget accounting. Keep the
    # complete owner set so exact-symbol/file evaluation does not confuse the
    # rendering container with a different source node.
    represented_owners: tuple[RenderedOwner, ...] = ()

    def effective_rendered_spans(self) -> tuple[tuple[int, int], ...]:
        if self.rendered_spans is not None:
            return self.rendered_spans
        if self.code and self.start_line > 0 and self.end_line >= self.start_line:
            return ((self.start_line, self.end_line),)
        return ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "distance_from_seed": self.distance_from_seed,
            "expansion_step": self.expansion_step,
            "code": self.code,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "direction": self.direction,
            "edge_type": self.edge_type,
            "relevance_score": self.relevance_score,
            "utility_score": self.utility_score,
            "query_similarity": self.query_similarity,
            "semantic_excess": self.semantic_excess,
            "tier_weight": self.tier_weight,
            "structural_weight": self.structural_weight,
        }
        if self.start_line > 0:
            payload["start_line"] = self.start_line
        if self.end_line >= self.start_line > 0:
            payload["end_line"] = self.end_line
        payload["rendered_spans"] = self.effective_rendered_spans()
        payload["retrieval_spans"] = self.retrieval_spans
        payload["retrieval_channels"] = self.retrieval_channels
        payload["exact_symbol_match"] = self.exact_symbol_match
        payload["supporting_roles"] = self.supporting_roles
        payload["lexical_span_score"] = self.lexical_span_score
        if self.represented_owners:
            payload["represented_owners"] = [owner.to_dict() for owner in self.represented_owners]
        return payload


_CALLER_KINDS = frozenset(
    {
        "reverse_calls",
        "impacted_tests",
        "structural_inheritor",
        "forward_affects",
        "trace_callers",
    }
)


def _candidate_direction(candidate: RoleCandidate) -> str:
    kind = candidate.satisfying_kinds[0] if candidate.satisfying_kinds else ""
    return "caller" if kind in _CALLER_KINDS else "callee"


@dataclass(frozen=True)
class ContextRenderBudget:
    """Echelon-2 render and token-packing knobs for ``build_context_for_candidates``."""

    token_budget: int | None = None
    render_mode: str = "full"
    per_transaction_share: float = 0.10
    file_soft_cap_share: float = 0.25
    signature_only_initial: bool = False
    min_utility_per_token: float | None = None
    upgrade_min_utility_per_token: float | None = None
    freeze_at_utility_plateau: bool = False
    plateau_upgrade_reserve_share: float = 0.0
    node_semantic_utility_weight: float = 0.0
    rank_decay_body_allocation: bool = False
    rank_decay_head_share: float = 0.15
    rank_decay_rate: float = 0.75
    rank_decay_max_coverage_share: float = 0.65
    decoupled_symbol_body_allocation: bool = True
    decoupled_seed_span_reserve_share: float = 0.10
    span_line_rerank: bool = False
    span_rank_max_symbols: int = 48
    span_rank_max_candidates_per_symbol: int = 24
    span_rank_max_body_lines: int = 6


@dataclass(frozen=True)
class TokenCreditAttribution:
    """Exact-token share of an accepted upgrade along audit dimensions."""

    scope: str
    evidence: str
    edge_type: str
    depth: int
    delta_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "evidence": self.evidence,
            "edge_type": self.edge_type,
            "depth": self.depth,
            "delta_tokens": self.delta_tokens,
        }


@dataclass(frozen=True)
class TokenCreditTransaction:
    """One accepted marginal purchase in the Token Credit packer."""

    phase: str
    uid: str
    role: str
    render_mode: str
    delta_utility: float
    effective_utility: float
    delta_tokens: int
    utility_per_token: float
    effective_utility_per_token: float
    cumulative_utility: float
    cumulative_tokens: int
    semantic_delta_utility: float = 0.0
    new_files: int = 0
    new_role: bool = False
    new_steps: int = 0
    attribution: tuple[TokenCreditAttribution, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "uid": self.uid,
            "role": self.role,
            "render_mode": self.render_mode,
            "delta_utility": self.delta_utility,
            "effective_utility": self.effective_utility,
            "delta_tokens": self.delta_tokens,
            "utility_per_token": self.utility_per_token,
            "effective_utility_per_token": self.effective_utility_per_token,
            "cumulative_utility": self.cumulative_utility,
            "cumulative_tokens": self.cumulative_tokens,
            "semantic_delta_utility": self.semantic_delta_utility,
            "new_files": self.new_files,
            "new_role": self.new_role,
            "new_steps": self.new_steps,
            "attribution": [row.to_dict() for row in self.attribution],
        }


@dataclass
class TokenCreditTrace:
    """Opt-in transaction curve for plateau analysis; never changes selection."""

    token_budget: int = 0
    noise_level: float = 0.0
    leader_count: int = 0
    transaction_limit: int = 0
    transactions: list[TokenCreditTransaction] = field(default_factory=list)
    cumulative_utility: float = 0.0
    used_tokens: int = 0
    cutoff_density: float | None = None
    cutoff_rejections: int = 0
    spend_ceiling: int = 0
    coverage_tokens: int = 0
    coverage_share: float = 0.0
    allocation_mode: str = "legacy"
    rank_decay_coverage_threshold: float = 0.0
    graph_fanout_base_limit: int = 0
    graph_fanout_tier_counts: dict[str, int] = field(default_factory=dict)
    graph_fanout_limit_counts: dict[int, int] = field(default_factory=dict)

    def begin(
        self,
        *,
        token_budget: int,
        noise_level: float,
        leader_count: int,
        transaction_limit: int,
        cutoff_density: float | None = None,
    ) -> None:
        self.token_budget = token_budget
        self.noise_level = noise_level
        self.leader_count = leader_count
        self.transaction_limit = transaction_limit
        self.transactions.clear()
        self.cumulative_utility = 0.0
        self.used_tokens = 0
        self.cutoff_density = cutoff_density
        self.cutoff_rejections = 0
        self.spend_ceiling = token_budget
        self.coverage_tokens = 0
        self.coverage_share = 0.0
        self.allocation_mode = "legacy"
        self.rank_decay_coverage_threshold = 0.0

    def record(
        self,
        *,
        phase: str,
        bundle: ContextBundle,
        render_mode: str,
        delta_utility: float,
        effective_utility: float | None = None,
        delta_tokens: int,
        new_files: int = 0,
        new_role: bool = False,
        new_steps: int = 0,
        semantic_delta_utility: float = 0.0,
        attribution: tuple[TokenCreditAttribution, ...] = (),
    ) -> None:
        effective = float(delta_utility if effective_utility is None else effective_utility)
        self.cumulative_utility += float(delta_utility)
        self.used_tokens += int(delta_tokens)
        self.transactions.append(
            TokenCreditTransaction(
                phase=phase,
                uid=bundle.seed.uid,
                role=bundle.role,
                render_mode=render_mode,
                delta_utility=float(delta_utility),
                effective_utility=effective,
                delta_tokens=int(delta_tokens),
                utility_per_token=float(delta_utility) / max(1, int(delta_tokens)),
                effective_utility_per_token=effective / max(1, int(delta_tokens)),
                cumulative_utility=self.cumulative_utility,
                cumulative_tokens=self.used_tokens,
                semantic_delta_utility=semantic_delta_utility,
                new_files=new_files,
                new_role=new_role,
                new_steps=new_steps,
                attribution=attribution,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_budget": self.token_budget,
            "noise_level": self.noise_level,
            "leader_count": self.leader_count,
            "transaction_limit": self.transaction_limit,
            "used_tokens": self.used_tokens,
            "cumulative_utility": self.cumulative_utility,
            "cutoff_density": self.cutoff_density,
            "cutoff_rejections": self.cutoff_rejections,
            "spend_ceiling": self.spend_ceiling,
            "coverage_tokens": self.coverage_tokens,
            "coverage_share": self.coverage_share,
            "allocation_mode": self.allocation_mode,
            "rank_decay_coverage_threshold": self.rank_decay_coverage_threshold,
            "graph_fanout_base_limit": self.graph_fanout_base_limit,
            "graph_fanout_tier_counts": dict(self.graph_fanout_tier_counts),
            "graph_fanout_limit_counts": dict(self.graph_fanout_limit_counts),
            "transactions": [transaction.to_dict() for transaction in self.transactions],
        }


@dataclass(frozen=True)
class ContextBundle:
    """Bundle for one seed candidate: the seed plus its expanded
    related symbols, ordered closest-first."""

    role: str
    seed: ContextSymbol
    related: tuple[ContextSymbol, ...] = field(default_factory=tuple)
    utility_score: float = 0.0
    passive: bool = False
    render_mode: str = "full"

    def all_symbols(self) -> list[ContextSymbol]:
        return [self.seed, *self.related]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "seed": self.seed.to_dict(),
            "related": [s.to_dict() for s in self.related],
            "utility_score": self.utility_score,
            "passive": self.passive,
            "render_mode": self.render_mode,
        }


def _quote_sql_value(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _symbol_payload_columns(table) -> list[str]:
    columns = ["uid", "code", "workspace_id"]
    try:
        schema_names = set(table.schema.names)
        for optional in ("qualified_name", "name", "file_path", "start_line", "end_line"):
            if optional in schema_names:
                columns.append(optional)
    except Exception:
        pass
    return columns


def _symbol_payload_filter_sql(
    workspace_id: str,
    uids: set[str],
    *,
    sym_table_fn,
) -> str:
    from context_engine.database.lance_workspace_tables import workspace_partitioned_enabled

    uid_filter = ", ".join(_quote_sql_value(uid) for uid in sorted(uids))
    if workspace_partitioned_enabled() and callable(sym_table_fn):
        return f"uid IN ({uid_filter})"
    return f"workspace_id = {_quote_sql_value(workspace_id)} AND uid IN ({uid_filter})"


def _resolve_symbols_table(lance, workspace_id: str):
    sym_table_fn = getattr(lance, "symbols_table", None)
    if callable(sym_table_fn):
        return sym_table_fn(workspace_id), sym_table_fn
    return lance._sym_table, sym_table_fn  # noqa: SLF001


def _filter_symbol_payload_arrow(arrow, workspace_id: str, uids: set[str]):
    try:
        import pyarrow as pa
        import pyarrow.compute as pc

        uid_set = pa.array(list(uids), type=arrow["uid"].type)
        mask = pc.and_(
            pc.equal(
                arrow["workspace_id"],
                pa.scalar(workspace_id, type=arrow["workspace_id"].type),
            ),
            pc.is_in(arrow["uid"], value_set=uid_set),
        )
        return arrow.filter(mask)
    except Exception:
        return arrow


def _load_symbol_payload_arrow(
    lance_table, columns: list[str], filter_sql: str, workspace_id: str, uids: set[str]
):
    try:
        return lance_table.to_table(columns=columns, filter=filter_sql)
    except TypeError:
        arrow = lance_table.to_table(columns=columns)
        return _filter_symbol_payload_arrow(arrow, workspace_id, uids)


def _payloads_from_pylist_fallback(
    arrow,
    workspace_id: str,
    uids: set[str],
) -> dict[str, _PayloadRow]:
    out: dict[str, _PayloadRow] = {}
    for row in arrow.to_pylist():
        uid = str(row.get("uid") or "")
        if row.get("workspace_id") == workspace_id and uid in uids:
            out[uid] = {
                "code": row.get("code"),
                "qualified_name": row.get("qualified_name") or "",
                "name": row.get("name") or "",
                "file_path": row.get("file_path") or "",
                "start_line": row.get("start_line") or 0,
                "end_line": row.get("end_line") or 0,
            }
    return out


def _arrow_column_pylist(arrow, column: str, length: int) -> list:
    try:
        return cast("list", arrow[column].to_pylist())
    except Exception:
        return [""] * length


def _payloads_from_arrow_table(
    arrow,
    workspace_id: str,
    uids: set[str],
) -> dict[str, _PayloadRow]:
    try:
        row_uids = arrow["uid"].to_pylist()
        codes = arrow["code"].to_pylist()
        workspace_ids = arrow["workspace_id"].to_pylist()
    except Exception:
        return _payloads_from_pylist_fallback(arrow, workspace_id, uids)

    row_count = len(row_uids)
    qualified_names = _arrow_column_pylist(arrow, "qualified_name", row_count)
    names = _arrow_column_pylist(arrow, "name", row_count)
    file_paths = _arrow_column_pylist(arrow, "file_path", row_count)
    start_lines = _arrow_column_pylist(arrow, "start_line", row_count)
    end_lines = _arrow_column_pylist(arrow, "end_line", row_count)

    out: dict[str, _PayloadRow] = {}
    for (
        uid_raw,
        code,
        row_workspace_id,
        qualified_name,
        name,
        file_path,
        start_line,
        end_line,
    ) in zip(
        row_uids,
        codes,
        workspace_ids,
        qualified_names,
        names,
        file_paths,
        start_lines,
        end_lines,
        strict=False,
    ):
        if row_workspace_id != workspace_id:
            continue
        uid = str(uid_raw or "")
        if uid in uids:
            out[uid] = {
                "code": code,
                "qualified_name": str(qualified_name or ""),
                "name": str(name or ""),
                "file_path": str(file_path or ""),
                "start_line": _int_payload_value(start_line),
                "end_line": _int_payload_value(end_line),
            }
    return out


def _fetch_symbol_payloads(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, _PayloadRow]:
    """Pull ``code`` + lightweight render metadata for a set of uids.

    Lance does not give us a clean WHERE-by-list across heterogeneous
    columns; one full scan filtered in-process is acceptable for the
    workspaces we currently target (thousands of symbols, not millions).
    """
    if not uids:
        return {}
    table, sym_table_fn = _resolve_symbols_table(lance, workspace_id)
    columns = _symbol_payload_columns(table)
    filter_sql = _symbol_payload_filter_sql(workspace_id, uids, sym_table_fn=sym_table_fn)
    arrow = _load_symbol_payload_arrow(table.to_lance(), columns, filter_sql, workspace_id, uids)
    return _payloads_from_arrow_table(arrow, workspace_id, uids)


def _fetch_codes(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, str | None]:
    """Backward-compatible code-only view used by older callers/tests."""
    return {
        uid: payload.get("code")
        for uid, payload in _fetch_symbol_payloads(lance, workspace_id, uids).items()
    }


def _read_symbol_code_from_file(
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    workspace_root: Path | None,
) -> str:
    from context_engine.workspace_paths import resolve_graph_file_path

    resolved = resolve_graph_file_path(file_path, workspace_root=workspace_root)
    if not resolved:
        return ""
    try:
        lines = Path(resolved).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if start_line < 1 or end_line < start_line:
        return ""
    return "\n".join(lines[start_line - 1 : end_line])


def _missing_symbol_uids(uids: set[str], payloads: dict[str, _PayloadRow]) -> list[str]:
    return [uid for uid in uids if not str((payloads.get(uid) or {}).get("code") or "").strip()]


def _int_payload_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_payload_span(row: _PayloadRow | None, span: dict[str, Any]) -> _PayloadRow:
    out: _PayloadRow = dict(row or {})
    name = str(span.get("name") or "")
    file_path = str(span.get("file_path") or "")
    if name and not out.get("name"):
        out["name"] = name
    if file_path and not out.get("file_path"):
        out["file_path"] = file_path
    start_line = _int_payload_value(span.get("start_line"))
    end_line = _int_payload_value(span.get("end_line"))
    if start_line > 0:
        out["start_line"] = start_line
    if end_line >= start_line > 0:
        out["end_line"] = end_line
    return out


def _payload_span(uid: str, row: _PayloadRow | None) -> dict[str, Any] | None:
    if not row:
        return None
    start_line = _int_payload_value(row.get("start_line"))
    end_line = _int_payload_value(row.get("end_line"))
    if start_line <= 0 or end_line < start_line:
        return None
    return {
        "uid": uid,
        "name": str(row.get("name") or ""),
        "file_path": str(row.get("file_path") or ""),
        "start_line": start_line,
        "end_line": end_line,
    }


def _merge_symbol_spans(
    db,
    workspace_id: str,
    uids: set[str],
    payloads: dict[str, _PayloadRow],
) -> dict[str, _PayloadRow]:
    if not uids or db is None:
        return payloads

    get_spans = getattr(db, "get_symbol_spans_by_uids", None)
    if not callable(get_spans):
        return payloads

    spans = get_spans(list(uids), workspace_id=workspace_id)
    if not spans:
        return payloads

    out = dict(payloads)
    for uid, span in spans.items():
        if uid in uids:
            out[uid] = _merge_payload_span(out.get(uid), span)
    return out


def _hydrate_payload_from_span(
    span: dict,
    *,
    workspace_root,
    existing: _PayloadRow | None,
) -> _PayloadRow | None:
    code = _read_symbol_code_from_file(
        str(span.get("file_path") or ""),
        int(span.get("start_line") or 0),
        int(span.get("end_line") or 0),
        workspace_root=workspace_root,
    )
    if not code.strip():
        return None
    row = _merge_payload_span(existing, span)
    row.setdefault("name", str(span.get("name") or ""))
    row.setdefault("file_path", str(span.get("file_path") or ""))
    row["code"] = code
    return row


def _hydrate_missing_symbol_code(
    db,
    workspace_id: str,
    uids: set[str],
    payloads: dict[str, _PayloadRow],
) -> dict[str, _PayloadRow]:
    """Fill empty Lance payloads from on-disk source using Neo4j line spans."""
    if not uids or db is None:
        return payloads

    missing = _missing_symbol_uids(uids, payloads)
    if not missing:
        return payloads

    get_spans = getattr(db, "get_symbol_spans_by_uids", None)
    if not callable(get_spans):
        return payloads

    from context_engine.workspace_paths import registered_workspace_root

    workspace_root = registered_workspace_root(db, workspace_id)
    spans = {
        uid: span for uid in missing if (span := _payload_span(uid, payloads.get(uid))) is not None
    }
    still_missing_span = [uid for uid in missing if uid not in spans]
    if still_missing_span:
        spans.update(get_spans(still_missing_span, workspace_id=workspace_id) or {})
    if not spans:
        return payloads

    out = dict(payloads)
    for uid in missing:
        span = spans.get(uid)
        if not span:
            continue
        hydrated = _hydrate_payload_from_span(
            span,
            workspace_root=workspace_root,
            existing=out.get(uid),
        )
        if hydrated is not None:
            out[uid] = hydrated
    return out


def _is_callable_header(stripped: str) -> bool:
    return stripped.startswith(("def ", "async def ", _CLASS_DEF_PREFIX))


def _merge_rendered_spans(
    *span_groups: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    intervals = sorted(
        (int(start), int(end))
        for spans in span_groups
        for start, end in spans
        if int(start) > 0 and int(end) >= int(start)
    )
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def _symbol_source_lines(sym: ContextSymbol) -> tuple[int | None, ...]:
    """Best-effort source line for each line in the current rendered code."""
    line_count = len((sym.code or "").splitlines())
    if line_count <= 0:
        return ()
    if sym.rendered_spans is None:
        if sym.start_line <= 0 or sym.end_line < sym.start_line:
            return (None,) * line_count
        return tuple(
            line if line <= sym.end_line else None
            for line in range(sym.start_line, sym.start_line + line_count)
        )

    source_lines: list[int | None] = [
        line
        for start, end in sym.rendered_spans
        for line in range(start, end + 1)
        if start > 0 and end >= start
    ]
    if len(source_lines) < line_count:
        source_lines.extend([None] * (line_count - len(source_lines)))
    return tuple(source_lines[:line_count])


def _replace_symbol_render(
    sym: ContextSymbol,
    code: str,
    selected_line_indices: Iterable[int | None],
) -> ContextSymbol:
    source_lines = _symbol_source_lines(sym)
    selected_source_lines = [
        source_lines[index]
        for index in selected_line_indices
        if index is not None and 0 <= index < len(source_lines) and source_lines[index] is not None
    ]
    spans = _merge_rendered_spans(
        (line, line) for line in selected_source_lines if line is not None
    )
    return cast(ContextSymbol, replace(sym, code=code, rendered_spans=spans))


def _code_signature_selection(code: str | None) -> tuple[str, tuple[int, ...]]:
    """Best-effort, parser-free trim of a symbol's code to its signature:
    leading decorators plus the ``def``/``class`` header (through the line
    ending in ``:``, so multi-line parameter lists survive). Non-callable
    symbols (a module-level assignment, say) collapse to their first
    non-empty line. Empty in, empty out."""
    if not code:
        return "", ()
    lines = code.splitlines()
    out: list[str] = []
    selected: list[int] = []
    i, n = 0, len(lines)
    while i < n and lines[i].strip().startswith("@"):  # decorators
        out.append(lines[i])
        selected.append(i)
        i += 1
    started = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not started:
            if not _is_callable_header(stripped):
                # Not a callable/class — first non-empty line is the "signature".
                if stripped:
                    out.append(line)
                    selected.append(i)
                    break
                i += 1
                continue
            started = True
        out.append(line)
        selected.append(i)
        if stripped.endswith(":"):
            break
        i += 1
    return "\n".join(out), tuple(selected)


def _code_signature(code: str | None) -> str:
    return _code_signature_selection(code)[0]


def _code_impact_surface(
    code: str | None,
    *,
    qualified_name: str = "",
    name: str = "",
) -> str:
    """One-line blast-radius stub — cheapest render for impact breadth."""
    return _code_impact_surface_selection(
        code,
        qualified_name=qualified_name,
        name=name,
    )[0]


def _code_impact_surface_selection(
    code: str | None,
    *,
    qualified_name: str = "",
    name: str = "",
) -> tuple[str, tuple[int, ...]]:
    signature, indices = _code_signature_selection(code or "")
    for line, index in zip(signature.splitlines(), indices, strict=True):
        if line.strip():
            return line.strip(), (index,)
    return (qualified_name or name or "").strip(), ()


def _collapse_long_line(line: str, *, limit: int = 140) -> str:
    stripped = line.rstrip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 4].rstrip() + " ..."


def _looks_like_assignment(stripped: str) -> bool:
    if "==" in stripped or "!=" in stripped or "<=" in stripped or ">=" in stripped:
        return False
    return "=" in stripped


def _keep_compact_body_line(stripped: str) -> bool:
    if not stripped or stripped.startswith("#"):
        return False
    prefixes = (
        "if ",
        "elif ",
        "else:",
        "for ",
        "async for ",
        "while ",
        "try:",
        "except ",
        "finally:",
        "with ",
        "async with ",
        "match ",
        "case ",
        "return ",
        "yield ",
        "yield from ",
        "raise ",
        "import ",
        "from ",
        "assert ",
    )
    if stripped.startswith(prefixes):
        return True
    if _looks_like_assignment(stripped):
        return True
    return "(" in stripped and ")" in stripped


def _advance_docstring_state(stripped: str, in_docstring: bool) -> tuple[bool, bool]:
    """Return ``(new_in_docstring, should_skip_line)`` for compact body scans."""
    if not stripped.startswith(('"""', "'''")):
        return in_docstring, False
    if stripped.count('"""') == 1 or stripped.count("'''") == 1:
        in_docstring = not in_docstring
    return in_docstring, True


def _compact_body_selection(
    lines: list[str],
    *,
    start_index: int,
    max_body_lines: int,
) -> tuple[list[str], tuple[int | None, ...]]:
    out: list[str] = []
    selected: list[int | None] = []
    kept = 0
    in_docstring = False
    for index, line in enumerate(lines[start_index:], start=start_index):
        stripped = line.strip()
        if not stripped:
            continue
        in_docstring, skip = _advance_docstring_state(stripped, in_docstring)
        if skip or in_docstring:
            continue
        if not _keep_compact_body_line(stripped):
            continue
        out.append(_collapse_long_line(line))
        selected.append(index)
        kept += 1
        if kept >= max_body_lines:
            out.append("    ...")
            selected.append(None)
            break
    if kept == 0:
        out.append("    ...")
        selected.append(None)
    return out, tuple(selected)


def _code_compact_selection(
    code: str | None,
    *,
    max_body_lines: int = 24,
) -> tuple[str, tuple[int | None, ...]]:
    signature, signature_indices = _code_signature_selection(code)
    if not code or not signature:
        return signature, signature_indices
    lines = code.splitlines()
    if not signature_indices or max(signature_indices) >= len(lines) - 1:
        return signature, signature_indices

    body, body_indices = _compact_body_selection(
        lines,
        start_index=max(signature_indices) + 1,
        max_body_lines=max_body_lines,
    )
    return "\n".join([*signature.splitlines(), *body]), (*signature_indices, *body_indices)


def _code_compact(code: str | None, *, max_body_lines: int = 24) -> str:
    """Parser-light body compaction: signature + structural/call-bearing lines."""
    return _code_compact_selection(code, max_body_lines=max_body_lines)[0]


_SPAN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_SPAN_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPAN_LINE_HINT_RE = re.compile(
    r"(?:\blines?\b|:)[ \t]*(\d{1,7})(?:[ \t]*[-–][ \t]*(\d{1,7}))?",
    re.IGNORECASE,
)
_SPAN_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "before",
        "code",
        "does",
        "from",
        "function",
        "handle",
        "handles",
        "into",
        "method",
        "return",
        "that",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


def _span_terms(text: str) -> frozenset[str]:
    terms: set[str] = set()
    for token in _SPAN_WORD_RE.findall(text or ""):
        expanded = _SPAN_CAMEL_BOUNDARY_RE.sub(" ", token).replace("_", " ")
        for part in expanded.lower().split():
            if len(part) >= 3 and part not in _SPAN_STOP_WORDS:
                terms.add(part)
    return frozenset(terms)


def _span_query_line_numbers(text: str, *, max_range: int = 100) -> frozenset[int]:
    lines: set[int] = set()
    for match in _SPAN_LINE_HINT_RE.finditer(text or ""):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start <= 0 or end < start or end - start > max_range:
            continue
        lines.update(range(start, end + 1))
    return frozenset(lines)


def query_has_explicit_line_hint(text: str) -> bool:
    """Return whether a query names a bounded source line or line range."""
    return bool(_span_query_line_numbers(text))


def _sample_evenly(values: list[int], limit: int) -> list[int]:
    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[len(values) // 2]]
    indices = {round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)}
    return [values[index] for index in sorted(indices)]


def _span_renderable_body_lines(lines: list[str], *, body_start: int) -> set[int]:
    renderable: set[int] = set()
    in_docstring = False
    for index, line in enumerate(lines[body_start:], start=body_start):
        stripped = line.strip()
        if not stripped:
            continue
        in_docstring, skip = _advance_docstring_state(stripped, in_docstring)
        if skip or in_docstring or stripped.startswith("#"):
            continue
        renderable.add(index)
    return renderable


def _span_window_start(index: int, *, body_start: int, line_count: int, width: int) -> int:
    latest = max(body_start, line_count - width)
    return min(latest, max(body_start, index - width // 2))


def _span_candidates(
    sym: ContextSymbol,
    *,
    query_terms: frozenset[str],
    query_lines: frozenset[int],
    max_candidates: int,
    window_lines: int = 6,
) -> tuple[_SpanCandidate, ...]:
    code = sym.code or ""
    lines = code.splitlines()
    if not lines or max_candidates <= 0:
        return ()
    _signature, signature_indices = _code_signature_selection(code)
    body_start = max(signature_indices, default=-1) + 1
    if body_start >= len(lines):
        return ()
    renderable = _span_renderable_body_lines(lines, body_start=body_start)
    if not renderable:
        return ()

    width = max(2, int(window_lines))
    stride = max(1, width - 2)
    uniform_starts = list(range(body_start, len(lines), stride))
    final_start = max(body_start, len(lines) - width)
    if final_start not in uniform_starts:
        uniform_starts.append(final_start)
    uniform_starts = sorted(set(uniform_starts))

    retrieval_lines = {
        source_line
        for start_line, end_line in sym.retrieval_spans
        for source_line in range(int(start_line), int(end_line) + 1)
        if int(start_line) > 0 and int(end_line) >= int(start_line)
    }
    lexical_starts: list[int] = []
    if query_terms:
        for index in sorted(renderable):
            if query_terms & _span_terms(lines[index]):
                lexical_starts.append(
                    _span_window_start(
                        index,
                        body_start=body_start,
                        line_count=len(lines),
                        width=width,
                    )
                )
    if sym.start_line > 0 and query_lines:
        for source_line in sorted(query_lines):
            index = source_line - sym.start_line
            if index in renderable:
                lexical_starts.append(
                    _span_window_start(
                        index,
                        body_start=body_start,
                        line_count=len(lines),
                        width=width,
                    )
                )
    if sym.start_line > 0 and retrieval_lines:
        for source_line in sorted(retrieval_lines):
            index = source_line - sym.start_line
            if index in renderable:
                lexical_starts.append(
                    _span_window_start(
                        index,
                        body_start=body_start,
                        line_count=len(lines),
                        width=width,
                    )
                )
    lexical_starts = sorted(set(lexical_starts))
    if len(lexical_starts) > max_candidates:
        lexical_starts = _sample_evenly(lexical_starts, max_candidates)
    remaining = max_candidates - len(lexical_starts)
    other_starts = [start for start in uniform_starts if start not in set(lexical_starts)]
    starts = [*lexical_starts, *_sample_evenly(other_starts, remaining)]

    candidates: list[_SpanCandidate] = []
    seen: set[tuple[int, ...]] = set()
    for start in starts:
        indices = tuple(
            index for index in range(start, min(len(lines), start + width)) if index in renderable
        )
        if not indices or indices in seen:
            continue
        seen.add(indices)
        text = "\n".join(lines[index] for index in indices)
        candidate_terms = _span_terms(text)
        lexical_score = (
            len(query_terms & candidate_terms) / max(1, len(query_terms)) if query_terms else 0.0
        )
        structural_score = sum(
            1.0 for index in indices if _keep_compact_body_line(lines[index].strip())
        ) / max(1, len(indices))
        source_lines = {sym.start_line + index for index in indices if sym.start_line > 0}
        anchored_indices = tuple(
            index
            for index in indices
            if sym.start_line > 0 and sym.start_line + index in (query_lines | retrieval_lines)
        )
        candidates.append(
            _SpanCandidate(
                line_indices=indices,
                text=text,
                lexical_score=lexical_score,
                structural_score=structural_score,
                line_anchor_score=(
                    len(source_lines & query_lines) / max(1, len(query_lines))
                    if query_lines
                    else 0.0
                ),
                retrieval_anchor_score=(
                    len(source_lines & retrieval_lines) / max(1, len(source_lines))
                    if retrieval_lines
                    else 0.0
                ),
                anchored_line_indices=anchored_indices,
            )
        )
    return tuple(candidates)


def probe_candidate_lexical_spans(
    candidates: Iterable[RoleCandidate],
    *,
    workspace_id: str,
    lance: Any,
    db: Any | None = None,
    query_text: str,
    max_symbols: int = 96,
    max_windows_per_symbol: int = 3,
    max_candidates_per_symbol: int = 24,
    window_lines: int = 6,
) -> tuple[dict[str, LexicalSpanEvidence], LexicalSpanProbeTrace]:
    """Find query-bearing source windows inside a bounded pre-graph pool.

    This is intentionally lexical and query-time: it reuses symbol bodies
    already stored in Lance and creates no persistent chunk index. A candidate
    receives evidence only when a body window contains a query term or an
    explicit query line hint; uniformly sampled zero-match windows are not
    promoted to retrieval spans.
    """
    unique: dict[str, RoleCandidate] = {}
    for candidate in candidates:
        if candidate.uid:
            unique.setdefault(candidate.uid, candidate)
    candidate_count = len(unique)
    bounded = list(unique.values())[: max(0, int(max_symbols))]
    empty_trace = LexicalSpanProbeTrace(
        candidate_count=candidate_count,
        bounded_candidates=len(bounded),
        payload_count=0,
        matched_symbols=0,
        span_count=0,
        covered_lines=0,
    )
    if not bounded or not query_text or max_windows_per_symbol <= 0:
        return {}, empty_trace

    try:
        uids = {candidate.uid for candidate in bounded}
        payloads = (
            _resolve_context_payloads(
                lance,
                db,
                workspace_id,
                uids,
                overlay=None,
                user_id="anonymous",
            )
            if db is not None
            else _fetch_symbol_payloads(lance, workspace_id, uids)
        )
    except Exception:
        return {}, replace(empty_trace, fetch_failed=True)

    query_terms = _span_terms(query_text)
    query_lines = _span_query_line_numbers(query_text)
    payload_terms = {
        uid: _span_terms(str(payload.get("code") or "")) for uid, payload in payloads.items()
    }
    document_frequency = Counter(
        term for terms in payload_terms.values() for term in query_terms & terms
    )
    document_count = max(1, len(payload_terms))
    term_weights = {
        term: math.log(1.0 + (document_count - frequency + 0.5) / (frequency + 0.5))
        for term, frequency in document_frequency.items()
    }
    weight_ceiling = sum(term_weights.values()) or 1.0
    evidence_by_uid: dict[str, LexicalSpanEvidence] = {}
    for candidate in bounded:
        payload = payloads.get(candidate.uid) or {}
        code = str(payload.get("code") or "")
        start_line = _int_payload_value(payload.get("start_line"))
        end_line = _int_payload_value(payload.get("end_line"))
        if not code or start_line <= 0 or end_line < start_line:
            continue
        symbol = ContextSymbol(
            uid=candidate.uid,
            name=candidate.name,
            qualified_name=candidate.qualified_name,
            file_path=candidate.file_path,
            role=candidate.role,
            distance_from_seed=candidate.depth or 0,
            expansion_step=None,
            code=code,
            start_line=start_line,
            end_line=end_line,
            retrieval_spans=candidate.retrieval_spans,
            retrieval_channels=candidate.retrieval_channels,
            exact_symbol_match=candidate.exact_symbol_match,
            supporting_roles=candidate.supporting_roles,
            lexical_span_score=candidate.lexical_span_score,
        )
        windows = _span_candidates(
            symbol,
            query_terms=query_terms,
            query_lines=query_lines,
            max_candidates=max(1, int(max_candidates_per_symbol)),
            window_lines=max(2, int(window_lines)),
        )
        scored_windows = []
        for window in windows:
            matched_terms = query_terms & _span_terms(window.text)
            weighted_score = (
                sum(term_weights.get(term, 0.0) for term in matched_terms) / weight_ceiling
            )
            if window.line_anchor_score <= 0.0 and weighted_score <= 0.0:
                continue
            scored_windows.append((window, weighted_score, matched_terms))
        ranked = sorted(
            scored_windows,
            key=lambda row: (
                row[0].line_anchor_score,
                row[1],
                row[0].structural_score,
                -row[0].line_indices[0],
            ),
            reverse=True,
        )
        selected_indices: set[int] = set()
        selected_terms: set[str] = set()
        explicit_line_match = False
        selected_windows = 0
        for window, _weighted_score, matched_terms in ranked:
            new_indices = set(window.line_indices) - selected_indices
            if len(new_indices) < max(1, len(window.line_indices) // 2):
                continue
            selected_indices.update(window.line_indices)
            selected_terms.update(matched_terms)
            explicit_line_match = explicit_line_match or window.line_anchor_score > 0.0
            selected_windows += 1
            if selected_windows >= max(1, int(max_windows_per_symbol)):
                break
        if not selected_indices:
            continue
        spans = _merge_rendered_spans(
            (start_line + index, start_line + index) for index in selected_indices
        )
        score = sum(term_weights.get(term, 0.0) for term in selected_terms) / weight_ceiling
        evidence_by_uid[candidate.uid] = LexicalSpanEvidence(
            spans=spans,
            score=max(score, 1.0 if explicit_line_match else 0.0),
            matched_terms=tuple(sorted(selected_terms)),
        )

    all_spans = [span for evidence in evidence_by_uid.values() for span in evidence.spans]
    return evidence_by_uid, LexicalSpanProbeTrace(
        candidate_count=candidate_count,
        bounded_candidates=len(bounded),
        payload_count=len(payloads),
        matched_symbols=len(evidence_by_uid),
        span_count=len(all_spans),
        covered_lines=sum(end - start + 1 for start, end in all_spans),
    )


def _span_candidate_oracle_recall(
    sym: ContextSymbol,
    *,
    query_text: str,
    gold_lines: Iterable[int],
    max_candidates: int = 24,
) -> float:
    """Upper-bound line recall before candidate ranking and body truncation."""
    gold = {int(line) for line in gold_lines if int(line) > 0}
    if not gold or sym.start_line <= 0 or not sym.code:
        return 0.0
    _signature, signature_indices = _code_signature_selection(sym.code)
    candidates = _span_candidates(
        sym,
        query_terms=_span_terms(query_text),
        query_lines=_span_query_line_numbers(query_text),
        max_candidates=max(1, int(max_candidates)),
    )
    covered_indices = set(signature_indices)
    for candidate in candidates:
        covered_indices.update(candidate.line_indices)
    covered_lines = {sym.start_line + index for index in covered_indices}
    return len(gold & covered_lines) / len(gold)


def _span_semantic_excess(scores: list[float]) -> list[float]:
    if not scores:
        return []
    ordered = sorted(float(score) for score in scores)
    middle = len(ordered) // 2
    floor = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    ceiling = ordered[-1]
    span = ceiling - floor
    if span <= 1e-9:
        return [0.0] * len(scores)
    return [min(1.0, max(0.0, (float(score) - floor) / span)) for score in scores]


def _span_ranked_selection(
    sym: ContextSymbol,
    candidates: tuple[_SpanCandidate, ...],
    semantic_scores: list[float],
    *,
    max_body_lines: int,
    max_selected_spans: int = 3,
) -> tuple[str, tuple[int | None, ...]]:
    code = sym.code or ""
    lines = code.splitlines()
    signature, signature_indices = _code_signature_selection(code)
    if not lines or not candidates or max_body_lines <= 0:
        return signature, signature_indices

    scores = semantic_scores if len(semantic_scores) == len(candidates) else [0.0] * len(candidates)
    excess = _span_semantic_excess(scores)
    ranked = sorted(
        zip(candidates, excess, strict=True),
        key=lambda row: (
            1.25 * row[0].line_anchor_score
            + 1.00 * row[0].retrieval_anchor_score
            + 0.78 * row[1]
            + 0.17 * row[0].lexical_score
            + 0.05 * row[0].structural_score,
            row[1],
            row[0].lexical_score,
            -len(row[0].line_indices),
            -row[0].line_indices[0],
        ),
        reverse=True,
    )
    final_scores = [
        1.25 * candidate.line_anchor_score
        + 1.00 * candidate.retrieval_anchor_score
        + 0.78 * semantic
        + 0.17 * candidate.lexical_score
        + 0.05 * candidate.structural_score
        for candidate, semantic in ranked
    ]
    best = final_scores[0]
    cutoff = max(0.08, best * 0.45)
    selected: set[int] = set(signature_indices)
    selected_body: set[int] = set()
    selected_spans = 0
    for (candidate, _semantic), score in zip(ranked, final_scores, strict=True):
        if selected_spans > 0 and score < cutoff:
            break
        line_priority = [
            *candidate.anchored_line_indices,
            *(
                index
                for index in candidate.line_indices
                if index not in candidate.anchored_line_indices
            ),
        ]
        new_lines = [index for index in line_priority if index not in selected]
        if not new_lines:
            continue
        remaining = max_body_lines - len(selected_body)
        if remaining <= 0:
            break
        chosen = new_lines[:remaining]
        selected.update(chosen)
        selected_body.update(chosen)
        selected_spans += 1
        if selected_spans >= max_selected_spans:
            break

    if not selected_body:
        selected_body.update(ranked[0][0].line_indices[:max_body_lines])

    out_lines = signature.splitlines()
    out_indices: list[int | None] = list(signature_indices)
    previous = max(signature_indices, default=-1)
    for index in sorted(selected_body):
        if previous >= 0 and index > previous + 1:
            indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
            out_lines.append(indent + "...")
            out_indices.append(None)
        out_lines.append(_collapse_long_line(lines[index]))
        out_indices.append(index)
        previous = index
    return "\n".join(out_lines), tuple(out_indices)


def _span_symbol_key(sym: ContextSymbol) -> tuple[str, str, int, int]:
    return (sym.uid, sym.code or "", sym.start_line, sym.end_line)


def _apply_span_line_rerank(
    bundles: list[ContextBundle],
    *,
    query_text: str,
    score_fn: SpanScoreFn | None,
    max_candidates_per_symbol: int,
    max_body_lines: int,
    max_symbols: int = 48,
) -> list[ContextBundle]:
    """Replace symbol bodies with their top query-ranked internal windows."""
    unique: dict[tuple[str, str, int, int], ContextSymbol] = {}
    for bundle in bundles:
        for symbol in bundle.all_symbols():
            if symbol.code:
                unique.setdefault(_span_symbol_key(symbol), symbol)
    if not unique:
        return bundles

    ranked_keys = set(list(unique)[: max(1, int(max_symbols))])

    query_terms = _span_terms(query_text)
    query_lines = _span_query_line_numbers(query_text)
    candidates_by_key: dict[tuple[str, str, int, int], tuple[_SpanCandidate, ...]] = {}
    flat_texts: list[str] = []
    slices: dict[tuple[str, str, int, int], tuple[int, int]] = {}
    for key, symbol in unique.items():
        if key not in ranked_keys:
            continue
        candidates = _span_candidates(
            symbol,
            query_terms=query_terms,
            query_lines=query_lines,
            max_candidates=max(1, int(max_candidates_per_symbol)),
        )
        candidates_by_key[key] = candidates
        start = len(flat_texts)
        flat_texts.extend(candidate.text for candidate in candidates)
        slices[key] = (start, len(flat_texts))

    semantic_scores = [0.0] * len(flat_texts)
    if score_fn is not None and flat_texts:
        try:
            scored = [float(score) for score in score_fn(flat_texts)]
            if len(scored) == len(flat_texts):
                semantic_scores = scored
        except Exception:
            pass

    rendered_by_key: dict[tuple[str, str, int, int], ContextSymbol] = {}
    for key, symbol in unique.items():
        if key not in ranked_keys:
            rendered_by_key[key] = _trim_symbol_for_mode(
                symbol,
                "signature_only",
                full_render_max_depth=0,
            )
            continue
        start, end = slices[key]
        code, indices = _span_ranked_selection(
            symbol,
            candidates_by_key[key],
            semantic_scores[start:end],
            max_body_lines=max(1, int(max_body_lines)),
        )
        rendered_by_key[key] = _replace_symbol_render(symbol, code, indices)

    return [
        cast(
            ContextBundle,
            replace(
                bundle,
                seed=rendered_by_key.get(_span_symbol_key(bundle.seed), bundle.seed),
                related=tuple(
                    rendered_by_key.get(_span_symbol_key(symbol), symbol)
                    for symbol in bundle.related
                ),
            ),
        )
        for bundle in bundles
    ]


def _class_parent_from_qualified_name(sym: ContextSymbol) -> str | None:
    """Return the parent class qualified name for callable class members.

    This is deliberately conservative. Top-level module functions also have
    dotted qualified names, so we only treat a symbol as a class member when its
    code header is callable-shaped and the qualified name has at least
    ``module.Class.member`` structure. Ambiguous singletons are left unfolded.
    """
    qn = sym.qualified_name.strip()
    if not qn or "." not in qn:
        return None
    raw_signature = _code_signature(sym.code)
    signature = raw_signature.lstrip()
    if signature.startswith(_CLASS_DEF_PREFIX):
        return qn
    if not signature.startswith(("def ", "async def ")):
        return None
    header_line = next(
        (
            line
            for line in raw_signature.splitlines()
            if line.strip() and not line.strip().startswith("@")
        ),
        "",
    )
    if not header_line[:1].isspace():
        return None
    parts = qn.split(".")
    if len(parts) < 3:
        return None
    return ".".join(parts[:-1])


def _class_name_from_qualified_name(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1] or qualified_name


def _indent_block(code: str) -> str:
    lines = (code or "").splitlines()
    if not lines:
        return "    ..."
    non_empty = [line for line in lines if line.strip()]
    min_indent = min(
        (len(line) - len(line.lstrip(" ")) for line in non_empty),
        default=0,
    )
    out = []
    for line in lines:
        stripped_indent = line[min_indent:] if len(line) >= min_indent else line
        out.append("    " + stripped_indent if stripped_indent.strip() else "")
    return "\n".join(out)


def _is_class_definition_symbol(sym: ContextSymbol, parent: str) -> bool:
    sig = _code_signature(sym.code).lstrip()
    return sig.startswith(_CLASS_DEF_PREFIX) and sym.qualified_name == parent


def _index_class_member_groups(
    symbols: list[ContextSymbol],
) -> tuple[dict[str, list[ContextSymbol]], dict[str, str]]:
    by_parent: dict[str, list[ContextSymbol]] = {}
    parent_by_uid: dict[str, str] = {}
    for sym in symbols:
        parent = _class_parent_from_qualified_name(sym)
        if parent is None:
            continue
        by_parent.setdefault(parent, []).append(sym)
        parent_by_uid[sym.uid] = parent
    return by_parent, parent_by_uid


def _should_fold_class_group(
    parent: str,
    members: list[ContextSymbol],
    *,
    core_tier_only: bool,
) -> bool:
    has_class_symbol = any(_is_class_definition_symbol(member, parent) for member in members)
    if not (has_class_symbol or len(members) >= 2):
        return False
    if core_tier_only and not any(_file_tier_from_path(m.file_path) == "core" for m in members):
        return False
    return True


def _foldable_class_parents(
    by_parent: dict[str, list[ContextSymbol]],
    *,
    core_tier_only: bool,
) -> set[str]:
    return {
        parent
        for parent, members in by_parent.items()
        if _should_fold_class_group(parent, members, core_tier_only=core_tier_only)
    }


def _class_header_from_members(parent: str, members: list[ContextSymbol]) -> str:
    class_header = f"class {_class_name_from_qualified_name(parent)}:"
    for member in members:
        if _is_class_definition_symbol(member, parent):
            sig = _code_signature(member.code).lstrip()
            class_header = sig.splitlines()[0]
            break
    return class_header


def _folded_member_body(member: ContextSymbol, *, parent: str, compact: bool) -> str | None:
    if _is_class_definition_symbol(member, parent):
        return None
    # Honor an upstream span-rerank selection: once rendered_spans is set, the
    # member's ``code`` IS the intended render (signature for query-irrelevant
    # members, selected relevant spans for the rest). Re-deriving it from the
    # structural keep-rule below would discard that selection — the fold/span
    # collision. rendered_spans is None on the non-span-rerank path, so this
    # branch is inert by default.
    if member.rendered_spans is not None:
        return _indent_block(member.code or "")
    sig = _code_signature(member.code).lstrip()
    if compact:
        return _indent_block(sig)
    keep_full = member.distance_from_seed == 0 or member.name == "__init__"
    return _indent_block((member.code or "") if keep_full else sig or "")


def _folded_class_code(parent: str, members: list[ContextSymbol], *, compact: bool) -> str:
    class_header = _class_header_from_members(parent, members)
    body_blocks = [
        block
        for member in members
        if (block := _folded_member_body(member, parent=parent, compact=compact)) is not None
    ]
    return "\n".join([class_header, *(body_blocks or ["    ..."])])


def _folded_class_spans(
    parent: str,
    members: list[ContextSymbol],
    *,
    compact: bool,
) -> tuple[tuple[int, int], ...]:
    span_groups: list[tuple[tuple[int, int], ...]] = []
    for member in members:
        if _is_class_definition_symbol(member, parent):
            signature, indices = _code_signature_selection(member.code)
            header = signature.splitlines()[0] if signature else ""
            rendered = _replace_symbol_render(member, header, indices[:1])
            span_groups.append(rendered.effective_rendered_spans())
            continue

        # Fold inherits the span-rerank selection verbatim so the reported spans
        # (what ContextBench scores) match the rendered code above. Inert on the
        # non-span-rerank path (rendered_spans is None).
        if member.rendered_spans is not None:
            span_groups.append(member.effective_rendered_spans())
            continue
        keep_full = not compact and (member.distance_from_seed == 0 or member.name == "__init__")
        rendered = member
        if not keep_full:
            signature, indices = _code_signature_selection(member.code)
            rendered = _replace_symbol_render(member, signature, indices)
        span_groups.append(rendered.effective_rendered_spans())
    return _merge_rendered_spans(*span_groups)


def _build_folded_class_symbol(
    parent: str,
    members: list[ContextSymbol],
    *,
    compact: bool,
) -> ContextSymbol:
    # A folded block is a render of the closest source symbol, not a new class
    # node.  Keep that symbol's identity alongside its uid.  Re-labelling a
    # method uid as its parent class makes first-wins prompt dedupe discard a
    # later exact method occurrence while the surviving row no longer matches
    # the method owner.  The class-shaped code and unioned spans still make the
    # aggregation explicit through ``expansion_step="fold"``.
    first = min(members, key=lambda m: (m.distance_from_seed, m.uid))
    represented: dict[tuple[str, str, str, str], RenderedOwner] = {}
    for member in members:
        member_owners = member.represented_owners or (
            RenderedOwner(
                uid=member.uid,
                name=member.name,
                qualified_name=member.qualified_name,
                file_path=member.file_path,
            ),
        )
        for owner in member_owners:
            key = (owner.uid, owner.file_path, owner.name, owner.qualified_name)
            represented.setdefault(key, owner)
    return cast(
        ContextSymbol,
        replace(
            first,
            distance_from_seed=min(m.distance_from_seed for m in members),
            expansion_step="fold",
            code=_folded_class_code(parent, members, compact=compact),
            rendered_spans=_folded_class_spans(parent, members, compact=compact),
            represented_owners=tuple(represented.values()),
        ),
    )


def _fold_class_symbols(
    symbols: list[ContextSymbol],
    *,
    compact: bool = False,
    core_tier_only: bool = False,
) -> list[ContextSymbol]:
    """Fold already-selected class members into synthetic class blocks.

    No graph lookup occurs here: the group is built only from symbols that are
    already in the bundle. A group with one ambiguous method is left alone; a
    group with a class symbol or multiple members is safe to render as a class
    skeleton.
    """
    by_parent, parent_by_uid = _index_class_member_groups(symbols)
    foldable = _foldable_class_parents(by_parent, core_tier_only=core_tier_only)

    out: list[ContextSymbol] = []
    emitted: set[str] = set()
    for sym in symbols:
        parent = parent_by_uid.get(sym.uid)
        if parent is None or parent not in foldable:
            out.append(sym)
            continue
        if parent in emitted:
            continue
        emitted.add(parent)
        out.append(_build_folded_class_symbol(parent, by_parent[parent], compact=compact))
    return out


def _apply_fold_render(
    bundles: list[ContextBundle],
    *,
    compact: bool = False,
) -> list[ContextBundle]:
    folded: list[ContextBundle] = []
    for bundle in bundles:
        symbols = _fold_class_symbols(bundle.all_symbols(), compact=compact)
        if not symbols:
            folded.append(bundle)
            continue
        seed = symbols[0]
        related = tuple(symbols[1:])
        folded.append(replace(bundle, seed=seed, related=related))
    return folded


def _render_impact_tiered(
    bundle: ContextBundle,
    *,
    full_render_max_depth: int = 0,
) -> ContextBundle:
    """Impact echelon-2: core-tier fold groups as ``fold_compact``, rest tiered.

    * **Upper tier + fold objects** — production (``core``) class blocks fold
      into a compact skeleton (``fold_compact``).
    * **Anchor symbols** on production (``core``) files — full signature header.
    * **Everything else** — one-line signature stub (``impact_surface``).
    """
    del full_render_max_depth
    symbols = _fold_class_symbols(
        list(bundle.all_symbols()),
        compact=True,
        core_tier_only=True,
    )
    if not symbols:
        return cast(
            ContextBundle,
            replace(bundle, render_mode="impact_tiered"),
        )
    trimmed: list[ContextSymbol] = []
    for sym in symbols:
        if sym.expansion_step == "fold":
            trimmed.append(sym)
        elif sym.distance_from_seed == 0 and _file_tier_from_path(sym.file_path) == "core":
            trimmed.append(
                _trim_symbol_for_mode(
                    sym,
                    "signature_only",
                    full_render_max_depth=0,
                )
            )
        else:
            trimmed.append(
                _trim_symbol_for_mode(
                    sym,
                    "impact_surface",
                    full_render_max_depth=0,
                )
            )
    seed = trimmed[0]
    related = tuple(trimmed[1:])
    return cast(
        ContextBundle,
        replace(bundle, seed=seed, related=related, render_mode="impact_tiered"),
    )


def _trim_symbol_for_mode(
    sym: ContextSymbol,
    render_mode: str,
    *,
    full_render_max_depth: int,
) -> ContextSymbol:
    if render_mode == "impact_surface":
        code, impact_indices = _code_impact_surface_selection(
            sym.code,
            qualified_name=sym.qualified_name,
            name=sym.name,
        )
        return _replace_symbol_render(sym, code, impact_indices)
    if render_mode == "signature_only":
        code, signature_indices = _code_signature_selection(sym.code)
        return _replace_symbol_render(sym, code, signature_indices)
    if render_mode == "hybrid_compact":
        if sym.distance_from_seed <= full_render_max_depth:
            code, compact_indices = _code_compact_selection(sym.code)
            return _replace_symbol_render(sym, code, compact_indices)
        code, compact_signature_indices = _code_signature_selection(sym.code)
        return _replace_symbol_render(sym, code, compact_signature_indices)
    if render_mode == "hybrid" and sym.distance_from_seed > full_render_max_depth:
        code, hybrid_signature_indices = _code_signature_selection(sym.code)
        return _replace_symbol_render(sym, code, hybrid_signature_indices)
    return sym


def _render_bundle(
    bundle: ContextBundle,
    render_mode: str,
    *,
    full_render_max_depth: int = 0,
) -> ContextBundle:
    if render_mode == "impact_tiered":
        return _render_impact_tiered(bundle, full_render_max_depth=full_render_max_depth)
    if render_mode in ("fold", "fold_compact"):
        rendered = _apply_fold_render(
            [bundle],
            compact=render_mode == "fold_compact",
        )[0]
        if rendered == bundle:
            return bundle
    elif render_mode in (
        "impact_surface",
        "signature_only",
        "hybrid",
        "hybrid_compact",
    ):
        rendered = cast(
            ContextBundle,
            replace(
                bundle,
                seed=_trim_symbol_for_mode(
                    bundle.seed,
                    render_mode,
                    full_render_max_depth=full_render_max_depth,
                ),
                related=tuple(
                    _trim_symbol_for_mode(
                        rel,
                        render_mode,
                        full_render_max_depth=full_render_max_depth,
                    )
                    for rel in bundle.related
                ),
            ),
        )
    else:
        rendered = bundle
    return cast(ContextBundle, replace(rendered, render_mode=render_mode))


def _bundle_token_count(bundle: ContextBundle) -> int:
    return sum(estimate_text_tokens(sym.code or "") for sym in bundle.all_symbols())


def _render_modes_for_credit(initial_mode: str) -> tuple[str, ...]:
    if initial_mode not in _RENDER_LADDER:
        return (initial_mode, "fold", "signature_only")
    idx = _RENDER_LADDER.index(initial_mode)
    return tuple(reversed(_RENDER_LADDER[: idx + 1]))


def _render_with_transaction_limit(
    bundle: ContextBundle,
    initial_mode: str,
    *,
    per_transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]] | None = None,
) -> tuple[ContextBundle, int]:
    # Within one budget pass per_transaction_limit/full_render_max_depth are
    # constant, so (bundle, initial_mode) fully determines the render. The
    # upgrade loop re-asks for the same modes across repeated pushes; memoise.
    cache_key = (id(bundle), initial_mode)
    if render_cache is not None and cache_key in render_cache:
        return render_cache[cache_key]
    last: tuple[ContextBundle, int] | None = None
    result: tuple[ContextBundle, int] | None = None
    for mode in _render_modes_for_credit(initial_mode):
        rendered = _render_bundle(
            bundle,
            mode,
            full_render_max_depth=full_render_max_depth,
        )
        cost = _bundle_token_count(rendered)
        last = (rendered, cost)
        if cost <= per_transaction_limit or mode in ("impact_surface", "signature_only"):
            result = (rendered, cost)
            break
    if result is None:
        assert last is not None
        result = last
    if render_cache is not None:
        render_cache[cache_key] = result
    return result


#: Utility added per extra class-member a bundle folds into a class block.
#: A fold-block stands for several symbols at a cheap folded cost, so a bundle
#: carrying a coherent N-method class ranks above a lone symbol of equal base
#: utility (the design's fold aggregation bonus). It is 0 until ``qualified_name``
#: is indexed (no qn -> no fold grouping -> no bonus), so it stays graceful
#: pre-reindex.
#: Kept deliberately even though it is file_recall-neutral in ablation: the
#: bonus optimizes fold PACKING DENSITY (a coherent class block per token), which
#: file_recall cannot measure — recall-neutral here does not mean useless.
FOLD_AGGREGATION_BONUS = 0.1

_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})
_EXAMPLE_SEGMENTS = frozenset(
    {
        "benchmarks",
        "codemods",
        "demo",
        "demos",
        "docs_src",
        "example",
        "examples",
        "sample",
        "samples",
        "tutorial",
        "tutorials",
    }
)
_DOC_SEGMENTS = frozenset({"doc", "docs", "documentation"})
_CORE_TIER_WEIGHT = {
    "core": 1.0,
    "stub": 0.5,
    "doc": 0.35,
    "example": 0.25,
    "test": 0.15,
}
_MODE_TIER_WEIGHT = {
    "core": 1.0,
    "stub": 0.5,
    "doc": 0.6,
    "example": 0.6,
    "test": 1.0,
}
_STRUCTURAL_BRIDGE_STEP_BONUS = {
    "deferred_runtime_dispatch": 0.45,
    "hook_transparency": 1.50,
}


def _fold_aggregation_bonus(bundle: ContextBundle, *, per_member: float) -> float:
    """Queue-utility bonus for a bundle that folds class members into blocks.

    Counts in-context members beyond the first in each foldable (>=2-member)
    class group — the same grouping ``_fold_class_symbols`` renders — so a bundle
    holding a 3-method class block outranks a single-symbol bundle of equal base
    utility. Bundles with no foldable group get 0 (unchanged ordering).
    """
    by_parent: dict[str, int] = {}
    for sym in bundle.all_symbols():
        parent = _class_parent_from_qualified_name(sym)
        if parent is not None:
            by_parent[parent] = by_parent.get(parent, 0) + 1
    extra = sum(count - 1 for count in by_parent.values() if count >= 2)
    return per_member * extra


def _bundle_files(bundle: ContextBundle) -> set[str]:
    return {sym.file_path for sym in bundle.all_symbols() if sym.file_path}


def _bundle_steps(bundle: ContextBundle) -> set[str]:
    return {sym.expansion_step for sym in bundle.all_symbols() if sym.expansion_step}


def _structural_bridge_bonus(bundle: ContextBundle) -> float:
    primary = _primary_file(bundle)
    if not primary:
        return 0.0
    bonus = 0.0
    seen: set[tuple[str, str]] = set()
    impact_mode = _bundle_impact_mode(bundle)
    for sym in bundle.related:
        step = sym.expansion_step
        if not step or not sym.file_path:
            continue
        if sym.file_path == primary:
            continue
        key = (step, sym.file_path)
        if key in seen:
            continue
        seen.add(key)
        step_bonus = _STRUCTURAL_BRIDGE_STEP_BONUS.get(step, 0.08)
        bonus += step_bonus * _tier_weight_for_path(sym.file_path, impact_mode=impact_mode)
    return bonus


def _primary_file(bundle: ContextBundle) -> str:
    return bundle.seed.file_path or ""


def _base_credit_utility(bundle: ContextBundle) -> float:
    return max(0.0, bundle.utility_score) + _fold_aggregation_bonus(
        bundle, per_member=FOLD_AGGREGATION_BONUS
    )


@lru_cache(maxsize=4096)
def _file_tier_from_path(path: str) -> str:
    if not path:
        return "core"
    norm = path.replace("\\", "/").lower()
    if is_test_path(norm):
        return "test"
    parts = [p for p in norm.split("/") if p]
    if any(part in _EXAMPLE_SEGMENTS for part in parts):
        return "example"
    if any(part in _DOC_SEGMENTS for part in parts):
        return "doc"
    if norm.endswith(".pyi"):
        return "stub"
    return "core"


def _tier_weight_for_path(path: str, *, impact_mode: bool) -> float:
    table = _MODE_TIER_WEIGHT if impact_mode else _CORE_TIER_WEIGHT
    return table.get(_file_tier_from_path(path), 1.0)


def _bundle_impact_mode(bundle: ContextBundle) -> bool:
    return bundle.role in _MODE_ROLES


def _bundle_tier_weight(bundle: ContextBundle) -> float:
    files = _bundle_files(bundle)
    if not files:
        return 1.0
    impact_mode = _bundle_impact_mode(bundle)
    weights = [_tier_weight_for_path(path, impact_mode=impact_mode) for path in files]
    primary = _primary_file(bundle)
    if primary:
        weights.append(_tier_weight_for_path(primary, impact_mode=impact_mode))
    return max(weights)


def _initial_credit_render(
    bundle: ContextBundle,
    *,
    transaction_limit: int,
    full_render_max_depth: int,
    initial_mode: str = "signature_only",
    signature_only_initial: bool = False,
) -> tuple[ContextBundle, int]:
    """Cheap coverage render: signatures everywhere, compact fold when cheap.

    A compact fold block is still a cheap coverage artifact, but it carries
    class topology that a plain signature loses. Passive seeds stay
    signature-first; they are the breadth reservoir and can be upgraded later.
    Impact profile uses ``impact_tiered``: core-tier fold groups render as
    ``fold_compact`` blocks; every other symbol keeps a full signature header.
    """
    if initial_mode == "impact_tiered":
        rendered = _render_impact_tiered(
            bundle,
            full_render_max_depth=full_render_max_depth,
        )
        return rendered, _bundle_token_count(rendered)
    if signature_only_initial or bundle.passive:
        rendered = _render_bundle(
            bundle,
            initial_mode,
            full_render_max_depth=full_render_max_depth,
        )
        return rendered, _bundle_token_count(rendered)
    signature = _render_bundle(
        bundle,
        "signature_only",
        full_render_max_depth=full_render_max_depth,
    )
    signature_cost = _bundle_token_count(signature)
    if bundle.passive:
        return signature, signature_cost
    folded = _render_bundle(
        bundle,
        "fold_compact",
        full_render_max_depth=full_render_max_depth,
    )
    if folded.render_mode != "fold_compact":
        return signature, signature_cost
    folded_cost = _bundle_token_count(folded)
    if folded_cost <= transaction_limit:
        return folded, folded_cost
    return signature, signature_cost


def _target_credit_modes(bundle: ContextBundle, render_mode: str) -> tuple[str, ...]:
    """Upgrade rungs available to a bundle under credit budgeting.

    The profile ``render_mode`` shapes the cheap initial coverage, but it is
    NOT a hard ceiling any more: the exact printed-cost accounting is what
    bounds spending now, and capping the ladder at the profile mode left
    budget unspendable while answer bodies sat one rung above (a ``hybrid``
    profile renders related symbols as signatures forever — pydantic_q05's
    gold froze at 18 tokens with 4.7k unspent). ``impact_tiered`` keeps its
    tiered base render and climbs the rich rungs from there.
    """
    del bundle
    if render_mode == "impact_tiered":
        return ("impact_tiered", "hybrid", "full")
    return _RENDER_LADDER


def _next_upgrade_render(
    bundle: ContextBundle,
    *,
    current_mode: str,
    current_cost: int,
    render_mode: str,
    transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict | None = None,
) -> tuple[ContextBundle, int] | None:
    modes = _target_credit_modes(bundle, render_mode)
    try:
        start = modes.index(current_mode) + 1
    except ValueError:
        start = 0
    for mode in modes[start:]:
        rendered, cost = _render_with_transaction_limit(
            bundle,
            mode,
            per_transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
        )
        if mode.startswith("fold") and rendered.render_mode != mode:
            continue
        # Strict progress only. Equal-cost collapses (e.g. hybrid → fold_compact
        # under a tight transaction_limit) used to oscillate forever with
        # out-of-ladder currents like fold_compact ↔ impact_tiered at the same
        # printed size — hung axis_benchmark on SWE-bench django__django-15400.
        if rendered.render_mode == current_mode or cost <= current_cost:
            continue
        try:
            if modes.index(rendered.render_mode) < start:
                continue
        except ValueError:
            continue
        return rendered, cost
    return None


#: Selection-independent per-bundle inputs to the coverage/upgrade gains.
#: Computed once per budget pass so the lazy-greedy heap re-evaluations reuse the
#: expensive structural rollups instead of recomputing them on every pop.
_BundleStatic = namedtuple(
    "_BundleStatic",
    "files steps tier_weight impact_mode base_utility structural_bridge",
)


def _dedupe_bundles_by_seed_uid(bundles: list[ContextBundle]) -> list[ContextBundle]:
    """One packer row per seed symbol — axis flattening repeats the same uid.

    Impact pools often carry the same symbol on seeds, structural walks, and
    impact_analysis (185 rows, 172 uids). Marginal packing runs on unique
    symbols; the highest-utility row wins when roles disagree.
    """
    best: dict[str, ContextBundle] = {}
    order: list[str] = []
    for bundle in bundles:
        if bundle.seed is None or not bundle.seed.uid:
            continue
        uid = bundle.seed.uid
        if uid not in best:
            order.append(uid)
            best[uid] = bundle
        elif bundle.utility_score > best[uid].utility_score:
            best[uid] = bundle
    return [best[uid] for uid in order]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    center = _median(values)
    return _median([abs(v - center) for v in values])


def _noise_level_from_tail(
    tail: list[float],
    *,
    k: float = 1.0,
) -> float:
    """``median(tail) + k * 1.4826 * MAD(tail)`` with ``noise >= 1``."""
    if not tail:
        return 1.0
    med = _median(tail)
    mad = _mad(tail)
    return max(1.0, med + k * 1.4826 * mad)


def _leader_pool_metrics(
    static: list[_BundleStatic],
    *,
    k: float = 1.0,
) -> tuple[float, int]:
    """Tail noise threshold and leader breadth for the ladder cap.

    Floor and signal live on the SAME axis: ``marginality = 100 * (u/peak)``
    (closeness to peak). The noise floor is the robust location + spread of
    the weak half's marginality::

        noise_level = median(marginality_tail) + k * 1.4826 * MAD(tail)

    Leaders are symbols with ``marginality > noise_level`` — above what the
    noise typically reaches. A weaker tail LOWERS the floor (signal stands out
    against weak noise); a tail hugging the peak raises it (nothing clearly
    leads). The pre-2026-07 form measured the floor on the complementary
    distance-from-peak axis, which inverted that response.  Then::

        %leader = 100 / COUNT(marginality > noise_level)

    Weak symbols below the floor are excluded from the divisor; they still enter
    via marginal packer on leftover budget.
    """
    n = len(static)
    if n <= 1:
        return 1.0, 1
    utilities = [max(1e-9, st.base_utility) for st in static]
    peak = max(utilities)
    sorted_u = sorted(utilities)
    median_u = _median(sorted_u)
    tail_u = [u for u in sorted_u if u <= median_u]
    if not tail_u:
        tail_u = sorted_u
    tail = [100.0 * (u / peak) for u in tail_u]
    noise_level = _noise_level_from_tail(tail, k=k)
    leader_count = sum(1 for u in utilities if (100.0 * u / peak) > noise_level)
    return noise_level, max(1, leader_count)


def _leader_transaction_limit(
    token_budget: int,
    *,
    leader_count: int,
) -> int:
    """Per-symbol ladder-step cap: ``budget * (100 / leader_count) / 100``."""
    if token_budget <= 0:
        return 1
    count = max(1, leader_count)
    if count <= 1:
        return max(1, int(token_budget))
    leader_share = (100.0 / count) / 100.0
    return max(1, int(token_budget * leader_share))


_UPGRADE_MODE_BONUS: dict[str, float] = {
    "span_evidence": 0.18,
    "seed_body": 0.38,
    "related_body": 0.30,
    "impact_tiered": 0.12,
    "impact_surface": 0.08,
    "fold_compact": 0.18,
    "fold": 0.22,
    "hybrid_compact": 0.25,
    "hybrid": 0.28,
    "full": 0.38,
}


@dataclass
class _TokenCreditCoverageState:
    file_soft_cap: int
    covered_files: set[str] = field(default_factory=set)
    covered_roles: set[str] = field(default_factory=set)
    covered_steps: set[str] = field(default_factory=set)
    file_counts: dict[str, int] = field(default_factory=dict)
    file_tokens: dict[str, int] = field(default_factory=dict)
    # First-wins uid set. The prompt path (``axis_bundles_to_prompt_context``)
    # dedupes symbols by uid keeping the FIRST occurrence in bundle order, so
    # a uid repeated across bundles prints exactly once. Purchases price only
    # uids not yet present (exact during phase 1 — entries are append-only and
    # renders don't change there). Upgrades change renders and may DROP uids
    # from a bundle (fold regrouping), which shifts which occurrence prints —
    # so the upgrade phase never trusts membership bookkeeping and re-derives
    # printed size from the selected entries directly
    # (``_first_wins_printed_tokens``).
    printed_uids: set[str] = field(default_factory=set)
    cutoff_rejections: int = 0

    def marginal_purchase_cost(self, rendered: ContextBundle) -> int:
        seen: set[str] = set()
        total = 0
        for sym in rendered.all_symbols():
            uid = sym.uid
            if uid in seen or uid in self.printed_uids:
                continue
            seen.add(uid)
            total += estimate_text_tokens(sym.code or "")
        return total

    def charge_purchase(self, rendered: ContextBundle) -> None:
        for sym in rendered.all_symbols():
            self.printed_uids.add(sym.uid)

    def file_saturation_penalty(self, bundle: ContextBundle) -> float:
        primary = _primary_file(bundle)
        if not primary:
            return 0.0
        return min(0.5, self.file_tokens.get(primary, 0) / self.file_soft_cap)

    def record_selection(self, bundle: ContextBundle, cost: int) -> None:
        files = _bundle_files(bundle)
        self.covered_files.update(files)
        self.covered_roles.add(bundle.role)
        self.covered_steps.update(_bundle_steps(bundle))
        for path in files:
            self.file_counts[path] = self.file_counts.get(path, 0) + 1
        primary = _primary_file(bundle)
        if primary:
            self.file_tokens[primary] = self.file_tokens.get(primary, 0) + cost


def _bundle_static_rows(bundles: list[ContextBundle]) -> list[_BundleStatic]:
    return [
        _BundleStatic(
            files=_bundle_files(b),
            steps=_bundle_steps(b),
            tier_weight=_bundle_tier_weight(b),
            impact_mode=_bundle_impact_mode(b),
            base_utility=_base_credit_utility(b),
            structural_bridge=_structural_bridge_bonus(b),
        )
        for b in bundles
    ]


def _symbol_semantic_value(symbol: ContextSymbol) -> float:
    """Query-calibrated value carried by one rendered symbol in ``[0, 1]``."""
    return min(1.0, max(0.0, symbol.semantic_excess * symbol.tier_weight))


def _marginal_render_semantic_value(
    rendered: ContextBundle,
    *,
    already_printed: set[str],
) -> float:
    """Token-weighted semantic value of UIDs this render newly prints."""
    seen: set[str] = set()
    weighted_value = 0.0
    token_total = 0
    for symbol in rendered.all_symbols():
        if symbol.uid in seen or symbol.uid in already_printed:
            continue
        seen.add(symbol.uid)
        tokens = estimate_text_tokens(symbol.code or "")
        if tokens <= 0:
            continue
        weighted_value += tokens * _symbol_semantic_value(symbol)
        token_total += tokens
    return weighted_value / max(1, token_total)


def _credit_coverage_gain_raw(
    idx: int,
    bundle: ContextBundle,
    *,
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    rendered: ContextBundle | None = None,
    node_semantic_utility_weight: float = 0.0,
) -> float:
    st = static[idx]
    files = st.files
    new_files = files - state.covered_files
    new_steps = st.steps - state.covered_steps
    gain: float = st.base_utility * st.tier_weight
    if new_files:
        new_file_weight = max(
            _tier_weight_for_path(path, impact_mode=st.impact_mode) for path in new_files
        )
        gain += (0.35 + 0.08 * max(0, len(new_files) - 1)) * new_file_weight
    if bundle.role not in state.covered_roles:
        gain += 0.20
    gain += 0.10 * len(new_steps)
    gain += st.structural_bridge * st.tier_weight
    gain += 0.05 if not bundle.passive else -0.03
    gain -= 0.15 * sum(state.file_counts.get(path, 0) for path in files)
    gain -= state.file_saturation_penalty(bundle)
    if rendered is not None and node_semantic_utility_weight > 0.0:
        gain += node_semantic_utility_weight * _marginal_render_semantic_value(
            rendered,
            already_printed=state.printed_uids,
        )
    return gain


def _credit_coverage_gain(
    idx: int,
    bundle: ContextBundle,
    *,
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
) -> float:
    return max(0.001, _credit_coverage_gain_raw(idx, bundle, static=static, state=state))


def _credit_optimistic_value(st: _BundleStatic, bundle: ContextBundle, cost: int) -> float:
    optimistic_gain = st.base_utility + 0.35 + 0.20
    optimistic_gain *= st.tier_weight
    optimistic_gain += 0.10 * len(st.steps)
    optimistic_gain += st.structural_bridge * st.tier_weight
    if not bundle.passive:
        optimistic_gain += 0.05
    return float(optimistic_gain / max(1, cost))


def _build_initial_credit_heap(
    bundles: list[ContextBundle],
    static: list[_BundleStatic],
    *,
    transaction_limit: int,
    full_render_max_depth: int,
    initial_mode: str,
    signature_only_initial: bool,
) -> list[tuple[float, int, int, ContextBundle]]:
    initial: list[tuple[float, int, int, ContextBundle]] = []
    for idx, bundle in enumerate(bundles):
        rendered, cost = _initial_credit_render(
            bundle,
            transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            initial_mode=initial_mode,
            signature_only_initial=signature_only_initial,
        )
        value = _credit_optimistic_value(static[idx], bundle, cost)
        heapq.heappush(initial, (-value, idx, cost, rendered))
    return initial


def _select_bundles_under_credit_budget(
    initial: list[tuple[float, int, int, ContextBundle]],
    bundles: list[ContextBundle],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    token_budget: int,
    credit_trace: TokenCreditTrace | None = None,
    min_utility_per_token: float | None = None,
    node_semantic_utility_weight: float = 0.0,
) -> tuple[list[dict[str, object]], int]:
    selected: list[dict[str, object]] = []
    selected_indices: set[int] = set()
    skipped_indices: set[int] = set()
    used = 0

    while initial:
        _neg_value, idx, cost, rendered = heapq.heappop(initial)
        if idx in selected_indices or idx in skipped_indices:
            continue
        source = bundles[idx]
        # ``cost`` is the gross render size (kept on the entry for ladder
        # monotonicity); the budget pays only the marginal price — tokens of
        # uids the prompt's first-wins dedupe will actually print.
        marginal_cost = state.marginal_purchase_cost(rendered)
        semantic_delta_utility = _marginal_render_semantic_value(
            rendered,
            already_printed=state.printed_uids,
        )
        raw_delta_utility = _credit_coverage_gain_raw(
            idx,
            source,
            static=static,
            state=state,
            rendered=rendered,
            node_semantic_utility_weight=node_semantic_utility_weight,
        )
        effective_utility = max(0.001, raw_delta_utility)
        current_value = effective_utility / max(1, marginal_cost)
        best_competing = -initial[0][0] if initial else -1.0
        if current_value + 1e-12 < best_competing:
            heapq.heappush(initial, (-current_value, idx, cost, rendered))
            continue
        raw_density = raw_delta_utility / max(1, marginal_cost)
        if (
            min_utility_per_token is not None
            and marginal_cost > 0
            and raw_density <= min_utility_per_token
        ):
            skipped_indices.add(idx)
            state.cutoff_rejections += 1
            if credit_trace is not None:
                credit_trace.cutoff_rejections += 1
            continue
        if selected and used + marginal_cost > token_budget:
            skipped_indices.add(idx)
            continue
        selected.append(
            {
                "index": idx,
                "source": source,
                "rendered": rendered,
                "cost": cost,
            }
        )
        selected_indices.add(idx)
        used += marginal_cost
        if credit_trace is not None:
            files = static[idx].files
            steps = static[idx].steps
            credit_trace.record(
                phase="coverage",
                bundle=source,
                render_mode=rendered.render_mode,
                delta_utility=raw_delta_utility,
                effective_utility=effective_utility,
                delta_tokens=marginal_cost,
                new_files=len(files - state.covered_files),
                new_role=source.role not in state.covered_roles,
                new_steps=len(steps - state.covered_steps),
                semantic_delta_utility=semantic_delta_utility,
            )
        state.record_selection(source, marginal_cost)
        state.charge_purchase(rendered)
        if used >= token_budget:
            break
    return selected, used


def _first_wins_printed_tokens(selected: list[dict[str, object]]) -> int:
    """Exact deduped-prompt size of the selected renders (first-wins by uid).

    This is the ground truth ``used`` must track: renders change during the
    upgrade phase and fold regrouping can drop uids from a bundle, silently
    promoting a later bundle's occurrence into the prompt — so printed size is
    re-derived from the entries, never inferred from ownership bookkeeping.
    Cost is a ``len()`` sweep over selected symbols — negligible.
    """
    seen: set[str] = set()
    total = 0
    for entry in selected:
        rendered = entry.get("rendered")
        if not isinstance(rendered, ContextBundle):
            continue
        for sym in rendered.all_symbols():
            if sym.uid in seen:
                continue
            seen.add(sym.uid)
            total += estimate_text_tokens(sym.code or "")
    return total


def _first_wins_printed_symbols(
    selected: list[dict[str, object]],
) -> dict[str, ContextSymbol]:
    """Return the actual prompt owner for every UID in a selected render set.

    Prompt assembly is first-wins by UID.  Upgrade utility must use exactly
    the same ownership model: an expanded duplicate can be free, while an
    upgrade that makes an earlier occurrence win can change what the prompt
    actually contains.
    """
    printed: dict[str, ContextSymbol] = {}
    for entry in selected:
        rendered = entry.get("rendered")
        if not isinstance(rendered, ContextBundle):
            continue
        for symbol in rendered.all_symbols():
            printed.setdefault(symbol.uid, symbol)
    return printed


def _upgrade_semantic_value(
    selected: list[dict[str, object]],
    entry_index: int,
    upgraded: ContextBundle,
) -> float:
    """Token-weighted query value of text newly printed by one upgrade.

    This is deliberately a *delta*, not the average quality of the upgraded
    bundle.  A high-similarity symbol that was already printed elsewhere must
    not receive credit twice; a body expansion receives credit only for its
    newly visible tokens.  The resulting ``[0, 1]`` value is multiplied by the
    opt-in budget weight in the same units as the structural credit score.
    """
    before = _first_wins_printed_symbols(selected)
    entry = selected[entry_index]
    previous = entry.get("rendered")
    entry["rendered"] = upgraded
    try:
        after = _first_wins_printed_symbols(selected)
    finally:
        entry["rendered"] = previous

    weighted_value = 0.0
    added_tokens = 0
    for uid, symbol in after.items():
        before_symbol = before.get(uid)
        before_tokens = estimate_text_tokens(before_symbol.code or "") if before_symbol else 0
        delta_tokens = estimate_text_tokens(symbol.code or "") - before_tokens
        if delta_tokens <= 0:
            continue
        weighted_value += delta_tokens * _symbol_semantic_value(symbol)
        added_tokens += delta_tokens
    return weighted_value / max(1, added_tokens)


def _symbol_has_direct_retrieval_evidence(symbol: ContextSymbol) -> bool:
    return bool(
        symbol.exact_symbol_match
        or symbol.retrieval_channels
        or symbol.retrieval_spans
        or symbol.role in {"anchor_symbol", "overlay_anchor"}
    )


def _symbol_has_strong_body_evidence(symbol: ContextSymbol) -> bool:
    """Evidence strong enough to buy a neighbour body, not just coverage."""
    if symbol.exact_symbol_match or symbol.role in {"anchor_symbol", "overlay_anchor"}:
        return True
    families = retrieval_channel_families(symbol.retrieval_channels)
    if "semantic_chunk" in symbol.retrieval_channels or len(families) >= 2:
        return True
    return bool(symbol.retrieval_spans and float(symbol.lexical_span_score or 0.0) >= 0.15)


def _symbol_has_graph_body_evidence(
    symbol: ContextSymbol,
) -> bool:
    """Own query evidence strong enough for an otherwise graph-only body."""
    return symbol.semantic_excess >= 0.50


def _upgrade_token_attribution(
    selected: list[dict[str, object]],
    entry_index: int,
    upgraded: ContextBundle,
    *,
    entry_source: ContextBundle,
    exact_delta: int,
) -> tuple[TokenCreditAttribution, ...]:
    """Attribute the exact paid delta to changed first-wins symbols.

    A fold swap can add some symbol bodies while dropping text elsewhere, so
    positive per-symbol deltas may exceed the transaction's net price. Scale
    grouped positive deltas back to ``exact_delta`` with deterministic largest
    remainders; audit totals then stay exactly reconcilable with Token Credit.
    """
    if exact_delta <= 0:
        return ()
    before = _first_wins_printed_symbols(selected)
    entry = selected[entry_index]
    previous = entry.get("rendered")
    entry["rendered"] = upgraded
    try:
        after = _first_wins_printed_symbols(selected)
    finally:
        entry["rendered"] = previous

    raw_by_key: Counter[tuple[str, str, str, int]] = Counter()
    for uid, symbol in after.items():
        before_symbol = before.get(uid)
        before_tokens = estimate_text_tokens(before_symbol.code or "") if before_symbol else 0
        added = estimate_text_tokens(symbol.code or "") - before_tokens
        if added <= 0:
            continue
        scope = "seed" if uid == entry_source.seed.uid else "related"
        evidence = (
            "retrieval_backed" if _symbol_has_direct_retrieval_evidence(symbol) else "graph_only"
        )
        edge_type = symbol.edge_type or symbol.expansion_step or ("SEED" if scope == "seed" else "")
        raw_by_key[(scope, evidence, edge_type, max(0, int(symbol.distance_from_seed)))] += added

    raw_total = sum(raw_by_key.values())
    if raw_total <= 0:
        evidence = (
            "retrieval_backed"
            if _symbol_has_direct_retrieval_evidence(entry_source.seed)
            else "graph_only"
        )
        return (TokenCreditAttribution("seed", evidence, "SEED", 0, exact_delta),)

    allocations: list[list[Any]] = []
    allocated = 0
    for key in sorted(raw_by_key):
        numerator = exact_delta * raw_by_key[key]
        whole, remainder = divmod(numerator, raw_total)
        allocations.append([key, whole, remainder])
        allocated += whole
    for row in sorted(allocations, key=lambda item: (-item[2], item[0]))[: exact_delta - allocated]:
        row[1] += 1
    return tuple(
        TokenCreditAttribution(
            scope=key[0],
            evidence=key[1],
            edge_type=key[2],
            depth=key[3],
            delta_tokens=int(tokens),
        )
        for key, tokens, _remainder in allocations
        if tokens > 0
    )


def _upgrade_exact_delta(
    selected: list[dict[str, object]],
    entry_index: int,
    upgraded: ContextBundle,
    *,
    printed_before: int | None = None,
) -> int:
    """Printed-size delta of swapping ``entry_index``'s render for ``upgraded``.

    ``printed_before`` lets callers that maintain the ``used == printed``
    invariant skip the pre-swap sweep (the upgrade loop calls this per
    enqueue/pop, so the saved sweep is the dominant accounting cost).
    """
    entry = selected[entry_index]
    previous = entry.get("rendered")
    if printed_before is None:
        printed_before = _first_wins_printed_tokens(selected)
    entry["rendered"] = upgraded
    after = _first_wins_printed_tokens(selected)
    entry["rendered"] = previous
    return after - printed_before


def _credit_upgrade_entry_parts(
    entry: dict[str, object],
) -> tuple[ContextBundle, ContextBundle, int, int] | None:
    entry_source = entry.get("source")
    current = entry.get("rendered")
    current_cost = entry.get("cost")
    bundle_index = entry.get("index")
    if not isinstance(current, ContextBundle) or not isinstance(current_cost, int):
        return None
    if not isinstance(entry_source, ContextBundle):
        return None
    if not isinstance(bundle_index, int):
        return None
    return entry_source, current, current_cost, bundle_index


def _credit_upgrade_gain_raw(
    st: _BundleStatic,
    bundle: ContextBundle,
    rendered: ContextBundle,
    state: _TokenCreditCoverageState,
    *,
    semantic_delta_utility: float = 0.0,
    node_semantic_utility_weight: float = 0.0,
) -> float:
    mode_bonus = _UPGRADE_MODE_BONUS.get(rendered.render_mode, 0.10)
    gain: float = 0.35 * st.base_utility * st.tier_weight
    gain += mode_bonus
    gain += 0.05 if not bundle.passive else 0.03
    gain -= state.file_saturation_penalty(bundle)
    if node_semantic_utility_weight > 0.0:
        gain += node_semantic_utility_weight * semantic_delta_utility
    return gain


def _credit_upgrade_gain(
    st: _BundleStatic,
    bundle: ContextBundle,
    rendered: ContextBundle,
    state: _TokenCreditCoverageState,
    *,
    semantic_delta_utility: float = 0.0,
    node_semantic_utility_weight: float = 0.0,
) -> float:
    return max(
        0.001,
        _credit_upgrade_gain_raw(
            st,
            bundle,
            rendered,
            state,
            semantic_delta_utility=semantic_delta_utility,
            node_semantic_utility_weight=node_semantic_utility_weight,
        ),
    )


def _enqueue_credit_upgrade(
    entry_index: int,
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    upgrade_heap: list[tuple[float, int, int, ContextBundle, int]],
    *,
    render_mode: str,
    transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]],
    printed_tokens: int,
    node_semantic_utility_weight: float,
) -> None:
    parts = _credit_upgrade_entry_parts(selected[entry_index])
    if parts is None:
        return
    entry_source, current, current_cost, bundle_index = parts
    candidate = _next_upgrade_render(
        entry_source,
        current_mode=current.render_mode,
        current_cost=current_cost,
        render_mode=render_mode,
        transaction_limit=transaction_limit,
        full_render_max_depth=full_render_max_depth,
        render_cache=render_cache,
    )
    if candidate is None:
        return
    upgraded, upgraded_cost = candidate
    if upgraded_cost < current_cost:
        return
    # Priority divides by the exact printed delta: re-rendering uids whose
    # first-wins occurrence lives in another entry costs nothing in the
    # prompt, so such upgrades rank high.
    exact_delta = _upgrade_exact_delta(
        selected, entry_index, upgraded, printed_before=printed_tokens
    )
    semantic_delta_utility = _upgrade_semantic_value(selected, entry_index, upgraded)
    structural_priority = _credit_upgrade_gain(
        static[bundle_index],
        entry_source,
        upgraded,
        state,
        semantic_delta_utility=semantic_delta_utility,
        node_semantic_utility_weight=node_semantic_utility_weight,
    ) / max(1, exact_delta)
    if _AUCTION_SEMANTIC_PRIMARY:
        # Decoupled design: order upgrades by the seed's RAW query↔node cosine so
        # the budget flows to the most query-relevant bundles first, fully
        # upgrading the head before the low-relevance tail is touched (which then
        # stays at its cheap render). We deliberately use raw query_similarity,
        # NOT the calibrated semantic_excess/_symbol_semantic_value: the
        # median+MAD noise floor zeroes that signal for ~half the symbols
        # (measured), destroying the ordering the sim showed works. The structural
        # density is a bounded tie-break for ladder steps within one bundle.
        seed_sim = entry_source.seed.query_similarity
        seed_rank = seed_sim if seed_sim is not None else 0.0
        priority = seed_rank + 1e-4 * structural_priority
    else:
        priority = structural_priority
    heapq.heappush(
        upgrade_heap,
        (-priority, entry_index, current_cost, upgraded, upgraded_cost),
    )


def _apply_credit_upgrades(
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    *,
    render_mode: str,
    transaction_limit: int,
    full_render_max_depth: int,
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]],
    token_budget: int,
    used: int,
    entry_filter: set[int] | None = None,
    phase: str = "upgrade_capped",
    credit_trace: TokenCreditTrace | None = None,
    min_utility_per_token: float | None = None,
    protected_cutoff_bypasses: int = 0,
    allow_free_at_ceiling: bool = False,
    node_semantic_utility_weight: float = 0.0,
) -> int:
    remaining_protected_cutoff_bypasses = max(0, int(protected_cutoff_bypasses))
    upgrade_heap: list[tuple[float, int, int, ContextBundle, int]] = []
    for entry_index in range(len(selected)):
        if entry_filter is not None and entry_index not in entry_filter:
            continue
        _enqueue_credit_upgrade(
            entry_index,
            selected,
            static,
            state,
            upgrade_heap,
            render_mode=render_mode,
            transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
            printed_tokens=used,
            node_semantic_utility_weight=node_semantic_utility_weight,
        )

    # Bound the heap walk: equal-cost / collapsed-mode thrash used to spin
    # forever when coverage left only a few tokens (django__django-15400).
    max_steps = max(64, len(selected) * len(_RENDER_LADDER) * 4)
    steps = 0
    while upgrade_heap and (used < token_budget or allow_free_at_ceiling):
        steps += 1
        if steps > max_steps:
            break
        _neg_priority, entry_index, expected_cost, upgraded, upgraded_cost = heapq.heappop(
            upgrade_heap
        )
        entry = selected[entry_index]
        current_cost = entry["cost"]
        if current_cost != expected_cost or not isinstance(current_cost, int):
            continue
        if upgraded_cost < current_cost:
            continue
        # Recompute the exact delta at pop time — earlier upgrades changed
        # other entries' renders (and hence first-wins winners) since this
        # entry was enqueued.
        exact_delta = _upgrade_exact_delta(selected, entry_index, upgraded, printed_before=used)
        if used + exact_delta > token_budget:
            continue
        entry_source = entry["source"]
        raw_delta_utility = 0.0
        effective_utility = 0.0
        semantic_delta_utility = 0.0
        if isinstance(entry_source, ContextBundle):
            bundle_index = entry.get("index")
            if isinstance(bundle_index, int):
                semantic_delta_utility = _upgrade_semantic_value(
                    selected,
                    entry_index,
                    upgraded,
                )
                raw_delta_utility = _credit_upgrade_gain_raw(
                    static[bundle_index],
                    entry_source,
                    upgraded,
                    state,
                    semantic_delta_utility=semantic_delta_utility,
                    node_semantic_utility_weight=node_semantic_utility_weight,
                )
                effective_utility = max(0.001, raw_delta_utility)
        raw_density = raw_delta_utility / max(1, exact_delta)
        cutoff_rejected = (
            min_utility_per_token is not None
            and exact_delta > 0
            and raw_density <= min_utility_per_token
        )
        cutoff_protected = exact_delta > 0 and remaining_protected_cutoff_bypasses > 0
        if cutoff_rejected and not cutoff_protected:
            if credit_trace is not None:
                credit_trace.cutoff_rejections += 1
            continue
        attribution: tuple[TokenCreditAttribution, ...] = ()
        if credit_trace is not None and isinstance(entry_source, ContextBundle):
            attribution = _upgrade_token_attribution(
                selected,
                entry_index,
                upgraded,
                entry_source=entry_source,
                exact_delta=exact_delta,
            )
        entry["rendered"] = upgraded
        entry["cost"] = upgraded_cost
        used += exact_delta
        if cutoff_rejected and cutoff_protected:
            remaining_protected_cutoff_bypasses -= 1
        if isinstance(entry_source, ContextBundle):
            if credit_trace is not None:
                credit_trace.record(
                    phase=phase,
                    bundle=entry_source,
                    render_mode=upgraded.render_mode,
                    delta_utility=raw_delta_utility,
                    effective_utility=effective_utility,
                    delta_tokens=exact_delta,
                    semantic_delta_utility=semantic_delta_utility,
                    attribution=attribution,
                )
            primary = _primary_file(entry_source)
            if primary:
                state.file_tokens[primary] = state.file_tokens.get(primary, 0) + exact_delta
        _enqueue_credit_upgrade(
            entry_index,
            selected,
            static,
            state,
            upgrade_heap,
            render_mode=render_mode,
            transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
            printed_tokens=used,
            node_semantic_utility_weight=node_semantic_utility_weight,
        )
    return used


def _retrieval_span_symbol_render(
    symbol: ContextSymbol,
    *,
    max_body_lines: int = 24,
    window_lines: int = 6,
) -> ContextSymbol:
    """Render signature + bounded source windows anchored by retrieval spans."""
    # A windowed class no longer honestly represents every contained member;
    # full-body enrichment is restored only on the later body rung.
    symbol = cast(ContextSymbol, replace(symbol, represented_owners=()))
    if not symbol.retrieval_spans:
        return _trim_symbol_for_mode(
            symbol,
            "signature_only",
            full_render_max_depth=0,
        )
    candidates = _span_candidates(
        symbol,
        query_terms=frozenset(),
        query_lines=frozenset(),
        max_candidates=max(3, len(symbol.retrieval_spans) * 3),
        window_lines=window_lines,
    )
    code, indices = _span_ranked_selection(
        symbol,
        candidates,
        [0.0] * len(candidates),
        max_body_lines=max_body_lines,
        max_selected_spans=max(1, len(symbol.retrieval_spans)),
    )
    return _replace_symbol_render(symbol, code, indices)


def _full_body_with_contained_owners(
    symbol: ContextSymbol,
    source_symbols: Iterable[ContextSymbol],
) -> ContextSymbol:
    """Mark indexed members honestly contained by a full class/source body."""
    if symbol.start_line <= 0 or symbol.end_line < symbol.start_line:
        return symbol
    owners: dict[str, RenderedOwner] = {owner.uid: owner for owner in symbol.represented_owners}
    for candidate in source_symbols:
        if candidate.uid == symbol.uid or candidate.file_path != symbol.file_path:
            continue
        if candidate.start_line < symbol.start_line or candidate.end_line > symbol.end_line:
            continue
        if candidate.start_line <= 0 or candidate.end_line < candidate.start_line:
            continue
        owners.setdefault(
            candidate.uid,
            RenderedOwner(
                uid=candidate.uid,
                name=candidate.name,
                qualified_name=candidate.qualified_name,
                file_path=candidate.file_path,
            ),
        )
    return cast(
        ContextSymbol,
        replace(symbol, represented_owners=tuple(owners.values())),
    )


def _with_richest_source_body(
    symbol: ContextSymbol,
    richest_by_uid: dict[str, ContextSymbol],
) -> ContextSymbol:
    richest = richest_by_uid.get(symbol.uid)
    if richest is None or estimate_text_tokens(richest.code or "") <= estimate_text_tokens(
        symbol.code or ""
    ):
        return symbol
    return cast(
        ContextSymbol,
        replace(
            symbol,
            code=richest.code,
            qualified_name=symbol.qualified_name or richest.qualified_name,
            start_line=richest.start_line,
            end_line=richest.end_line,
            rendered_spans=richest.rendered_spans,
        ),
    )


def _merge_direct_symbol_evidence(
    symbol: ContextSymbol,
    evidence: ContextSymbol,
) -> ContextSymbol:
    """Attach a UID's seed evidence to its graph-neighbour occurrence."""
    return cast(
        ContextSymbol,
        replace(
            symbol,
            retrieval_channels=tuple(
                dict.fromkeys([*symbol.retrieval_channels, *evidence.retrieval_channels])
            ),
            retrieval_spans=tuple(
                sorted(set(symbol.retrieval_spans) | set(evidence.retrieval_spans))
            ),
            exact_symbol_match=symbol.exact_symbol_match or evidence.exact_symbol_match,
            supporting_roles=tuple(
                dict.fromkeys([*symbol.supporting_roles, *evidence.supporting_roles])
            ),
            lexical_span_score=max(
                float(symbol.lexical_span_score or 0.0),
                float(evidence.lexical_span_score or 0.0),
            )
            or None,
            relevance_score=max(symbol.relevance_score, evidence.relevance_score),
            utility_score=max(symbol.utility_score, evidence.utility_score),
            query_similarity=(
                evidence.query_similarity
                if evidence.query_similarity is not None
                else symbol.query_similarity
            ),
            semantic_excess=max(symbol.semantic_excess, evidence.semantic_excess),
        ),
    )


def _merge_contained_retrieval_evidence(
    container: ContextSymbol,
    evidence_symbols: Iterable[ContextSymbol],
) -> ContextSymbol:
    """Promote retrieval evidence from indexed members to their owner body.

    A method hit is evidence for the enclosing class body, but not an exact
    name match for that class.  Keep the owner's identity while carrying only
    the member's channels and source windows upward.
    """
    result = container
    for evidence in evidence_symbols:
        if evidence.uid == container.uid or evidence.file_path != container.file_path:
            continue
        if not _symbol_has_direct_retrieval_evidence(evidence):
            continue
        if (
            container.start_line <= 0
            or container.end_line < container.start_line
            or evidence.start_line < container.start_line
            or evidence.end_line > container.end_line
        ):
            continue
        result = cast(
            ContextSymbol,
            replace(
                result,
                retrieval_channels=tuple(
                    dict.fromkeys([*result.retrieval_channels, *evidence.retrieval_channels])
                ),
                retrieval_spans=tuple(
                    sorted(set(result.retrieval_spans) | set(evidence.retrieval_spans))
                ),
                supporting_roles=tuple(
                    dict.fromkeys([*result.supporting_roles, *evidence.supporting_roles])
                ),
                lexical_span_score=max(
                    float(result.lexical_span_score or 0.0),
                    float(evidence.lexical_span_score or 0.0),
                )
                or None,
                relevance_score=max(result.relevance_score, evidence.relevance_score),
                utility_score=max(result.utility_score, evidence.utility_score),
                semantic_excess=max(result.semantic_excess, evidence.semantic_excess),
            ),
        )
    return result


def _retrieval_span_contained_members(
    container: ContextSymbol,
    source_symbols: Iterable[ContextSymbol],
) -> list[ContextSymbol]:
    """Resolve container spans to indexed members they actually intersect."""
    if not container.retrieval_spans:
        return []
    members: list[ContextSymbol] = []
    seen: set[str] = set()
    for candidate in source_symbols:
        if candidate.uid == container.uid or candidate.uid in seen:
            continue
        if candidate.file_path != container.file_path:
            continue
        if candidate.start_line <= 0 or candidate.end_line < candidate.start_line:
            continue
        if container.start_line > 0 and (
            candidate.start_line < container.start_line or candidate.end_line > container.end_line
        ):
            continue
        intersections = tuple(
            (max(start, candidate.start_line), min(end, candidate.end_line))
            for start, end in container.retrieval_spans
            if max(start, candidate.start_line) <= min(end, candidate.end_line)
        )
        if not intersections:
            continue
        member = _merge_direct_symbol_evidence(candidate, container)
        members.append(cast(ContextSymbol, replace(member, retrieval_spans=intersections)))
        seen.add(candidate.uid)
    return members


def _replace_rendered_bundle_symbol(
    current: ContextBundle,
    target: ContextSymbol,
    *,
    render_mode: str,
) -> ContextBundle | None:
    if current.seed.uid == target.uid:
        if current.seed.represented_owners:
            if render_mode.endswith("_span"):
                # A bounded evidence window no longer represents every member
                # previously grouped into the fold.  Replace the aggregate and
                # deliberately drop those stale first-wins ownership claims.
                return cast(
                    ContextBundle,
                    replace(current, seed=target, render_mode=render_mode),
                )
            if not render_mode.endswith("_body") or target.rendered_spans is not None:
                return None
            target = cast(
                ContextSymbol,
                replace(target, represented_owners=current.seed.represented_owners),
            )
        return cast(ContextBundle, replace(current, seed=target, render_mode=render_mode))
    related = list(current.related)
    for index, symbol in enumerate(related):
        if symbol.uid != target.uid:
            continue
        if symbol.represented_owners:
            if render_mode.endswith("_span"):
                related[index] = target
                return cast(
                    ContextBundle,
                    replace(current, related=tuple(related), render_mode=render_mode),
                )
            if not render_mode.endswith("_body") or target.rendered_spans is not None:
                return None
            target = cast(
                ContextSymbol,
                replace(target, represented_owners=symbol.represented_owners),
            )
        related[index] = target
        return cast(
            ContextBundle,
            replace(current, related=tuple(related), render_mode=render_mode),
        )
    return cast(
        ContextBundle,
        replace(current, related=(*current.related, target), render_mode=render_mode),
    )


def _rendered_symbol_source_line_set(symbol: ContextSymbol) -> set[int]:
    return {
        line
        for start, end in symbol.effective_rendered_spans()
        for line in range(start, end + 1)
        if start > 0 and end >= start
    }


def _upgrade_span_redundancy(
    selected: list[dict[str, object]],
    entry_index: int,
    upgraded: ContextBundle,
) -> float:
    """Share of newly represented source lines already covered by other UIDs."""
    before = _first_wins_printed_symbols(selected)
    entry = selected[entry_index]
    previous = entry.get("rendered")
    entry["rendered"] = upgraded
    try:
        after = _first_wins_printed_symbols(selected)
    finally:
        entry["rendered"] = previous

    lines_by_file: defaultdict[str, set[int]] = defaultdict(set)
    for symbol in before.values():
        lines_by_file[symbol.file_path].update(_rendered_symbol_source_line_set(symbol))

    added_total = 0
    overlap_total = 0
    for uid, symbol in after.items():
        previous_symbol = before.get(uid)
        previous_lines = (
            _rendered_symbol_source_line_set(previous_symbol) if previous_symbol else set()
        )
        added_lines = _rendered_symbol_source_line_set(symbol) - previous_lines
        if not added_lines:
            continue
        other_lines = set(lines_by_file.get(symbol.file_path, set())) - previous_lines
        added_total += len(added_lines)
        overlap_total += len(added_lines & other_lines)
    return overlap_total / added_total if added_total else 0.0


def _decoupled_upgrade_gain_raw(
    st: _BundleStatic,
    bundle: ContextBundle,
    target: ContextSymbol,
    *,
    mode: str,
    span_redundancy: float,
) -> float:
    """Symbol-local utility: evidence and overlap replace file saturation."""
    is_seed = target.uid == bundle.seed.uid
    target_weight = (
        1.0
        if is_seed
        else min(
            1.0,
            max(0.25, target.relevance_score, target.utility_score),
        )
    )
    evidence_bonus = 0.0
    if target.exact_symbol_match:
        evidence_bonus += 0.18
    families = retrieval_channel_families(target.retrieval_channels)
    if families:
        evidence_bonus += 0.08 + 0.04 * min(2, len(families) - 1)
    if target.retrieval_spans:
        evidence_bonus += 0.12
    gain = 0.35 * st.base_utility * st.tier_weight * target_weight
    gain += _UPGRADE_MODE_BONUS.get(mode, 0.10)
    gain += evidence_bonus
    gain += 0.05 if not bundle.passive else 0.03
    gain -= 0.50 * min(1.0, max(0.0, span_redundancy))
    return gain


def _direct_seed_evidence_by_uid(
    selected: list[dict[str, object]],
) -> dict[str, ContextSymbol]:
    evidence: dict[str, ContextSymbol] = {}
    for entry in selected:
        source = entry.get("source")
        if not isinstance(source, ContextBundle):
            continue
        seed = source.seed
        if not _symbol_has_direct_retrieval_evidence(seed):
            continue
        existing = evidence.get(seed.uid)
        if existing is None:
            evidence[seed.uid] = seed
            continue
        existing_strength = (
            int(existing.exact_symbol_match),
            len(retrieval_channel_families(existing.retrieval_channels)),
            len(existing.retrieval_spans),
            existing.relevance_score,
        )
        seed_strength = (
            int(seed.exact_symbol_match),
            len(retrieval_channel_families(seed.retrieval_channels)),
            len(seed.retrieval_spans),
            seed.relevance_score,
        )
        if seed_strength > existing_strength:
            evidence[seed.uid] = seed
    return evidence


def _apply_one_decoupled_upgrade(
    selected: list[dict[str, object]],
    entry_index: int,
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    *,
    target: ContextSymbol,
    upgraded: ContextBundle,
    mode: str,
    phase: str,
    token_ceiling: int,
    used: int,
    credit_trace: TokenCreditTrace | None,
    min_utility_per_token: float | None,
    cutoff_bypass: bool,
) -> tuple[int, str, bool]:
    """Try one symbol-local rung; return used, outcome and bypass consumption."""
    entry = selected[entry_index]
    entry_source = entry.get("source")
    bundle_index = entry.get("index")
    if not isinstance(entry_source, ContextBundle) or not isinstance(bundle_index, int):
        return used, "missing", False
    exact_delta = _upgrade_exact_delta(
        selected,
        entry_index,
        upgraded,
        printed_before=used,
    )
    if used + exact_delta > token_ceiling:
        return used, "budget", False
    semantic_delta = _upgrade_semantic_value(selected, entry_index, upgraded)
    redundancy = _upgrade_span_redundancy(selected, entry_index, upgraded)
    raw_utility = _decoupled_upgrade_gain_raw(
        static[bundle_index],
        entry_source,
        target,
        mode=mode,
        span_redundancy=redundancy,
    )
    raw_density = raw_utility / max(1, exact_delta)
    cutoff_rejected = (
        min_utility_per_token is not None
        and exact_delta > 0
        and raw_density <= min_utility_per_token
    )
    if cutoff_rejected and not cutoff_bypass:
        if credit_trace is not None:
            credit_trace.cutoff_rejections += 1
        return used, "cutoff", False

    attribution: tuple[TokenCreditAttribution, ...] = ()
    if credit_trace is not None:
        attribution = _upgrade_token_attribution(
            selected,
            entry_index,
            upgraded,
            entry_source=entry_source,
            exact_delta=exact_delta,
        )
    entry["rendered"] = upgraded
    entry["cost"] = _bundle_token_count(upgraded)
    used += exact_delta
    if credit_trace is not None:
        credit_trace.record(
            phase=phase,
            bundle=entry_source,
            render_mode=mode,
            delta_utility=raw_utility,
            effective_utility=max(0.001, raw_utility),
            delta_tokens=exact_delta,
            semantic_delta_utility=semantic_delta,
            attribution=attribution,
        )
    if target.file_path:
        state.file_tokens[target.file_path] = (
            state.file_tokens.get(target.file_path, 0) + exact_delta
        )
    return used, "accepted", cutoff_rejected and cutoff_bypass


def _symbol_upgrade_variants(
    current: ContextBundle,
    target: ContextSymbol,
    *,
    scope: str,
) -> list[tuple[str, str, ContextBundle]]:
    variants: list[tuple[str, str, ContextBundle]] = []
    if target.retrieval_spans:
        span_target = _retrieval_span_symbol_render(target)
        span_bundle = _replace_rendered_bundle_symbol(
            current,
            span_target,
            render_mode=f"{scope}_span",
        )
        if span_bundle is not None:
            variants.append(("span_evidence", f"upgrade_{scope}_span", span_bundle))
            current = span_bundle
    body_bundle = _replace_rendered_bundle_symbol(
        current,
        target,
        render_mode=f"{scope}_body",
    )
    if body_bundle is not None:
        variants.append(
            (
                "seed_body" if scope == "seed" else "related_body",
                f"upgrade_{scope}_body",
                body_bundle,
            )
        )
    return variants


def _first_rendered_entry_for_uid(
    selected: list[dict[str, object]],
    uid: str,
) -> int | None:
    for entry_index, entry in enumerate(selected):
        rendered = entry.get("rendered")
        if not isinstance(rendered, ContextBundle):
            continue
        if any(symbol.uid == uid for symbol in rendered.all_symbols()):
            return entry_index
    return None


def _apply_decoupled_seed_span_reserve(
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    *,
    source_symbols: list[ContextSymbol],
    richest_by_uid: dict[str, ContextSymbol],
    token_budget: int,
    used: int,
    reserve_share: float,
    credit_trace: TokenCreditTrace | None,
    min_utility_per_token: float | None,
) -> tuple[int, set[str]]:
    """Buy direct seed windows before body credits consume their local lane.

    Rank decay is intentionally strict for full bodies, but retrieval windows
    are evidence coverage rather than body enrichment.  Give direct seed spans
    one bounded global reserve so a rank-11 exact window is not forced to buy a
    700-token body merely because its geometric body credit is small.
    """
    reserve_tokens = int(token_budget * min(1.0, max(0.0, reserve_share)))
    if reserve_tokens <= 0:
        return used, set()
    reserve_ceiling = min(token_budget, used + reserve_tokens)
    upgraded_uids: set[str] = set()
    ordered_entries = sorted(
        range(len(selected)),
        key=lambda entry_index: int(selected[entry_index].get("index", 1 << 30)),
    )
    seen: set[str] = set()
    for entry_index in ordered_entries:
        source = selected[entry_index].get("source")
        if not isinstance(source, ContextBundle):
            continue
        target = source.seed
        if target.uid in seen or not target.retrieval_spans:
            continue
        seen.add(target.uid)
        if not _symbol_has_direct_retrieval_evidence(target):
            continue
        target = _with_richest_source_body(target, richest_by_uid)
        target = _full_body_with_contained_owners(target, source_symbols)
        target_entry_index = _first_rendered_entry_for_uid(selected, target.uid)
        if target_entry_index is None:
            continue
        target_entry = selected[target_entry_index]
        target_source = target_entry.get("source")
        current = target_entry.get("rendered")
        if not isinstance(target_source, ContextBundle) or not isinstance(current, ContextBundle):
            continue
        scope = "seed" if target_source.seed.uid == target.uid else "related"
        span_variant = next(
            (
                (mode, phase, upgraded)
                for mode, phase, upgraded in _symbol_upgrade_variants(
                    current,
                    target,
                    scope=scope,
                )
                if mode == "span_evidence"
            ),
            None,
        )
        if span_variant is None:
            continue
        mode, phase, upgraded = span_variant
        used, outcome, _consumed_bypass = _apply_one_decoupled_upgrade(
            selected,
            target_entry_index,
            static,
            state,
            target=target,
            upgraded=upgraded,
            mode=mode,
            phase=phase,
            token_ceiling=reserve_ceiling,
            used=used,
            credit_trace=credit_trace,
            min_utility_per_token=min_utility_per_token,
            cutoff_bypass=False,
        )
        if outcome == "accepted":
            upgraded_uids.add(target.uid)
        if used >= reserve_ceiling:
            break
    return used, upgraded_uids


def _apply_decoupled_rank_decay_credit_upgrades(
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    *,
    token_budget: int,
    used: int,
    head_share: float,
    decay_rate: float,
    seed_span_reserve_share: float,
    credit_trace: TokenCreditTrace | None,
    min_utility_per_token: float | None,
) -> int:
    """Buy seed span/body lanes; body-upgrade neighbours only with own evidence."""
    evidence_by_uid = _direct_seed_evidence_by_uid(selected)
    source_symbols = [
        symbol
        for entry in selected
        if isinstance((source := entry.get("source")), ContextBundle)
        for symbol in source.all_symbols()
    ]
    richest_by_uid: dict[str, ContextSymbol] = {}
    for symbol in source_symbols:
        existing = richest_by_uid.get(symbol.uid)
        if existing is None or estimate_text_tokens(symbol.code or "") > estimate_text_tokens(
            existing.code or ""
        ):
            richest_by_uid[symbol.uid] = symbol
    ordered_entries = sorted(
        range(len(selected)),
        key=lambda entry_index: int(selected[entry_index].get("index", 1 << 30)),
    )
    used, reserved_seed_span_uids = _apply_decoupled_seed_span_reserve(
        selected,
        static,
        state,
        source_symbols=source_symbols,
        richest_by_uid=richest_by_uid,
        token_budget=token_budget,
        used=used,
        reserve_share=seed_span_reserve_share,
        credit_trace=credit_trace,
        min_utility_per_token=min_utility_per_token,
    )
    processed_target_uids: set[str] = set()
    for entry_index in ordered_entries:
        entry = selected[entry_index]
        bundle_index = entry.get("index")
        source = entry.get("source")
        current = entry.get("rendered")
        if not isinstance(bundle_index, int) or not isinstance(source, ContextBundle):
            continue
        if not isinstance(current, ContextBundle):
            continue
        candidate_rank = bundle_index + 1
        body_credit = _rank_decay_body_credit(
            token_budget,
            candidate_rank=candidate_rank,
            head_share=head_share,
            decay_rate=decay_rate,
        )
        if body_credit <= 0:
            break
        local_ceiling = min(token_budget, used + body_credit)
        bypass_available = bool(
            min_utility_per_token is not None
            and min_utility_per_token > 0.0
            and candidate_rank <= _RANK_DECAY_SPAN_PROTECTED_HEAD
            and source.seed.retrieval_spans
        )

        lanes: list[tuple[str, ContextSymbol, str]] = []
        if _symbol_has_direct_retrieval_evidence(source.seed):
            lanes.append(("seed", source.seed, source.seed.uid))
        related_targets: list[ContextSymbol] = []
        for related in source.related:
            related = _merge_contained_retrieval_evidence(
                related,
                evidence_by_uid.values(),
            )
            evidence = (
                related
                if _symbol_has_direct_retrieval_evidence(related)
                else evidence_by_uid.get(related.uid)
            )
            target = (
                _merge_direct_symbol_evidence(related, evidence)
                if evidence is not None
                else related
            )
            if _symbol_has_strong_body_evidence(target) or _symbol_has_graph_body_evidence(target):
                related_targets.append(target)
        related_targets.sort(
            key=lambda symbol: (
                int(symbol.exact_symbol_match),
                len(retrieval_channel_families(symbol.retrieval_channels)),
                len(symbol.retrieval_spans),
                symbol.relevance_score,
            ),
            reverse=True,
        )
        lanes.extend(("related", target, target.uid) for target in related_targets)

        # A retrieval window can be attached to an owner/container even though
        # the indexed member inside that window is what should receive tokens.
        # Keep the member as a separate first-wins symbol, but let it consume
        # the container entry's local credit when it has no rendered entry yet.
        span_containers = [
            target
            for target in (source.seed, *related_targets)
            if _symbol_has_direct_retrieval_evidence(target) and target.retrieval_spans
        ]
        for container in span_containers:
            lanes.extend(
                ("related", member, container.uid)
                for member in _retrieval_span_contained_members(
                    container,
                    source_symbols,
                )
            )

        for scope, target, fallback_uid in lanes:
            if target.uid in processed_target_uids:
                continue
            target = _with_richest_source_body(target, richest_by_uid)
            target = _full_body_with_contained_owners(target, source_symbols)
            target_entry_index = _first_rendered_entry_for_uid(selected, target.uid)
            if target_entry_index is None:
                target_entry_index = _first_rendered_entry_for_uid(
                    selected,
                    fallback_uid,
                )
            if target_entry_index is None:
                continue
            target_entry = selected[target_entry_index]
            target_source = target_entry.get("source")
            actual_scope = (
                "seed"
                if isinstance(target_source, ContextBundle) and target_source.seed.uid == target.uid
                else "related"
            )
            current = target_entry.get("rendered")
            if not isinstance(current, ContextBundle):
                continue
            for mode, phase, upgraded in _symbol_upgrade_variants(
                current,
                target,
                scope=actual_scope,
            ):
                if mode == "span_evidence" and target.uid in reserved_seed_span_uids:
                    continue
                used, outcome, consumed_bypass = _apply_one_decoupled_upgrade(
                    selected,
                    target_entry_index,
                    static,
                    state,
                    target=target,
                    upgraded=upgraded,
                    mode=mode,
                    phase=phase,
                    token_ceiling=local_ceiling,
                    used=used,
                    credit_trace=credit_trace,
                    min_utility_per_token=min_utility_per_token,
                    cutoff_bypass=bypass_available and scope == "seed",
                )
                if consumed_bypass:
                    bypass_available = False
                if outcome in {"budget", "cutoff", "missing"}:
                    break
                current = target_entry.get("rendered")
                if not isinstance(current, ContextBundle):
                    break
            processed_target_uids.add(target.uid)
            if used >= token_budget:
                return used
    return used


def _rank_decay_body_credit(
    token_budget: int,
    *,
    candidate_rank: int,
    head_share: float,
    decay_rate: float,
) -> int:
    """Maximum paid body tokens for one pre-budget candidate rank.

    Rank is one-based. With the experimental defaults (15%, 0.75), the
    infinite geometric envelope is bounded by 60% of the total budget. The
    global budget still includes the recall-safe coverage floor, so this is an
    eligibility ceiling rather than a reservation. The first five ranks
    receive ~76% of the body envelope, while the far tail quickly falls below
    the price of any paid render rung.
    """
    if token_budget <= 0 or candidate_rank <= 0:
        return 0
    share = min(1.0, max(0.0, float(head_share)))
    decay = min(0.999, max(0.0, float(decay_rate)))
    return max(0, int(token_budget * share * (decay ** (candidate_rank - 1))))


def _apply_rank_decay_credit_upgrades(
    selected: list[dict[str, object]],
    static: list[_BundleStatic],
    state: _TokenCreditCoverageState,
    *,
    render_mode: str,
    full_render_max_depth: int,
    token_budget: int,
    used: int,
    head_share: float,
    decay_rate: float,
    credit_trace: TokenCreditTrace | None,
    min_utility_per_token: float | None,
    node_semantic_utility_weight: float,
) -> int:
    """Spend geometrically decaying body credits in candidate-rank order.

    The legacy allocator gives every robust-tail "leader" the same render cap
    and orders all cheap upgrades by density before rich leader relaxation.
    That erases the pre-budget rank: small tail upgrades can consume the
    8k→10k interval before a large rank-1 body is even eligible. This arm keeps
    coverage unchanged, then gives each selected candidate an independent
    paid-upgrade credit and resolves ranks strictly head-first. A candidate's
    unused credit is deliberately not rolled into the tail.

    ``transaction_limit`` is the global budget here, so the candidate's rich
    ladder rungs are visible immediately. The local spend ceiling below prices
    them by exact first-wins delta and enforces the rank credit.
    """
    ordered_entries = sorted(
        range(len(selected)),
        key=lambda entry_index: int(selected[entry_index].get("index", 1 << 30)),
    )
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]] = {}
    for entry_index in ordered_entries:
        bundle_index = selected[entry_index].get("index")
        if not isinstance(bundle_index, int):
            continue
        body_credit = _rank_decay_body_credit(
            token_budget,
            candidate_rank=bundle_index + 1,
            head_share=head_share,
            decay_rate=decay_rate,
        )
        if body_credit <= 0:
            break
        entry_source = selected[entry_index].get("source")
        span_protected = (
            min_utility_per_token is not None
            and min_utility_per_token > 0.0
            and bundle_index + 1 <= _RANK_DECAY_SPAN_PROTECTED_HEAD
            and isinstance(entry_source, ContextBundle)
            and bool(entry_source.seed.retrieval_spans)
        )
        local_ceiling = min(token_budget, used + body_credit)
        used = _apply_credit_upgrades(
            selected,
            static,
            state,
            render_mode=render_mode,
            transaction_limit=token_budget,
            full_render_max_depth=full_render_max_depth,
            render_cache=render_cache,
            token_budget=local_ceiling,
            used=used,
            entry_filter={entry_index},
            phase="upgrade_rank_decay",
            credit_trace=credit_trace,
            min_utility_per_token=min_utility_per_token,
            protected_cutoff_bypasses=1 if span_protected else 0,
            # Preserve zero-cost first-wins improvements even when the paid
            # rank credit is exactly exhausted.
            allow_free_at_ceiling=True,
            node_semantic_utility_weight=node_semantic_utility_weight,
        )
        if used >= token_budget:
            break
    return used


def _selected_credit_rendered_bundles(selected: list[dict[str, object]]) -> list[ContextBundle]:
    return [
        rendered_bundle
        for entry in selected
        if isinstance((rendered_bundle := entry.get("rendered")), ContextBundle)
    ]


def _freeze_cross_file_member_bodies(bundles: list[ContextBundle]) -> list[ContextBundle]:
    """Cap related members outside every seed's file at signature render.

    Expansion members whose file no seed points at are overwhelmingly noise
    (~5% of their printed tokens land in expected files, vs ~23% for members
    in seed-covered files — 98q pack), yet ``full``/``hybrid`` renders print
    their whole bodies. Trimming the source ``code`` here means no ladder rung
    can re-inflate them, while the symbol (and its file) stays in the bundle,
    so file-level coverage is unaffected. Seeds themselves are never trimmed.
    """
    seed_files = {b.seed.file_path for b in bundles if b.seed.file_path}
    frozen: list[ContextBundle] = []
    for bundle in bundles:
        related = tuple(
            rel
            if (rel.file_path or "") in seed_files
            else _trim_symbol_for_mode(
                rel,
                "signature_only",
                full_render_max_depth=0,
            )
            for rel in bundle.related
        )
        if related == bundle.related:
            frozen.append(bundle)
        else:
            frozen.append(cast(ContextBundle, replace(bundle, related=related)))
    return frozen


def _apply_token_credit_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int,
    render_mode: str,
    full_render_max_depth: int,
    per_transaction_share: float = 0.10,
    file_soft_cap_share: float = 0.25,
    signature_only_initial: bool = False,
    credit_trace: TokenCreditTrace | None = None,
    min_utility_per_token: float | None = None,
    upgrade_min_utility_per_token: float | None = None,
    freeze_at_utility_plateau: bool = False,
    plateau_upgrade_reserve_share: float = 0.0,
    node_semantic_utility_weight: float = 0.0,
    rank_decay_body_allocation: bool = False,
    rank_decay_head_share: float = 0.15,
    rank_decay_rate: float = 0.75,
    rank_decay_max_coverage_share: float = 0.65,
    decoupled_symbol_body_allocation: bool = False,
    decoupled_seed_span_reserve_share: float = 0.10,
) -> list[ContextBundle]:
    """Token Credit System v2 prototype: coverage-first marginal transactions.

    Phase 1 dedupes to unique seed symbols, then buys cheap coverage ordered
    by marginal utility per token. Both phases price transactions by the
    MARGINAL printed cost (the uid ledger on ``_TokenCreditCoverageState``):
    the prompt path dedupes symbols first-wins by uid, so a uid already bought
    by an earlier entry re-renders for free and ``used`` matches the deduped
    prompt size instead of over-billing shared neighbours. Each ladder step is
    capped by ``_leader_transaction_limit`` from a robust tail noise estimate::

        noise_level = median(marginality_tail) + k * 1.4826 * MAD(tail)
        %leader = 100 / COUNT(marginality > noise_level)

    ``marginality = 100 * (u/u_peak)`` on each deduped symbol.  Phase 2 uses
    the same cap, then — if budget remains once every capped upgrade has been
    resolved — reruns the sweep with the leftover budget as the per-step
    limit (cap relaxation: the cap manages contention, and contention is over).
    """
    if token_budget <= 0:
        return bundles

    del per_transaction_share  # profile knob; leader % comes from tail noise

    if _AUCTION_SEMANTIC_PRIMARY:
        # Decoupled design needs a CHEAP coverage floor so the token budget is
        # spent by the UPGRADE phase (where the semantic-primary ordering lives),
        # not by a rich profile initial render. Force signature-only coverage:
        # every file still gets a signature (recall-safe), then upgrades flow to
        # the query-relevant head first.
        signature_only_initial = True

    bundles = _dedupe_bundles_by_seed_uid(bundles)
    bundles = _freeze_cross_file_member_bodies(bundles)
    state = _TokenCreditCoverageState(
        file_soft_cap=max(1, int(token_budget * file_soft_cap_share)),
    )
    initial_mode = render_mode if render_mode in _RENDER_LADDER else "signature_only"
    render_cache: dict[tuple[int, str], tuple[ContextBundle, int]] = {}
    static = _bundle_static_rows(bundles)
    noise_level, leader_count = _leader_pool_metrics(static)
    transaction_limit = _leader_transaction_limit(
        token_budget,
        leader_count=leader_count,
    )
    upgrade_cutoff = (
        min_utility_per_token
        if upgrade_min_utility_per_token is None
        else upgrade_min_utility_per_token
    )
    if credit_trace is not None:
        credit_trace.begin(
            token_budget=token_budget,
            noise_level=noise_level,
            leader_count=leader_count,
            transaction_limit=transaction_limit,
            cutoff_density=(
                min_utility_per_token if min_utility_per_token is not None else upgrade_cutoff
            ),
        )
    peak_utility = max((st.base_utility for st in static), default=0.0)
    leader_indices = {
        idx
        for idx, st in enumerate(static)
        if peak_utility > 0 and (100.0 * st.base_utility / peak_utility) > noise_level
    }

    initial = _build_initial_credit_heap(
        bundles,
        static,
        transaction_limit=transaction_limit,
        full_render_max_depth=full_render_max_depth,
        initial_mode=initial_mode,
        signature_only_initial=signature_only_initial,
    )
    selected, used = _select_bundles_under_credit_budget(
        initial,
        bundles,
        static,
        state,
        token_budget,
        credit_trace=credit_trace,
        min_utility_per_token=min_utility_per_token,
        node_semantic_utility_weight=node_semantic_utility_weight,
    )
    coverage_share = used / token_budget if token_budget > 0 else 1.0
    base_coverage_share = min(1.0, max(0.0, rank_decay_max_coverage_share))
    guard_span = _RANK_DECAY_GUARD_END_TOKENS - _RANK_DECAY_GUARD_START_TOKENS
    guard_progress = min(
        1.0,
        max(
            0.0,
            (token_budget - _RANK_DECAY_GUARD_START_TOKENS) / max(1, guard_span),
        ),
    )
    max_coverage_share = base_coverage_share + ((1.0 - base_coverage_share) * guard_progress)
    rank_decay_active = rank_decay_body_allocation and coverage_share <= max_coverage_share
    if credit_trace is not None:
        credit_trace.coverage_tokens = used
        credit_trace.coverage_share = coverage_share
        credit_trace.allocation_mode = "rank_decay" if rank_decay_active else "legacy"
        credit_trace.rank_decay_coverage_threshold = max_coverage_share
    reserve_tokens = int(token_budget * min(1.0, max(0.0, plateau_upgrade_reserve_share)))
    upgrade_budget = (
        min(token_budget, used + reserve_tokens)
        if freeze_at_utility_plateau and state.cutoff_rejections > 0
        else token_budget
    )
    if credit_trace is not None:
        credit_trace.spend_ceiling = upgrade_budget
    if rank_decay_active:
        if decoupled_symbol_body_allocation:
            _apply_decoupled_rank_decay_credit_upgrades(
                selected,
                static,
                state,
                token_budget=upgrade_budget,
                used=used,
                head_share=rank_decay_head_share,
                decay_rate=rank_decay_rate,
                seed_span_reserve_share=decoupled_seed_span_reserve_share,
                credit_trace=credit_trace,
                min_utility_per_token=upgrade_cutoff,
            )
            return _selected_credit_rendered_bundles(selected)
        _apply_rank_decay_credit_upgrades(
            selected,
            static,
            state,
            render_mode=render_mode,
            full_render_max_depth=full_render_max_depth,
            token_budget=upgrade_budget,
            used=used,
            head_share=rank_decay_head_share,
            decay_rate=rank_decay_rate,
            credit_trace=credit_trace,
            min_utility_per_token=upgrade_cutoff,
            node_semantic_utility_weight=node_semantic_utility_weight,
        )
        return _selected_credit_rendered_bundles(selected)
    used = _apply_credit_upgrades(
        selected,
        static,
        state,
        render_mode=render_mode,
        transaction_limit=transaction_limit,
        full_render_max_depth=full_render_max_depth,
        render_cache=render_cache,
        token_budget=upgrade_budget,
        used=used,
        phase="upgrade_capped",
        credit_trace=credit_trace,
        min_utility_per_token=upgrade_cutoff,
        allow_free_at_ceiling=freeze_at_utility_plateau,
        node_semantic_utility_weight=node_semantic_utility_weight,
    )
    # Waves 2 and 3 both run with the whole budget as the per-step limit, so
    # they can share one fresh render cache (the memo key assumes a constant
    # limit — sharing with the CAPPED pass above would be wrong).
    relaxed_cache: dict[tuple[int, str], tuple[ContextBundle, int]] = {}
    if used < upgrade_budget:
        # Cap relaxation on leftover budget: every capped upgrade has landed
        # or been rejected, so contention — the reason the leader cap exists —
        # is over. Re-run the upgrade sweep with the whole remaining budget as
        # the per-step limit so large high-gain bodies the cap kept at
        # signature level can finally buy their render. Leaders only: rendering
        # rich variants is the expensive part of this pass, and symbols below
        # the noise floor are noise by the pool's own measure. Fresh render
        # cache: the memo key is (bundle, mode) and assumes a constant limit.
        relaxed_entries = {
            entry_index
            for entry_index, entry in enumerate(selected)
            if isinstance((bundle_index := entry.get("index")), int)
            and bundle_index in leader_indices
        }
        if relaxed_entries:
            used = _apply_credit_upgrades(
                selected,
                static,
                state,
                render_mode=render_mode,
                transaction_limit=upgrade_budget,
                full_render_max_depth=full_render_max_depth,
                render_cache=relaxed_cache,
                token_budget=upgrade_budget,
                used=used,
                entry_filter=relaxed_entries,
                phase="upgrade_leader_relaxed",
                credit_trace=credit_trace,
                min_utility_per_token=upgrade_cutoff,
                allow_free_at_ceiling=freeze_at_utility_plateau,
                node_semantic_utility_weight=node_semantic_utility_weight,
            )
    if used < upgrade_budget:
        # Third wave: the leader set is saturated and budget still remains.
        # ``_leader_pool_metrics`` promises that symbols below the noise floor
        # "still enter via marginal packer on leftover budget" — deliver that
        # for upgrades too: same gain/delta economics, eligibility widened to
        # every entry. Saturated leaders re-enqueue as cheap no-ops
        # (``_next_upgrade_render`` returns None at the ladder top), so the
        # wave only costs renders where there is genuinely something to buy.
        used = _apply_credit_upgrades(
            selected,
            static,
            state,
            render_mode=render_mode,
            transaction_limit=upgrade_budget,
            full_render_max_depth=full_render_max_depth,
            render_cache=relaxed_cache,
            token_budget=upgrade_budget,
            used=used,
            phase="upgrade_tail_relaxed",
            credit_trace=credit_trace,
            min_utility_per_token=upgrade_cutoff,
            allow_free_at_ceiling=freeze_at_utility_plateau,
            node_semantic_utility_weight=node_semantic_utility_weight,
        )
    return _selected_credit_rendered_bundles(selected)


def _apply_render_and_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int | None,
    render_mode: str,
    full_render_max_depth: int = 0,
    per_transaction_share: float = 0.10,
    file_soft_cap_share: float = 0.25,
    signature_only_initial: bool = False,
    credit_trace: TokenCreditTrace | None = None,
    min_utility_per_token: float | None = None,
    upgrade_min_utility_per_token: float | None = None,
    freeze_at_utility_plateau: bool = False,
    plateau_upgrade_reserve_share: float = 0.0,
    node_semantic_utility_weight: float = 0.0,
    rank_decay_body_allocation: bool = False,
    rank_decay_head_share: float = 0.15,
    rank_decay_rate: float = 0.75,
    rank_decay_max_coverage_share: float = 0.65,
    decoupled_symbol_body_allocation: bool = False,
    decoupled_seed_span_reserve_share: float = 0.10,
) -> list[ContextBundle]:
    """Echelon 2: render-trim then token-pack the assembled bundles.

    With ``token_budget`` set the Token Credit System packs the full pool in
    marginal-utility order, buying the minimal render context per bundle along
    its own render ladder — so ``render_mode`` is the ceiling, not a pre-trim,
    and the packer does its own rendering.

    With no budget we just apply ``render_mode`` to every bundle:
      * ``"full"`` — never trims (whole pool, full code).
      * ``"impact_tiered"`` — core fold groups compact; anchors full signature,
        tail one-line stubs.
      * ``"impact_surface"`` — one-line stub per symbol (impact breadth).
      * ``"signature_only"`` — every symbol collapses to its signature.
      * ``"hybrid"`` — only neighbours past ``full_render_max_depth`` collapse
        (default 0 → the seed stays full, every expanded neighbour collapses to
        a signature). Keeping even depth-1 deps full barely economizes (they
        dominate the volume), so the default is seed-only.
      * ``"fold"`` — group each bundle's class members into a folded block.
    """

    if token_budget is not None:
        return _apply_token_credit_budget(
            bundles,
            token_budget=token_budget,
            render_mode=render_mode,
            full_render_max_depth=full_render_max_depth,
            per_transaction_share=per_transaction_share,
            file_soft_cap_share=file_soft_cap_share,
            signature_only_initial=signature_only_initial,
            credit_trace=credit_trace,
            min_utility_per_token=min_utility_per_token,
            upgrade_min_utility_per_token=upgrade_min_utility_per_token,
            freeze_at_utility_plateau=freeze_at_utility_plateau,
            plateau_upgrade_reserve_share=plateau_upgrade_reserve_share,
            node_semantic_utility_weight=node_semantic_utility_weight,
            rank_decay_body_allocation=rank_decay_body_allocation,
            rank_decay_head_share=rank_decay_head_share,
            rank_decay_rate=rank_decay_rate,
            rank_decay_max_coverage_share=rank_decay_max_coverage_share,
            decoupled_symbol_body_allocation=decoupled_symbol_body_allocation,
            decoupled_seed_span_reserve_share=decoupled_seed_span_reserve_share,
        )

    if render_mode in (
        "impact_tiered",
        "impact_surface",
        "fold",
        "fold_compact",
        "signature_only",
        "hybrid",
        "hybrid_compact",
    ):
        bundles = [
            _render_bundle(b, render_mode, full_render_max_depth=full_render_max_depth)
            for b in bundles
        ]
    return bundles


#: Hook/event archetype edges. Hop 1 crosses the EVENT channel from a topic to
#: its sub/pub sites; hop 2 crosses the HOOK wrapper from those sites to the
#: registration / dispatch API they go through (``listens_for``/``dispatch``/…).
#: ``DECORATED_BY``/``HANDLES`` stay in hop 2 as the legacy decorator path that
#: reached the same API before the dedicated HOOK edge existed — kept as a
#: recall-safe fallback until a benchmark confirms HOOK_* parity lets it drop.
#: All hops are named syntactic edges — no god-type ``USES_TYPE`` fan — so the
#: chain stays precise.
_HOOK_DECL_EDGES: tuple[str, ...] = ("EVENT_SUB", "EVENT_PUB")
_HOOK_REGISTER_API_EDGES: tuple[str, ...] = (
    "HOOK_CONFIG",
    "HOOK_EXEC",
    "DECORATED_BY",
    "HANDLES",
)


def _hook_transparency_hits(
    db, workspace_id: str, seed_uids: list[str], *, limit: int
) -> dict[str, list[_Hit]]:
    """Open hook-DECLARATION seeds through their registration lifecycle.

    An event topic (e.g. ``MapperEvents.before_insert``) is a documented
    near-stub — reaching it renders a docstring, not the mechanism. The wiring
    lives one archetype hop away. Walk the lifecycle two hops: the topic's
    incoming EVENT_SUB/EVENT_PUB sites (who subscribes / publishes — crossing the
    channel), then those sites' HOOK_CONFIG/HOOK_EXEC surface (the registration /
    dispatch API they go through, e.g. ``event.listens_for``), and attribute that
    API surface back to the topic as a distance-2 ``hook_transparency`` hit.

    Seeds with no incoming EVENT edge yield no sites, so this is inert for
    non-topic seeds. The two short hops over named edges keep it precise — it is
    the literal channel->wrapper archetype chain, not a blind binding widening.
    """
    sites_by_seed = walk_neighbours_grouped(
        db,
        workspace_id,
        seed_uids,
        edges=_HOOK_DECL_EDGES,
        direction="undirected",
        max_hops=1,
        limit_per_seed=limit * 4,
    )
    site_to_seeds: dict[str, list[str]] = {}
    for seed, sites in sites_by_seed.items():
        for s in sites:
            site_to_seeds.setdefault(s.uid, []).append(seed)
    if not site_to_seeds:
        return {}
    api_by_site = walk_neighbours_grouped(
        db,
        workspace_id,
        sorted(site_to_seeds),
        edges=_HOOK_REGISTER_API_EDGES,
        direction="undirected",
        max_hops=1,
        limit_per_seed=limit * 4,
    )
    out: dict[str, list[_Hit]] = {}
    for site_uid, apis in api_by_site.items():
        for seed in site_to_seeds.get(site_uid, ()):
            bucket = out.setdefault(seed, [])
            for a in apis:
                bucket.append(_Hit(a.uid, a.name, a.file_path, 2, "hook_transparency"))
    return out


def _candidate_utility_score(
    candidate: RoleCandidate,
    utility_score_fn: Callable[[RoleCandidate], float] | None,
) -> float:
    if utility_score_fn is None:
        return candidate.utility_score if candidate.utility_score is not None else candidate.score
    return utility_score_fn(candidate)


def _evidence_graph_fanout_limit(
    candidate: RoleCandidate,
    *,
    rank: int,
    max_per_seed: int,
    min_per_seed: int,
    protected_head: int,
) -> tuple[int, str]:
    """Allocate a reduce-only neighbour cap from pre-graph evidence.

    Exact/anchor seeds, the ranked head, and any retrieval-backed or
    independently corroborated candidates retain the established fanout. Only
    unique graph-only tail candidates fall toward half of the base fanout,
    bounded by ``min_per_seed``.
    """
    ceiling = max(1, int(max_per_seed))
    floor = min(ceiling, max(1, int(min_per_seed)))
    weak_limit = max(floor, (ceiling + 1) // 2)
    supporting_roles = frozenset((candidate.role, *candidate.supporting_roles))
    channel_families = retrieval_channel_families(candidate.retrieval_channels)
    anchor = candidate.role in {"anchor_symbol", "overlay_anchor"} or bool(
        supporting_roles.intersection({"anchor_symbol", "overlay_anchor"})
    )
    if anchor or candidate.exact_symbol_match:
        return ceiling, "protected"
    if rank <= max(0, int(protected_head)):
        return ceiling, "ranked_head"
    if len(channel_families) >= 2 or len(supporting_roles) >= 3:
        return ceiling, "strong_consensus"
    if channel_families or len(supporting_roles) >= 2 or candidate.retrieval_spans:
        return ceiling, "supported_tail"
    return weak_limit, "weak_tail"


def _merge_grouped_walk_hits(
    hits_per_seed: dict[str, list[_Hit]],
    grouped: dict[str, list],
    step_name: str,
) -> None:
    for su, neighbours in grouped.items():
        bucket = hits_per_seed.get(su)
        if bucket is None:
            continue
        for nb in neighbours:
            bucket.append(_Hit(nb.uid, nb.name, nb.file_path, nb.depth, step_name))


def _append_hook_transparency_hits(
    hits_per_seed: dict[str, list[_Hit]],
    db,
    workspace_id: str,
    seed_uids: list[str],
    *,
    max_per_seed: int,
) -> None:
    for su, extra in _hook_transparency_hits(
        db, workspace_id, seed_uids, limit=max_per_seed
    ).items():
        bucket = hits_per_seed.get(su)
        if bucket is not None:
            bucket.extend(extra)


def _collect_hits_per_seed(
    db,
    workspace_id: str,
    seed_uids: list[str],
    *,
    traversal_mode: str | None,
    max_per_seed: int,
    hook_transparency: bool,
    semantic_rerank: bool = False,
    max_per_seed_by_uid: dict[str, int] | None = None,
) -> dict[str, list[_Hit]]:
    """Batch graph walks for every seed; optionally attach hook-transparency hits.

    One grouped walk per expansion step over all seed uids (not one round-trip
    per candidate). ``walk_neighbours_grouped`` still returns per-seed buckets.
    """
    hits_per_seed: dict[str, list[_Hit]] = {u: [] for u in seed_uids}
    steps = steps_for_mode(traversal_mode) if traversal_mode is not None else ()
    for step_name, edges, direction, max_hops in steps:
        # Semantic selection happens after the graph walk.  Keep a much wider
        # safety reservoir than the historical 4x depth/uid cut so a relevant
        # depth-2 node is unlikely to disappear before query scoring sees it.
        # The cap still bounds high-fanout hubs on the Neo4j fallback path.
        candidate_limit = max(256, max_per_seed * 32) if semantic_rerank else max_per_seed * 4
        candidate_limits_by_uid = None
        if max_per_seed_by_uid:
            candidate_limits_by_uid = {}
            for uid in seed_uids:
                selected_limit = max_per_seed_by_uid.get(uid, max_per_seed)
                if selected_limit >= max_per_seed:
                    candidate_limits_by_uid[uid] = candidate_limit
                elif semantic_rerank:
                    candidate_limits_by_uid[uid] = max(64, selected_limit * 32)
                else:
                    candidate_limits_by_uid[uid] = max(1, selected_limit * 4)
        grouped = walk_neighbours_grouped(
            db,
            workspace_id,
            seed_uids,
            edges=edges,
            direction=direction,
            max_hops=max_hops,
            limit_per_seed=candidate_limit,
            limit_per_seed_by_uid=candidate_limits_by_uid,
        )
        _merge_grouped_walk_hits(hits_per_seed, grouped, step_name)

    if hook_transparency:
        _append_hook_transparency_hits(
            hits_per_seed,
            db,
            workspace_id,
            seed_uids,
            max_per_seed=max_per_seed,
        )
    return hits_per_seed


def _nearest_expansion_hits(
    hits: list[_Hit],
    *,
    include_tests: bool,
    max_per_seed: int,
    query_scoring: QueryScoringContext | None = None,
    semantic_alpha: float = 0.70,
    structural_reserve: int = 1,
    impact_mode: bool = False,
    semantic_rerank: bool = True,
) -> list[_Hit]:
    """Dedupe expansion hits, then select structural bridges + semantic leaders.

    Depth still decides which occurrence represents a duplicated uid and
    reserves a small number of direct structural neighbours.  The remaining
    bundle slots are ranked by robustly calibrated query similarity blended
    with depth decay, so a relevant depth-2 node can beat depth-1 noise.
    """
    nearest_by_uid: dict[str, _Hit] = {}
    for h in hits:
        if not include_tests and is_test_path(h.file_path or ""):
            continue
        existing = nearest_by_uid.get(h.uid)
        if existing is None or h.depth < existing.depth:
            nearest_by_uid[h.uid] = h
    structural = sorted(
        nearest_by_uid.values(),
        key=lambda h: (h.depth, (h.name or "").lower(), h.uid),
    )
    similarities = {
        hit.uid: similarity
        for hit in structural
        if query_scoring is not None
        and (similarity := query_scoring.similarity_for(hit.uid)) is not None
    }
    values = list(similarities.values())
    floor = semantic_noise_floor(values) if values else 0.0
    ordered_values = sorted(values)
    q95_index = (
        min(len(ordered_values) - 1, round(0.95 * (len(ordered_values) - 1)))
        if ordered_values
        else 0
    )
    ceiling = ordered_values[q95_index] if ordered_values else floor
    span = max(1e-9, ceiling - floor)
    alpha = min(1.0, max(0.0, semantic_alpha))

    annotated: list[_Hit] = []
    for hit in structural:
        similarity = similarities.get(hit.uid)
        semantic_excess = (
            min(1.0, max(0.0, (similarity - floor) / span)) if similarity is not None else 0.0
        )
        tier_weight = _tier_weight_for_path(hit.file_path, impact_mode=impact_mode)
        structural_weight = 1.0 / max(1, int(hit.depth))
        weighted_semantic = semantic_excess * tier_weight
        relevance_score = (
            alpha * weighted_semantic + (1.0 - alpha) * structural_weight
            if similarity is not None
            else structural_weight * tier_weight
        )
        annotated.append(
            replace(
                hit,
                query_similarity=similarity,
                semantic_excess=semantic_excess,
                tier_weight=tier_weight,
                structural_weight=structural_weight,
                relevance_score=relevance_score,
            )
        )

    if not semantic_rerank or not similarities or len(annotated) <= max_per_seed:
        return annotated[:max_per_seed]

    reserve_count = min(max_per_seed, max(0, structural_reserve))
    reserved = annotated[:reserve_count]
    reserved_uids = {hit.uid for hit in reserved}

    def _semantic_rank(hit: _Hit) -> tuple[float, float, int, str, str]:
        weighted_semantic = hit.semantic_excess * hit.tier_weight
        return (
            -hit.relevance_score,
            -weighted_semantic,
            int(hit.depth),
            (hit.name or "").lower(),
            hit.uid,
        )

    semantic = sorted(
        (hit for hit in annotated if hit.uid not in reserved_uids),
        key=_semantic_rank,
    )
    return reserved + semantic[: max_per_seed - reserve_count]


def _plan_candidate_expansions(
    candidates: list[RoleCandidate],
    hits_per_seed: dict[str, list[_Hit]],
    *,
    include_tests: bool,
    max_per_seed: int,
    max_per_seed_by_uid: dict[str, int] | None = None,
    query_scoring: QueryScoringContext | None = None,
    semantic_alpha: float = 0.70,
    structural_reserve: int = 1,
    semantic_rerank: bool = False,
) -> tuple[list[tuple[RoleCandidate, list[_Hit]]], set[str]]:
    expansion_per_candidate: list[tuple[RoleCandidate, list[_Hit]]] = []
    uids_to_fetch: set[str] = set()
    for cand in candidates:
        uids_to_fetch.add(cand.uid)
        candidate_limit = (
            max_per_seed_by_uid.get(cand.uid, max_per_seed)
            if max_per_seed_by_uid is not None
            else max_per_seed
        )
        ordered = _nearest_expansion_hits(
            hits_per_seed.get(cand.uid, []),
            include_tests=include_tests,
            max_per_seed=candidate_limit,
            query_scoring=query_scoring,
            semantic_alpha=semantic_alpha,
            structural_reserve=structural_reserve,
            impact_mode=cand.role in _MODE_ROLES,
            semantic_rerank=semantic_rerank,
        )
        expansion_per_candidate.append((cand, ordered))
        for h in ordered:
            uids_to_fetch.add(h.uid)
    return expansion_per_candidate, uids_to_fetch


def _resolve_context_payloads(
    lance,
    db,
    workspace_id: str,
    uids_to_fetch: set[str],
    *,
    overlay: Any | None,
    user_id: str,
) -> dict[str, _PayloadRow]:
    payload_by_uid = _fetch_symbol_payloads(lance, workspace_id, uids_to_fetch)
    payload_by_uid = _merge_symbol_spans(db, workspace_id, uids_to_fetch, payload_by_uid)
    if overlay is not None:
        from context_engine.axis.overlay_context import merge_saved_overlay_payloads

        payload_by_uid = merge_saved_overlay_payloads(
            payload_by_uid,
            overlay=overlay,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    return _hydrate_missing_symbol_code(
        db,
        workspace_id,
        uids_to_fetch,
        payload_by_uid,
    )


def _context_symbol_from_hit(
    hit: _Hit,
    payload_by_uid: dict[str, _PayloadRow],
) -> ContextSymbol:
    payload = payload_by_uid.get(hit.uid, {})
    return ContextSymbol(
        uid=hit.uid,
        name=hit.name,
        file_path=hit.file_path,
        role=hit.step or "related",
        distance_from_seed=hit.depth,
        expansion_step=hit.step,
        code=payload.get("code"),
        qualified_name=str(payload.get("qualified_name") or ""),
        relevance_score=hit.relevance_score,
        utility_score=hit.structural_weight * hit.tier_weight,
        query_similarity=hit.query_similarity,
        semantic_excess=hit.semantic_excess,
        tier_weight=hit.tier_weight,
        structural_weight=hit.structural_weight,
        start_line=_int_payload_value(payload.get("start_line")),
        end_line=_int_payload_value(payload.get("end_line")),
    )


def _context_bundle_for_candidate(
    cand: RoleCandidate,
    hits: list[_Hit],
    payload_by_uid: dict[str, _PayloadRow],
    *,
    utility_score_fn: Callable[[RoleCandidate], float] | None,
) -> ContextBundle:
    seed_payload = payload_by_uid.get(cand.uid, {})
    seed = ContextSymbol(
        uid=cand.uid,
        name=cand.name,
        file_path=cand.file_path,
        role=cand.role,
        distance_from_seed=cand.depth or 0,
        expansion_step=None,
        code=seed_payload.get("code"),
        qualified_name=cand.qualified_name or str(seed_payload.get("qualified_name") or ""),
        kind=cand.satisfying_kinds[0] if cand.satisfying_kinds else "",
        direction=_candidate_direction(cand),
        edge_type=cand.edge_type,
        relevance_score=cand.score,
        utility_score=cand.utility_score if cand.utility_score is not None else cand.score,
        start_line=_int_payload_value(seed_payload.get("start_line")),
        end_line=_int_payload_value(seed_payload.get("end_line")),
        retrieval_spans=cand.retrieval_spans,
        retrieval_channels=cand.retrieval_channels,
        exact_symbol_match=cand.exact_symbol_match,
        supporting_roles=cand.supporting_roles,
        lexical_span_score=cand.lexical_span_score,
    )
    related = tuple(_context_symbol_from_hit(h, payload_by_uid) for h in hits)
    return ContextBundle(
        role=cand.role,
        seed=seed,
        related=related,
        utility_score=_candidate_utility_score(cand, utility_score_fn),
    )


def _build_context_bundles(
    expansion_per_candidate: list[tuple[RoleCandidate, list[_Hit]]],
    payload_by_uid: dict[str, dict[str, str | None]],
    *,
    utility_score_fn: Callable[[RoleCandidate], float] | None,
) -> list[ContextBundle]:
    return [
        _context_bundle_for_candidate(
            cand,
            hits,
            payload_by_uid,
            utility_score_fn=utility_score_fn,
        )
        for cand, hits in expansion_per_candidate
    ]


def _apply_overlay_to_context_bundles(
    bundles: list[ContextBundle],
    *,
    overlay: Any,
    workspace_id: str,
    user_id: str,
) -> list[ContextBundle]:
    from context_engine.axis.overlay_context import apply_dirty_overlay_to_bundles

    return apply_dirty_overlay_to_bundles(
        bundles,
        overlay=overlay,
        workspace_id=workspace_id,
        user_id=user_id,
    )


def _pack_with_render_budget(
    bundles: list[ContextBundle],
    budget: ContextRenderBudget,
    *,
    credit_trace: TokenCreditTrace | None,
) -> list[ContextBundle]:
    return _apply_render_and_budget(
        bundles,
        token_budget=budget.token_budget,
        render_mode=budget.render_mode,
        per_transaction_share=budget.per_transaction_share,
        file_soft_cap_share=budget.file_soft_cap_share,
        signature_only_initial=budget.signature_only_initial,
        credit_trace=credit_trace,
        min_utility_per_token=budget.min_utility_per_token,
        upgrade_min_utility_per_token=budget.upgrade_min_utility_per_token,
        freeze_at_utility_plateau=budget.freeze_at_utility_plateau,
        plateau_upgrade_reserve_share=budget.plateau_upgrade_reserve_share,
        node_semantic_utility_weight=budget.node_semantic_utility_weight,
        rank_decay_body_allocation=budget.rank_decay_body_allocation,
        rank_decay_head_share=budget.rank_decay_head_share,
        rank_decay_rate=budget.rank_decay_rate,
        rank_decay_max_coverage_share=budget.rank_decay_max_coverage_share,
        decoupled_symbol_body_allocation=budget.decoupled_symbol_body_allocation,
        decoupled_seed_span_reserve_share=budget.decoupled_seed_span_reserve_share,
    )


def build_context_for_candidates(
    candidates: Iterable[RoleCandidate],
    *,
    workspace_id: str,
    db,
    lance,
    max_per_seed: int = 6,
    traversal_mode: str | None = "deferred_binding_flow",
    include_tests: bool = False,
    hook_transparency: bool = False,
    render_budget: ContextRenderBudget | None = None,
    utility_score_fn: Callable[[RoleCandidate], float] | None = None,
    query_scoring: QueryScoringContext | None = None,
    semantic_expansion_alpha: float = 0.70,
    semantic_expansion_structural_reserve: int = 1,
    semantic_expansion_rerank: bool = False,
    evidence_graph_fanout: bool = False,
    evidence_graph_fanout_min: int = 2,
    evidence_graph_fanout_protected_head: int = 5,
    span_query_text: str = "",
    span_score_fn: SpanScoreFn | None = None,
    credit_trace: TokenCreditTrace | None = None,
    overlay: Any | None = None,
    user_id: str = "anonymous",
) -> list[ContextBundle]:
    """Expand each candidate into a ``ContextBundle`` of related code.

    Every candidate gets a graph WALK (the expensive part); the Token Credit
    budget downstream packs the full pool, so there is no active/passive split
    to bound the walk here.

    ``max_per_seed`` caps how many related symbols come back per seed. Without
    ``query_scoring`` it keeps the legacy depth-then-name order; with scoring
    it reserves direct structural context and fills the remaining slots by a
    robust query/depth blend. ``traversal_mode`` picks the expansion pattern
    from ``AxisQueryPlan``; defaults to deferred-binding
    because every current contract uses it. ``None`` keeps explicitly
    supplied impact/trace candidates as a flat, directionally-labelled set
    without expanding them into siblings through a second graph walk.

    ``include_tests`` mirrors the retrieval-pass flag — by default,
    expansion hits that land in conventional test surfaces are
    dropped. Impact-style consumers can flip the flag to keep them.

    ``render_budget`` / ``token_budget`` are the echelon-2 budget knobs
    (default off = whole pool, full code = benchmark behaviour):
    ``signature_only`` trims each symbol to its signature, and a non-None
    ``token_budget`` hands the pool to the Token Credit packer, which buys the
    minimal render per bundle by marginal utility per token.
    """
    candidates = list(candidates)
    if not candidates:
        return []

    budget = render_budget or ContextRenderBudget()
    fanout_limits = None
    if credit_trace is not None:
        credit_trace.graph_fanout_base_limit = 0
        credit_trace.graph_fanout_tier_counts = {}
        credit_trace.graph_fanout_limit_counts = {}
    if evidence_graph_fanout:
        fanout_decisions = {
            candidate.uid: _evidence_graph_fanout_limit(
                candidate,
                rank=rank,
                max_per_seed=max_per_seed,
                min_per_seed=evidence_graph_fanout_min,
                protected_head=evidence_graph_fanout_protected_head,
            )
            for rank, candidate in enumerate(candidates, start=1)
        }
        fanout_limits = {uid: decision[0] for uid, decision in fanout_decisions.items()}
        if credit_trace is not None:
            credit_trace.graph_fanout_base_limit = max(1, int(max_per_seed))
            credit_trace.graph_fanout_tier_counts = dict(
                Counter(decision[1] for decision in fanout_decisions.values())
            )
            credit_trace.graph_fanout_limit_counts = dict(
                Counter(decision[0] for decision in fanout_decisions.values())
            )
    all_uids = [c.uid for c in candidates]
    hits_per_seed = _collect_hits_per_seed(
        db,
        workspace_id,
        all_uids,
        traversal_mode=traversal_mode,
        max_per_seed=max_per_seed,
        hook_transparency=hook_transparency,
        semantic_rerank=semantic_expansion_rerank,
        max_per_seed_by_uid=fanout_limits,
    )
    expansion_per_candidate, uids_to_fetch = _plan_candidate_expansions(
        candidates,
        hits_per_seed,
        include_tests=include_tests,
        max_per_seed=max_per_seed,
        max_per_seed_by_uid=fanout_limits,
        query_scoring=query_scoring,
        semantic_alpha=semantic_expansion_alpha,
        structural_reserve=semantic_expansion_structural_reserve,
        semantic_rerank=semantic_expansion_rerank,
    )
    payload_by_uid = _resolve_context_payloads(
        lance,
        db,
        workspace_id,
        uids_to_fetch,
        overlay=overlay,
        user_id=user_id,
    )
    bundles = _build_context_bundles(
        expansion_per_candidate,
        payload_by_uid,
        utility_score_fn=utility_score_fn,
    )
    if overlay is not None:
        bundles = _apply_overlay_to_context_bundles(
            bundles,
            overlay=overlay,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    if not budget.span_line_rerank:
        return _pack_with_render_budget(bundles, budget, credit_trace=credit_trace)

    # Cheap first pass: identify the bundles Token Credit would actually buy.
    # Embedding every span in the full graph-expanded pool is both wasteful and
    # slower than retrieval itself. Re-rank only the provisional winners, then
    # run the real budget pass over their pruned bodies so accounting remains
    # exact and no later render upgrade can restore discarded source lines.
    provisional = _pack_with_render_budget(bundles, budget, credit_trace=None)
    source_by_uid = {bundle.seed.uid: bundle for bundle in _dedupe_bundles_by_seed_uid(bundles)}
    selected_sources = [
        source
        for rendered in provisional
        if (source := source_by_uid.get(rendered.seed.uid)) is not None
    ]
    if selected_sources:
        selected_sources = _apply_span_line_rerank(
            selected_sources,
            query_text=span_query_text,
            score_fn=span_score_fn,
            max_symbols=budget.span_rank_max_symbols,
            max_candidates_per_symbol=budget.span_rank_max_candidates_per_symbol,
            max_body_lines=budget.span_rank_max_body_lines,
        )
    return _pack_with_render_budget(
        selected_sources or bundles,
        budget,
        credit_trace=credit_trace,
    )


__all__ = [
    "ContextBundle",
    "ContextRenderBudget",
    "ContextSymbol",
    "LexicalSpanEvidence",
    "LexicalSpanProbeTrace",
    "TokenCreditTrace",
    "TokenCreditTransaction",
    "build_context_for_candidates",
    "probe_candidate_lexical_spans",
    "query_has_explicit_line_hint",
]
