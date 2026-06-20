"""Unit tests for the P7 axis benchmark gate helpers."""

from __future__ import annotations

import pytest

from QA.axis_benchmark import assert_p7_baseline


def test_assert_p7_baseline_passes_on_meeting_floors() -> None:
    baseline = {
        "scored": 2,
        "min_overall_mean_recall": 0.5,
        "min_overall_seed_mean_recall": 0.25,
        "min_overall_pool_mean_recall": 0.4,
        "max_zero_recall_count": 1,
        "min_full_recall_count": 1,
        "per_question_min_file_recall": {"q1": 0.5},
    }
    summary = {
        "scored": 2,
        "overall_mean_recall": 0.75,
        "overall_seed_mean_recall": 0.5,
        "overall_pool_mean_recall": 0.6,
        "zero_recall_questions": 0,
        "full_recall_questions": 1,
        "per_question": [
            {"question_id": "q1", "file_recall": 1.0},
            {"question_id": "q2", "file_recall": 0.5},
        ],
    }
    assert_p7_baseline(summary, baseline)


def test_assert_p7_baseline_fails_on_regression() -> None:
    baseline = {
        "scored": 1,
        "min_overall_mean_recall": 0.8,
        "min_overall_seed_mean_recall": 0.0,
        "min_overall_pool_mean_recall": 0.0,
        "max_zero_recall_count": 0,
        "min_full_recall_count": 0,
    }
    summary = {
        "scored": 1,
        "overall_mean_recall": 0.2,
        "overall_seed_mean_recall": 0.2,
        "overall_pool_mean_recall": 0.2,
        "zero_recall_questions": 1,
        "full_recall_questions": 0,
        "per_question": [],
    }
    with pytest.raises(AssertionError, match="bundle recall"):
        assert_p7_baseline(summary, baseline)
