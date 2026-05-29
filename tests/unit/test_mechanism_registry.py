import json

from sidecar.context.mechanism_registry import (
    ROLE_CATALOG_MECHANISM_BACKFILL_KEY,
    ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY,
    determine_preloaded_mechanism,
    known_mechanisms,
    merge_preloaded_mechanisms_into_role_catalog,
    pick_mechanism_by_role_overlap,
    preloaded_mechanism_catalog_extensions,
    required_roles_for_mechanism,
    role_backfill_specs_for_mechanism,
)
from sidecar.context.types import SubgraphNode


def _target(name: str, file_path: str = "/repo/src/main.py") -> SubgraphNode:
    return SubgraphNode(
        uid=f"u:{name}",
        name=name,
        file_path=file_path,
        range=[1, 10],
        token_estimate=80,
        relation="target",
        direction="primary",
        depth=0,
        relevance_score=1.0,
        kind="function",
    )


def test_preloaded_dispatch_stub_returns_empty_for_framework_like_symbols():
    assert (
        determine_preloaded_mechanism(
            _target("Application", "/repo/app/application.py"),
            "How does Application register handlers?",
        )
        == ""
    )
    assert (
        determine_preloaded_mechanism(
            _target("RecordModel", "/repo/modeling/main.py"),
            "How does RecordModel validation flow work?",
        )
        == ""
    )
    assert (
        determine_preloaded_mechanism(
            _target("createClient", "/repo/packages/client/src/query/createClient.ts"),
            "How does createClient define a query surface and connect generated endpoints?",
        )
        == ""
    )


def test_unknown_mechanism_has_no_builtin_roles():
    assert required_roles_for_mechanism("generated_api_schema") == []


def test_builtin_ranker_fusion_mechanism_no_longer_hardcoded():
    # surgical_context_ranker_fusion is no longer a builtin — all preloaded dispatch removed.
    assert "surgical_context_ranker_fusion" not in known_mechanisms()
    assert required_roles_for_mechanism("surgical_context_ranker_fusion") == []
    assert role_backfill_specs_for_mechanism("surgical_context_ranker_fusion") == {}


def test_preloaded_mechanism_always_empty():
    # determine_preloaded_mechanism is intentionally inert — no hardcoded dispatch for any repo.
    assert (
        determine_preloaded_mechanism(_target("v1", "/repo/pydantic/v1/__init__.py"), "compat")
        == ""
    )
    assert (
        determine_preloaded_mechanism(
            _target("Controller", "/repo/nestjs/controller.decorator.ts"), "route"
        )
        == ""
    )
    assert (
        determine_preloaded_mechanism(
            _target("UnifiedRanker", "/repo/sidecar/context/unified_ranker.py"), "rank"
        )
        == ""
    )


def test_known_mechanisms_includes_catalog_overlay_keys():
    catalog = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {"custom_mech": ["api_surface"]},
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {"other_mech": {}},
    }
    assert set(known_mechanisms(role_catalog=catalog)) == {
        "custom_mech",
        "other_mech",
    }


def test_preloaded_registry_returns_empty_for_unknown_codebase():
    assert determine_preloaded_mechanism(_target("Router"), "How does middleware execute?") == ""
    assert required_roles_for_mechanism("unknown") == []


def test_role_catalog_overrides_required_roles():
    catalog = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {
            "generated_api_schema": ["executor", "runtime_surface"],
        }
    }
    roles = required_roles_for_mechanism(
        "generated_api_schema",
        role_catalog=catalog,
    )
    assert roles == ["executor", "runtime_surface"]
    assert (
        required_roles_for_mechanism(
            "generated_api_schema",
            role_catalog={ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {}},
        )
        == []
    )


def test_role_catalog_overrides_backfill_specs():
    catalog = {
        ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {
            "handler_execution": {
                "executor": [
                    {"name": "custom_runner", "path_hint": "/repo/run.py", "priority": 1.0},
                ],
            },
        }
    }
    specs = role_backfill_specs_for_mechanism(
        "handler_execution",
        role_catalog=catalog,
    )
    assert specs["executor"][0]["name"] == "custom_runner"
    assert specs["executor"][0]["path_hint"] == "/repo/run.py"
    builtin = role_backfill_specs_for_mechanism(
        "handler_execution",
        role_catalog={ROLE_CATALOG_MECHANISM_BACKFILL_KEY: {}},
    )
    assert builtin == {}


def test_builtin_backfill_specs_empty_without_optional_pack():
    assert role_backfill_specs_for_mechanism("auto:registration_flow") == {}
    assert role_backfill_specs_for_mechanism("unknown_mech") == {}


# Example (inactive): mirrors sidecar/context/mechanism_packs/bundled/celery_publish_consume.yaml.
# Uncomment the YAML pack and this test together when tuning celery publish/consume.
#
# def test_celery_publish_consume_pack_matches_question_mechanisms(monkeypatch):
#     from pathlib import Path
#
#     pack = (
#         Path(__file__).resolve().parents[2]
#         / "sidecar/context/mechanism_packs/bundled/celery_publish_consume.yaml"
#     )
#     monkeypatch.setenv("MECHANISM_PACK_PATH", str(pack))
#     ext = preloaded_mechanism_catalog_extensions()
#     publish = role_backfill_specs_for_mechanism(
#         "celery_task_publish",
#         role_catalog=ext,
#     )
#     assert "orchestrator" in publish
#     assert any(row["name"] == "apply_async" for row in publish["orchestrator"])
#     assert any(row["name"] == "Producer" for row in publish["integration_surface"])
#     assert any(row["name"] == "send_task_message" for row in publish["orchestrator"])
#     consume = role_backfill_specs_for_mechanism(
#         "celery_worker_consume",
#         role_catalog=ext,
#     )
#     assert any(row["name"] == "Request" for row in consume["runtime_surface"])
#     assert any(row["name"] == "Strategy" for row in consume["executor"])


# Example (inactive): mirrors sidecar/context/mechanism_packs/bundled/flask_registration.yaml.
# Uncomment the YAML pack and this test together when tuning Flask registration_flow.
#
# def test_flask_registration_pack_provides_auto_registration_flow(monkeypatch):
#     from pathlib import Path
#
#     pack = (
#         Path(__file__).resolve().parents[2]
#         / "sidecar/context/mechanism_packs/bundled/flask_registration.yaml"
#     )
#     monkeypatch.setenv("MECHANISM_PACK_PATH", str(pack))
#     ext = preloaded_mechanism_catalog_extensions()
#     specs = role_backfill_specs_for_mechanism(
#         "auto:registration_flow",
#         role_catalog=ext,
#     )
#     assert "factory_surface" in specs
#     assert any(row["name"] == "register_blueprint" for row in specs["factory_surface"])
#     assert "runtime_surface" in specs
#     assert any(row["name"] == "wsgi_app" for row in specs["runtime_surface"])


def test_pick_mechanism_by_role_overlap_requires_two_distinct_roles():
    assert pick_mechanism_by_role_overlap(["executor"]) == ""
    assert pick_mechanism_by_role_overlap(["executor", "executor"]) == ""


def test_pick_mechanism_by_role_overlap_matches_catalog_template():
    catalog = {
        ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY: {
            "handler_execution": ["executor", "runtime_surface"],
        }
    }
    mech = pick_mechanism_by_role_overlap(
        {"executor", "runtime_surface", "api_surface"},
        target_role="executor",
        role_catalog=catalog,
        min_score=0.41,
    )
    assert mech == "handler_execution"


def test_preloaded_mechanism_catalog_extensions_are_json_serializable():
    ext = preloaded_mechanism_catalog_extensions()
    raw = json.dumps(ext, sort_keys=True)
    loaded = json.loads(raw)
    merged = merge_preloaded_mechanisms_into_role_catalog({"schema_version": 2})
    assert merged["schema_version"] == 2
    assert (
        merged[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY]
        == loaded[ROLE_CATALOG_MECHANISM_REQUIRED_ROLES_KEY]
    )
