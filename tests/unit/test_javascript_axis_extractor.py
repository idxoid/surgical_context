import textwrap

from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter
from context_engine.parser.adapters.javascript_axis_extractor import JavaScriptAxisExtractor


def test_javascript_axis_extractor_emits_loop_and_assignment_bits():
    adapter = JavaScriptAdapter()
    source = textwrap.dedent(
        """
        function install(callbacks) {
          callbacks.push(onEvent);
          for (const cb of callbacks) {
            cb();
          }
        }

        function onEvent() {}
        """
    )
    profile = JavaScriptAxisExtractor(adapter).extract(source, "lib/install.js").profiles_by_qualified_name[
        "lib.install.install"
    ]

    assert {"call_site", "loop_driver", "method_dispatch", "value_call"} <= profile.cfg_bits
    assert {
        "call_argument",
        "container_write_value",
        "iteration_source",
    } <= profile.dfg_bits


def test_javascript_adapter_extract_axis_facts_wires_extractor():
    adapter = JavaScriptAdapter()
    source = "function run() { return 1; }"
    facts = adapter.extract_axis_facts(source, "lib/run.js")
    bits = {fact.bit for fact in facts}
    assert "callable_body" in bits
    assert "return_exit" in bits
