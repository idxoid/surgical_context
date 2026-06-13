"""Structural path-locality boost for seed ranking (Phase 1f, B_proximity).

The ``S_utility`` ranking that picks ACTIVE (walked) seeds is
``S_vector × W_type + B_proximity``. The first two already live in
``find_seeds_by_vector`` (semantic score × file_tier weight); this module is
the third term: an additive boost from the **ask anchor** — the file the user
is working in (the IDE's open file / an agent's active dir). Seeds in the same
folder as the anchor are most likely part of the feature under work, so they
earn a walk slot and a packing-priority bump.

It is a *ranking prior*, not a role/structure decision — no symbol-name or
path-stem matching assigns meaning here; the anchor is an explicit, caller-
supplied locality signal. With no anchor the boost is uniformly 0, so the
ranking falls back to ``S_vector × W_type`` unchanged (no downside).

Suffix-aware on purpose: the anchor may be a short repo-relative path
(``celery/worker/state.py``) while a seed carries an absolute path
(``/abs/.../celery/worker/strategy.py``) — we compare directory tails so the
two still register as same-folder.
"""

from __future__ import annotations


def _norm_dir(path: str) -> str:
    """Directory of ``path``, normalised to forward slashes, no trailing slash."""
    p = (path or "").replace("\\", "/").rstrip("/")
    if "/" not in p:
        return ""
    return p.rsplit("/", 1)[0]


def _suffix_match(a: str, b: str) -> bool:
    """True when the shorter of ``a``/``b`` is a path-suffix of the longer
    (or they are equal). Both are directory strings."""
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long == short or long.endswith("/" + short)


def proximity_boost(
    seed_path: str,
    anchor_path: str | None,
    *,
    same_dir: float = 0.15,
    sibling_dir: float = 0.05,
) -> float:
    """Additive locality boost for a seed given the ask anchor.

    ``same_dir`` when the seed lives in the anchor's directory; ``sibling_dir``
    when they share an immediate parent (one folder over, or a sub-folder of
    the anchor's dir); ``0.0`` otherwise or when there is no anchor.
    """
    if not seed_path or not anchor_path:
        return 0.0
    seed_dir = _norm_dir(seed_path)
    anchor_dir = _norm_dir(anchor_path)
    if not seed_dir or not anchor_dir:
        return 0.0
    if _suffix_match(seed_dir, anchor_dir):
        return same_dir
    # Sibling: the seed dir is a child of the anchor dir, the anchor dir is a
    # child of the seed dir, or the two share an immediate parent ("one folder
    # over"). Suffix-aware so absolute vs repo-relative paths still match.
    seed_parent = _norm_dir(seed_dir)
    anchor_parent = _norm_dir(anchor_dir)
    if (
        _suffix_match(seed_parent, anchor_dir)
        or _suffix_match(seed_dir, anchor_parent)
        or _suffix_match(seed_parent, anchor_parent)
    ):
        return sibling_dir
    return 0.0


__all__ = ["proximity_boost"]
