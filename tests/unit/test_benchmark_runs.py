import json

from QA.benchmark_runs import audit_pruned, filter_runs, load_runs, select_last


def test_load_filter_and_select_benchmark_runs(tmp_path):
    manifest = tmp_path / "benchmark_runs.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps({"repo": "fastapi", "core12_only": True, "pass_rate": 1.0}),
                json.dumps({"repo": "pydantic", "core12_only": True, "pass_rate": 0.875}),
                json.dumps({"repo": "fastapi", "core12_only": False, "pass_rate": 1.0}),
                "not json",
                "",
            ]
        )
    )

    entries = load_runs(manifest)
    filtered = filter_runs(entries, repo="fastapi", core12=True)

    assert len(entries) == 3
    assert len(filtered) == 1
    assert filtered[0]["pass_rate"] == 1.0
    assert select_last(entries, 2) == entries[-2:]
    assert select_last(entries, 0) == entries


def test_audit_pruned_counts_reasons_and_expected_symbol_hits(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "expected_symbols": ["solve_dependencies", "Depends"],
                        "ready_context": {
                            "contract": {
                                "pruned": [
                                    {
                                        "name": "solve_dependencies",
                                        "reason": "over_budget",
                                    },
                                    {
                                        "name": "tutorial",
                                        "reason": "low_marginal_gain",
                                    },
                                ]
                            }
                        },
                    }
                ]
            }
        )
    )
    entries = [{"report_path": str(report)}]

    summary = audit_pruned(entries)

    assert summary["reports_read"] == 1
    assert summary["questions_seen"] == 1
    assert summary["pruned_items_seen"] == 2
    assert ("over_budget", 1) in summary["top_reasons"]
    assert ("solve_dependencies", 1) in summary["expected_symbols_in_pruned"]
