"""P7 empirical gate: axis_benchmark file_recall on surgical_context.

Indexes this repository under a dedicated CI workspace, replays the seven
``surgical_context_*`` questions from ``questions_python.yaml`` with the
production /ask budget path, and asserts the
checked-in baseline in ``QA/fixtures/baselines/p7_surgical_context_axis.json``.

To refresh the baseline after an intentional engine improvement::

    INDEX_PROFILE=axis_python_v1 \\
    python -m context_engine.indexer.fast . \\
        --workspace ci/surgical_context@main \\
        --index-profile axis_python_v1 --fresh
    python -m QA.axis_benchmark \\
        --pack QA/fixtures/questions_python.yaml \\
        --repo surgical_context \\
        --out /tmp/p7_axis_refresh
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from context_engine.index_profile import AXIS_PYTHON_V1_PROFILE, resolve_index_profile
from QA.axis_benchmark import (
    _load_pack,
    assert_p7_baseline,
    run_axis_pack,
    summarise,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "QA/fixtures/questions_python.yaml"
BASELINE_PATH = REPO_ROOT / "QA/fixtures/baselines/p7_surgical_context_axis.json"
CI_BASE_WORKSPACE = "ci/surgical_context@main"


@pytest.fixture(scope="module")
def surgical_context_workspace() -> str:
    """Fresh axis_python_v1 index of this repo for the P7 gate."""
    from context_engine.database.neo4j_client import Neo4jClient
    from context_engine.indexer.fast.pipeline import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

    try:
        probe = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        with probe.driver.session() as session:
            session.run("RETURN 1").single()
        probe.close()
    except Exception as exc:
        pytest.skip(f"Neo4j unavailable for P7 axis gate: {exc}")

    os.environ["INDEX_PROFILE"] = AXIS_PYTHON_V1_PROFILE
    profile = resolve_index_profile(AXIS_PYTHON_V1_PROFILE)
    workspace_id = profile.workspace_id(CI_BASE_WORKSPACE)

    from context_engine.indexer.fast import run_fast_indexing
    from context_engine.indexer.fast.__main__ import _wipe_workspace

    _wipe_workspace(CI_BASE_WORKSPACE, AXIS_PYTHON_V1_PROFILE)
    run_fast_indexing(
        str(REPO_ROOT),
        workspace_id=CI_BASE_WORKSPACE,
        index_profile=AXIS_PYTHON_V1_PROFILE,
    )
    return workspace_id


@pytest.fixture(scope="module")
def p7_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


@pytest.mark.integration
def test_p7_surgical_context_axis_file_recall(
    surgical_context_workspace: str,
    p7_baseline: dict,
) -> None:
    questions = [q for q in _load_pack(PACK) if q.get("repo") == p7_baseline["repo_filter"]]
    assert len(questions) == p7_baseline["scored"]

    caps = p7_baseline["caps"]
    context_seed_cap = caps.get("context_seeds_per_role")
    results = run_axis_pack(
        questions,
        per_role_limit=int(caps["per_role_limit"]),
        max_impacted=int(caps["max_impacted"]),
        context_seeds_per_role=(None if context_seed_cap is None else int(context_seed_cap)),
        intent_budget=bool(caps["intent_budget"]),
        workspace_overrides={"surgical_context": surgical_context_workspace},
    )
    summary = summarise(results)
    assert_p7_baseline(summary, p7_baseline)
