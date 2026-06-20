"""JavaScript AST extractor for physical CFG/DFG/Structural axis bits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from context_engine.parser.adapters.typescript_axis_extractor import TypeScriptAxisExtractor

if TYPE_CHECKING:
    from context_engine.parser.adapters.javascript_adapter import JavaScriptAdapter


class JavaScriptAxisExtractor(TypeScriptAxisExtractor):
    """Reuse the TS/JS tree-sitter axis walker for plain JavaScript sources."""

    def __init__(self, adapter: JavaScriptAdapter) -> None:
        super().__init__(adapter)


__all__ = ["JavaScriptAxisExtractor"]
