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
from collections.abc import Iterable
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "file_path": self.file_path,
            "role": self.role,
            "distance_from_seed": self.distance_from_seed,
            "expansion_step": self.expansion_step,
            "code": self.code,
        }


@dataclass(frozen=True)
class ContextBundle:
    """Bundle for one seed candidate: the seed plus its expanded
    related symbols, ordered closest-first."""

    role: str
    seed: ContextSymbol
    related: tuple[ContextSymbol, ...] = field(default_factory=tuple)

    def all_symbols(self) -> list[ContextSymbol]:
        return [self.seed, *self.related]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "seed": self.seed.to_dict(),
            "related": [s.to_dict() for s in self.related],
        }


def _fetch_codes(
    lance,
    workspace_id: str,
    uids: set[str],
) -> dict[str, str | None]:
    """Pull ``code`` for a set of uids in one table scan.

    Lance does not give us a clean WHERE-by-list across heterogeneous
    columns; one full scan filtered in-process is acceptable for the
    workspaces we currently target (thousands of symbols, not millions).
    """
    if not uids:
        return {}
    table = lance._sym_table  # noqa: SLF001
    columns = ["uid", "code", "workspace_id"]

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
        return {
            r["uid"]: r.get("code")
            for r in arrow.to_pylist()
            if r.get("workspace_id") == workspace_id and r.get("uid") in uids
        }

    out: dict[str, str | None] = {}
    for uid_raw, code, row_workspace_id in zip(row_uids, codes, workspace_ids):
        if row_workspace_id != workspace_id:
            continue
        uid = str(uid_raw or "")
        if uid in uids:
            out[uid] = code
    return out


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


def _apply_render_and_budget(
    bundles: list[ContextBundle],
    *,
    token_budget: int | None,
    render_mode: str,
) -> list[ContextBundle]:
    """Echelon 2: render-trim then token-pack the assembled bundles.

    ``render_mode="signature_only"`` replaces each symbol's code with its
    signature first (so many files fit cheaply). When ``token_budget`` is
    set we then pack bundles in rank order — seed first, then its related
    symbols — summing estimated tokens and dropping the tail once the budget
    is hit. The first bundle's seed is always kept so there is a primary.
    Cross-bundle duplicates are counted (not deduped here — the PromptContext
    adapter dedups by uid), so the cut is conservative."""
    if render_mode == "signature_only":
        bundles = [
            replace(
                b,
                seed=replace(b.seed, code=_code_signature(b.seed.code)),
                related=tuple(
                    replace(r, code=_code_signature(r.code)) for r in b.related
                ),
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


def build_context_for_candidates(
    candidates: Iterable[RoleCandidate],
    *,
    workspace_id: str,
    db,
    lance,
    max_per_seed: int = 6,
    traversal_mode: str = "deferred_binding_flow",
    include_tests: bool = False,
    token_budget: int | None = None,
    render_mode: str = "full",
) -> list[ContextBundle]:
    """Expand each candidate into a ``ContextBundle`` of related code.

    ``max_per_seed`` caps how many related symbols come back per seed
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
    if not candidates:
        return []

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
            key=lambda h: (h.depth, (h.name or "").lower()),
        )[:max_per_seed]
        expansion_per_candidate.append((cand, ordered))
        for h in ordered:
            uids_to_fetch.add(h.uid)

    code_by_uid = _fetch_codes(lance, workspace_id, uids_to_fetch)

    bundles: list[ContextBundle] = []
    for cand, hits in expansion_per_candidate:
        seed = ContextSymbol(
            uid=cand.uid,
            name=cand.name,
            file_path=cand.file_path,
            role=cand.role,
            distance_from_seed=0,
            expansion_step=None,
            code=code_by_uid.get(cand.uid),
        )
        related = tuple(
            ContextSymbol(
                uid=h.uid,
                name=h.name,
                file_path=h.file_path,
                role=cand.role,
                distance_from_seed=h.depth,
                expansion_step=h.step,
                code=code_by_uid.get(h.uid),
            )
            for h in hits
        )
        bundles.append(
            ContextBundle(role=cand.role, seed=seed, related=related)
        )
    return _apply_render_and_budget(
        bundles, token_budget=token_budget, render_mode=render_mode
    )


__all__ = [
    "ContextBundle",
    "ContextSymbol",
    "build_context_for_candidates",
]
