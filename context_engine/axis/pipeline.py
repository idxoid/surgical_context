"""Canonical axis retrieval pipeline (Phase 1b).

ONE function — :func:`run_axis_retrieval` — runs the full axis read-side:
intent -> workspace scan -> role/vector seeds -> cross-role lookahead ->
structural / inheritance / phased pool passes -> impact / trace mode
passes -> cross-role intersection -> intent-axis ranking -> per-candidate
context expansion. Three consumers share it so they cannot drift:

  * the ``/ask/axis`` endpoint (:func:`context_engine.main.ask_axis`) — shapes
    the result into ``AskAxisResponse``.
  * ``QA.axis_benchmark.run_question`` — measures the seed / pool / bundle
    recall layers off the result; the benchmark is the spec that
    validates *this exact code*.
  * the ``ContextBundle -> PromptContext`` provider
    (``axis_bundles_to_prompt_context``) — Phase 1c.

Design notes that matter for the seam:

* **Module-qualified calls.** Stage functions are reached through their
  *source modules* (``intent_classifier.classify_intent`` rather than a
  bound ``from ... import classify_intent``) so a consumer's monkeypatch
  on ``context_engine.axis.<module>.<fn>`` is honoured — the endpoint test relies
  on this.
* **Caller-owned ``db``.** The endpoint opens one ``db_session`` and the
  benchmark a single ``Neo4jClient``; both pass it through every stage
  instead of re-opening per pass.
* **Optional ``trace``.** The endpoint passes its request trace so each
  stage keeps a span; the benchmark passes nothing and gets a null tracer.
* **Optional ``context_seeds_per_role`` cap.** ``None`` feeds the whole pool
  into context expansion. Callers may pass a positive value for latency A/B,
  but the production endpoint leaves it unset so the Token Credit budget can
  rank the full scope instead of post-processing a pre-truncated pool.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from typing import Any

from context_engine.axis import (
    axis_phased,
    axis_ranking,
    context_builder,
    cross_role_boost,
    doc_anchor_bridge,
    hook_api_bridge,
    http_endpoint_bridge,
    impact_traversal,
    inheritance_ancestors,
    intent_classifier,
    role_lookahead,
    role_retrieval,
    structural_neighbours,
    trace_traversal,
)
from context_engine.axis.context_builder import ContextBundle, ContextRenderBudget, ContextSymbol
from context_engine.axis.intent_classifier import IntentMatch
from context_engine.axis.proximity import proximity_boost
from context_engine.axis.retrieval_budget import ARCHITECTURE, budget_for_intent
from context_engine.axis.role_retrieval import RoleCandidate
from context_engine.axis.stage_warnings import collect_stage_warnings, stage_warning_dicts

# Question-shape pseudo-roles: modes, not retrieval roles. They drive the
# blast-radius / call-chain passes and are excluded from the pools that
# *anchor* those passes.
_MODE_ROLES = frozenset({"impact_analysis", "trace_dependency"})
# Seed-budget multiplier for mode (impact / trace) intents — their answer
# surface is a blast radius and the seed layer now feeds a multi-stage pool.
# Was 2× per_role_limit (=16 at default 8); tightened to 1× to cut
# impact/trace latency on large workspaces. Default caps: 7/35.
_MODE_SEED_LIMIT_FACTOR = 1


class _NullTrace:
    """Tracer stand-in: ``.stage(name)`` is a no-op context manager."""

    def stage(self, _name: str):  # noqa: D401 - trivial
        return contextlib.nullcontext()


@dataclass
class AxisRetrievalResult:
    """Everything the three consumers need, shaped by none of them.

    ``raw_by_role`` is the final pool (post intersection + intent-axis
    boost); ``seed_files`` is the retrieval layer before broad pool
    expansion (intent roles, vector_seed, and doc-anchor *bridge*
    implementors — not the doc-anchor owner files themselves);
    ``candidates_for_context`` is the flattened list actually fed to
    context expansion (already capped when the caller passed
    ``context_seeds_per_role``); ``bundles`` is empty when
    ``with_context`` is false.
    """

    intent: list[IntentMatch]
    raw_by_role: dict[str, list[RoleCandidate]]
    seed_files: list[str]
    candidates_for_context: list[RoleCandidate]
    bundles: list[ContextBundle] = field(default_factory=list)
    render_mode: str = "full"
    stage_warnings: list[dict[str, Any]] = field(default_factory=list)


# Synthetic anchor for a symbol that lives only in the editor overlay (typed
# but not yet indexed). It carries no Neo4j node and no graph edges, so the
# caller must render it overlay-only and skip every graph walk.
_OVERLAY_ANCHOR_ROLE = "overlay_anchor"


def _overlay_anchor_candidate(
    overlay: Any | None,
    *,
    name: str,
    file_path: str,
    workspace_id: str,
    user_id: str,
) -> RoleCandidate | None:
    """Resolve ``name`` against the live editor buffer when the index misses.

    Commit/index is the usual route to a ``uid``; this is the bridge that lets
    a brand-new symbol parsed in the overlay anchor an ask without one. Returns
    ``None`` unless the buffer is present and actually defines ``name``.
    """
    if overlay is None or not file_path or not name:
        return None
    try:
        if not overlay.has(file_path, workspace_id=workspace_id, user_id=user_id):
            return None
        symbols = overlay.get_symbols(file_path, workspace_id=workspace_id, user_id=user_id)
    except Exception:
        return None
    if name not in symbols:
        return None
    return RoleCandidate(
        uid=f"overlay::{workspace_id}::{file_path}::{name}",
        name=name,
        qualified_name="",
        file_path=file_path,
        role=_OVERLAY_ANCHOR_ROLE,
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=1.0,
    )


def _is_overlay_anchor(candidate: RoleCandidate | None) -> bool:
    return candidate is not None and candidate.role == _OVERLAY_ANCHOR_ROLE


def _anchor_path_matches(stored_path: str, requested_path: str) -> bool:
    if not requested_path:
        return True
    stored = stored_path.strip()
    if not stored:
        return False
    if stored == requested_path or stored.endswith(requested_path):
        return True
    if requested_path.endswith(stored) or requested_path.endswith(f"/{stored.rsplit('/', 1)[-1]}"):
        return True
    return stored.endswith(f"/{requested_path.rsplit('/', 1)[-1]}")


def _reorder_candidates_front(
    candidates: list[RoleCandidate],
    *,
    pinned: RoleCandidate,
    skip_uid: str | None = None,
) -> list[RoleCandidate]:
    if candidates and candidates[0].uid == pinned.uid:
        return candidates
    exclude = skip_uid or pinned.uid
    return [pinned, *[candidate for candidate in candidates if candidate.uid != exclude]]


def _best_path_matched_candidate(
    candidates: list[RoleCandidate],
    name: str,
    requested_path: str,
) -> RoleCandidate | None:
    path_matches = [
        candidate
        for candidate in candidates
        if candidate.name == name and _anchor_path_matches(candidate.file_path, requested_path)
    ]
    if not path_matches:
        return None
    return min(
        path_matches,
        key=lambda candidate: (
            10 if "/context_engine/" in (candidate.file_path or "").lower() else 0,
            len(candidate.file_path or ""),
        ),
    )


def _try_pin_from_candidate_pool(
    candidates: list[RoleCandidate],
    name: str,
    requested_path: str,
) -> list[RoleCandidate] | None:
    if requested_path:
        pinned = _best_path_matched_candidate(candidates, name, requested_path)
        if pinned is None:
            return None
        return _reorder_candidates_front(candidates, pinned=pinned)

    for index, candidate in enumerate(candidates):
        if candidate.name != name:
            continue
        if index == 0:
            return candidates
        return [candidate, *[c for i, c in enumerate(candidates) if i != index]]
    return None


def _resolve_anchor_from_scanned(
    scanned: Any | None,
    name: str,
    requested_path: str,
) -> tuple[str, str]:
    if scanned is None:
        return "", ""
    for row in getattr(scanned, "rows", ()) or ():
        if str(row.get("name") or "") != name:
            continue
        row_path = str(row.get("file_path") or "")
        if requested_path and row_path and not _anchor_path_matches(row_path, requested_path):
            continue
        return str(row.get("uid") or ""), row_path
    return "", ""


def _resolve_anchor_from_db(
    db: Any | None,
    name: str,
    requested_path: str,
    workspace_id: str,
) -> tuple[str, str]:
    if db is None:
        return "", ""
    uid = ""
    if requested_path and hasattr(db, "get_symbol_uid_by_name_in_file"):
        uid = (
            db.get_symbol_uid_by_name_in_file(
                name,
                requested_path,
                workspace_id=workspace_id,
            )
            or ""
        )
    elif hasattr(db, "get_symbol_uid_by_name"):
        uid = db.get_symbol_uid_by_name(name, workspace_id=workspace_id) or ""
    file_path = ""
    if uid and hasattr(db, "get_file_path_for_symbol"):
        file_path = db.get_file_path_for_symbol(uid, workspace_id=workspace_id)
    return uid, file_path


def _resolve_anchor_uid_and_path(
    *,
    name: str,
    requested_path: str,
    workspace_id: str,
    scanned: Any | None,
    db: Any | None,
) -> tuple[str, str]:
    uid, file_path = _resolve_anchor_from_scanned(scanned, name, requested_path)
    if uid:
        return uid, file_path
    return _resolve_anchor_from_db(db, name, requested_path, workspace_id)


def _injected_anchor_candidate(uid: str, name: str, file_path: str) -> RoleCandidate:
    return RoleCandidate(
        uid=uid,
        name=name,
        qualified_name="",
        file_path=file_path,
        role="anchor_symbol",
        satisfying_contracts=(),
        satisfying_kinds=(),
        contract_count=0,
        kind_count=0,
        vector_distance=None,
        score=1.0,
    )


def _pin_anchor_symbol(
    candidates: list[RoleCandidate],
    *,
    anchor_symbol: str | None,
    anchor_path: str | None = None,
    workspace_id: str,
    db: Any,
    scanned: Any | None,
    overlay: Any | None = None,
    user_id: str = "anonymous",
) -> list[RoleCandidate]:
    """Move ``anchor_symbol`` to the front of the context pool when set.

    Symbol-targeted asks and benchmark questions name the intended seed
    explicitly; without pinning, homonyms (e.g. ``_StageTimer.stage`` vs
    ``RequestTrace.stage``) can outrank the real entrypoint on vector
    similarity alone.
    """
    name = (anchor_symbol or "").strip()
    if not name:
        return candidates

    requested_path = (anchor_path or "").strip()
    from_pool = _try_pin_from_candidate_pool(candidates, name, requested_path)
    if from_pool is not None:
        return from_pool

    uid, file_path = _resolve_anchor_uid_and_path(
        name=name,
        requested_path=requested_path,
        workspace_id=workspace_id,
        scanned=scanned,
        db=db,
    )
    if not uid:
        synthetic = _overlay_anchor_candidate(
            overlay,
            name=name,
            file_path=requested_path,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        if synthetic is not None:
            return _reorder_candidates_front(candidates, pinned=synthetic)
        return candidates

    injected = _injected_anchor_candidate(uid, name, file_path)
    return _reorder_candidates_front(candidates, pinned=injected, skip_uid=uid)


def _symbol_targeted_budget(
    *,
    intent: list[IntentMatch],
    intent_budget: bool,
    base_token_budget: int,
    render_mode_override: str | None,
) -> tuple[int | None, str, Any | None]:
    if not intent_budget:
        return None, "full", None
    budget_profile = budget_for_intent(intent)
    token_budget = budget_profile.effective_tokens(base_token_budget)
    render_mode = (
        budget_profile.render_mode if render_mode_override is None else render_mode_override
    )
    return token_budget, render_mode, budget_profile


def _impact_mode_context_candidates(
    anchor: RoleCandidate,
    *,
    db,
    workspace_id: str,
    max_impacted: int,
    intent_roles: list[str],
    intent_similarities: dict[str, float],
) -> tuple[list[RoleCandidate], str | None, bool]:
    impact_anchor = replace(
        anchor,
        role="impact_analysis",
        satisfying_kinds=("target_seed",),
        kind_count=1,
        depth=0,
        edge_type="TARGET",
        utility_score=1.0,
    )
    impacted = impact_traversal.expand_impact_neighbourhood(
        [impact_anchor],
        db=db,
        workspace_id=workspace_id,
        max_impacted=max_impacted,
        include_tests=True,
        intent_roles=intent_roles,
        intent_similarities=intent_similarities,
    )
    return [impact_anchor, *impacted], None, True


def _trace_mode_context_candidates(
    anchor: RoleCandidate,
    *,
    db,
    workspace_id: str,
) -> tuple[list[RoleCandidate], str | None]:
    trace_anchor = replace(
        anchor,
        role="trace_dependency",
        satisfying_kinds=("target_seed",),
        kind_count=1,
        depth=0,
        edge_type="TARGET",
        utility_score=1.0,
    )
    traced = trace_traversal.expand_trace_neighbourhood(
        [trace_anchor],
        db=db,
        workspace_id=workspace_id,
    )
    return [trace_anchor, *traced], None


def _symbol_targeted_context_plan(
    anchor: RoleCandidate,
    mode_roles: set[str],
    *,
    db,
    workspace_id: str,
    max_impacted: int,
    intent: list[IntentMatch],
    include_tests_in_walks: bool,
) -> tuple[list[RoleCandidate], str | None, bool]:
    intent_roles = [match.role for match in intent]
    intent_similarities = {match.role: match.similarity for match in intent}
    if "impact_analysis" in mode_roles:
        return _impact_mode_context_candidates(
            anchor,
            db=db,
            workspace_id=workspace_id,
            max_impacted=max_impacted,
            intent_roles=intent_roles,
            intent_similarities=intent_similarities,
        )
    if "trace_dependency" in mode_roles:
        candidates, traversal_mode = _trace_mode_context_candidates(
            anchor,
            db=db,
            workspace_id=workspace_id,
        )
        return candidates, traversal_mode, include_tests_in_walks
    return [anchor], "deferred_binding_flow", include_tests_in_walks


@dataclass(frozen=True)
class _SymbolTargetedRetrievalOptions:
    workspace_id: str
    db: Any
    lance: Any
    user_id: str
    overlay: Any | None
    trace: Any
    with_context: bool
    context_per_seed: int
    max_impacted: int
    hook_transparency: bool
    include_tests_in_walks: bool


@dataclass(frozen=True)
class _ContextBuildOptions:
    workspace_id: str
    db: Any
    lance: Any
    context_per_seed: int
    hook_transparency: bool
    token_budget: int | None
    render_mode: str
    budget_profile: Any | None
    utility_score_fn: Any | None
    traversal_mode: str | None
    include_tests: bool
    overlay: Any | None
    user_id: str


def _build_context_bundles_with_budget(
    candidates: list[RoleCandidate],
    options: _ContextBuildOptions,
) -> list[ContextBundle]:
    profile = options.budget_profile
    budget = ContextRenderBudget(
        token_budget=options.token_budget,
        render_mode=options.render_mode,
        per_transaction_share=profile.per_transaction_share if profile else 0.10,
        file_soft_cap_share=profile.file_soft_cap_share if profile else 0.25,
        signature_only_initial=profile.signature_only_initial if profile else False,
    )
    return context_builder.build_context_for_candidates(
        candidates,
        workspace_id=options.workspace_id,
        db=options.db,
        lance=options.lance,
        max_per_seed=options.context_per_seed,
        hook_transparency=options.hook_transparency,
        render_budget=budget,
        traversal_mode=options.traversal_mode,
        include_tests=options.include_tests,
        overlay=options.overlay,
        user_id=options.user_id,
        utility_score_fn=options.utility_score_fn,
    )


def _move_anchor_bundle_first(
    bundles: list[ContextBundle],
    anchor_uid: str,
) -> list[ContextBundle]:
    if len(bundles) <= 1:
        return bundles
    anchor_index = next(
        (index for index, bundle in enumerate(bundles) if bundle.seed.uid == anchor_uid),
        None,
    )
    if anchor_index is None or anchor_index == 0:
        return bundles
    anchor_bundle = bundles.pop(anchor_index)
    bundles.insert(0, anchor_bundle)
    return bundles


def _try_symbol_targeted_retrieval(
    *,
    anchor_symbol: str | None,
    anchor_path: str | None,
    anchor_only: bool,
    options: _SymbolTargetedRetrievalOptions,
    intent: list[IntentMatch],
    intent_budget: bool,
    base_token_budget: int,
    render_mode_override: str | None,
) -> AxisRetrievalResult | None:
    """CodeLens / ask-from-code fast path — pin the seed, skip pool retrieval.

    An OVERLAY-only anchor (a brand-new symbol that exists solely in the editor
    buffer, with no graph node) always short-circuits here — there is nothing to
    walk. For an INDEXED anchor the fast path is opt-in via ``anchor_only``:
    otherwise a named symbol is a pinned seed hint over full question retrieval,
    so naming a symbol does not silently skip the pool walk (and collapse recall)
    on the /ask path.
    """
    if not (anchor_symbol or "").strip():
        return None

    workspace_id = options.workspace_id
    pinned = _pin_anchor_symbol(
        [],
        anchor_symbol=anchor_symbol,
        anchor_path=anchor_path,
        workspace_id=workspace_id,
        db=options.db,
        scanned=None,
        overlay=options.overlay,
        user_id=options.user_id,
    )
    if not pinned:
        return None

    anchor = pinned[0]
    if _is_overlay_anchor(anchor):
        # Brand-new symbol resolved from the editor buffer alone: it has no
        # graph node, so render its overlay body and skip every walk.
        return _overlay_only_result(
            anchor,
            overlay=options.overlay,
            workspace_id=workspace_id,
            user_id=options.user_id,
            with_context=options.with_context,
            render_mode=render_mode_override or "full",
            intent=intent,
        )

    # Indexed anchor: the fast path (skip pool walk) is opt-in. A question that
    # merely names a symbol still gets full retrieval with the symbol pinned.
    if not anchor_only:
        return None

    token_budget, render_mode, budget_profile = _symbol_targeted_budget(
        intent=intent,
        intent_budget=intent_budget,
        base_token_budget=base_token_budget,
        render_mode_override=render_mode_override,
    )

    from context_engine.axis import graph_walk_inproc

    if graph_walk_inproc.should_use(workspace_id):
        with options.trace.stage("adjacency"):
            graph_walk_inproc.load_adjacency(options.db, workspace_id)

    intent_roles = [match.role for match in intent]
    mode_roles = set(intent_roles) & _MODE_ROLES
    if options.with_context and mode_roles:
        context_candidates, traversal_mode, include_tests = _symbol_targeted_context_plan(
            anchor,
            mode_roles,
            db=options.db,
            workspace_id=workspace_id,
            max_impacted=options.max_impacted,
            intent=intent,
            include_tests_in_walks=options.include_tests_in_walks,
        )
    else:
        context_candidates = [anchor]
        traversal_mode = "deferred_binding_flow"
        include_tests = options.include_tests_in_walks

    bundles: list[ContextBundle] = []
    if options.with_context:
        with options.trace.stage("context"):
            bundles = _build_context_bundles_with_budget(
                context_candidates,
                _ContextBuildOptions(
                    workspace_id=workspace_id,
                    db=options.db,
                    lance=options.lance,
                    context_per_seed=options.context_per_seed,
                    hook_transparency=options.hook_transparency,
                    token_budget=token_budget,
                    render_mode=render_mode,
                    budget_profile=budget_profile,
                    utility_score_fn=None,
                    traversal_mode=traversal_mode,
                    include_tests=include_tests,
                    overlay=options.overlay,
                    user_id=options.user_id,
                ),
            )
            bundles = _move_anchor_bundle_first(bundles, anchor.uid)

    seed_path = (anchor.file_path or "").strip()
    return AxisRetrievalResult(
        intent=list(intent),
        raw_by_role={},
        seed_files=[seed_path] if seed_path else [],
        candidates_for_context=context_candidates,
        bundles=list(bundles),
        render_mode=render_mode,
    )


def _overlay_only_result(
    anchor: RoleCandidate,
    *,
    overlay: Any | None,
    workspace_id: str,
    user_id: str,
    with_context: bool,
    render_mode: str,
    intent: list[IntentMatch] | None = None,
) -> AxisRetrievalResult:
    """Assemble context for an overlay-only anchor without any graph walk.

    The symbol is not in the index, so there is no adjacency to load and no
    neighbourhood to expand. The single bundle carries the buffer body — the
    minimum useful context for an ask about the symbol the user just typed.
    """
    bundles: list[ContextBundle] = []
    if with_context and overlay is not None:
        code: str | None = None
        try:
            symbols = overlay.get_symbols(
                anchor.file_path, workspace_id=workspace_id, user_id=user_id
            )
            span = symbols.get(anchor.name)
            if span is not None:
                start, end = span
                code = overlay.read_lines(
                    anchor.file_path,
                    start,
                    end,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
        except Exception:
            code = None
        seed = ContextSymbol(
            uid=anchor.uid,
            name=anchor.name,
            file_path=anchor.file_path,
            role=_OVERLAY_ANCHOR_ROLE,
            distance_from_seed=0,
            expansion_step=None,
            code=code,
        )
        bundles = [
            ContextBundle(
                role=_OVERLAY_ANCHOR_ROLE,
                seed=seed,
                related=(),
                render_mode=render_mode,
            )
        ]

    seed_path = (anchor.file_path or "").strip()
    return AxisRetrievalResult(
        intent=list(intent or []),
        raw_by_role={},
        seed_files=[seed_path] if seed_path else [],
        candidates_for_context=[anchor],
        bundles=bundles,
        render_mode=render_mode,
    )


def _classify_retrieval_intent(
    question: str,
    lance: Any,
    *,
    intent_override: list[IntentMatch] | None,
    top_roles: int,
    intent_threshold: float,
    trace: Any,
) -> list[IntentMatch]:
    def _embed(text: str):
        return lance._embed([text])[0]  # noqa: SLF001

    with trace.stage("intent"):
        if intent_override is not None:
            return list(intent_override)
        return intent_classifier.classify_intent(
            question,
            _embed,
            top_k=top_roles,
            threshold=intent_threshold,
        )


def _retrieve_role_seeds(
    question: str,
    intent: list[IntentMatch],
    *,
    workspace_id: str,
    db: Any,
    lance: Any,
    seed_limit: int,
    trace: Any,
):
    with trace.stage("retrieval"):
        scanned = role_retrieval.scan_workspace_rows(workspace_id, lance=lance)
        raw_by_role = role_retrieval.find_symbols_by_roles(
            workspace_id,
            [m.role for m in intent],
            query_text=question,
            embed_fn=lambda text: lance._embed([text])[0],  # noqa: SLF001
            limit=seed_limit,
            prescanned=scanned,
        )
        from context_engine.axis import graph_walk_inproc

        if graph_walk_inproc.should_use(workspace_id):
            with trace.stage("adjacency"):
                graph_walk_inproc.load_adjacency(db, workspace_id)
    return scanned, raw_by_role


def _candidate_file_paths(candidates: list[RoleCandidate]) -> set[str]:
    return {getattr(c, "file_path", "") or "" for c in candidates}


def _run_doc_anchor_bridge(
    raw_by_role: dict[str, list[RoleCandidate]],
    *,
    db: Any,
    workspace_id: str,
    scanned,
    include_tests: bool,
    trace: Any,
) -> set[str]:
    doc_anchor_seeds = raw_by_role.get("doc_anchor") or []
    if not doc_anchor_seeds:
        return set()
    with trace.stage("doc_anchor_bridge"):
        raw_by_role["doc_anchor_bridge"] = doc_anchor_bridge.expand_doc_anchor_bridge(
            doc_anchor_seeds,
            db=db,
            workspace_id=workspace_id,
            prescanned=scanned,
            include_tests=include_tests,
        )
    return _candidate_file_paths(raw_by_role.get("doc_anchor_bridge", []))


def _run_http_endpoint_bridge(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent: list[IntentMatch],
    *,
    db: Any,
    workspace_id: str,
    scanned,
    include_tests: bool,
    trace: Any,
) -> set[str]:
    http_bridge_roles = {m.role for m in intent} | {
        "routing_surface",
        "trace_dependency",
        "vector_seed",
    }
    http_bridge_seeds = [
        c for role in http_bridge_roles for c in raw_by_role.get(role, []) if getattr(c, "uid", "")
    ]
    if not http_bridge_seeds:
        return set()
    with trace.stage("http_endpoint_bridge"):
        raw_by_role["http_endpoint_bridge"] = http_endpoint_bridge.expand_http_endpoint_bridge(
            http_bridge_seeds,
            db=db,
            workspace_id=workspace_id,
            prescanned=scanned,
            include_tests=include_tests,
        )
    return _candidate_file_paths(raw_by_role.get("http_endpoint_bridge", []))


def _run_hook_api_bridge(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent: list[IntentMatch],
    *,
    db: Any,
    workspace_id: str,
    scanned,
    include_tests: bool,
    trace: Any,
) -> set[str]:
    hook_bridge_roles = {m.role for m in intent} | {
        "routing_surface",
        "trace_dependency",
        "vector_seed",
        "binding_surface",
    }
    hook_bridge_seeds = [
        c for role in hook_bridge_roles for c in raw_by_role.get(role, []) if getattr(c, "uid", "")
    ]
    if not hook_bridge_seeds:
        return set()
    with trace.stage("hook_api_bridge"):
        raw_by_role["hook_api_bridge"] = hook_api_bridge.expand_hook_api_bridge(
            hook_bridge_seeds,
            db=db,
            workspace_id=workspace_id,
            prescanned=scanned,
            include_tests=include_tests,
        )
    return _candidate_file_paths(raw_by_role.get("hook_api_bridge", []))


def _run_structural_pool_passes(
    raw_by_role: dict[str, list[RoleCandidate]],
    *,
    db: Any,
    lance: Any,
    workspace_id: str,
    scanned,
    trace: Any,
) -> None:
    existing_pool_for_struct = [
        c
        for role, cands in raw_by_role.items()
        if role not in {"impact_analysis", "structural_neighbour"}
        for c in cands
    ]
    if not existing_pool_for_struct:
        return
    with trace.stage("structural_neighbours"):
        affects_pool = structural_neighbours.expand_structural_neighbours(
            existing_pool_for_struct,
            db=db,
            workspace_id=workspace_id,
        )
    ancestor_pool = inheritance_ancestors.expand_inheritance_ancestors(
        existing_pool_for_struct,
        db=db,
        workspace_id=workspace_id,
        exclude_uids=[c.uid for c in affects_pool],
    )
    already = {c.uid for c in (list(affects_pool) + list(ancestor_pool))}
    with trace.stage("phased"):
        phased_pool = axis_phased.expand_phased(
            existing_pool_for_struct,
            db=db,
            lance=lance,
            workspace_id=workspace_id,
            exclude_uids=already,
            prescanned=scanned,
        )
    raw_by_role["structural_neighbour"] = (
        list(affects_pool) + list(ancestor_pool) + list(phased_pool)
    )


def _run_mode_traversal_passes(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent: list[IntentMatch],
    *,
    db: Any,
    workspace_id: str,
    max_impacted: int,
    include_tests_in_walks: bool,
    trace: Any,
) -> None:
    mode_intents_present = {m.role for m in intent if m.role in _MODE_ROLES}
    if not mode_intents_present:
        return
    existing_pool = [
        c for role, cands in raw_by_role.items() if role not in _MODE_ROLES for c in cands
    ]
    if not existing_pool:
        return
    if "impact_analysis" in mode_intents_present:
        with trace.stage("impact_traversal"):
            raw_by_role["impact_analysis"] = impact_traversal.expand_impact_neighbourhood(
                existing_pool,
                db=db,
                workspace_id=workspace_id,
                max_impacted=max_impacted,
                include_tests=include_tests_in_walks,
                intent_roles=[m.role for m in intent],
                intent_similarities={m.role: m.similarity for m in intent},
            )
    if "trace_dependency" in mode_intents_present:
        with trace.stage("trace_traversal"):
            raw_by_role["trace_dependency"] = trace_traversal.expand_trace_neighbourhood(
                existing_pool,
                db=db,
                workspace_id=workspace_id,
            )


def _apply_cross_role_intersection(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent: list[IntentMatch],
    *,
    db: Any,
    workspace_id: str,
    trace: Any,
) -> None:
    has_mode_intent = any(m.role in _MODE_ROLES for m in intent)
    if len(intent) < 2 or has_mode_intent:
        return
    with trace.stage("cross_role_intersection"):
        for i, match in enumerate(intent):
            primary = raw_by_role.get(match.role) or []
            secondary = {
                other.role: raw_by_role.get(other.role) or []
                for j, other in enumerate(intent)
                if j != i
            }
            raw_by_role[match.role] = cross_role_boost.intersect_by_cross_role_proximity(
                primary,
                secondary,
                db=db,
                workspace_id=workspace_id,
            )


def _flatten_candidates_for_context(
    raw_by_role: dict[str, list[RoleCandidate]],
    intent: list[IntentMatch],
    *,
    context_seeds_per_role: int | None,
) -> list[RoleCandidate]:
    intent_role_keys = [m.role for m in intent]
    ordered_keys = intent_role_keys + [r for r in raw_by_role if r not in set(intent_role_keys)]
    candidates_for_context: list[RoleCandidate] = []
    seen_keys: set[str] = set()
    for key in ordered_keys:
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cands = raw_by_role.get(key) or []
        if context_seeds_per_role is not None:
            cands = cands[:context_seeds_per_role]
        candidates_for_context.extend(cands)
    return candidates_for_context


def _prepare_budgeted_candidates(
    candidates_for_context: list[RoleCandidate],
    intent: list[IntentMatch],
    *,
    intent_budget: bool,
    base_token_budget: int,
    render_mode_override: str | None,
    anchor_path: str | None,
    anchor_symbol: str | None,
) -> tuple[
    list[RoleCandidate],
    int | None,
    str,
    Any | None,
    Any | None,
]:
    token_budget: int | None = None
    render_mode = "full"
    budget_profile = None
    utility_score_fn = None
    active = candidates_for_context
    symbol_targeted = bool((anchor_symbol or "").strip())
    if not intent_budget:
        return active, token_budget, render_mode, budget_profile, utility_score_fn

    budget_profile = ARCHITECTURE if symbol_targeted else budget_for_intent(intent)

    def _budget_utility_score(c: RoleCandidate) -> float:
        return c.score + proximity_boost(c.file_path, anchor_path)

    utility_score_fn = _budget_utility_score
    active = sorted(
        candidates_for_context,
        key=lambda c: c.score + proximity_boost(c.file_path, anchor_path),
        reverse=True,
    )
    token_budget = budget_profile.effective_tokens(base_token_budget)
    render_mode = (
        budget_profile.render_mode if render_mode_override is None else render_mode_override
    )
    return active, token_budget, render_mode, budget_profile, utility_score_fn


@dataclass(frozen=True)
class AxisRetrievalConfig:
    top_roles: int = 3
    per_role_limit: int = 7
    max_impacted: int = 35
    intent_threshold: float = 0.20
    with_context: bool = True
    context_per_seed: int = 4
    context_seeds_per_role: int | None = None
    intent_budget: bool = True
    base_token_budget: int = 6000
    render_mode_override: str | None = None
    anchor_path: str | None = None
    anchor_symbol: str | None = None
    # Opt-in latency fast path: when True AND anchor_symbol is set, retrieval
    # returns only the pinned symbol's neighbourhood (CodeLens "explain this
    # symbol"), skipping intent embed + pool walks. Default False so a named
    # symbol is a SEED HINT (pinned) over full question retrieval — naming a
    # symbol must not silently collapse recall on the /ask path.
    anchor_only: bool = False
    hook_transparency: bool = False
    trace: Any | None = None
    overlay: Any | None = None
    user_id: str = "anonymous"
    intent_override: list[IntentMatch] | None = None


def _attach_stage_warnings_to_trace(
    trace: Any | None,
    warnings: list[dict[str, Any]],
) -> None:
    if trace is None or not warnings:
        return
    warn_stage = getattr(trace, "warn_stage", None)
    if not callable(warn_stage):
        return
    for warning in warnings:
        warn_stage(warning)


def run_axis_retrieval(
    question: str,
    *,
    workspace_id: str,
    db: Any,
    lance: Any,
    config: AxisRetrievalConfig | None = None,
) -> AxisRetrievalResult:
    cfg = config or AxisRetrievalConfig()
    with collect_stage_warnings() as warning_bucket:
        result = _run_axis_retrieval_impl(
            question,
            workspace_id=workspace_id,
            db=db,
            lance=lance,
            config=cfg,
        )
    warnings = stage_warning_dicts(warning_bucket)
    result.stage_warnings = warnings
    _attach_stage_warnings_to_trace(cfg.trace, warnings)
    return result


def _run_axis_retrieval_impl(
    question: str,
    *,
    workspace_id: str,
    db: Any,
    lance: Any,
    config: AxisRetrievalConfig | None = None,
) -> AxisRetrievalResult:
    """Run the axis read-side pipeline and return its layered result.

    ``db`` is any live Neo4j handle (``db_session`` value or
    ``Neo4jClient``); ``lance`` is a ``LanceDBClient`` used for both intent
    embedding and the vector seeds. ``trace`` may be any object exposing a
    ``stage(name)`` context manager; pass ``None`` for an un-instrumented
    run. ``context_seeds_per_role=None`` feeds the entire pool into context
    expansion (the benchmarked behaviour); a positive value caps the
    per-role context seeds.

    ``intent_override`` bypasses the embedding role-classifier: when supplied,
    those roles drive retrieval directly (the caller picked them). The vector
    seeds still rerank by embedding, so only role *selection* is replaced. When
    ``None`` (every existing caller) the classifier runs as before.
    """

    cfg = config or AxisRetrievalConfig()
    tr = cfg.trace if cfg.trace is not None else _NullTrace()
    intent = _classify_retrieval_intent(
        question,
        lance,
        intent_override=cfg.intent_override,
        top_roles=cfg.top_roles,
        intent_threshold=cfg.intent_threshold,
        trace=tr,
    )

    if (cfg.anchor_symbol or "").strip():
        fast = _try_symbol_targeted_retrieval(
            anchor_symbol=cfg.anchor_symbol,
            anchor_path=cfg.anchor_path,
            anchor_only=cfg.anchor_only,
            options=_SymbolTargetedRetrievalOptions(
                workspace_id=workspace_id,
                db=db,
                lance=lance,
                user_id=cfg.user_id,
                overlay=cfg.overlay,
                trace=tr,
                with_context=cfg.with_context,
                context_per_seed=cfg.context_per_seed,
                max_impacted=cfg.max_impacted,
                hook_transparency=cfg.hook_transparency,
                include_tests_in_walks=False,
            ),
            intent=list(intent),
            intent_budget=cfg.intent_budget,
            base_token_budget=cfg.base_token_budget,
            render_mode_override=cfg.render_mode_override,
        )
        if fast is not None:
            return fast

    include_tests_in_walks = any(m.role == "impact_analysis" for m in intent)
    impact_mode = any(m.role in _MODE_ROLES for m in intent)
    seed_limit = cfg.per_role_limit * _MODE_SEED_LIMIT_FACTOR if impact_mode else cfg.per_role_limit

    scanned, raw_by_role = _retrieve_role_seeds(
        question,
        intent,
        workspace_id=workspace_id,
        db=db,
        lance=lance,
        seed_limit=seed_limit,
        trace=tr,
    )

    seed_files = {
        getattr(c, "file_path", "") or "" for cands in raw_by_role.values() for c in cands
    }

    if len(intent) >= 2 and any(raw_by_role.values()):
        with tr.stage("cross_role_lookahead"):
            raw_by_role = role_lookahead.expand_candidates_via_neighbourhood(
                [m.role for m in intent],
                raw_by_role,
                db=db,
                lance=lance,
                workspace_id=workspace_id,
                prescanned=scanned,
            )

    with tr.stage("vector_seeds"):
        raw_by_role["vector_seed"] = role_retrieval.find_seeds_by_vector(
            workspace_id,
            question,
            embed_fn=lambda text: lance._embed([text])[0],  # noqa: SLF001
            limit=seed_limit,
            impact_mode=impact_mode,
            prescanned=scanned,
        )
        raw_by_role["doc_anchor"] = role_retrieval.find_seeds_by_doc_anchor(
            workspace_id,
            question,
            embed_fn=lambda text: lance._embed([text])[0],  # noqa: SLF001
            limit=seed_limit,
            impact_mode=impact_mode,
            prescanned=scanned,
            lance=lance,
        )

    seed_files |= _run_doc_anchor_bridge(
        raw_by_role,
        db=db,
        workspace_id=workspace_id,
        scanned=scanned,
        include_tests=include_tests_in_walks,
        trace=tr,
    )
    seed_files |= _candidate_file_paths(raw_by_role.get("vector_seed", []))
    seed_files |= _run_http_endpoint_bridge(
        raw_by_role,
        intent,
        db=db,
        workspace_id=workspace_id,
        scanned=scanned,
        include_tests=include_tests_in_walks,
        trace=tr,
    )
    seed_files |= _run_hook_api_bridge(
        raw_by_role,
        intent,
        db=db,
        workspace_id=workspace_id,
        scanned=scanned,
        include_tests=include_tests_in_walks,
        trace=tr,
    )

    _run_structural_pool_passes(
        raw_by_role,
        db=db,
        lance=lance,
        workspace_id=workspace_id,
        scanned=scanned,
        trace=tr,
    )
    _run_mode_traversal_passes(
        raw_by_role,
        intent,
        db=db,
        workspace_id=workspace_id,
        max_impacted=cfg.max_impacted,
        include_tests_in_walks=include_tests_in_walks,
        trace=tr,
    )
    _apply_cross_role_intersection(
        raw_by_role,
        intent,
        db=db,
        workspace_id=workspace_id,
        trace=tr,
    )

    raw_by_role = axis_ranking.apply_intent_axis_boost(raw_by_role, [m.role for m in intent])
    candidates_for_context = _flatten_candidates_for_context(
        raw_by_role,
        intent,
        context_seeds_per_role=cfg.context_seeds_per_role,
    )
    active, token_budget, render_mode, budget_profile, utility_score_fn = (
        _prepare_budgeted_candidates(
            candidates_for_context,
            intent,
            intent_budget=cfg.intent_budget,
            base_token_budget=cfg.base_token_budget,
            render_mode_override=cfg.render_mode_override,
            anchor_path=cfg.anchor_path,
            anchor_symbol=cfg.anchor_symbol,
        )
    )

    if cfg.anchor_symbol:
        active = _pin_anchor_symbol(
            active,
            anchor_symbol=cfg.anchor_symbol,
            anchor_path=cfg.anchor_path,
            workspace_id=workspace_id,
            db=db,
            scanned=scanned,
        )
        candidates_for_context = _pin_anchor_symbol(
            candidates_for_context,
            anchor_symbol=cfg.anchor_symbol,
            anchor_path=cfg.anchor_path,
            workspace_id=workspace_id,
            db=db,
            scanned=scanned,
        )
        # Collapse the rendered context to just the anchor ONLY for the opt-in
        # CodeLens fast context. A question that merely names a symbol keeps the
        # full ranked pool (anchor pinned to the front) — otherwise the bundle
        # covers only the anchor's neighbourhood and recall collapses.
        if cfg.anchor_only and active:
            active = [active[0]]

    bundles: list[ContextBundle] = []
    if cfg.with_context and active:
        with tr.stage("context"):
            bundles = _build_context_bundles_with_budget(
                active,
                _ContextBuildOptions(
                    workspace_id=workspace_id,
                    db=db,
                    lance=lance,
                    context_per_seed=cfg.context_per_seed,
                    hook_transparency=cfg.hook_transparency,
                    token_budget=token_budget,
                    render_mode=render_mode,
                    budget_profile=budget_profile,
                    utility_score_fn=utility_score_fn,
                    traversal_mode="deferred_binding_flow",
                    include_tests=include_tests_in_walks,
                    overlay=cfg.overlay,
                    user_id=cfg.user_id,
                ),
            )

    return AxisRetrievalResult(
        intent=list(intent),
        raw_by_role=raw_by_role,
        seed_files=sorted(f for f in seed_files if f),
        candidates_for_context=candidates_for_context,
        bundles=list(bundles),
        render_mode=render_mode,
    )


__all__ = ["AxisRetrievalConfig", "AxisRetrievalResult", "run_axis_retrieval"]
