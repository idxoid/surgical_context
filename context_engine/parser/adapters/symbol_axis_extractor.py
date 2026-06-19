"""Language-neutral axis facts derived from parser ``SymbolMetadata``.

This extractor is intentionally shallow: it turns typed parser metadata
available for every language into the shared axis-bit vocabulary. Rich AST
extractors (for Python today, TS/JS later) can add more precise facts on top,
but this common floor prevents non-Python rows from losing L2/L3 materialized
payloads entirely.
"""

from __future__ import annotations

from collections.abc import Iterable

from context_engine.axis.schema import AxisExtraction, AxisFact, AxisName
from context_engine.parser.protocol import SymbolMetadata


class SymbolAxisExtractor:
    """Extract conservative, language-neutral axis facts from symbols."""

    def extract(
        self,
        symbols: Iterable[SymbolMetadata],
        file_path: str,
    ) -> AxisExtraction:
        facts: list[AxisFact] = []

        def emit(
            sym: SymbolMetadata,
            axis: AxisName,
            bit: str,
            *,
            ast_kind: str,
            payload: dict[str, object] | None = None,
        ) -> None:
            facts.append(
                AxisFact(
                    symbol_uid=sym.uid,
                    qualified_name=sym.qualified_name or sym.name,
                    symbol_kind=sym.kind,
                    axis=axis,
                    bit=bit,
                    line=int(sym.start_line or 0),
                    evidence=f"<{ast_kind}:{bit}>",
                    ast_kind=ast_kind,
                    payload=payload or {},
                )
            )

        for sym in symbols:
            if sym.kind == "class":
                emit(
                    sym,
                    "struct",
                    "class_def",
                    ast_kind="SymbolMetadata",
                    payload={"name": sym.name},
                )
                emit(
                    sym,
                    "dfg",
                    "callable_value",
                    ast_kind="SymbolMetadata",
                    payload={
                        "callable_kind": "class",
                        "origin": "definition",
                        "name": sym.name,
                    },
                )
            elif sym.kind in {"function", "method"}:
                emit(
                    sym,
                    "struct",
                    "function_def",
                    ast_kind="SymbolMetadata",
                    payload={"name": sym.name},
                )
                emit(
                    sym,
                    "cfg",
                    "callable_body",
                    ast_kind="SymbolMetadata",
                    payload={"callable_kind": sym.kind},
                )
                emit(
                    sym,
                    "dfg",
                    "callable_value",
                    ast_kind="SymbolMetadata",
                    payload={
                        "callable_kind": sym.kind,
                        "origin": "definition",
                        "name": sym.name,
                    },
                )
            elif sym.kind == "variable":
                emit(
                    sym,
                    "struct",
                    "variable_decl",
                    ast_kind="SymbolMetadata",
                    payload={"name": sym.name},
                )

            if sym.returns_function_expression:
                emit(
                    sym,
                    "dfg",
                    "callable_value",
                    ast_kind="ReturnShape",
                    payload={"callable_kind": "function_expression", "origin": "return"},
                )
                emit(sym, "dfg", "return_output", ast_kind="ReturnShape")
            if sym.returns_mapping:
                emit(
                    sym,
                    "dfg",
                    "return_output",
                    ast_kind="ReturnShape",
                    payload={"shape": "mapping"},
                )
                emit(
                    sym,
                    "dfg",
                    "collection_assembly",
                    ast_kind="ReturnShape",
                    payload={"shape": "mapping"},
                )
                emit(
                    sym,
                    "struct",
                    "literal_shape",
                    ast_kind="ReturnShape",
                    payload={"shape": "mapping"},
                )
            if sym.returns_sequence:
                emit(
                    sym,
                    "dfg",
                    "return_output",
                    ast_kind="ReturnShape",
                    payload={"shape": "sequence"},
                )
                emit(
                    sym,
                    "dfg",
                    "collection_assembly",
                    ast_kind="ReturnShape",
                    payload={"shape": "sequence"},
                )
                emit(
                    sym,
                    "struct",
                    "literal_shape",
                    ast_kind="ReturnShape",
                    payload={"shape": "sequence"},
                )
            if sym.returns_constructed_type:
                emit(
                    sym,
                    "dfg",
                    "return_output",
                    ast_kind="ReturnShape",
                    payload={"shape": "constructed"},
                )
                emit(sym, "cfg", "constructor_call", ast_kind="ReturnShape")
                emit(sym, "dfg", "constructor_value", ast_kind="ReturnShape")
            if sym.iterates_attr_call:
                emit(sym, "dfg", "iteration_source", ast_kind="IterationShape")
                emit(sym, "cfg", "value_call", ast_kind="IterationShape")
            if sym.assembles_mapping_in_loop:
                emit(
                    sym,
                    "dfg",
                    "container_write_value",
                    ast_kind="IterationShape",
                    payload={"container": "loop_mapping"},
                )
                emit(
                    sym,
                    "dfg",
                    "keyed_write",
                    ast_kind="IterationShape",
                    payload={"container": "loop_mapping"},
                )
            if sym.is_getter:
                emit(
                    sym,
                    "struct",
                    "property_accessor",
                    ast_kind="SymbolMetadata",
                    payload={"kind": "get"},
                )
            if sym.is_setter:
                emit(
                    sym,
                    "struct",
                    "property_accessor",
                    ast_kind="SymbolMetadata",
                    payload={"kind": "set"},
                )
            if sym.is_react_hook:
                emit(sym, "struct", "hook_convention", ast_kind="SymbolMetadata")

        return AxisExtraction(file_path=file_path, facts=facts)


__all__ = ["SymbolAxisExtractor"]
