"""Phase 1f echelon-2: signature trimming + token-budget packing.

Targets the pure helpers (``_code_signature``, ``_apply_render_and_budget``)
directly — the graph walk in ``build_context_for_candidates`` is covered by
the benchmark and the live gate.
"""

from __future__ import annotations

from sidecar.axis.context_builder import (
    ContextBundle,
    ContextSymbol,
    _apply_render_and_budget,
    _code_signature,
)


# --- _code_signature -------------------------------------------------------


def test_signature_function_drops_body_keeps_decorators():
    code = "@cached\ndef foo(a, b):\n    x = a + b\n    return x\n"
    assert _code_signature(code) == "@cached\ndef foo(a, b):"


def test_signature_multiline_header_survives_to_the_colon():
    code = "def foo(\n    a: int,\n    b: int,\n) -> int:\n    return a + b\n"
    assert _code_signature(code) == "def foo(\n    a: int,\n    b: int,\n) -> int:"


def test_signature_class_header():
    assert _code_signature("class Foo(Base):\n    x = 1\n") == "class Foo(Base):"


def test_signature_non_callable_is_first_nonempty_line():
    assert _code_signature("\napp = Flask(__name__)\napp.run()\n") == "app = Flask(__name__)"


def test_signature_empty_in_empty_out():
    assert _code_signature("") == ""
    assert _code_signature(None) == ""


# --- _apply_render_and_budget ---------------------------------------------


def _sym(uid: str, code: str) -> ContextSymbol:
    return ContextSymbol(
        uid=uid,
        name=uid,
        file_path="/f.py",
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code=code,
    )


def _bundle(seed_uid: str, seed_code: str, related=()) -> ContextBundle:
    return ContextBundle(
        role="r",
        seed=_sym(seed_uid, seed_code),
        related=tuple(_sym(u, c) for u, c in related),
    )


def test_no_budget_no_render_is_passthrough():
    bundles = [_bundle("a", "x" * 40, [("b", "y" * 40)])]
    assert _apply_render_and_budget(bundles, token_budget=None, render_mode="full") == bundles


def test_signature_only_trims_every_symbol():
    bundles = [_bundle("a", "def f(x):\n    return x\n", [("b", "class C:\n    pass\n")])]
    out = _apply_render_and_budget(bundles, token_budget=None, render_mode="signature_only")
    assert out[0].seed.code == "def f(x):"
    assert out[0].related[0].code == "class C:"


def test_token_budget_keeps_first_seed_drops_the_rest():
    big = "z" * 100_000  # far over any small budget
    bundles = [_bundle("a", big, [("a_rel", big)]), _bundle("b", big)]
    out = _apply_render_and_budget(bundles, token_budget=1, render_mode="full")
    # the first bundle's seed is always kept; its related and the next bundle
    # fall off once the budget is spent.
    assert [b.seed.uid for b in out] == ["a"]
    assert out[0].related == ()


def test_hybrid_keeps_seed_full_and_signatures_the_rest():
    # Default full_render_max_depth=0: only the seed (depth 0) stays full;
    # every expanded neighbour (depth >= 1) collapses to a signature.
    def _at(uid, depth, code):
        return ContextSymbol(
            uid=uid, name=uid, file_path="/f.py", role="r",
            distance_from_seed=depth, expansion_step=None, code=code,
        )

    bundle = ContextBundle(
        role="r",
        seed=_at("s", 0, "def seed(x):\n    return x\n"),
        related=(
            _at("near", 1, "def near(y):\n    return y\n"),
            _at("far", 2, "def far(z):\n    return z\n"),
        ),
    )
    out = _apply_render_and_budget([bundle], token_budget=None, render_mode="hybrid")
    assert out[0].seed.code == "def seed(x):\n    return x\n"  # depth 0 full
    assert out[0].related[0].code == "def near(y):"           # depth 1 signature
    assert out[0].related[1].code == "def far(z):"            # depth 2 signature


def test_generous_budget_keeps_everything():
    bundles = [_bundle("a", "x" * 40, [("b", "y" * 40)]), _bundle("c", "z" * 40)]
    out = _apply_render_and_budget(bundles, token_budget=10_000, render_mode="full")
    assert [b.seed.uid for b in out] == ["a", "c"]
    assert [r.uid for r in out[0].related] == ["b"]
