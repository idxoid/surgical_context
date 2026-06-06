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
        "literal_shape",
    } <= class_profile.struct_bits
    assert "collection_assembly" in class_profile.dfg_bits

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
