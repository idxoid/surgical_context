"""Gate test for the library marker catalogue.

Pins the structural-evidence snapshot for catalogue entries — every
canonical entry classifies as either ``structurally_backed`` (at least
one method on the class or an ancestor carries a registry-shape contract
in an indexed library workspace) or ``absent`` (the library isn't yet
indexed, so the catalogue claim can't be checked) or ``unproven`` (the
class exists in an indexed workspace but the current L2 predicates don't
fingerprint its registry shape — debt to track).

The gate doesn't require the report to be regenerated every CI run — it
verifies a checked-in baseline so contributors notice when a catalogue
entry changes status. To refresh:

    python -m QA.library_marker_evidence \
        --workspace qa_repo/flask@axis-v4+axis_python_v1 \
        --workspace qa_repo/fastapi@axis-v4+axis_python_v1 \
        --workspace qa_repo/celery@axis-v4+axis_python_v1 \
        --out QA/baselines/library_marker_evidence

This test reads ``QA/baselines/library_marker_evidence/summary.json`` and
asserts the catalogue size + status mix matches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidecar.axis.library_marker_catalogue import LIBRARY_MARKER_CATALOGUE

_BASELINE_PATH = (
    Path(__file__).resolve().parents[2]
    / "QA"
    / "baselines"
    / "library_marker_evidence"
    / "summary.json"
)


@pytest.fixture(scope="module")
def baseline() -> dict:
    if not _BASELINE_PATH.exists():
        pytest.skip(
            f"baseline not yet generated: {_BASELINE_PATH}. Run "
            "`python -m QA.library_marker_evidence --out QA/baselines/library_marker_evidence ...`"
        )
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def test_catalogue_size_matches_baseline(baseline: dict) -> None:
    """The number of canonical catalogue entries must match the snapshot.

    Either the catalogue grew (good, but rerun the evidence report to
    pin the new entry's structural status) or shrunk (also good, but
    rerun to capture the new baseline). The gate fails on drift so the
    catalogue file and the evidence baseline stay consistent.
    """
    assert len(LIBRARY_MARKER_CATALOGUE) == baseline["total"], (
        f"catalogue has {len(LIBRARY_MARKER_CATALOGUE)} entries, "
        f"baseline expects {baseline['total']}. Regenerate the baseline "
        "with QA.library_marker_evidence after changing the catalogue."
    )


def test_at_least_one_entry_is_structurally_backed(baseline: dict) -> None:
    """The catalogue is a transition shim. ZERO structurally-backed entries
    means *no* catalogue claim could be proven from indexed library
    evidence — the shim is unmoored and growth must stop until the
    evidence pipeline is fixed.
    """
    backed = baseline["by_status"].get("structurally_backed", 0)
    assert backed > 0, (
        "no catalogue entry is structurally backed by any indexed library. "
        "Either index a relevant library workspace, or fix the evidence "
        "pipeline (QA.library_marker_evidence) before continuing to grow "
        "the catalogue."
    )


def test_unproven_entries_do_not_silently_grow(baseline: dict) -> None:
    """The ``unproven`` count tracks catalogue claims whose class is in an
    indexed workspace but whose registry shape the current L2 predicates
    can't yet detect. Growth here is hidden debt — flag it.
    """
    unproven = baseline["by_status"].get("unproven", 0)
    # After cross-file ``registry_class`` propagation through DEPENDS_ON +
    # parsed-base-name alias resolution landed, every catalogue entry whose
    # canonical class is in an indexed workspace is structurally backed.
    # If this floor cracks, either a catalogue addition outran the
    # ``registry_class`` evidence pipeline or a regression broke the
    # propagation pass — either way, investigate before raising the ceiling.
    assert unproven == 0, (
        f"{unproven} catalogue entries are unproven against indexed-library "
        "evidence (was 0 at baseline). Either:\n"
        "  - the catalogue grew but the registry_class propagation no longer\n"
        "    matches the inheritance shape of the new class, OR\n"
        "  - the propagation pass regressed.\n"
        "Inspect QA/baselines/library_marker_evidence/evidence.md for which\n"
        "entries lost their backing before adjusting this ceiling."
    )
