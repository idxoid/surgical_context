import pytest

from context_engine.axis.query_plan import (
    AxisQueryRequest,
    AxisRequirement,
    compile_axis_query,
    render_lance_predicate,
)


def test_render_lance_predicate_uses_axis_array_filters_and_escapes_workspace():
    predicate = render_lance_predicate(
        "team/repo'one@main",
        required_bits=(
            AxisRequirement("struct", "decorator_attachment"),
            AxisRequirement("dfg", "callable_value"),
            AxisRequirement("cfg", "decorator_application"),
        ),
    )

    assert predicate == (
        "workspace_id = 'team/repo''one@main' "
        "AND array_has(cfg_bits, 'decorator_application') "
        "AND array_has(dfg_bits, 'callable_value') "
        "AND array_has(struct_bits, 'decorator_attachment')"
    )


def test_render_lance_predicate_can_filter_persisted_container_kinds():
    predicate = render_lance_predicate(
        "ws",
        required_bits=(AxisRequirement("dfg", "keyed_write"),),
        container_kinds=("metadata_carrier", "data_model"),
    )

    assert predicate == (
        "workspace_id = 'ws' "
        "AND array_has(dfg_bits, 'keyed_write') "
        "AND array_has(container_kinds, 'data_model') "
        "AND array_has(container_kinds, 'metadata_carrier')"
    )


def test_compile_immediate_control_flow_query_plan():
    plan = compile_axis_query(
        AxisQueryRequest(
            traversal_mode="immediate_control_flow",
            required_bits=(AxisRequirement("cfg", "call_site"),),
            optional_bits=(AxisRequirement("dfg", "return_output"),),
            limit=12,
        ),
        workspace_id="ws",
    )

    assert plan.traversal_mode == "immediate_control_flow"
    assert plan.lance_predicate == "workspace_id = 'ws' AND array_has(cfg_bits, 'call_site')"
    assert len(plan.expansion_steps) == 2
    assert [step.name for step in plan.expansion_steps if step.enabled] == [
        "control_call_expansion"
    ]
    assert plan.expansion_steps[0].direction == "out"
    assert plan.expansion_steps[0].max_depth == 2
    assert plan.expansion_steps[1].enabled is False
    assert plan.stop_conditions == ("token_budget", "call_depth_exhausted", "")
    assert plan.limit == 12


def test_compile_deferred_binding_query_plan_keeps_binding_then_dispatch_order():
    plan = compile_axis_query(
        AxisQueryRequest(
            traversal_mode="deferred_binding_flow",
            required_bits=(
                AxisRequirement("struct", "decorator_attachment"),
                AxisRequirement("dfg", "container_write_value"),
            ),
            container_kinds=("middleware_chain",),
        ),
        workspace_id="ws",
    )

    assert len(plan.expansion_steps) == 2
    assert [step.name for step in plan.expansion_steps if step.enabled] == [
        "binding_structure_expansion",
        "deferred_runtime_dispatch",
    ]
    assert plan.expansion_steps[0].direction == "both"
    assert "DECORATED_BY" in plan.expansion_steps[0].edge_types
    assert "CALLS_DYNAMIC" in plan.expansion_steps[1].edge_types
    assert plan.stop_conditions == (
        "registry_or_metadata_read_reached",
        "dispatch_target_reached",
        "token_budget",
    )


def test_query_plan_does_not_invent_axis_requirements_from_mode():
    plan = compile_axis_query(
        AxisQueryRequest(traversal_mode="deferred_binding_flow"),
        workspace_id="ws",
    )

    assert plan.required_bits == ()
    assert plan.container_kinds == ()
    assert plan.lance_predicate == "workspace_id = 'ws'"


def test_axis_requirement_rejects_unknown_axis_and_empty_bit():
    with pytest.raises(ValueError, match="Unknown axis"):
        AxisRequirement("registry", "write")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="cannot be empty"):
        AxisRequirement("cfg", "")
