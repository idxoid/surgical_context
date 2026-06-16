"""error_model classification — language-level exception inheritance."""

from __future__ import annotations

from sidecar.indexer.fast.registry_class_inheritance import (
    _EXCEPTION_BASES,
    resolve_error_model_uids,
)


def test_direct_builtin_subclass_is_anchor():
    """``ClickException(Exception)`` names a builtin exception in its
    parsed bases — a direct error_model anchor."""
    parsed = {"ce": ["Exception"]}
    out = resolve_error_model_uids(parsed, {"ce": set()})
    assert out == {"ce"}


def test_runtimeerror_subclass_is_anchor():
    """``Exit(RuntimeError)`` / ``Abort(RuntimeError)`` — RuntimeError is
    in the builtin set, so these anchor directly."""
    parsed = {"exit": ["RuntimeError"], "abort": ["RuntimeError"]}
    out = resolve_error_model_uids(parsed, {"exit": set(), "abort": set()})
    assert out == {"exit", "abort"}


def test_transitive_inheritance_through_workspace_class():
    """``UsageError(ClickException)`` reaches the builtin anchor through
    the in-workspace ``ClickException`` via DEPENDS_ON ancestry."""
    parsed = {
        "ce": ["Exception"],  # anchor
        "ue": ["ClickException"],  # no builtin base of its own
        "bad": ["UsageError"],  # two hops from the anchor
    }
    ancestors = {
        "ce": set(),
        "ue": {"ce"},
        "bad": {"ue", "ce"},  # DEPENDS_ON*1..6 is transitive
    }
    out = resolve_error_model_uids(parsed, ancestors)
    assert out == {"ce", "ue", "bad"}


def test_non_exception_class_is_not_error_model():
    """A plain class whose bases never reach a builtin exception is not
    an error_model — the kind must not leak onto data models."""
    parsed = {
        "cmd": ["object"],
        "model": ["BaseModel"],
        "ce": ["Exception"],
    }
    ancestors = {"cmd": set(), "model": set(), "ce": set()}
    out = resolve_error_model_uids(parsed, ancestors)
    assert out == {"ce"}


def test_alias_only_ancestor_edge_converges():
    """When the anchor is reachable only through an alias-resolved
    ancestor edge (added as a single hop, not transitive), the fixpoint
    iteration still propagates error_model down a multi-level chain."""
    parsed = {
        "root": ["Exception"],  # anchor
        "mid": ["AliasedRoot"],  # alias edge → root (only direct hop known)
        "leaf": ["Mid"],  # alias edge → mid
    }
    # Each class only knows its immediate alias-resolved parent, not the
    # full transitive set — the fixpoint must still reach leaf.
    ancestors = {
        "root": set(),
        "mid": {"root"},
        "leaf": {"mid"},
    }
    out = resolve_error_model_uids(parsed, ancestors)
    assert out == {"root", "mid", "leaf"}


def test_empty_input_returns_empty():
    assert resolve_error_model_uids({}, {}) == set()


def test_exception_base_set_is_language_level_not_domain():
    """Sanity: the anchor set is the Python builtin exception hierarchy
    (a language contract), so the common roots are present and a
    domain word like 'Command' is not."""
    assert "Exception" in _EXCEPTION_BASES
    assert "BaseException" in _EXCEPTION_BASES
    assert "RuntimeError" in _EXCEPTION_BASES
    assert "ValueError" in _EXCEPTION_BASES
    assert "Command" not in _EXCEPTION_BASES
    assert "BaseModel" not in _EXCEPTION_BASES
    # Warnings are deliberately excluded — not error-surface answers.
    assert "Warning" not in _EXCEPTION_BASES
    assert "DeprecationWarning" not in _EXCEPTION_BASES
