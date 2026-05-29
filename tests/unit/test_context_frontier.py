from QA.context_frontier import (
    _citation_matches_file,
    _citation_matches_symbol,
    _evidence_roles_covered_set,
    _file_path_matches,
    _quote_matches_context,
    build_budget_variants,
    score_citation_gate,
    select_units_under_budget,
    units_from_result,
)


def _sample_result():
    return {
        "id": "click_q01",
        "question": "How is a function registered?",
        "expected_files": ["src/click/core.py"],
        "expected_symbols": ["command"],
        "required_roles_canonical": ["api_surface"],
        "ready_context": {
            "token_count": 999,
            "contract": {
                "primary_source": {
                    "symbol": "command",
                    "file_path": "/repo/src/click/core.py",
                    "code": "def command():\n    return decorator\n",
                },
                "graph_context": [
                    {
                        "symbol": "decorator",
                        "file_path": "/repo/src/click/core.py",
                        "relation": "CALLS_DYNAMIC",
                        "depth": 1,
                        "relevance_score": 0.9,
                        "code": "def decorator(f):\n    self.add_command(f)\n",
                    }
                ],
                "documentation": [],
            },
        },
    }


def test_units_from_result_rebuilds_primary_and_graph_units():
    units = units_from_result(_sample_result())

    assert [unit.kind for unit in units] == ["primary", "graph"]
    assert units[0].symbol == "command"
    assert units[1].relation == "CALLS_DYNAMIC"


def test_build_budget_variants_keeps_full_context_candidate():
    variants = build_budget_variants(_sample_result(), [50, 500])

    assert variants
    assert any(variant.variant_id == "full" for variant in variants)
    assert all(any(unit.kind == "primary" for unit in variant.units) for variant in variants)


def test_select_units_under_budget_prioritizes_mandatory_and_expected_symbols():
    units = units_from_result(_sample_result())
    mandatory = units[1]
    mandatory = type(mandatory)(
        **{
            **mandatory.__dict__,
            "relation": "MANDATORY_CALLEE",
            "symbol": "apply_async",
            "score": 9.0,
            "token_count": 200,
        }
    )
    noise = type(units[1])(
        **{
            **units[1].__dict__,
            "symbol": "noise_helper",
            "relation": "CALLS_DIRECT",
            "score": 0.1,
            "token_count": 150,
        }
    )
    selected = select_units_under_budget(
        [units[0], noise, mandatory],
        500,
        expected_symbols=["apply_async"],
    )
    assert mandatory in selected
    assert mandatory in selected[:3]


def test_score_citation_gate_requires_context_citations_and_roles():
    variant = build_budget_variants(_sample_result(), [500])[-1]
    payload = {
        "verdict": "pass",
        "correctness": "correct",
        "grounding": "grounded",
        "completeness": "complete",
        "context_sufficient": "yes",
        "citations": [
            {
                "claim": "command is the target",
                "file_path": "/repo/src/click/core.py",
                "symbol": "command",
                "quote": "def command():",
            }
        ],
        "evidence_roles_covered": ["api_surface"],
    }

    gate_pass, reasons = score_citation_gate(payload, variant, _sample_result())

    assert gate_pass is True
    assert reasons == []


def test_file_path_matches_relative_citation_to_absolute_context():
    absolute = "/home/user/QA/repos/click/src/click/core.py"
    assert _file_path_matches("src/click/core.py", {absolute}) is True
    assert _file_path_matches("click/core.py", {absolute}) is True
    assert _citation_matches_file(
        {"file_path": "src/click/decorators.py"},
        {"/home/user/QA/repos/click/src/click/decorators.py"},
    )


def test_file_path_matches_absolute_citation_to_absolute_context():
    context = "/repo/src/click/core.py"
    assert _file_path_matches("/repo/src/click/core.py", {context}) is True


def test_quote_matches_context_exact_and_whitespace_normalized():
    context = "def command():\n    return decorator\n"
    assert _quote_matches_context("def command():", context) is True
    assert _quote_matches_context("def  command():", context) is True


def test_quote_matches_context_fuzzy_on_minor_drift():
    context = "self.parent = parent  #: the parent context or `None` if none exists."
    quote = "self.parent = parent  # the parent context or None if none exists"
    assert _quote_matches_context(quote, context) is True


def test_quote_matches_context_rejects_unrelated_quote():
    assert _quote_matches_context("totally unrelated text here", "def foo(): pass") is False


def test_evidence_roles_covered_set_parses_role_colon_description():
    raw = [
        "core_runtime: Context class structure",
        "composition_surface: pass_context decorators",
        "orchestrator: invoke() sub-contexts",
    ]
    assert _evidence_roles_covered_set(raw) == {
        "core_runtime",
        "composition_surface",
        "orchestrator",
    }


def test_evidence_roles_covered_set_parses_role_parenthesis_description():
    raw = [
        "integration_surface (Consumer bootstep)",
        "orchestrator (update_strategies, task_message_handler)",
        "executor (Pool, on_task_request)",
        "runtime_surface (task_consumer connection)",
    ]
    assert _evidence_roles_covered_set(raw) == {
        "integration_surface",
        "orchestrator",
        "executor",
        "runtime_surface",
    }


def test_score_citation_gate_accepts_judge_role_colon_format():
    variant = build_budget_variants(_sample_result(), [500])[-1]
    result = {
        **_sample_result(),
        "required_roles_canonical": ["api_surface", "core_runtime", "composition_surface"],
    }
    payload = {
        "verdict": "pass",
        "correctness": "correct",
        "grounding": "grounded",
        "completeness": "complete",
        "context_sufficient": "yes",
        "citations": [
            {
                "file_path": "/repo/src/click/core.py",
                "symbol": "command",
                "quote": "def command():",
            }
        ],
        "evidence_roles_covered": [
            "api_surface: command decorator entry",
            "core_runtime: runtime body",
            "composition_surface: wiring",
        ],
    }

    gate_pass, reasons = score_citation_gate(payload, variant, result)

    assert gate_pass is True
    assert "evidence_roles_missing" not in " ".join(reasons)


def test_citation_matches_symbol_accepts_qualified_tail():
    context_symbols = {"execute", "Consumer", "on_task_request"}
    assert _citation_matches_symbol({"symbol": "Request.execute"}, context_symbols)
    assert _citation_matches_symbol({"symbol": "Consumer.create"}, context_symbols)


def test_score_citation_gate_accepts_qualified_expected_symbol_citation():
    variant = build_budget_variants(_sample_result(), [500])[-1]
    result = {
        **_sample_result(),
        "expected_symbols": ["command"],
    }
    payload = {
        "verdict": "pass",
        "correctness": "correct",
        "grounding": "grounded",
        "completeness": "complete",
        "context_sufficient": "yes",
        "citations": [
            {
                "file_path": "/repo/src/click/core.py",
                "symbol": "command.decorator",
                "quote": "def command():",
            }
        ],
        "evidence_roles_covered": ["api_surface"],
    }
    gate_pass, reasons = score_citation_gate(payload, variant, result)
    assert gate_pass is True
    assert "expected_symbols_not_cited" not in reasons


def test_score_citation_gate_fails_without_expected_symbol_citation():
    variant = build_budget_variants(_sample_result(), [500])[-1]
    payload = {
        "verdict": "pass",
        "correctness": "correct",
        "grounding": "grounded",
        "completeness": "complete",
        "context_sufficient": "yes",
        "citations": [{"file_path": "/repo/src/click/core.py", "quote": "def command():"}],
        "evidence_roles_covered": ["api_surface"],
    }

    gate_pass, reasons = score_citation_gate(payload, variant, _sample_result())

    assert gate_pass is False
    assert "expected_symbols_not_cited" in reasons
