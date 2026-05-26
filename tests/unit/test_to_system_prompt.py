"""Tests for PromptContext.to_system_prompt() rendering."""

from sidecar.context.types import DocChunk, PromptContext, SymbolContext


def _make_ctx(
    *,
    primary_code: str = "def target(): pass",
    deps: list[dict] | None = None,
    docs: list[dict] | None = None,
    missing_roles: list[str] | None = None,
    stopped_reason: str = "",
    budget: dict | None = None,
) -> PromptContext:
    primary = SymbolContext(
        symbol="target",
        file_path="/repo/target.py",
        relation="target",
        code=primary_code,
    )
    graph_context = []
    for d in deps or []:
        graph_context.append(
            SymbolContext(
                symbol=d["symbol"],
                file_path=d.get("file_path", "/repo/dep.py"),
                relation=d.get("relation", "caller"),
                direction=d.get("direction", "caller"),
                depth=d.get("depth", 1),
                blended_score=d.get("blended_score", 0.0),
                code=d.get("code", f"def {d['symbol']}(): pass"),
            )
        )
    documentation = []
    for doc in docs or []:
        documentation.append(
            DocChunk(
                source_file=doc["source_file"],
                chunk_id=f"{doc['source_file']}::0",
                content=doc["content"],
            )
        )
    return PromptContext(
        primary_source=primary,
        graph_context=graph_context,
        documentation=documentation,
        missing_roles=missing_roles or [],
        stopped_reason=stopped_reason,
        budget=budget or {},
    )


# ---------------------------------------------------------------------------
# #1 — dependency annotations (relation, depth, score)
# ---------------------------------------------------------------------------


def test_dep_annotation_includes_relation_depth_score():
    ctx = _make_ctx(
        deps=[
            {
                "symbol": "caller_fn",
                "relation": "caller",
                "direction": "caller",
                "depth": 1,
                "blended_score": 0.75,
            }
        ]
    )
    prompt = ctx.to_system_prompt()
    assert "# caller_fn [caller, depth=1, score=0.75]:" in prompt


def test_dep_annotation_omits_zero_depth_and_zero_score():
    ctx = _make_ctx(
        deps=[
            {
                "symbol": "sibling",
                "relation": "callee",
                "direction": "callee",
                "depth": 0,
                "blended_score": 0.0,
            }
        ]
    )
    prompt = ctx.to_system_prompt()
    # depth=0 and score=0.0 should be omitted
    assert "depth=0" not in prompt
    assert "score=0.00" not in prompt
    assert "# sibling [callee]:" in prompt


# ---------------------------------------------------------------------------
# #2 — incompleteness disclaimer
# ---------------------------------------------------------------------------


def test_missing_roles_disclaimer_appears_before_target():
    ctx = _make_ctx(missing_roles=["runtime_surface", "tests"])
    prompt = ctx.to_system_prompt()
    disclaimer_pos = prompt.find("# Context note: partial")
    target_pos = prompt.find("--- TARGET SYMBOL:")
    assert disclaimer_pos != -1, "missing_roles disclaimer not found"
    assert disclaimer_pos < target_pos, "disclaimer must precede target block"
    assert "runtime_surface" in prompt
    assert "tests" in prompt


def test_budget_limit_disclaimer_on_budget_exhausted():
    ctx = _make_ctx(
        stopped_reason="budget_exhausted",
        budget={"spent": 3800, "limit": 4000},
    )
    prompt = ctx.to_system_prompt()
    assert "budget limit reached" in prompt
    assert "3800/4000" in prompt


def test_no_disclaimer_when_context_is_complete():
    ctx = _make_ctx(missing_roles=[], stopped_reason="role_complete")
    prompt = ctx.to_system_prompt()
    assert "Context note" not in prompt


# ---------------------------------------------------------------------------
# #3 — callers sort before callees
# ---------------------------------------------------------------------------


def test_callers_appear_before_callees_in_prompt():
    ctx = _make_ctx(
        deps=[
            {
                "symbol": "deep_callee",
                "relation": "callee",
                "direction": "callee",
                "depth": 2,
                "blended_score": 0.9,
            },
            {
                "symbol": "direct_caller",
                "relation": "caller",
                "direction": "caller",
                "depth": 1,
                "blended_score": 0.5,
            },
        ]
    )
    prompt = ctx.to_system_prompt()
    caller_pos = prompt.index("direct_caller")
    callee_pos = prompt.index("deep_callee")
    assert caller_pos < callee_pos, "callers must appear before callees"


def test_ordered_graph_context_matches_prompt_filtering():
    ctx = _make_ctx(
        deps=[
            {
                "symbol": "deep_callee",
                "relation": "callee",
                "direction": "callee",
                "depth": 2,
                "blended_score": 0.9,
            },
            {
                "symbol": "direct_caller",
                "relation": "caller",
                "direction": "caller",
                "depth": 1,
                "blended_score": 0.5,
            },
            {"symbol": "no_code", "code": "", "direction": "caller"},
        ]
    )

    assert [dep.symbol for dep in ctx.ordered_graph_context()] == [
        "direct_caller",
        "deep_callee",
    ]
    assert [dep.symbol for dep in ctx.ordered_graph_context(include_empty_code=True)] == [
        "direct_caller",
        "no_code",
        "deep_callee",
    ]


def test_among_callers_shallower_depth_comes_first():
    ctx = _make_ctx(
        deps=[
            {
                "symbol": "caller_depth2",
                "relation": "caller",
                "direction": "caller",
                "depth": 2,
                "blended_score": 0.8,
            },
            {
                "symbol": "caller_depth1",
                "relation": "caller",
                "direction": "caller",
                "depth": 1,
                "blended_score": 0.3,
            },
        ]
    )
    prompt = ctx.to_system_prompt()
    assert prompt.index("caller_depth1") < prompt.index("caller_depth2")


# ---------------------------------------------------------------------------
# #9 — empty code entries are filtered out
# ---------------------------------------------------------------------------


def test_empty_code_dep_not_rendered():
    ctx = _make_ctx(
        deps=[
            {"symbol": "has_code", "code": "def has_code(): pass", "direction": "caller"},
            {"symbol": "no_code", "code": "", "direction": "caller"},
            {"symbol": "whitespace_only", "code": "   \n  ", "direction": "caller"},
        ]
    )
    prompt = ctx.to_system_prompt()
    assert "has_code" in prompt
    assert "no_code" not in prompt
    assert "whitespace_only" not in prompt


def test_deps_section_absent_when_all_codes_empty():
    ctx = _make_ctx(deps=[{"symbol": "ghost", "code": ""}])
    prompt = ctx.to_system_prompt()
    assert "--- DEPENDENCIES ---" not in prompt


# ---------------------------------------------------------------------------
# Baseline — structure is preserved
# ---------------------------------------------------------------------------


def test_target_block_always_present():
    ctx = _make_ctx()
    prompt = ctx.to_system_prompt()
    assert "--- TARGET SYMBOL: target ---" in prompt
    assert "def target(): pass" in prompt


def test_docs_section_present_when_docs_given():
    ctx = _make_ctx(docs=[{"source_file": "docs/api.md", "content": "API reference"}])
    prompt = ctx.to_system_prompt()
    assert "--- DOCUMENTATION ---" in prompt
    assert "docs/api.md" in prompt
    assert "API reference" in prompt


def test_docs_appear_after_dependencies():
    ctx = _make_ctx(
        deps=[{"symbol": "dep", "code": "def dep(): pass", "direction": "caller"}],
        docs=[{"source_file": "docs/api.md", "content": "reference"}],
    )
    prompt = ctx.to_system_prompt()
    assert prompt.index("--- DEPENDENCIES ---") < prompt.index("--- DOCUMENTATION ---")
