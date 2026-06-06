import textwrap

from sidecar.axis import PythonAxisExtractor


def _profile(source: str, qualified_name: str, file_path: str = "pkg/tasks.py"):
    extraction = PythonAxisExtractor().extract(textwrap.dedent(source), file_path)
    return extraction.profiles_by_qualified_name[qualified_name]


def _fact_payloads(profile, bit: str):
    return [fact.payload for fact in profile.facts if fact.bit == bit]


def test_decorated_function_gets_cfg_dfg_struct_bits():
    profile = _profile(
        """
        @app.task(name="jobs.run")
        def run(x: int):
            return {"x": x}
        """,
        "pkg.tasks.run",
    )

    assert {"callable_body", "decorator_application", "return_exit"} <= profile.cfg_bits
    assert {
        "call_argument",
        "callable_value",
        "parameter_input",
        "collection_assembly",
        "return_output",
    } <= profile.dfg_bits
    assert {
        "function_def",
        "parameter_decl",
        "annotation",
        "decorator_attachment",
        "decorator_shape",
        "literal_shape",
    } <= profile.struct_bits

    decorator_shape = _fact_payloads(profile, "decorator_shape")[0]
    assert decorator_shape["callee"] == "app.task"
    assert decorator_shape["keywords"][0]["name"] == "name"
    assert decorator_shape["keywords"][0]["literal"] == "jobs.run"


def test_method_dispatch_and_state_mutation_bits_are_physical():
    init_profile = _profile(
        """
        class Worker:
            def __init__(self):
                self.client = Client()

            def run(self, user):
                result = self.client.fetch(user.id)
                self.cache["last"] = result
                return result
        """,
        "pkg.tasks.Worker.__init__",
    )
    run_profile = _profile(
        """
        class Worker:
            def __init__(self):
                self.client = Client()

            def run(self, user):
                result = self.client.fetch(user.id)
                self.cache["last"] = result
                return result
        """,
        "pkg.tasks.Worker.run",
    )

    assert {"callable_body", "constructor_call"} <= init_profile.cfg_bits
    assert {"attr_write", "call_result_origin", "constructor_value"} <= init_profile.dfg_bits
    assert "instance_attribute_hint" in init_profile.struct_bits

    assert {"call_site", "method_dispatch", "return_exit"} <= run_profile.cfg_bits
    assert {
        "assignment_binding",
        "attr_read",
        "call_result_origin",
        "subscript_write",
        "return_output",
    } <= run_profile.dfg_bits


def test_class_structure_bits_capture_type_shape_without_roles():
    class_profile = _profile(
        """
        class Child(Base, metaclass=Meta):
            config: dict[str, str] = {}

            def build(self, model: Model) -> Result:
                return Result(model)
        """,
        "pkg.tasks.Child",
    )
    method_profile = _profile(
        """
        class Child(Base, metaclass=Meta):
            config: dict[str, str] = {}

            def build(self, model: Model) -> Result:
                return Result(model)
        """,
        "pkg.tasks.Child.build",
    )

    assert {
        "class_def",
        "inheritance",
        "metaclass",
        "class_attribute",
        "annotation",
        "generic_shape",
        "literal_shape",
    } <= class_profile.struct_bits
    assert "collection_assembly" in class_profile.dfg_bits
    assert "subscript_read" not in class_profile.dfg_bits

    assert {"function_def", "method_member", "parameter_decl", "annotation"} <= (
        method_profile.struct_bits
    )
    assert {"callable_body", "call_site", "constructor_call", "return_exit"} <= (
        method_profile.cfg_bits
    )
    assert {"parameter_input", "constructor_value", "return_output"} <= method_profile.dfg_bits


def test_async_exception_and_context_control_bits():
    profile = _profile(
        """
        async def load(manager):
            async with manager() as resource:
                try:
                    await resource.read()
                except Error as exc:
                    raise exc
            for item in []:
                yield item
        """,
        "pkg.tasks.load",
    )

    assert {
        "async_suspend_resume",
        "callable_body",
        "context_enter_exit",
        "exception_transfer",
        "generator_yield",
        "loop_driver",
    } <= profile.cfg_bits
    assert {
        "context_resource",
        "exception_value",
        "yield_output",
        "collection_assembly",
    } <= profile.dfg_bits


def test_parameter_defaults_and_call_arguments_are_axis_facts():
    profile = _profile(
        """
        def get_db():
            return object()

        def endpoint(dep=Depends(get_db), token: str = Header(default="x")):
            return dep
        """,
        "pkg.tasks.endpoint",
    )

    assert {"parameter_decl", "parameter_default", "annotation"} <= profile.struct_bits
    assert {
        "call_argument",
        "callable_value",
        "parameter_default_value",
        "parameter_input",
    } <= profile.dfg_bits

    call_arguments = _fact_payloads(profile, "call_argument")
    assert any(
        payload.get("callee") == "Depends" and payload.get("name") == "get_db"
        for payload in call_arguments
    )
    assert any(
        payload.get("callee") == "Header"
        and payload.get("keyword") == "default"
        and payload.get("literal") == "x"
        for payload in call_arguments
    )

    callable_values = _fact_payloads(profile, "callable_value")
    assert any(
        payload.get("source") == "call_argument" and payload.get("name") == "get_db"
        for payload in callable_values
    )


def test_value_call_marks_dynamic_callable_expression_without_registry_semantics():
    profile = _profile(
        """
        def dispatch(callbacks, key):
            handler = callbacks[key]
            return handler()
        """,
        "pkg.tasks.dispatch",
    )

    assert {"call_site", "return_exit", "value_call"} <= profile.cfg_bits
    assert {"assignment_binding", "return_output", "subscript_read"} <= profile.dfg_bits

    value_calls = _fact_payloads(profile, "value_call")
    assert value_calls[0]["callee"] == "handler"
    assert value_calls[0]["callee_kind"] == "Name"


def test_subscript_key_read_write_facts_are_physical_container_facts():
    profile = _profile(
        """
        def run(registry):
            def handler():
                return None

            registry["task"] = handler
            picked = registry["task"]
            return picked()
        """,
        "pkg.tasks.run",
    )

    assert {"return_exit", "value_call"} <= profile.cfg_bits
    assert {
        "assignment_binding",
        "callable_value",
        "container_read_key",
        "container_write_value",
        "keyed_read",
        "keyed_write",
        "subscript_read",
        "subscript_write",
    } <= profile.dfg_bits
    assert "literal_key" in profile.struct_bits

    writes = _fact_payloads(profile, "keyed_write")
    assert any(
        payload.get("container") == "registry"
        and payload.get("key_literal") == "task"
        and payload.get("value") == "handler"
        for payload in writes
    )

    reads = _fact_payloads(profile, "container_read_key")
    assert any(
        payload.get("container") == "registry" and payload.get("key_literal") == "task"
        for payload in reads
    )


def test_collection_mutator_call_and_iteration_source_are_axis_facts():
    profile = _profile(
        """
        def on_event():
            pass

        def install(callbacks):
            callbacks.append(on_event)
            for cb in callbacks:
                cb()
        """,
        "pkg.tasks.install",
    )

    assert {"call_site", "loop_driver", "method_dispatch", "value_call"} <= profile.cfg_bits
    assert {
        "assignment_binding",
        "call_argument",
        "callable_value",
        "container_write_value",
        "iteration_source",
    } <= profile.dfg_bits

    writes = _fact_payloads(profile, "container_write_value")
    assert any(
        payload.get("container") == "callbacks"
        and payload.get("method") == "append"
        and payload.get("value") == "on_event"
        for payload in writes
    )

    iterations = _fact_payloads(profile, "iteration_source")
    assert iterations[0]["target"] == "cb"
    assert iterations[0]["iterable"] == "callbacks"


def test_dict_literal_keys_emit_keyed_write_without_role_semantics():
    profile = _profile(
        """
        def handler():
            pass

        def table():
            return {"task": handler}
        """,
        "pkg.tasks.table",
    )

    assert "return_exit" in profile.cfg_bits
    assert {"callable_value", "collection_assembly", "keyed_write", "return_output"} <= (
        profile.dfg_bits
    )
    assert {"literal_key", "literal_shape"} <= profile.struct_bits

    writes = _fact_payloads(profile, "keyed_write")
    assert any(
        payload.get("container") == "dict_literal"
        and payload.get("key_literal") == "task"
        and payload.get("value") == "handler"
        for payload in writes
    )


def test_branch_condition_and_generic_annotation_payloads_are_axis_facts():
    profile = _profile(
        """
        def choose(config: dict[str, bool], items: list[int]) -> list[int]:
            if config["enabled"] and items:
                return items
            return []
        """,
        "pkg.tasks.choose",
    )

    assert {"branch_condition", "branch_selector", "return_exit"} <= profile.cfg_bits
    assert {"branch_influence", "return_shape_kind"} <= profile.dfg_bits
    assert {"annotation", "generic_shape"} <= profile.struct_bits

    branch_conditions = _fact_payloads(profile, "branch_condition")
    condition = next(
        payload for payload in branch_conditions if payload["condition"] == "config['enabled'] and items"
    )
    assert condition["kind"] == "if"
    assert any(
        read.get("read_kind") == "subscript"
        and read.get("container") == "config"
        and read.get("key_literal") == "enabled"
        for read in condition["reads"]
    )
    assert any(read.get("read_kind") == "name" and read.get("name") == "items" for read in condition["reads"])

    generic_shapes = _fact_payloads(profile, "generic_shape")
    assert any(payload.get("generic") == "dict[str, bool]" for payload in generic_shapes)
    assert any(payload.get("generic") == "list[int]" for payload in generic_shapes)


def test_exception_raise_and_handler_type_payloads_are_axis_facts():
    profile = _profile(
        """
        def guard(value):
            try:
                if not value:
                    raise ValidationError("missing")
            except (ValidationError, TypeError) as exc:
                return {"error": exc}
        """,
        "pkg.tasks.guard",
    )

    assert {
        "branch_condition",
        "exception_handler_type",
        "exception_raise_value",
        "exception_transfer",
        "return_exit",
    } <= profile.cfg_bits
    assert {"branch_influence", "return_shape_kind"} <= profile.dfg_bits

    raises = _fact_payloads(profile, "exception_raise_value")
    assert any(
        payload.get("callee") == "ValidationError"
        and payload.get("destination") == "raise"
        for payload in raises
    )

    handlers = _fact_payloads(profile, "exception_handler_type")
    assert any(
        payload.get("caught_types") == ["ValidationError", "TypeError"]
        and payload.get("bound_name") == "exc"
        for payload in handlers
    )

    return_shapes = _fact_payloads(profile, "return_shape_kind")
    assert any(payload.get("shape_kind") == "mapping" for payload in return_shapes)


def test_constructed_output_and_class_keyword_payloads_are_physical():
    class_profile = _profile(
        """
        class Result(Base, frozen=True):
            pass

        def build(user) -> Result:
            result = Result(name=user.name)
            return Result(item=result)
        """,
        "pkg.tasks.Result",
    )
    build_profile = _profile(
        """
        class Result(Base, frozen=True):
            pass

        def build(user) -> Result:
            result = Result(name=user.name)
            return Result(item=result)
        """,
        "pkg.tasks.build",
    )

    assert "base_keyword" in class_profile.struct_bits
    assert {"call_result_origin", "constructed_output", "return_shape_kind"} <= (
        build_profile.dfg_bits
    )

    class_keywords = _fact_payloads(class_profile, "base_keyword")
    assert class_keywords[0]["keyword"] == "frozen"
    assert class_keywords[0]["value"] == "True"

    constructed = _fact_payloads(build_profile, "constructed_output")
    assert any(
        payload.get("destination") == "assignment"
        and payload.get("callee") == "Result"
        and payload.get("keywords", [])[0].get("keyword") == "name"
        for payload in constructed
    )
    assert any(
        payload.get("destination") == "return"
        and payload.get("callee") == "Result"
        and payload.get("keywords", [])[0].get("keyword") == "item"
        for payload in constructed
    )

    return_shapes = _fact_payloads(build_profile, "return_shape_kind")
    assert any(payload.get("shape_kind") == "constructed" for payload in return_shapes)
