from __future__ import annotations

import csv

from QA.contextbench_subset import build_subset, select_rows


def _row(repo: str, number: int, *, language: str = "python") -> dict[str, str]:
    return {
        "bench": "Verified",
        "instance_id": f"opaque-{repo}-{number}",
        "original_inst_id": f"owner__{repo}-{number}",
        "language": language,
    }


def test_select_rows_balances_repositories_and_is_deterministic():
    rows = [_row("django", i) for i in range(10)] + [_row("flask", 1), _row("express", 1)]

    first = select_rows(
        rows,
        limit=5,
        seed="fixed",
        benches={"Verified"},
        languages={"python"},
        repos=None,
    )
    second = select_rows(
        rows,
        limit=5,
        seed="fixed",
        benches={"Verified"},
        languages={"python"},
        repos=None,
    )

    assert first == second
    assert {row["original_inst_id"].split("__", 1)[1].split("-", 1)[0] for row in first[:3]} == {
        "django",
        "express",
        "flask",
    }


def test_build_subset_filters_and_preserves_source_columns(tmp_path):
    source = tmp_path / "selected.csv"
    output = tmp_path / "smoke.csv"
    rows = [_row("django", 1), _row("flask", 1), _row("express", 1, language="javascript")]
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    count = build_subset(
        source,
        output,
        limit=5,
        seed="fixed",
        benches={"Verified"},
        languages={"python"},
        repos={"django", "flask"},
    )

    with output.open(newline="", encoding="utf-8") as stream:
        written = list(csv.DictReader(stream))
    assert count == 2
    assert {row["original_inst_id"] for row in written} == {
        "owner__django-1",
        "owner__flask-1",
    }
