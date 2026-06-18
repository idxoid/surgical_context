from context_engine.indexer.mro_api_bridge import (
    ClassRecord,
    MethodRecord,
    _classes_by_file,
    _method_owner_class,
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


def test_build_mro_api_edges_materializes_only_direct_methods():
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
        owner_class_uid="base",
        owner_class_name="BaseTask",
    )
    delay = MethodRecord(
        uid="m2",
        name="delay",
        qualified_name="celery.app.task.Task.delay",
        owner_class_uid="task",
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
    assert ("task", "m1") not in by_type


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
            MethodRecord(
                uid="m3",
                name="__init__",
                qualified_name="pkg.Task.__init__",
                owner_class_name="Task",
            ),
        ]
    )
    assert "Task" in methods
    assert [method.uid for method in methods["Task"]] == ["m2", "m3"]


def test_method_owner_class_uses_file_and_longest_qualified_prefix():
    outer = ClassRecord(
        uid="outer",
        name="Outer",
        qualified_name="pkg.mod.Outer",
        file_path="pkg/mod.py",
    )
    nested = ClassRecord(
        uid="nested",
        name="Inner",
        qualified_name="pkg.mod.Outer.Inner",
        file_path="pkg/mod.py",
    )
    same_name_elsewhere = ClassRecord(
        uid="other",
        name="Inner",
        qualified_name="pkg.other.Inner",
        file_path="pkg/other.py",
    )
    by_file = _classes_by_file([outer, nested, same_name_elsewhere])

    owner = _method_owner_class("pkg.mod.Outer.Inner.run", "pkg/mod.py", by_file)

    assert owner == nested
