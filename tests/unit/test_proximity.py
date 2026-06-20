"""Phase 1f B_proximity: path-locality boost from the ask anchor."""

from __future__ import annotations

from context_engine.axis.proximity import proximity_boost


def test_no_anchor_is_zero():
    assert proximity_boost("/repo/src/orders/views.py", None) == 0.0
    assert proximity_boost("/repo/src/orders/views.py", "") == 0.0
    assert proximity_boost("", "/repo/src/orders/views.py") == 0.0


def test_same_directory_gets_full_boost():
    b = proximity_boost("/repo/src/orders/models.py", "/repo/src/orders/views.py")
    assert b == 0.15


def test_sibling_directory_gets_partial_boost():
    # sub-folder of the anchor's dir
    assert proximity_boost("/repo/src/orders/services/tax.py", "/repo/src/orders/views.py") == 0.05
    # one folder over, same parent
    assert proximity_boost("/repo/src/billing/views.py", "/repo/src/orders/views.py") == 0.05


def test_far_directory_is_zero():
    assert proximity_boost("/repo/lib/utils/io.py", "/repo/src/orders/views.py") == 0.0


def test_suffix_aware_absolute_vs_relative():
    # anchor is a short repo-relative path; seed is absolute — same folder.
    assert (
        proximity_boost(
            "/abs/QA/repos/celery/celery/worker/strategy.py",
            "celery/worker/state.py",
        )
        == 0.15
    )
    # sibling under the same parent, mixed path forms.
    assert (
        proximity_boost(
            "/abs/QA/repos/celery/celery/concurrency/base.py",
            "celery/worker/state.py",
        )
        == 0.05
    )
