"""Unit tests for RepoBench → question-pack conversion."""

from __future__ import annotations

from QA.build_repobench_python_pack import (
    make_question_text,
    row_to_question,
    select_rows,
)


def test_make_question_text_includes_file_and_next_line():
    text = make_question_text("src/click/termui.py", "def clear():\n    pass\n", "    if WIN:")
    assert "`src/click/termui.py`" in text
    assert "`    if WIN:`" in text
    assert "```python" in text


def test_row_to_question_maps_click():
    target = {
        "repo_full": "pallets/click",
        "id": "click",
        "name": "Click",
        "overlap": "exact",
        "proxies_for": "click",
        "rationale": "test",
    }
    row = {
        "repo_name": "pallets/click",
        "file_path": "src/click/termui.py",
        "context": [
            "def isatty(self) -> bool:\n    return True\n",
            "WIN = sys.platform.startswith('win')\n",
        ],
        "import_statement": "import sys",
        "code": "def clear():\n    if not isatty(sys.stdout):\n        return\n\n",
        "next_line": "    if WIN:",
        "golden_snippet_index": 1,
        "_setting": "cff",
        "_split": "train",
        "_difficulty": "hard",
        "_row_idx": 42,
    }
    q = row_to_question(row, target)
    assert q is not None
    assert q["repo"] == "click"
    assert q["overlap"] == "exact"
    assert q["expected_files"] == ["src/click/termui.py"]
    assert q["repobench"]["gold_snippet_index"] == 1
    assert q["repobench"]["n_candidates"] == 2
    assert q["intent"] == "complete_line"
    assert "WIN" in q["repobench"]["gold_snippet_preview"]


def test_select_rows_prefers_test_and_diversifies_files():
    rows = []
    for i in range(5):
        rows.append(
            {
                "file_path": "a.py",
                "next_line": f"line_{i}",
                "golden_snippet_index": 0,
                "context": ["x"],
                "_setting": "cff",
                "_split": "train",
                "_difficulty": "easy",
                "_row_idx": i,
            }
        )
    rows.append(
        {
            "file_path": "b.py",
            "next_line": "from_test",
            "golden_snippet_index": 0,
            "context": ["x", "y"],
            "_setting": "cff",
            "_split": "test",
            "_difficulty": "hard",
            "_row_idx": 99,
        }
    )
    selected = select_rows(rows, per_repo=3, max_per_file=2)
    assert selected[0]["file_path"] == "b.py"
    assert len(selected) == 3
    assert sum(1 for r in selected if r["file_path"] == "a.py") <= 2
