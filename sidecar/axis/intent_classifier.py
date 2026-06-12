"""Intent classifier — free-text question → L4 role(s).

The bridge between a user's question and the structurally-derived role
layer. Today the legacy ``/ask`` endpoint does intent + retrieval +
ranking in one tangled cascade; this module is the smallest honest
shim that lets a future axis-only ``/ask`` reach L4 roles from plain
English / Russian without the cascade.

Mechanism: each role carries a short canonical description (one
sentence stating what kind of question the role answers). At call time
we embed both the question and every role description using the same
embedder Lance uses for symbol vectors, then rank roles by cosine
similarity. The role embeddings are cached after first use; only the
incoming question pays for an embed per call.

Discipline:

  - No keyword / regex rules. Surface-level token matching is fragile
    and quickly becomes the next ``_target_query_bonus`` (Phase 9.5).
  - No LLM call. Intent classification has to be deterministic and
    local so retrieval is reproducible across processes.
  - Every role described here must also exist in ``ROLE_CONTRACT_MAP``;
    a description that points at a non-existent role is dead code and
    flagged by a unit test.
  - Descriptions are *what kind of question the role answers*, not
    *what kind of symbol satisfies it*. That keeps the embeddings
    closer to user-shaped phrasing.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from sidecar.axis.role_resolver import ROLE_CONTRACT_MAP


# Each description is one short sentence the embedder can map to a
# query in similar shape. Keep them user-facing, not implementation-
# facing. Add a description only when a role is actually intent-shaped
# (i.e. a real consumer might ask a question that points at it).
ROLE_INTENT_DESCRIPTIONS: dict[str, str] = {
    "routing_surface": (
        "How are URL routes, HTTP endpoints, WebSocket connections, or "
        "request handlers registered, matched, and dispatched?"
    ),
    "task_surface": (
        "How are background tasks, queued jobs, or scheduled workers "
        "registered and executed?"
    ),
    "error_surface": (
        "How are exception handlers, error responses, and error "
        "middleware dispatched?"
    ),
    "proxy_mechanism": (
        "How does a lazy proxy or context-bound global (like "
        "current_app, request) resolve to its underlying object?"
    ),
    "dependency_solver": (
        "How is a dependency injected into a function parameter via a "
        "provider, marker, or Depends-style binding?"
    ),
    "data_model_surface": (
        "Where are data model classes, typed records, or schema-like "
        "structures declared?"
    ),
    "configuration_surface": (
        "Where are configuration carriers, settings classes, or option "
        "defaults defined?"
    ),
    "metadata_surface": (
        "Where is keyed metadata stored, written, and read back by "
        "name?"
    ),
    "dispatch_surface": (
        "How does a middleware chain or callable container iterate and "
        "invoke its stored callables at runtime?"
    ),
    "binding_surface": (
        "How is a value bound or registered now so that it can be "
        "dispatched later by the runtime?"
    ),
    "impact_analysis": (
        "If this code changes, what callers, tests, downstream "
        "modules, or example code would break or be affected?"
    ),
    "trace_dependency": (
        "What is the call chain from this code: which callers reach "
        "it and which functions does it invoke or delegate to?"
    ),
}


_missing_roles = set(ROLE_INTENT_DESCRIPTIONS) - set(ROLE_CONTRACT_MAP)
if _missing_roles:  # pragma: no cover - module-load consistency check
    raise RuntimeError(
        f"intent descriptions reference unknown roles: {sorted(_missing_roles)}"
    )


@dataclass(frozen=True)
class IntentMatch:
    """One role that the classifier thinks the question is asking about."""

    role: str
    similarity: float
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "similarity": self.similarity,
            "description": self.description,
        }


_ROLE_VECTOR_CACHE: dict[str, list[float]] = {}

# Initial threshold says "is this role at all plausible?". The secondary
# gate below says "is this runner-up strong enough to spend graph budget
# on?". A close-tie escape hatch keeps genuinely ambiguous questions multi-role
# even when every absolute score is low.
_SECONDARY_ABSOLUTE_THRESHOLD = 0.21
_SECONDARY_RELATIVE_TO_TOP = 0.92


def _ensure_list(vector) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(x) for x in vector]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _role_vector(role: str, embed_fn: Callable[[str], object]) -> list[float]:
    cached = _ROLE_VECTOR_CACHE.get(role)
    if cached is not None:
        return cached
    vec = _ensure_list(embed_fn(ROLE_INTENT_DESCRIPTIONS[role]))
    _ROLE_VECTOR_CACHE[role] = vec
    return vec


def clear_role_vector_cache() -> None:
    """Test/debug helper — forget cached role embeddings."""
    _ROLE_VECTOR_CACHE.clear()


def _prune_weak_tail(
    matches: list[IntentMatch],
    *,
    threshold: float,
    secondary_threshold: float | None = None,
    secondary_relative_to_top: float = _SECONDARY_RELATIVE_TO_TOP,
) -> list[IntentMatch]:
    """Keep the primary match, then drop weak low-margin runner-ups.

    ``threshold`` remains the caller-controlled plausibility floor. This
    second gate is a resource-management guard: additional roles trigger
    cross-role lookahead, graph walks, and context expansion, so a role
    barely above the floor should only survive when it is either
    absolutely strong enough or a near-tie with the primary role.
    """
    if not matches:
        return []
    primary = matches[0]
    if len(matches) == 1:
        return list(matches)

    secondary_floor = max(
        threshold,
        _SECONDARY_ABSOLUTE_THRESHOLD
        if secondary_threshold is None
        else secondary_threshold,
    )
    relative_floor = primary.similarity * secondary_relative_to_top
    pruned = [primary]
    for match in matches[1:]:
        if match.similarity >= secondary_floor or match.similarity >= relative_floor:
            pruned.append(match)
    return pruned


def classify_intent(
    question: str,
    embed_fn: Callable[[str], object],
    *,
    top_k: int = 3,
    threshold: float = 0.20,
    secondary_threshold: float | None = None,
    secondary_relative_to_top: float = _SECONDARY_RELATIVE_TO_TOP,
) -> list[IntentMatch]:
    """Return up to ``top_k`` roles whose canonical description embeds
    closest to ``question`` (cosine ≥ ``threshold``), sorted desc.

    ``embed_fn`` is supplied by the caller so this module stays
    decoupled from the concrete embedder. Production code should pass
    LanceDB's embedder so query / role / symbol vectors all share the
    same space; tests can pass any deterministic function.

    The best match is always kept. Additional matches pass a secondary
    tail gate so near-threshold noise does not fan out every downstream
    graph pass; close ties survive via ``secondary_relative_to_top``.
    """
    if not question.strip():
        return []
    query_vec = _ensure_list(embed_fn(question))
    matches: list[IntentMatch] = []
    for role, description in ROLE_INTENT_DESCRIPTIONS.items():
        role_vec = _role_vector(role, embed_fn)
        sim = _cosine_similarity(query_vec, role_vec)
        if sim >= threshold:
            matches.append(
                IntentMatch(
                    role=role,
                    similarity=sim,
                    description=description,
                )
            )
    matches.sort(key=lambda m: m.similarity, reverse=True)
    matches = _prune_weak_tail(
        matches,
        threshold=threshold,
        secondary_threshold=secondary_threshold,
        secondary_relative_to_top=secondary_relative_to_top,
    )
    # Cap to top-k *role* intents; ``impact_analysis`` and
    # ``trace_dependency`` are question-shape modes (no retrieval pool
    # of their own — see ``role_resolver.ROLE_EVIDENCE_MAP``) so they
    # must not displace a role intent in the cut. If a mode intent
    # crossed threshold but ranked outside the top-k, it gets appended;
    # otherwise the corresponding traversal would simply not run on a
    # question that asked for it.
    mode_roles = {"impact_analysis", "trace_dependency"}
    role_intents = [m for m in matches if m.role not in mode_roles][:top_k]
    mode_intents = [m for m in matches if m.role in mode_roles]
    appended: list[IntentMatch] = []
    role_role_set = {m.role for m in role_intents}
    for m in mode_intents:
        if m.role not in role_role_set and m not in role_intents:
            appended.append(m)
    if appended:
        merged = sorted(
            role_intents + appended,
            key=lambda m: m.similarity,
            reverse=True,
        )
        return merged
    return role_intents


__all__ = [
    "IntentMatch",
    "ROLE_INTENT_DESCRIPTIONS",
    "classify_intent",
    "clear_role_vector_cache",
]
