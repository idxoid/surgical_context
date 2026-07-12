"""Parity + gating tests for the tree-sitter axis extractor.

The corpus-scale contract lives in ``scripts/axis_ts_parity.py`` (run over the
QA repos); these tests pin the same contract on a representative snippet so a
regression in either twin fails fast in CI.
"""

import textwrap
from collections import Counter

from context_engine.axis.container_kind import ContainerKindClassifier, NullGraphProbe
from context_engine.parser.adapters.python_axis_extractor import PythonAxisExtractor
from context_engine.parser.adapters.python_axis_extractor_ts import PythonAxisExtractorTS

_FIXTURE = textwrap.dedent(
    '''
    """Module doc."""
    from __future__ import annotations
    import os, sys as system
    from ..pkg import thing as alias

    REGISTRY: dict[str, int] = {"a": 1, **extra}

    @decorate(arg, kw=1)
    class Service(Base, metaclass=Meta):
        limit = 10
        cache: t.Optional[dict] = None

        @property
        async def handler(self, req, timeout: float = 1.5, *args, **kwargs) -> dict[str, list[int]]:
            items = [x.value for x in req.parts if x.ok]
            d = {}
            d["key"] = Factory()
            d.setdefault("k2", make())
            got = cfg.get("name", fallback)
            self.state, other = compute(), None
            a = b = build(*parts, mode="fast")
            try:
                async with open_conn() as (conn, extra):
                    async for row in conn:
                        await push(row)
                        yield row
            except (ValueError, KeyError) as exc:
                raise Wrapped(exc) from exc
            if req.flag and not other:
                return {"result": [v for v in items], "n": len(items)}
            elif req.other[0]:
                return Factory(a, key=1)
            return None if got is None else got >= 0

    def top(fn=lambda q: q + 1):
        matrix[i, j] = fn
        del matrix[k]
        results.append(fn),
        return fn
    '''
)


def _facts(extractor, source: str):
    return extractor.extract_facts(source, "pkg/mod.py")


def _tier_a_key(fact):
    return (fact.qualified_name, fact.axis, fact.bit, fact.line, fact.ast_kind)


def test_ts_extractor_matches_ast_facts_on_fixture():
    old = _facts(PythonAxisExtractor(), _FIXTURE)
    new = _facts(PythonAxisExtractorTS(), _FIXTURE)
    assert Counter(map(_tier_a_key, old)) == Counter(map(_tier_a_key, new))


def test_ts_extractor_matches_key_literals_and_payload_fields():
    keyed_bits = {"keyed_read", "keyed_write", "container_read_key", "literal_key"}

    def keyed(facts):
        return Counter(
            (f.bit, f.payload.get("key_kind"), repr(f.payload.get("key_literal")))
            for f in facts
            if f.bit in keyed_bits
        )

    old = _facts(PythonAxisExtractor(), _FIXTURE)
    new = _facts(PythonAxisExtractorTS(), _FIXTURE)
    assert keyed(old) == keyed(new)


def test_ts_extractor_matches_classifier_output():
    old_ex = PythonAxisExtractor().extract(_FIXTURE, "pkg/mod.py")
    new_ex = PythonAxisExtractorTS().extract(_FIXTURE, "pkg/mod.py")
    classifier = ContainerKindClassifier(NullGraphProbe())
    old_kinds = {
        qn: {m.kind for m in classifier.classify(p)}
        for qn, p in old_ex.profiles_by_qualified_name.items()
    }
    new_kinds = {
        qn: {m.kind for m in classifier.classify(p)}
        for qn, p in new_ex.profiles_by_qualified_name.items()
    }
    assert old_kinds == new_kinds


def test_ts_extractor_exact_fact_dicts_on_expressions():
    """Expression-level facts should be byte-identical dicts (Tier B exact).

    Facts anchored on body-carrying statements (module/def/if/…) keep source
    quoting in their evidence — a documented display-only divergence — so the
    comparison excludes those ast_kinds.
    """
    statement_kinds = {
        "Module",
        "FunctionDef",
        "AsyncFunctionDef",
        "ClassDef",
        "If",
        "For",
        "AsyncFor",
        "While",
        "With",
        "AsyncWith",
        "Try",
        "Match",
        "ExceptHandler",
    }
    src = textwrap.dedent(
        """
        def run(cfg, items):
            name = cfg.get("name", 'default')
            data = {"k": [1, 0x10, *items], "n": (1,)}
            if cfg.flag is not None and len(items) >= 2:
                return build(*items, mode="x")
        """
    )

    def exact(facts):
        return {
            (_tier_a_key(f), f.evidence, tuple(sorted(map(str, f.payload.items()))))
            for f in facts
            if f.ast_kind not in statement_kinds
        }

    old = exact(_facts(PythonAxisExtractor(), src))
    new = exact(_facts(PythonAxisExtractorTS(), src))
    assert old == new


def test_ts_extractor_yields_partial_facts_on_broken_source():
    src = "def ok():\n    return 1\n\ndef broken(:\n    pass\n"
    new = _facts(PythonAxisExtractorTS(), src)
    assert any(f.qualified_name.endswith(".ok") for f in new)


def test_adapter_gate_env_switch(monkeypatch):
    from context_engine.parser.adapters.python_adapter import PythonAdapter

    adapter = PythonAdapter()
    src = "def f(a):\n    return a + 1\n"
    monkeypatch.setenv("AXIS_TS_EXTRACTOR", "0")
    ast_facts = adapter.extract_axis_facts(src, "pkg/mod.py")
    monkeypatch.setenv("AXIS_TS_EXTRACTOR", "1")
    ts_facts = adapter.extract_axis_facts(src, "pkg/mod.py")
    assert Counter(map(_tier_a_key, ast_facts)) == Counter(map(_tier_a_key, ts_facts))
    assert any(f.bit == "callable_body" for f in ts_facts)
