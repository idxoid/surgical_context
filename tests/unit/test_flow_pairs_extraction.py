"""FLOWS_INTO extraction: co-invocation dataflow pairs.

``x = A(...); B(x)`` and ``B(A(...))`` are static AST facts — the caller binds
A's result and hands it to B. Pairs are intra-caller, follow rebinding, see
uses inside nested argument expressions (keywords, comprehensions), and stop
at nested-call boundaries (the nested call is itself the source).
"""

from __future__ import annotations

import textwrap

from context_engine.parser.adapters.python_adapter import PythonAdapter
from context_engine.parser.uid import project_root_scope


def _pairs(source: str, file_path: str = "/proj/pkg/mod.py") -> list[dict]:
    adapter = PythonAdapter()
    with project_root_scope("/proj", "test/flow_pairs@ws"):
        return adapter.extract_flow_pairs(textwrap.dedent(source), file_path)


def _names(pairs: list[dict]) -> set[tuple[str, str]]:
    return {(p["source_name"], p["target_name"]) for p in pairs}


def test_bound_result_flows_into_call():
    pairs = _pairs(
        """
        from pkg.lib import produce, consume

        def caller():
            x = produce(1)
            consume(x)
        """
    )
    assert _names(pairs) == {("produce", "consume")}
    pair = pairs[0]
    assert pair["source_qualified_name"] == "pkg.lib.produce"
    assert pair["target_qualified_name"] == "pkg.lib.consume"
    assert pair["caller_uid"]


def test_nested_call_flows_into_outer():
    pairs = _pairs(
        """
        from pkg.lib import produce, consume

        def caller():
            consume(produce(2))
        """
    )
    assert _names(pairs) == {("produce", "consume")}


def test_use_inside_keyword_comprehension_counts():
    pairs = _pairs(
        """
        from pkg.lib import produce, consume

        def caller():
            items = produce()
            consume(key=[i.name for i in items])
        """
    )
    assert _names(pairs) == {("produce", "consume")}


def test_rebinding_switches_the_source():
    pairs = _pairs(
        """
        from pkg.lib import produce, other, consume

        def caller():
            x = produce(1)
            x = other()
            consume(x)
        """
    )
    assert ("other", "consume") in _names(pairs)
    assert ("produce", "consume") not in _names(pairs)


def test_binding_not_visible_inside_its_own_assignment():
    # ``x = produce(consume(x))`` — consume reads the OLD x, so no
    # produce -> consume pair may be fabricated from the fresh binding.
    pairs = _pairs(
        """
        from pkg.lib import produce, consume, other

        def caller():
            x = other()
            x = produce(consume(x))
        """
    )
    names = _names(pairs)
    assert ("consume", "produce") in names  # nested call, honest
    assert ("other", "consume") in names  # old binding used in args
    assert ("produce", "consume") not in names


def test_tuple_unpack_binds_every_target():
    pairs = _pairs(
        """
        from pkg.lib import pair_source, consume

        def caller():
            a, b = pair_source()
            consume(b)
        """
    )
    assert _names(pairs) == {("pair_source", "consume")}


def test_await_bound_result_counts():
    pairs = _pairs(
        """
        from pkg.lib import produce, consume

        async def caller():
            x = await produce()
            consume(x)
        """
    )
    assert _names(pairs) == {("produce", "consume")}


def test_bindings_do_not_leak_across_functions():
    pairs = _pairs(
        """
        from pkg.lib import produce, consume

        def one():
            x = produce(1)

        def two():
            consume(x)
        """
    )
    assert _names(pairs) == set()


def test_self_pair_dropped():
    pairs = _pairs(
        """
        from pkg.lib import produce

        def caller():
            x = produce(1)
            produce(x)
        """
    )
    assert _names(pairs) == set()


def test_pairs_deduped_per_caller():
    pairs = _pairs(
        """
        from pkg.lib import produce, consume

        def caller():
            x = produce(1)
            consume(x)
            consume(x)
        """
    )
    assert len(pairs) == 1


def test_per_caller_cap_guards_pathological_functions():
    imports = ", ".join(["produce", *[f"c{i}" for i in range(80)]])
    body = "\n".join(f"    c{i}(x)" for i in range(80))
    source = f"from pkg.lib import {imports}\n\ndef caller():\n    x = produce(1)\n{body}\n"
    adapter = PythonAdapter()
    with project_root_scope("/proj", "test/flow_pairs@ws"):
        pairs = adapter.extract_flow_pairs(source, "/proj/pkg/mod.py")
    assert len(pairs) == PythonAdapter._FLOW_PAIRS_PER_CALLER_CAP


def test_name_only_targets_are_kept_for_linker_resolution():
    # ``registry.apply(...)`` with an unresolvable receiver keeps the bare
    # method name; the linker's workspace-unique-name rule decides its fate.
    pairs = _pairs(
        """
        from pkg.lib import produce

        def caller(registry):
            x = produce(1)
            registry.apply_intent_axis_boost(x)
        """
    )
    assert _names(pairs) == {("produce", "apply_intent_axis_boost")}
    pair = pairs[0]
    assert not pair["target_uid"] and not pair["target_qualified_name"]
    assert pair["target_name"] == "apply_intent_axis_boost"
