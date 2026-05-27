from QA.context_frontier import (
    build_budget_variants,
    score_citation_gate,
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
