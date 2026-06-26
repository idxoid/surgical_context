"""Axis -> PromptContext adapter.

The ``/ask`` consumer is isolated from how context is *built* by a fixed
contract: ``PromptContext`` (``context_engine.context_types``). This module
turns the axis pipeline's ``ContextBundle`` list into a ``PromptContext``
that ``to_system_prompt`` renders unchanged.

Modelling note: axis returns a RANKED SET (one bundle per candidate),
while ``PromptContext`` wants one ``primary_source`` + dependencies. The
top-ranked candidate's seed becomes ``primary_source`` (for a question
with no single target symbol, the most relevant file is the natural
"TARGET"); every other seed and all related symbols, deduped by uid,
become ``graph_context``. ``to_system_prompt`` skips entries with no
code, so code-less expansion hits cost nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.context_types import PromptContext, SymbolContext


def _relation_label(sym: ContextSymbol, *, bundle_role: str) -> str:
    """Pick a dependency annotation that reflects graph expansion, not seed role."""
    if sym.expansion_step:
        return sym.expansion_step
    if sym.role and sym.role not in ("anchor_symbol", bundle_role):
        return sym.role
    role = bundle_role or "related"
    return "related" if role == "anchor_symbol" else role


def _to_symbol_context(
    sym: ContextSymbol,
    *,
    relation: str,
    blended_score: float = 0.0,
    render_mode: str = "full",
) -> SymbolContext:
    line_range = (
        [sym.start_line, sym.end_line]
        if sym.start_line > 0 and sym.end_line >= sym.start_line
        else []
    )
    return SymbolContext(
        symbol=sym.name,
        file_path=sym.file_path,
        relation=relation,
        uid=sym.uid,
        range=line_range,
        kind=sym.kind,
        edge_type=sym.edge_type,
        direction=sym.direction,
        depth=sym.distance_from_seed,
        relevance_score=sym.relevance_score,
        graph_score=sym.utility_score,
        blended_score=blended_score,
        render_mode=render_mode,
        code=sym.code or "",
        provenance=[value for value in (sym.expansion_step, sym.kind, sym.edge_type) if value],
    )


def _seed_relation(bundle: ContextBundle) -> str:
    if bundle.role in {"impact_analysis", "trace_dependency"}:
        kind = bundle.seed.kind
        if kind and kind != "target_seed":
            return kind
    return bundle.role or "related"


def axis_bundles_to_prompt_context(
    bundles: Iterable[ContextBundle],
    *,
    workspace_id: str = "",
    intent: str = "",
    trace_id: str = "",
    mechanism: str = "",
    render_mode: str = "full",
) -> PromptContext | None:
    """Adapt the axis pipeline's bundles into a renderable PromptContext.

    Returns ``None`` when there is nothing to render (no bundles / no
    seed), so the caller can fall through to another provider. ``render_mode``
    labels every symbol with the granularity the pipeline actually produced
    (``signature_only`` for the impact budget profile) — the code is already
    trimmed upstream, this only keeps the metadata honest.
    """
    bundles = list(bundles)
    if not bundles:
        return None

    # bundles arrive in candidate-rank order (build_context_for_candidates
    # preserves the input order); the first seed is the primary target.
    primary_bundle = bundles[0]
    primary = _to_symbol_context(
        primary_bundle.seed,
        relation="primary",
        blended_score=1.0,
        render_mode=render_mode,
    )

    graph_context: list[SymbolContext] = []
    seen: set[str] = {primary.uid}
    # Higher-ranked bundles first; within a bundle the seed precedes its
    # related symbols. Dedupe by uid keeps the first (highest-rank /
    # shallowest) occurrence.
    for rank, bundle in enumerate(bundles):
        # other candidates' seeds are first-class context, not just deps
        if rank > 0 and bundle.seed.uid not in seen:
            seen.add(bundle.seed.uid)
            graph_context.append(
                _to_symbol_context(
                    bundle.seed,
                    relation=_seed_relation(bundle),
                    blended_score=bundle.utility_score,
                    render_mode=render_mode,
                )
            )
        for rel in bundle.related:
            if rel.uid in seen:
                continue
            seen.add(rel.uid)
            graph_context.append(
                _to_symbol_context(
                    rel,
                    relation=_relation_label(rel, bundle_role=bundle.role or "related"),
                    render_mode=render_mode,
                )
            )

    return PromptContext(
        primary_source=primary,
        graph_context=graph_context,
        documentation=[],
        mode="surgical_full",
        intent=intent,
        mechanism=mechanism,
        workspace_id=workspace_id,
        trace_id=trace_id,
    )


__all__ = ["axis_bundles_to_prompt_context"]
