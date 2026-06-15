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
    _bundle_token_count,
    _code_compact,
    _code_signature,
    _initial_credit_render,
    _render_bundle,
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


def test_code_compact_keeps_structure_calls_and_returns():
    code = '''def render(x):
    """Docstring goes away."""
    temp = prepare(x)
    noisy = "x" * 1000
    if temp:
        return finish(temp)
    debug_log("ignored enough to still keep call")
    return None
'''

    compact = _code_compact(code)

    assert '"""Docstring goes away."""' not in compact
    assert "def render(x):" in compact
    assert "temp = prepare(x)" in compact
    assert "if temp:" in compact
    assert "return finish(temp)" in compact
    assert "return None" in compact


# --- _apply_render_and_budget ---------------------------------------------


def _sym(
    uid: str,
    code: str,
    *,
    qualified_name: str = "",
    file_path: str = "/f.py",
) -> ContextSymbol:
    return ContextSymbol(
        uid=uid,
        name=uid,
        file_path=file_path,
        role="r",
        distance_from_seed=0,
        expansion_step=None,
        code=code,
        qualified_name=qualified_name,
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


def test_hybrid_compact_keeps_seed_compact_and_signatures_the_rest():
    bundle = ContextBundle(
        role="r",
        seed=_sym("s", "def seed(x):\n    y = build(x)\n    return y\n"),
        related=(
            ContextSymbol(
                uid="near",
                name="near",
                file_path="/f.py",
                role="r",
                distance_from_seed=1,
                expansion_step=None,
                code="def near(y):\n    return y\n",
            ),
        ),
    )

    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=None,
        render_mode="hybrid_compact",
    )

    assert out.seed.code.rstrip() == "def seed(x):\n    y = build(x)\n    return y"
    assert out.related[0].code == "def near(y):"


def test_generous_budget_keeps_everything():
    bundles = [_bundle("a", "x" * 40, [("b", "y" * 40)]), _bundle("c", "z" * 40)]
    out = _apply_render_and_budget(bundles, token_budget=10_000, render_mode="full")
    # Ample budget -> the packer keeps every bundle (full render, related intact).
    # Output is in selection order, so compare as a set.
    assert {b.seed.uid for b in out} == {"a", "c"}
    a_bundle = next(b for b in out if b.seed.uid == "a")
    assert [r.uid for r in a_bundle.related] == ["b"]


def test_fold_groups_class_members_and_signatures_siblings():
    seed = _sym(
        "target",
        "    def target(self):\n        value = self.helper()\n        return value\n",
        qualified_name="pkg.mod.Service.target",
    )
    sibling = ContextSymbol(
        uid="helper",
        name="helper",
        file_path="/f.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code="    def helper(self):\n        return 1\n",
        qualified_name="pkg.mod.Service.helper",
    )
    bundle = ContextBundle(role="r", seed=seed, related=(sibling,))

    [out] = _apply_render_and_budget([bundle], token_budget=None, render_mode="fold")

    assert out.seed.name == "Service"
    assert out.seed.qualified_name == "pkg.mod.Service"
    assert out.related == ()
    assert out.seed.code == (
        "class Service:\n"
        "    def target(self):\n"
        "        value = self.helper()\n"
        "        return value\n"
        "    def helper(self):"
    )


def test_fold_compact_groups_class_members_as_signatures_only():
    seed = _sym(
        "target",
        "    def target(self):\n        value = self.helper()\n        return value\n",
        qualified_name="pkg.mod.Service.target",
    )
    sibling = ContextSymbol(
        uid="helper",
        name="helper",
        file_path="/f.py",
        role="r",
        distance_from_seed=1,
        expansion_step="binding",
        code="    def helper(self):\n        return 1\n",
        qualified_name="pkg.mod.Service.helper",
    )
    bundle = ContextBundle(role="r", seed=seed, related=(sibling,))

    out = _render_bundle(bundle, "fold_compact")

    assert out.render_mode == "fold_compact"
    assert out.seed.code == (
        "class Service:\n"
        "    def target(self):\n"
        "    def helper(self):"
    )


def test_fold_leaves_ambiguous_single_method_alone():
    bundle = ContextBundle(
        role="r",
        seed=_sym(
            "target",
            "def target():\n    return 1\n",
            qualified_name="pkg.mod.target",
        ),
        related=(),
    )

    [out] = _apply_render_and_budget([bundle], token_budget=None, render_mode="fold")

    assert out == bundle


def test_token_credit_downgrades_oversized_full_candidate_to_signature():
    code = "def large():\n" + ("    x = 1\n" * 200)
    bundle = ContextBundle(
        role="r",
        seed=_sym("large", code, qualified_name="pkg.mod.large"),
        related=(),
        utility_score=1.0,
    )

    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=100,
        render_mode="full",
    )

    assert out.render_mode == "signature_only"
    assert out.seed.code == "def large():"


def test_token_credit_upgrades_passive_when_surplus_remains():
    bundle = ContextBundle(
        role="r",
        seed=_sym("passive", "def passive():\n    return 1\n"),
        related=(),
        utility_score=0.5,
        passive=True,
    )

    [out] = _apply_render_and_budget(
        [bundle],
        token_budget=100,
        render_mode="full",
    )

    assert out.render_mode == "full"
    assert out.seed.code == "def passive():\n    return 1\n"


def test_token_credit_coverage_prefers_new_file_over_duplicate_file():
    same_file_top = ContextBundle(
        role="r",
        seed=_sym("same_top", "def same_top():\n    return 1\n", file_path="/a.py"),
        related=(),
        utility_score=0.90,
    )
    same_file_second = ContextBundle(
        role="r",
        seed=_sym(
            "same_second",
            "def same_second():\n    return 2\n",
            file_path="/a.py",
        ),
        related=(),
        utility_score=0.89,
    )
    new_file = ContextBundle(
        role="r",
        seed=_sym("new_file", "def new_file():\n    return 3\n", file_path="/b.py"),
        related=(),
        utility_score=0.75,
    )

    out = _apply_render_and_budget(
        [same_file_top, same_file_second, new_file],
        token_budget=8,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["same_top", "new_file"]


def test_token_credit_coverage_demotes_example_tier_against_core():
    example = ContextBundle(
        role="r",
        seed=_sym(
            "example",
            "def example():\n    return 1\n",
            file_path="/repo/docs_src/tutorial001.py",
        ),
        related=(),
        utility_score=0.90,
    )
    core = ContextBundle(
        role="r",
        seed=_sym("core", "def core():\n    return 2\n", file_path="/repo/pkg/core.py"),
        related=(),
        utility_score=0.70,
    )

    out = _apply_render_and_budget(
        [example, core],
        token_budget=4,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["core"]


def test_token_credit_impact_analysis_reserves_test_surface():
    example = ContextBundle(
        role="impact_analysis",
        seed=_sym(
            "example",
            "app = FastAPI()\n",
            file_path="/repo/docs_src/tutorial001.py",
        ),
        related=(),
        utility_score=0.55,
    )
    test = ContextBundle(
        role="impact_analysis",
        seed=_sym(
            "test_surface",
            "def test_surface():\n    assert encode(value)\n",
            file_path="/repo/tests/test_encoder.py",
        ),
        related=(
            ContextSymbol(
                uid="core_encoder",
                name="core_encoder",
                file_path="/repo/pkg/encoder.py",
                role="impact_analysis",
                distance_from_seed=1,
                expansion_step="deferred_runtime_dispatch",
                code="def encode(value):\n    return value\n",
            ),
        ),
        utility_score=0.35,
    )

    out = _apply_render_and_budget(
        [example, test],
        token_budget=7,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["test_surface"]


def test_token_credit_coverage_prefers_structural_bridge_related_file():
    local = ContextBundle(
        role="r",
        seed=_sym("local", "def local():\n    return 1\n", file_path="/repo/pkg/local.py"),
        related=(),
        utility_score=0.75,
    )
    bridge = ContextBundle(
        role="r",
        seed=_sym(
            "seed",
            "def seed():\n    return 2\n",
            file_path="/repo/pkg/topic.py",
        ),
        related=(
            ContextSymbol(
                uid="api",
                name="api",
                file_path="/repo/pkg/api.py",
                role="r",
                distance_from_seed=2,
                expansion_step="hook_transparency",
                code="def api():\n    return 3\n",
            ),
        ),
        utility_score=0.60,
    )

    out = _apply_render_and_budget(
        [local, bridge],
        token_budget=7,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["seed"]
    assert [sym.uid for sym in out[0].related] == ["api"]


def test_token_credit_coverage_prefers_runtime_dispatch_bridge_to_core_file():
    local = ContextBundle(
        role="r",
        seed=_sym("local", "def local():\n    return helper()\n", file_path="/repo/pkg/local.py"),
        related=(
            ContextSymbol(
                uid="helper",
                name="helper",
                file_path="/repo/pkg/local.py",
                role="r",
                distance_from_seed=1,
                expansion_step="binding_structure_expansion",
                code="def helper():\n    return 1\n",
            ),
        ),
        utility_score=0.64,
    )
    bridge = ContextBundle(
        role="r",
        seed=_sym(
            "seed",
            "def seed():\n    return dispatch(value)\n",
            file_path="/repo/pkg/base.py",
        ),
        related=(
            ContextSymbol(
                uid="runtime",
                name="runtime",
                file_path="/repo/pkg/runtime.py",
                role="r",
                distance_from_seed=1,
                expansion_step="deferred_runtime_dispatch",
                code="def runtime(value):\n    return value\n",
            ),
        ),
        utility_score=0.50,
    )

    out = _apply_render_and_budget(
        [local, bridge],
        token_budget=9,
        render_mode="full",
    )

    assert [b.seed.uid for b in out] == ["seed"]
    assert [sym.uid for sym in out[0].related] == ["runtime"]


def test_token_credit_starts_foldable_active_bundle_as_fold_coverage():
    fold_bundle = ContextBundle(
        role="r",
        seed=_sym(
            "b_target",
            "    def target(self):\n        return 1\n",
            qualified_name="pkg.mod.Service.target",
        ),
        related=(
            ContextSymbol(
                uid="b_helper",
                name="helper",
                file_path="/f.py",
                role="r",
                distance_from_seed=1,
                expansion_step="binding",
                code="    def helper(self):\n        return 2\n",
                qualified_name="pkg.mod.Service.helper",
            ),
        ),
        utility_score=0.50,
    )
    rendered, cost = _initial_credit_render(
        fold_bundle,
        transaction_limit=10_000,
        full_render_max_depth=0,
    )

    assert cost == _bundle_token_count(rendered)
    assert rendered.render_mode == "fold_compact"
    assert rendered.seed.code == (
        "class Service:\n"
        "    def target(self):\n"
        "    def helper(self):"
    )
