import textwrap

from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter
from context_engine.parser.adapters.typescript_axis_extractor import TypeScriptAxisExtractor


def _profile(source: str, qualified_name: str, file_path: str = "src/tasks.ts"):
    adapter = TypeScriptAdapter()
    extraction = TypeScriptAxisExtractor(adapter).extract(textwrap.dedent(source), file_path)
    return extraction.profiles_by_qualified_name[qualified_name]


def _fact_payloads(profile, bit: str):
    return [fact.payload for fact in profile.facts if fact.bit == bit]


def test_decorated_method_gets_cfg_dfg_struct_bits():
    profile = _profile(
        """
        @Controller("users")
        export class AppController {
          @Get(":id")
          find(id: string) {
            return { id };
          }
        }
        """,
        "src.tasks.AppController.find",
    )

    assert {"callable_body", "decorator_application", "return_exit"} <= profile.cfg_bits
    assert {
        "collection_assembly",
        "parameter_input",
        "return_output",
        "return_shape_kind",
    } <= profile.dfg_bits
    assert {
        "function_def",
        "method_member",
        "parameter_decl",
        "decorator_attachment",
        "decorator_shape",
        "literal_shape",
    } <= profile.struct_bits


def test_async_exception_and_loop_control_bits():
    profile = _profile(
        """
        export async function load(manager: Manager) {
          try {
            await manager.read();
          } catch (err) {
            throw err;
          }
          for (const item of items) {
            yield item;
          }
        }
        """,
        "src.tasks.load",
    )

    assert {
        "async_suspend_resume",
        "callable_body",
        "exception_transfer",
        "generator_yield",
        "loop_driver",
    } <= profile.cfg_bits
    assert {"exception_value", "iteration_source", "yield_output"} <= profile.dfg_bits


def test_subscript_and_container_method_facts():
    profile = _profile(
        """
        export function run(registry: Record<string, () => void>) {
          registry["task"] = handler;
          const picked = registry["task"];
          return picked();
        }

        function handler() {}
        """,
        "src.tasks.run",
    )

    assert {"return_exit", "value_call"} <= profile.cfg_bits
    assert {
        "assignment_binding",
        "container_read_key",
        "container_write_value",
        "keyed_read",
        "keyed_write",
        "subscript_read",
        "subscript_write",
    } <= profile.dfg_bits
    assert "literal_key" in profile.struct_bits


def test_branch_condition_and_import_dependency_bits():
    module_profile = _profile(
        """
        import { ConfigService } from "./config";

        export function choose(config: Record<string, boolean>, items: number[]) {
          if (config["enabled"] && items.length) {
            return items;
          }
          return [];
        }
        """,
        "src.tasks",
        file_path="src/tasks.ts",
    )
    fn_profile = _profile(
        """
        import { ConfigService } from "./config";

        export function choose(config: Record<string, boolean>, items: number[]) {
          if (config["enabled"] && items.length) {
            return items;
          }
          return [];
        }
        """,
        "src.tasks.choose",
    )

    assert "import_dependency" in module_profile.struct_bits
    assert {"branch_condition", "branch_selector", "return_exit"} <= fn_profile.cfg_bits
    assert {"branch_influence", "return_shape_kind"} <= fn_profile.dfg_bits


def test_using_and_augmented_assignment_bits():
    profile = _profile(
        """
        export async function load(factory: () => Disposable) {
          using syncRes = factory();
          await using asyncRes = factory();
          counters.total += 1;
        }
        """,
        "src.tasks.load",
    )

    assert {"context_enter_exit", "async_suspend_resume"} <= profile.cfg_bits
    assert {"context_resource", "augmented_mutation"} <= profile.dfg_bits

    resources = _fact_payloads(profile, "context_resource")
    assert {payload.get("target") for payload in resources} >= {"syncRes", "asyncRes"}
