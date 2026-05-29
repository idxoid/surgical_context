from sidecar.indexer.mro_api_bridge import (
    ClassRecord,
    MethodRecord,
    build_mro_api_edges,
    index_methods_by_owner,
    parse_class_method_symbol,
)


def test_parse_class_method_symbol():
    assert parse_class_method_symbol("Task.apply_async") == ("Task", "apply_async")
    assert parse_class_method_symbol("ParamType.convert") == ("ParamType", "convert")
    assert parse_class_method_symbol("Command.invoke") == ("Command", "invoke")
    assert parse_class_method_symbol("task") is None
    assert parse_class_method_symbol("celery/app/task.py") is None


def test_build_mro_api_edges_marks_inherited_methods():
    task = ClassRecord(
        uid="task", name="Task", qualified_name="celery.app.task.Task", file_path="a.py"
    )
    base = ClassRecord(
        uid="base", name="BaseTask", qualified_name="celery.app.base.BaseTask", file_path="b.py"
    )
    apply_async = MethodRecord(
        uid="m1",
        name="apply_async",
        qualified_name="celery.app.base.BaseTask.apply_async",
        owner_class_name="BaseTask",
    )
    delay = MethodRecord(
        uid="m2",
        name="delay",
        qualified_name="celery.app.task.Task.delay",
        owner_class_name="Task",
    )
    methods_by_owner = index_methods_by_owner([apply_async, delay])
    edges = build_mro_api_edges(
        [task, base],
        inheritance={"task": ["base"]},
        methods_by_owner_name=methods_by_owner,
        class_by_uid={"task": task, "base": base},
    )
    by_type = {(edge.class_uid, edge.method_uid): edge for edge in edges}
    assert by_type[("task", "m2")].edge_type == "HAS_API"
    inherited = by_type[("task", "m1")]
    assert inherited.edge_type == "INHERITED_API"
    assert inherited.originating_class == "celery.app.base.BaseTask"


def test_index_methods_by_owner_skips_private_methods():
    methods = index_methods_by_owner(
        [
            MethodRecord(
                uid="m1",
                name="_private",
                qualified_name="pkg.Task._private",
                owner_class_name="Task",
            ),
            MethodRecord(
                uid="m2",
                name="delay",
                qualified_name="pkg.Task.delay",
                owner_class_name="Task",
            ),
        ]
    )
    assert "Task" in methods
    assert [method.uid for method in methods["Task"]] == ["m2"]
