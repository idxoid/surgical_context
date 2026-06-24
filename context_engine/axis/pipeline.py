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
from context_engine.axis.context_builder import ContextBundle, ContextSymbol
from context_engine.axis.intent_classifier import IntentMatch
from context_engine.axis.proximity import proximity_boost
from context_engine.axis.retrieval_budget import ARCHITECTURE, budget_for_intent
from context_engine.axis.role_retrieval import RoleCandidate

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

    def _path_matches(stored_path: str) -> bool:
        if not requested_path:
            return True
        stored = stored_path.strip()
        if not stored:
            return False
        if stored == requested_path or stored.endswith(requested_path):
            return True
        if requested_path.endswith(stored) or requested_path.endswith(
            f"/{stored.rsplit('/', 1)[-1]}"
        ):
            return True
        if "/context_engine/" in requested_path:
            alt = requested_path.replace("/context_engine/", "/context_engine/", 1)
            if stored == alt or stored.endswith(alt):
                return True
        elif "/context_engine/" in requested_path:
            alt = requested_path.replace("/context_engine/", "/context_engine/", 1)
            if stored == alt or stored.endswith(alt):
                return True
        return stored.endswith(f"/{requested_path.rsplit('/', 1)[-1]}")

    path_matches = [c for c in candidates if c.name == name and _path_matches(c.file_path)]
    if path_matches:
        pinned = min(
            path_matches,
            key=lambda c: (
                10 if "/context_engine/" in (c.file_path or "").lower() else 0,
                len(c.file_path or ""),
            ),
        )
        if candidates and candidates[0].uid == pinned.uid:
            return candidates
        return [pinned, *[c for c in candidates if c.uid != pinned.uid]]

    if not requested_path and candidates:
        for index, candidate in enumerate(candidates):
            if candidate.name == name:
                if index == 0:
                    return candidates
                pinned = candidates[index]
                return [pinned, *[c for i, c in enumerate(candidates) if i != index]]

    uid = ""
    file_path = ""
    if scanned is not None:
        for row in getattr(scanned, "rows", ()) or ():
            if str(row.get("name") or "") != name:
                continue
            row_path = str(row.get("file_path") or "")
            if requested_path and row_path and not _path_matches(row_path):
                continue
            uid = str(row.get("uid") or "")
            file_path = row_path
            break
    if not uid and db is not None:
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
        if uid and hasattr(db, "get_file_path_for_symbol"):
            file_path = db.get_file_path_for_symbol(uid, workspace_id=workspace_id)

    if not uid:
        # Index missed — fall through to the live editor buffer. Commit is not
        # the only way to anchor an ask; an overlay-parsed symbol is enough.
        synthetic = _overlay_anchor_candidate(
            overlay,
            name=name,
            file_path=requested_path,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        if synthetic is not None:
            return [synthetic, *[c for c in candidates if c.uid != synthetic.uid]]
        return candidates

    injected = RoleCandidate(
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
    return [injected, *[c for c in candidates if c.uid != uid]]


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


def _try_symbol_targeted_retrieval(
    *,
    anchor_symbol: str | None,
    anchor_path: str | None,
    workspace_id: str,
    db: Any,
    lance: Any,
    with_context: bool,
    context_per_seed: int,
    max_impacted: int,
    intent: list[IntentMatch],
    intent_budget: bool,
    base_token_budget: int,
    render_mode_override: str | None,
    hook_transparency: bool,
    include_tests_in_walks: bool,
    overlay: Any | None,
    user_id: str,
    trace: Any,
) -> AxisRetrievalResult | None:
    """CodeLens / ask-from-code fast path — pin the seed, skip pool retrieval.

    The question is still classified before this function runs.  A pinned
    symbol removes seed-search ambiguity; it does not erase whether the user
    asked for architecture, impact, or a call trace.
    """
    if not (anchor_symbol or "").strip():
        return None

    pinned = _pin_anchor_symbol(
        [],
        anchor_symbol=anchor_symbol,
        anchor_path=anchor_path,
        workspace_id=workspace_id,
        db=db,
        scanned=None,
        overlay=overlay,
        user_id=user_id,
    )
    if not pinned:
        return None

    anchor = pinned[0]
    if _is_overlay_anchor(anchor):
        # Brand-new symbol resolved from the editor buffer alone: it has no
        # graph node, so render its overlay body and skip every walk.
        return _overlay_only_result(
            anchor,
            overlay=overlay,
            workspace_id=workspace_id,
            user_id=user_id,
            with_context=with_context,
            render_mode=render_mode_override or "full",
            intent=intent,
        )

    token_budget, render_mode, budget_profile = _symbol_targeted_budget(
        intent=intent,
        intent_budget=intent_budget,
        base_token_budget=base_token_budget,
        render_mode_override=render_mode_override,
    )

    from context_engine.axis import graph_walk_inproc

    if graph_walk_inproc.should_use(workspace_id):
        with trace.stage("adjacency"):
            graph_walk_inproc.load_adjacency(db, workspace_id)

    intent_roles = [match.role for match in intent]
    intent_similarities = {match.role: match.similarity for match in intent}
    mode_roles = set(intent_roles) & _MODE_ROLES
    context_candidates = [anchor]
    traversal_mode: str | None = "deferred_binding_flow"
    include_tests = include_tests_in_walks

    if with_context and "impact_analysis" in mode_roles:
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
        context_candidates = [impact_anchor, *impacted]
        traversal_mode = None
        include_tests = True
    elif with_context and "trace_dependency" in mode_roles:
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
        context_candidates = [trace_anchor, *traced]
        traversal_mode = None

    bundles: list[ContextBundle] = []
    if with_context:
        with trace.stage("context"):
            bundles = context_builder.build_context_for_candidates(
                context_candidates,
                workspace_id=workspace_id,
                db=db,
                lance=lance,
                max_per_seed=context_per_seed,
                hook_transparency=hook_transparency,
                token_budget=token_budget,
                render_mode=render_mode,
                per_transaction_share=(
                    budget_profile.per_transaction_share if budget_profile else 0.10
                ),
                file_soft_cap_share=(
                    budget_profile.file_soft_cap_share if budget_profile else 0.25
                ),
                signature_only_initial=(
                    budget_profile.signature_only_initial if budget_profile else False
                ),
                traversal_mode=traversal_mode,
                include_tests=include_tests,
                overlay=overlay,
                user_id=user_id,
            )
            if len(bundles) > 1:
                anchor_index = next(
                    (
                        index
                        for index, bundle in enumerate(bundles)
                        if bundle.seed.uid == anchor.uid
                    ),
                    None,
                )
                if anchor_index is not None and anchor_index != 0:
                    anchor_bundle = bundles.pop(anchor_index)
                    bundles.insert(0, anchor_bundle)

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


def run_axis_retrieval(
    question: str,
    *,
    workspace_id: str,
    db: Any,
    lance: Any,
    top_roles: int = 3,
    per_role_limit: int = 7,
    max_impacted: int = 35,
    intent_threshold: float = 0.20,
    with_context: bool = True,
    context_per_seed: int = 4,
    context_seeds_per_role: int | None = None,
    intent_budget: bool = True,
    base_token_budget: int = 6000,
    render_mode_override: str | None = None,
    anchor_path: str | None = None,
    anchor_symbol: str | None = None,
    hook_transparency: bool = False,
    trace: Any | None = None,
    overlay: Any | None = None,
    user_id: str = "anonymous",
    intent_override: list[IntentMatch] | None = None,
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

    tr = trace if trace is not None else _NullTrace()

    def _embed(text: str):
        return lance._embed([text])[0]  # noqa: SLF001

    with tr.stage("intent"):
        if intent_override is not None:
            intent = list(intent_override)
        else:
            intent = intent_classifier.classify_intent(
                question,
                _embed,
                top_k=top_roles,
                threshold=intent_threshold,
            )

    symbol_targeted = bool((anchor_symbol or "").strip())
    if symbol_targeted:
        fast = _try_symbol_targeted_retrieval(
            anchor_symbol=anchor_symbol,
            anchor_path=anchor_path,
            workspace_id=workspace_id,
            db=db,
            lance=lance,
            with_context=with_context,
            context_per_seed=context_per_seed,
            max_impacted=max_impacted,
            intent=list(intent),
            intent_budget=intent_budget,
            base_token_budget=base_token_budget,
            render_mode_override=render_mode_override,
            hook_transparency=hook_transparency,
            include_tests_in_walks=False,
            overlay=overlay,
            user_id=user_id,
            trace=tr,
        )
        if fast is not None:
            return fast

    # Impact questions explicitly ask "what tests are affected". Tests reach
    # the pool ONLY through the dedicated, hub-gated ``impacted_tests`` walk in
    # ``impact_traversal`` (and the context render of what it finds) — never via
    # Lance seeds (test rows drown production symbols in vector top-k) nor via
    # the broad structural / lookahead / phased passes (a test flood there
    # displaces production neighbours unrelated to the change).
    include_tests_in_walks = any(m.role == "impact_analysis" for m in intent)

    # Seed budget. ``per_role_limit`` was tuned for the original two-stage
    # pipeline (seed -> walker). The seed layer now feeds a MULTI-stage pool
    # (cross-role lookahead, structural neighbours, phased — each a 1-hop
    # axis expansion) before the mode walker, and a mode question's answer
    # surface is a blast radius, not a point. Both widen what the seeds must
    # carry, so a budget sized for seed->walker starves the real seeds (the
    # changed symbol ranks just outside the cut and never anchors the impact
    # walk). Widen the seed budget for mode intents — a relative ×factor over
    # the caller's ``per_role_limit``, not an absolute cap.
    impact_mode = any(m.role in _MODE_ROLES for m in intent)
    seed_limit = per_role_limit * _MODE_SEED_LIMIT_FACTOR if impact_mode else per_role_limit

    with tr.stage("retrieval"):
        # One workspace-scoped scan (predicate pushdown + parse once)
        # feeds every role retrieval and the vector seeds.
        scanned = role_retrieval.scan_workspace_rows(workspace_id, lance=lance)
        raw_by_role: dict[str, list] = role_retrieval.find_symbols_by_roles(
            workspace_id,
            [m.role for m in intent],
            query_text=question,
            embed_fn=_embed,
            limit=seed_limit,
            prescanned=scanned,
        )
        from context_engine.axis import graph_walk_inproc

        if graph_walk_inproc.should_use(workspace_id):
            with tr.stage("adjacency"):
                graph_walk_inproc.load_adjacency(db, workspace_id)

    # Seed layer — intent-role retrieval plus role-agnostic vector seeds.
    # Doc-anchor owner paths are excluded; bridge implementors are included
    # once ``doc_anchor_bridge`` runs (see below).
    seed_files: set[str] = {
        getattr(c, "file_path", "") or "" for cands in raw_by_role.values() for c in cands
    }

    # Cross-role *lookahead*: walk K hops from each role's vector
    # candidates, inject neighbours whose container_kinds back a different
    # intent role. Closes the case where the intent classifier picks the
    # right theme but the answer lives in a sibling role. Injection-only —
    # it never displaces vector candidates.
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

    # Role-AGNOSTIC vector seeds — added AFTER lookahead (which rebuilds
    # the dict around intent roles and would drop a non-intent key). Intent
    # stays a resource manager (ranking + depth), out of structure
    # selection: pure similarity keeps the right nodes when intent misroutes.
    with tr.stage("vector_seeds"):
        raw_by_role["vector_seed"] = role_retrieval.find_seeds_by_vector(
            workspace_id,
            question,
            embed_fn=_embed,
            limit=seed_limit,
            impact_mode=impact_mode,
            prescanned=scanned,
        )
        raw_by_role["doc_anchor"] = role_retrieval.find_seeds_by_doc_anchor(
            workspace_id,
            question,
            embed_fn=_embed,
            limit=seed_limit,
            impact_mode=impact_mode,
            prescanned=scanned,
            lance=lance,
        )

    # Doc-anchor vector search lands on interface/docstring owners; seed
    # recall wants the reverse-USES_TYPE implementors (``doc_anchor_bridge``),
    # not those owner files.
    doc_anchor_seeds = raw_by_role.get("doc_anchor") or []
    if doc_anchor_seeds:
        with tr.stage("doc_anchor_bridge"):
            raw_by_role["doc_anchor_bridge"] = doc_anchor_bridge.expand_doc_anchor_bridge(
                doc_anchor_seeds,
                db=db,
                workspace_id=workspace_id,
                prescanned=scanned,
                include_tests=include_tests_in_walks,
            )

    seed_files |= {getattr(c, "file_path", "") or "" for c in raw_by_role.get("vector_seed", [])}
    seed_files |= {
        getattr(c, "file_path", "") or "" for c in raw_by_role.get("doc_anchor_bridge", [])
    }

    http_bridge_roles = {m.role for m in intent} | {
        "routing_surface",
        "trace_dependency",
        "vector_seed",
    }
    http_bridge_seeds = [
        c for role in http_bridge_roles for c in raw_by_role.get(role, []) if getattr(c, "uid", "")
    ]
    if http_bridge_seeds:
        with tr.stage("http_endpoint_bridge"):
            raw_by_role["http_endpoint_bridge"] = http_endpoint_bridge.expand_http_endpoint_bridge(
                http_bridge_seeds,
                db=db,
                workspace_id=workspace_id,
                prescanned=scanned,
                include_tests=include_tests_in_walks,
            )

    seed_files |= {
        getattr(c, "file_path", "") or "" for c in raw_by_role.get("http_endpoint_bridge", [])
    }

    hook_bridge_roles = {m.role for m in intent} | {
        "routing_surface",
        "trace_dependency",
        "vector_seed",
        "binding_surface",
    }
    hook_bridge_seeds = [
        c for role in hook_bridge_roles for c in raw_by_role.get(role, []) if getattr(c, "uid", "")
    ]
    if hook_bridge_seeds:
        with tr.stage("hook_api_bridge"):
            raw_by_role["hook_api_bridge"] = hook_api_bridge.expand_hook_api_bridge(
                hook_bridge_seeds,
                db=db,
                workspace_id=workspace_id,
                prescanned=scanned,
                include_tests=include_tests_in_walks,
            )

    seed_files |= {
        getattr(c, "file_path", "") or "" for c in raw_by_role.get("hook_api_bridge", [])
    }

    # Structural-neighbour pass — file-level adjacency via undirected
    # AFFECTS, plus the upward inheritance walk and the reactive phased
    # walk (REGISTRY*->CONTROL) seeded by the pool's kinds (not intent).
    existing_pool_for_struct = [
        c
        for role, cands in raw_by_role.items()
        if role not in {"impact_analysis", "structural_neighbour"}
        for c in cands
    ]
    if existing_pool_for_struct:
        with tr.stage("structural_neighbours"):
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
        with tr.stage("phased"):
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

    # Mode passes — both anchor on every concrete candidate already
    # nominated, but keep their traversal semantics separate:
    # impact_analysis is blast-radius; trace_dependency is CALLS-only.
    mode_intents_present = {m.role for m in intent if m.role in _MODE_ROLES}
    if mode_intents_present:
        existing_pool = [
            c for role, cands in raw_by_role.items() if role not in _MODE_ROLES for c in cands
        ]
        if existing_pool:
            if "impact_analysis" in mode_intents_present:
                with tr.stage("impact_traversal"):
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
                with tr.stage("trace_traversal"):
                    raw_by_role["trace_dependency"] = trace_traversal.expand_trace_neighbourhood(
                        existing_pool,
                        db=db,
                        workspace_id=workspace_id,
                    )

    # Multi-role *intersection* — weaker signals act as structural
    # constraints, not a separate pool. Skipped under a mode intent, where
    # the right answer often has no proximity to the tangential candidates.
    has_mode_intent = any(m.role in _MODE_ROLES for m in intent)
    if len(intent) >= 2 and not has_mode_intent:
        with tr.stage("cross_role_intersection"):
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

    # Intent-axis ranking — intent as a ranker (not a selector). Boost
    # candidates whose kind-axes match the intent's axes; pools re-sort.
    # Role-agnostic seeds (no kinds) pass through untouched.
    raw_by_role = axis_ranking.apply_intent_axis_boost(raw_by_role, [m.role for m in intent])

    # Flatten in intent-role order, then any lookahead-promoted roles.
    # ``raw_by_role`` may carry roles the intent classifier never produced
    # (see ``expand_candidates_via_neighbourhood`` auto-promote); skipping
    # them would discard graph-evidenced candidates.
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

    # Intent-driven budgeting (opt-in; benchmark leaves it off -> walk all).
    # The Token Credit System IS the budget: walk the full ranked scope (no
    # pre-cut), then let the marginal token-credit packer (echelon 2) buy the
    # minimal render context per bundle. Pre-cutting the walk would only let the
    # token budget post-process an already truncated scope, so there is no
    # active/passive split — the whole pool is active.
    token_budget: int | None = None
    render_mode = "full"
    budget_profile = None
    active = candidates_for_context
    utility_score_fn = None
    symbol_targeted = bool((anchor_symbol or "").strip())
    if intent_budget:
        # CodeLens / ask-from-code names the seed explicitly — prefer full
        # structural context on that symbol over impact blast-radius stubs.
        budget_profile = ARCHITECTURE if symbol_targeted else budget_for_intent(intent)
        # S_utility = score (S_vector × W_type, already in the candidate score)
        # + B_proximity (path-locality from the ask anchor). The boost only
        # reorders the packing priority; it does not mutate the candidate score
        # the response/bundle carries. anchor_path is None -> boost 0 -> rank by
        # score alone (no downside).
        active = sorted(
            candidates_for_context,
            key=lambda c: c.score + proximity_boost(c.file_path, anchor_path),
            reverse=True,
        )

        def _budget_utility_score(c: RoleCandidate) -> float:
            return c.score + proximity_boost(c.file_path, anchor_path)

        utility_score_fn = _budget_utility_score
        token_budget = budget_profile.effective_tokens(base_token_budget)
        render_mode = (
            budget_profile.render_mode if render_mode_override is None else render_mode_override
        )

    if anchor_symbol:
        active = _pin_anchor_symbol(
            active,
            anchor_symbol=anchor_symbol,
            anchor_path=anchor_path,
            workspace_id=workspace_id,
            db=db,
            scanned=scanned,
        )
        candidates_for_context = _pin_anchor_symbol(
            candidates_for_context,
            anchor_symbol=anchor_symbol,
            anchor_path=anchor_path,
            workspace_id=workspace_id,
            db=db,
            scanned=scanned,
        )
        # Symbol-targeted ask: expand only the pinned anchor's neighbourhood,
        # not every vector/role candidate in the pool.
        if active:
            active = [active[0]]

    bundles: list[ContextBundle] = []
    if with_context and active:
        with tr.stage("context"):
            bundles = context_builder.build_context_for_candidates(
                active,
                workspace_id=workspace_id,
                db=db,
                lance=lance,
                max_per_seed=context_per_seed,
                hook_transparency=hook_transparency,
                token_budget=token_budget,
                render_mode=render_mode,
                per_transaction_share=(
                    budget_profile.per_transaction_share if budget_profile else 0.10
                ),
                file_soft_cap_share=(
                    budget_profile.file_soft_cap_share if budget_profile else 0.25
                ),
                signature_only_initial=(
                    budget_profile.signature_only_initial if budget_profile else False
                ),
                utility_score_fn=utility_score_fn,
                include_tests=include_tests_in_walks,
                overlay=overlay,
                user_id=user_id,
            )

    return AxisRetrievalResult(
        intent=list(intent),
        raw_by_role=raw_by_role,
        seed_files=sorted(f for f in seed_files if f),
        candidates_for_context=candidates_for_context,
        bundles=list(bundles),
        render_mode=render_mode,
    )


__all__ = ["AxisRetrievalResult", "run_axis_retrieval"]
