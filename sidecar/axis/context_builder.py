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

from collections import namedtuple
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from sidecar.axis.graph_walk import steps_for_mode, walk_neighbours_grouped
from sidecar.axis.role_retrieval import RoleCandidate
from sidecar.observability.metrics import estimate_text_tokens

# One expansion hit: a neighbour reached from a seed, tagged with the
# step that found it. Mirrors the fields the bundle builder reads off the
# legacy ``AxisGraphHit``.
_Hit = namedtuple("_Hit", "uid name file_path depth step")


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "distance_from_seed": self.distance_from_seed,
            "expansion_step": self.expansion_step,
            "code": self.code,
            "qualified_name": self.qualified_name,
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


def _fetch_symbol_payloads(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, dict[str, str | None]]:
    """Pull ``code`` + lightweight render metadata for a set of uids.

    Lance does not give us a clean WHERE-by-list across heterogeneous
    columns; one full scan filtered in-process is acceptable for the
    workspaces we currently target (thousands of symbols, not millions).
    """
    if not uids:
        return {}
    table = lance._sym_table  # noqa: SLF001
    columns = ["uid", "code", "workspace_id"]
    try:
        if "qualified_name" in set(table.schema.names):
            columns.append("qualified_name")
    except Exception:
        pass

    def _quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    uid_filter = ", ".join(_quote(uid) for uid in sorted(uids))
    filter_sql = f"workspace_id = {_quote(workspace_id)} AND uid IN ({uid_filter})"
    lance_table = table.to_lance()
    try:
        arrow = lance_table.to_table(columns=columns, filter=filter_sql)
    except TypeError:
        arrow = lance_table.to_table(columns=columns)
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
            arrow = arrow.filter(mask)
        except Exception:
            pass

    try:
        row_uids = arrow["uid"].to_pylist()
        codes = arrow["code"].to_pylist()
        workspace_ids = arrow["workspace_id"].to_pylist()
    except Exception:
        out: dict[str, dict[str, str | None]] = {}
        for r in arrow.to_pylist():
            uid = str(r.get("uid") or "")
            if r.get("workspace_id") == workspace_id and uid in uids:
                out[uid] = {
                    "code": r.get("code"),
                    "qualified_name": r.get("qualified_name") or "",
                }
        return out

    try:
        qualified_names = arrow["qualified_name"].to_pylist()
    except Exception:
        qualified_names = [""] * len(row_uids)

    out: dict[str, dict[str, str | None]] = {}
    for uid_raw, code, row_workspace_id, qualified_name in zip(
        row_uids, codes, workspace_ids, qualified_names, strict=False
    ):
        if row_workspace_id != workspace_id:
            continue
        uid = str(uid_raw or "")
        if uid in uids:
            out[uid] = {
                "code": code,
                "qualified_name": str(qualified_name or ""),
            }
    return out


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


def _code_signature(code: str | None) -> str:
    """Best-effort, parser-free trim of a symbol's code to its signature:
    leading decorators plus the ``def``/``class`` header (through the line
    ending in ``:``, so multi-line parameter lists survive). Non-callable
    symbols (a module-level assignment, say) collapse to their first
    non-empty line. Empty in, empty out."""
    if not code:
        return ""
    lines = code.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n and lines[i].strip().startswith("@"):  # decorators
        out.append(lines[i])
        i += 1
    started = False
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not started:
            is_header = (
                stripped.startswith("def ")
                or stripped.startswith("async def ")
                or stripped.startswith("class ")
            )
            if not is_header:
                # Not a callable/class — first non-empty line is the "signature".
                if stripped:
                    out.append(line)
                    break
                i += 1
                continue
            started = True
        out.append(line)
        if stripped.endswith(":"):
            break
        i += 1
    return "\n".join(out)


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
    if signature.startswith("class "):
        return qn
    if not (signature.startswith("def ") or signature.startswith("async def ")):
        return None
    header_line = next(
        (line for line in raw_signature.splitlines() if line.strip() and not line.strip().startswith("@")),
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


def _fold_class_symbols(symbols: list[ContextSymbol]) -> list[ContextSymbol]:
    """Fold already-selected class members into synthetic class blocks.

    No graph lookup occurs here: the group is built only from symbols that are
    already in the bundle. A group with one ambiguous method is left alone; a
    group with a class symbol or multiple members is safe to render as a class
    skeleton.
    """
    by_parent: dict[str, list[ContextSymbol]] = {}
    parent_by_uid: dict[str, str] = {}
    for sym in symbols:
        parent = _class_parent_from_qualified_name(sym)
        if parent is None:
            continue
        by_parent.setdefault(parent, []).append(sym)
        parent_by_uid[sym.uid] = parent

    foldable: set[str] = set()
    for parent, members in by_parent.items():
        has_class_symbol = any(
            _code_signature(member.code).lstrip().startswith("class ")
            and member.qualified_name == parent
            for member in members
        )
        if has_class_symbol or len(members) >= 2:
            foldable.add(parent)

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
        members = by_parent[parent]
        class_header = f"class {_class_name_from_qualified_name(parent)}:"
        for member in members:
            sig = _code_signature(member.code).lstrip()
            if sig.startswith("class ") and member.qualified_name == parent:
                class_header = sig.splitlines()[0]
                break
        body_blocks: list[str] = []
        for member in members:
            sig = _code_signature(member.code).lstrip()
            if sig.startswith("class ") and member.qualified_name == parent:
                continue
            keep_full = member.distance_from_seed == 0 or member.name == "__init__"
            body_blocks.append(_indent_block(member.code if keep_full else sig))
        code = "\n".join([class_header, *(body_blocks or ["    ..."])])
        first = min(members, key=lambda m: (m.distance_from_seed, m.uid))
        out.append(
            replace(
                first,
                name=_class_name_from_qualified_name(parent),
                qualified_name=parent,
                distance_from_seed=min(m.distance_from_seed for m in members),
                expansion_step="fold",
                code=code,
            )
        )
    return out


def _apply_fold_render(bundles: list[ContextBundle]) -> list[ContextBundle]:
    folded: list[ContextBundle] = []
    for bundle in bundles:
        symbols = _fold_class_symbols(bundle.all_symbols())
        if not symbols:
            folded.append(bundle)
            continue
        seed = symbols[0]
        related = tuple(symbols[1:])
        folded.append(replace(bundle, seed=seed, related=related))
    return folded


def _trim_symbol_for_mode(
    sym: ContextSymbol,
    render_mode: str,
    *,
    full_render_max_depth: int,
) -> ContextSymbol:
    if render_mode == "signature_only" or (
        render_mode == "hybrid"
        and sym.distance_from_seed > full_render_max_depth
    ):
        return replace(sym, code=_code_signature(sym.code))
    return sym


def _render_bundle(
    bundle: ContextBundle,
    render_mode: str,
    *,
    full_render_max_depth: int = 0,
) -> ContextBundle:
    if render_mode == "fold":
        rendered = _apply_fold_render([bundle])[0]
        if rendered == bundle:
            return bundle
    elif render_mode in ("signature_only", "hybrid"):
        rendered = replace(
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
        )
    else:
        rendered = bundle
    return replace(rendered, render_mode=render_mode)


def _bundle_token_count(bundle: ContextBundle) -> int:
    return sum(estimate_text_tokens(sym.code or "") for sym in bundle.all_symbols())


def _render_modes_for_credit(initial_mode: str) -> tuple[str, ...]:
    if initial_mode == "signature_only":
        return ("signature_only",)
    if initial_mode == "fold":
        return ("fold", "signature_only")
    return (initial_mode, "fold", "signature_only")


def _render_with_transaction_limit(
    bundle: ContextBundle,
    initial_mode: str,
    *,
    per_transaction_limit: int,
    full_render_max_depth: int,
) -> tuple[ContextBundle, int]:
    last: tuple[ContextBundle, int] | None = None
    for mode in _render_modes_for_credit(initial_mode):
        rendered = _render_bundle(
            bundle,
            mode,
            full_render_max_depth=full_render_max_depth,
        )
        cost = _bundle_token_count(rendered)
        last = (rendered, cost)
        if cost <= per_transaction_limit or mode == "signature_only":
            return rendered, cost
    assert last is not None
    return last


#: Utility added per extra class-member a bundle folds into a class block.
#: A fold-block stands for several symbols at a cheap folded cost, so a bundle
#: carrying a coherent N-method class ranks above a lone symbol of equal base
#: utility (the design's fold aggregation bonus). It is 0 until ``qualified_name``
#: is indexed (no qn -> no fold grouping -> no bonus), so it stays graceful
#: pre-reindex.
FOLD_AGGREGATION_BONUS = 0.1


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


def _apply_token_credit_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int,
    render_mode: str,
    full_render_max_depth: int,
    per_transaction_share: float = 0.10,
) -> list[ContextBundle]:
    """Token Credit System v1: utility queue + tariffs + anti-oligarch cap.

    This replaces greedy "render then cut tail" when explicitly enabled. Every
    bundle buys a render mode from the same capital pool. Active bundles start
    at the profile render mode; passive bundles start at signatures. If a
    transaction exceeds its per-bundle limit, it downgrades full/hybrid -> fold
    -> signature instead of dropping the candidate. After the first pass, any
    surplus upgrades passive bundles toward full in utility order.
    """
    if token_budget <= 0:
        return bundles

    transaction_limit = max(1, int(token_budget * per_transaction_share))
    # Sort by utility + fold-aggregation bonus: a bundle whose class members
    # fold into a block stands for several symbols cheaply, so it outranks a lone
    # symbol of equal base utility. uid/index tiebreak keeps the order
    # deterministic (PYTHONHASHSEED-independent).
    queue = sorted(
        enumerate(bundles),
        key=lambda item: (
            item[1].utility_score
            + _fold_aggregation_bonus(item[1], per_member=FOLD_AGGREGATION_BONUS),
            -item[0],
        ),
        reverse=True,
    )
    selected: list[dict[str, object]] = []
    used = 0

    for _, bundle in queue:
        initial_mode = "signature_only" if bundle.passive else render_mode
        rendered, cost = _render_with_transaction_limit(
            bundle,
            initial_mode,
            per_transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
        )
        if selected and used + cost > token_budget:
            continue
        selected.append({"source": bundle, "rendered": rendered, "cost": cost})
        used += cost
        if used >= token_budget:
            break

    # Surplus loop: passive candidates paid the cheap signature tariff first.
    # Upgrade the highest-utility passive entries while capital remains.
    for entry in selected:
        if used >= token_budget:
            break
        source = entry["source"]
        if not isinstance(source, ContextBundle) or not source.passive:
            continue
        current = entry["rendered"]
        current_cost = entry["cost"]
        if not isinstance(current, ContextBundle) or not isinstance(current_cost, int):
            continue
        upgraded, upgraded_cost = _render_with_transaction_limit(
            source,
            "full",
            per_transaction_limit=transaction_limit,
            full_render_max_depth=full_render_max_depth,
        )
        delta = upgraded_cost - current_cost
        if delta <= 0 or used + delta > token_budget:
            continue
        entry["rendered"] = upgraded
        entry["cost"] = upgraded_cost
        used += delta

    return [
        entry["rendered"]
        for entry in selected
        if isinstance(entry.get("rendered"), ContextBundle)
    ]


def _apply_render_and_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int | None,
    render_mode: str,
    full_render_max_depth: int = 0,
    token_credit: bool = False,
) -> list[ContextBundle]:
    """Echelon 2: render-trim then token-pack the assembled bundles.

    ``render_mode`` granularity (a symbol is trimmed to its signature when):
      * ``"full"`` — never (whole pool, full code).
      * ``"signature_only"`` — always (max breadth, every symbol a signature).
      * ``"hybrid"`` — only when ``distance_from_seed > full_render_max_depth``
        (default 0 → the seed stays full, every expanded neighbour collapses to
        a signature). This is the architecture profile: full code on the
        answer's primary symbol, cheap signatures for all its context. Keeping
        even depth-1 deps full barely economizes (they dominate the volume), so
        the default is seed-only.

    When ``token_budget`` is set we then pack bundles in rank order — seed
    first, then its related symbols — summing estimated tokens and dropping
    the tail once the budget is hit. The first bundle's seed is always kept so
    there is a primary. Cross-bundle duplicates are counted (not deduped here —
    the PromptContext adapter dedups by uid), so the cut is conservative."""

    if token_credit and token_budget is not None:
        return _apply_token_credit_budget(
            bundles,
            token_budget=token_budget,
            render_mode=render_mode,
            full_render_max_depth=full_render_max_depth,
        )

    if render_mode == "fold":
        bundles = [
            _render_bundle(
                b,
                "fold",
                full_render_max_depth=full_render_max_depth,
            )
            for b in bundles
        ]
    if render_mode in ("signature_only", "hybrid"):
        bundles = [
            _render_bundle(
                b,
                render_mode,
                full_render_max_depth=full_render_max_depth,
            )
            for b in bundles
        ]
    if token_budget is None:
        return bundles

    used = 0
    out: list[ContextBundle] = []
    for bundle in bundles:
        seed_tokens = estimate_text_tokens(bundle.seed.code or "")
        if out and used + seed_tokens > token_budget:
            break  # cannot fit this bundle's seed — stop
        used += seed_tokens
        kept: list[ContextSymbol] = []
        for rel in bundle.related:
            rel_tokens = estimate_text_tokens(rel.code or "")
            if used + rel_tokens > token_budget:
                break
            used += rel_tokens
            kept.append(rel)
        out.append(replace(bundle, related=tuple(kept)))
        if used >= token_budget:
            break
    return out


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
        db, workspace_id, seed_uids,
        edges=_HOOK_DECL_EDGES, direction="undirected",
        max_hops=1, limit_per_seed=limit * 4,
    )
    site_to_seeds: dict[str, list[str]] = {}
    for seed, sites in sites_by_seed.items():
        for s in sites:
            site_to_seeds.setdefault(s.uid, []).append(seed)
    if not site_to_seeds:
        return {}
    api_by_site = walk_neighbours_grouped(
        db, workspace_id, sorted(site_to_seeds),
        edges=_HOOK_REGISTER_API_EDGES, direction="undirected",
        max_hops=1, limit_per_seed=limit * 4,
    )
    out: dict[str, list[_Hit]] = {}
    for site_uid, apis in api_by_site.items():
        for seed in site_to_seeds.get(site_uid, ()):
            bucket = out.setdefault(seed, [])
            for a in apis:
                bucket.append(_Hit(a.uid, a.name, a.file_path, 2, "hook_transparency"))
    return out


def build_context_for_candidates(
    candidates: Iterable[RoleCandidate],
    *,
    workspace_id: str,
    db,
    lance,
    passive: Iterable[RoleCandidate] = (),
    passive_shallow_hops: int = 0,
    passive_shallow_limit: int = 3,
    max_per_seed: int = 6,
    traversal_mode: str = "deferred_binding_flow",
    include_tests: bool = False,
    hook_transparency: bool = False,
    token_budget: int | None = None,
    render_mode: str = "full",
    token_credit: bool = False,
    utility_score_fn: Callable[[RoleCandidate], float] | None = None,
) -> list[ContextBundle]:
    """Expand each ACTIVE candidate into a ``ContextBundle`` of related code.

    ``candidates`` are the *active* seeds — the only ones that get a graph
    WALK (the expensive part). ``passive`` seeds skip the walk entirely: they
    join the bundle list as code-bearing, signature-rendered, seed-only
    bundles (no neighbours), so their files survive for the token budget at
    near-zero cost. This is echelon 1's active/passive split — bound the walk,
    keep the pool.

    ``max_per_seed`` caps how many related symbols come back per active seed
    (depth-then-name ordering). ``traversal_mode`` picks the expansion
    pattern from ``AxisQueryPlan``; defaults to deferred-binding
    because every current contract uses it.

    ``include_tests`` mirrors the retrieval-pass flag — by default,
    expansion hits that land in conventional test surfaces are
    dropped. Impact-style consumers can flip the flag to keep them.

    ``render_mode`` / ``token_budget`` are the echelon-2 budget knobs
    (default off = whole pool, full code = benchmark behaviour):
    ``signature_only`` trims each symbol to its signature, and a non-None
    ``token_budget`` packs bundles in rank order until the budget is spent.
    """
    from sidecar.axis.test_file_filter import is_test_path
    candidates = list(candidates)
    passive = list(passive)
    if not candidates and not passive:
        return []

    def _utility_score(candidate: RoleCandidate) -> float:
        if utility_score_fn is None:
            return candidate.score
        return utility_score_fn(candidate)

    # One batched grouped walk per expansion step over ALL candidate uids,
    # instead of a per-candidate traversal (N graph round-trips collapse to
    # one per step). Each seed still gets its OWN neighbourhood —
    # ``walk_neighbours_grouped`` returns ``{seed_uid: [neighbours]}`` — so
    # the per-seed dedupe/fence/cap below is byte-identical to the old
    # AxisGraphTraversal path. Steps run in order so a uid reached by an
    # earlier step keeps its (shallower) label on a depth tie.
    all_uids = [c.uid for c in candidates]
    hits_per_seed: dict[str, list[_Hit]] = {u: [] for u in all_uids}
    for step_name, edges, direction, max_hops in steps_for_mode(traversal_mode):
        grouped = walk_neighbours_grouped(
            db,
            workspace_id,
            all_uids,
            edges=edges,
            direction=direction,
            max_hops=max_hops,
            limit_per_seed=max_per_seed * 4,
        )
        for su, neighbours in grouped.items():
            bucket = hits_per_seed.get(su)
            if bucket is None:
                continue
            for nb in neighbours:
                bucket.append(
                    _Hit(nb.uid, nb.name, nb.file_path, nb.depth, step_name)
                )

    # Hook transparency: open hook-DECLARATION seeds through their registration
    # lifecycle (incoming HOOK sites -> the registration API they go through).
    # Inert for non-hook seeds. See ``_hook_transparency_hits``.
    if hook_transparency:
        for su, extra in _hook_transparency_hits(
            db, workspace_id, all_uids, limit=max_per_seed
        ).items():
            bucket = hits_per_seed.get(su)
            if bucket is not None:
                bucket.extend(extra)

    expansion_per_candidate: list[
        tuple[RoleCandidate, list]
    ] = []
    uids_to_fetch: set[str] = set()
    for cand in candidates:
        uids_to_fetch.add(cand.uid)
        hits = hits_per_seed.get(cand.uid, [])
        # Dedupe by uid, keep the shallowest occurrence (closer wins).
        # The test-file fence applies after dedup: an expansion hit
        # that lands in a test surface is dropped unless the caller
        # opted in via ``include_tests``.
        nearest_by_uid: dict[str, _Hit] = {}
        for h in hits:
            if not include_tests and is_test_path(h.file_path or ""):
                continue
            existing = nearest_by_uid.get(h.uid)
            if existing is None or h.depth < existing.depth:
                nearest_by_uid[h.uid] = h
        ordered = sorted(
            nearest_by_uid.values(),
            key=lambda h: (h.depth, (h.name or "").lower(), h.uid),
        )[:max_per_seed]
        expansion_per_candidate.append((cand, ordered))
        for h in ordered:
            uids_to_fetch.add(h.uid)

    # Shallow walk for passive seeds (opt-in): fetch each passive seed's
    # immediate neighbours so a relational answer (B = a 1-hop neighbour of a
    # passive seed) is covered without the full active walk. Tight cap
    # (passive_shallow_limit) + signature render keep the pool growth cheap.
    passive_hits: dict[str, list[_Hit]] = {}
    if passive and passive_shallow_hops > 0:
        p_uids = [p.uid for p in passive]
        p_buckets: dict[str, dict[str, _Hit]] = {u: {} for u in p_uids}
        for step_name, edges, direction, _mh in steps_for_mode(traversal_mode):
            grouped = walk_neighbours_grouped(
                db, workspace_id, p_uids,
                edges=edges, direction=direction,
                max_hops=passive_shallow_hops,
                limit_per_seed=passive_shallow_limit,
            )
            for su, neighbours in grouped.items():
                bucket = p_buckets.get(su)
                if bucket is None:
                    continue
                for nb in neighbours:
                    if not include_tests and is_test_path(nb.file_path or ""):
                        continue
                    ex = bucket.get(nb.uid)
                    if ex is None or nb.depth < ex.depth:
                        bucket[nb.uid] = _Hit(
                            nb.uid, nb.name, nb.file_path, nb.depth, step_name
                        )
        for su, bucket in p_buckets.items():
            ordered = sorted(
                bucket.values(), key=lambda h: (h.depth, (h.name or "").lower(), h.uid)
            )[:passive_shallow_limit]
            passive_hits[su] = ordered
            for h in ordered:
                uids_to_fetch.add(h.uid)

    for p in passive:
        uids_to_fetch.add(p.uid)
    payload_by_uid = _fetch_symbol_payloads(lance, workspace_id, uids_to_fetch)

    def _code(uid: str) -> str | None:
        return payload_by_uid.get(uid, {}).get("code")

    def _qualified_name(uid: str) -> str:
        return str(payload_by_uid.get(uid, {}).get("qualified_name") or "")

    bundles: list[ContextBundle] = []
    for cand, hits in expansion_per_candidate:
        seed = ContextSymbol(
            uid=cand.uid,
            name=cand.name,
            file_path=cand.file_path,
            role=cand.role,
            distance_from_seed=0,
            expansion_step=None,
            code=_code(cand.uid),
            qualified_name=cand.qualified_name or _qualified_name(cand.uid),
        )
        related = tuple(
            ContextSymbol(
                uid=h.uid,
                name=h.name,
                file_path=h.file_path,
                role=cand.role,
                distance_from_seed=h.depth,
                expansion_step=h.step,
                code=_code(h.uid),
                qualified_name=_qualified_name(h.uid),
            )
            for h in hits
        )
        bundles.append(
            ContextBundle(
                role=cand.role,
                seed=seed,
                related=related,
                utility_score=_utility_score(cand),
                passive=False,
            )
        )

    # Passive seeds: code-bearing context that skipped the (full) walk.
    # Rendered as signatures (cheap, file stays covered), appended after the
    # active bundles so the token budget fills active expansion first. With
    # shallow-walk on, each carries its immediate neighbours (also signatures).
    for p in passive:
        related = tuple(
            ContextSymbol(
                uid=h.uid,
                name=h.name,
                file_path=h.file_path,
                role=p.role,
                distance_from_seed=h.depth,
                expansion_step=h.step,
                code=_code_signature(_code(h.uid)),
                qualified_name=_qualified_name(h.uid),
            )
            for h in passive_hits.get(p.uid, [])
        )
        bundles.append(
            ContextBundle(
                role=p.role,
                utility_score=_utility_score(p),
                passive=True,
                seed=ContextSymbol(
                    uid=p.uid,
                    name=p.name,
                    file_path=p.file_path,
                    role=p.role,
                    distance_from_seed=0,
                    expansion_step=None,
                    code=_code_signature(_code(p.uid)),
                    qualified_name=p.qualified_name or _qualified_name(p.uid),
                ),
                related=related,
            )
        )
    return _apply_render_and_budget(
        bundles,
        token_budget=token_budget,
        render_mode=render_mode,
        token_credit=token_credit,
    )


__all__ = [
    "ContextBundle",
    "ContextSymbol",
    "build_context_for_candidates",
]
