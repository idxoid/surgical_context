"""File-graph re-export resolution for TypeScript / JavaScript imports."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from context_engine.parser.uid import current_project_root, module_name_from_path

if TYPE_CHECKING:
    from context_engine.parser.adapters.typescript_adapter import TypeScriptAdapter


class TsReexportResolver:
    """Resolve import binding qnames through barrel ``export-from`` / ``export *`` chains."""

    _MAX_DEPTH = 6

    def __init__(self, adapter: TypeScriptAdapter) -> None:
        self._adapter = adapter
        self._surface_cache: dict[str, dict[str, str]] = {}

    def resolve_binding_qn(self, qn: str, consumer_file: str) -> str:
        """Follow re-export surfaces until the binding reaches a concrete module symbol."""
        seen: set[str] = set()
        current = qn
        while "." in current:
            if current in seen:
                break
            seen.add(current)
            mod_qn, sym = current.rsplit(".", 1)
            barrel_path = self._module_qn_to_path(mod_qn, consumer_file)
            if not barrel_path:
                break
            surface = self.surface_exports(barrel_path)
            target = surface.get(sym)
            if not target or target == current:
                break
            current = target
        return current

    def surface_exports(self, barrel_path: str, *, depth: int = 0) -> dict[str, str]:
        abs_key = str(Path(barrel_path).resolve())
        cached = self._surface_cache.get(abs_key)
        if cached is not None:
            return cached
        if depth > self._MAX_DEPTH:
            return {}

        try:
            source = Path(barrel_path).read_text(encoding="utf-8")
        except OSError:
            self._surface_cache[abs_key] = {}
            return {}

        tree = self._adapter._parse(source)
        surface = self._surface_from_source(
            source,
            barrel_path,
            tree=tree,
            depth=depth,
        )
        self._surface_cache[abs_key] = surface
        return surface

    def _record_export_specifier(
        self,
        spec,
        *,
        target_module: str,
        seen: set[tuple[str, str]],
        out: dict[str, str],
    ) -> None:
        if spec.type != "export_specifier":
            return
        name_node = spec.child_by_field_name("name")
        alias_node = spec.child_by_field_name("alias")
        if name_node is None:
            return
        original = self._adapter._node_text(name_node)
        export_name = self._adapter._node_text(alias_node) if alias_node is not None else original
        if original == "default":
            return
        export_qn = f"{target_module}.{original}"
        key = (export_name, export_qn)
        if key in seen:
            return
        seen.add(key)
        out[export_name] = export_qn

    def _surface_from_export_statement(
        self,
        stmt,
        *,
        file_path: str,
        depth: int,
        seen: set[tuple[str, str]],
        out: dict[str, str],
    ) -> None:
        source_node = next((c for c in stmt.children if c.type == "string"), None)
        if source_node is None:
            return
        import_source = self._adapter._string_literal_text(source_node)
        if not import_source:
            return
        target_module = self._adapter._normalize_import_source(file_path, import_source)
        export_clause = next(
            (c for c in stmt.children if c.type == "export_clause"),
            None,
        )
        if export_clause is None:
            self._merge_star_export(out, file_path, target_module, depth=depth)
            return
        for spec in export_clause.named_children:
            self._record_export_specifier(
                spec,
                target_module=target_module,
                seen=seen,
                out=out,
            )

    def _surface_from_source(
        self,
        _source_code: str,
        file_path: str,
        *,
        tree,
        depth: int,
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        seen: set[tuple[str, str]] = set()

        for stmt in self._adapter._iter_nodes(tree.root_node):
            if stmt.type != "export_statement":
                continue
            self._surface_from_export_statement(
                stmt,
                file_path=file_path,
                depth=depth,
                seen=seen,
                out=out,
            )

        return out

    def _merge_star_export(
        self,
        out: dict[str, str],
        consumer_file: str,
        target_module: str,
        *,
        depth: int,
    ) -> None:
        target_path = self._module_qn_to_path(target_module, consumer_file)
        if not target_path:
            return
        try:
            target_source = Path(target_path).read_text(encoding="utf-8")
        except OSError:
            return

        target_mod = module_name_from_path(target_path)
        target_tree = self._adapter._parse(target_source)
        for symbol in self._adapter.extract_symbols(target_source, target_path, tree=target_tree):
            out.setdefault(symbol.name, f"{target_mod}.{symbol.name}")

        nested = self.surface_exports(target_path, depth=depth + 1)
        for name, qn in nested.items():
            out.setdefault(name, qn)

    def _module_qn_to_path(self, mod_qn: str, _hint_file: str) -> str | None:
        project_root = current_project_root()
        if not project_root:
            return None
        root = Path(project_root).resolve()
        rel = mod_qn.replace(".", "/")
        candidates = [
            root / f"{rel}.ts",
            root / f"{rel}.tsx",
            root / f"{rel}.js",
            root / f"{rel}.jsx",
            root / rel / "index.ts",
            root / rel / "index.tsx",
            root / rel / "index.js",
            root / rel / "index.jsx",
        ]
        if mod_qn.endswith(".index"):
            parent = mod_qn[: -len(".index")].replace(".", "/")
            candidates.extend(
                [
                    root / parent / "index.ts",
                    root / parent / "index.tsx",
                    root / parent / "index.js",
                    root / parent / "index.jsx",
                ]
            )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        return None

    @staticmethod
    def enrich_bindings(
        bindings: dict[str, str],
        consumer_file: str,
        resolve: Callable[[str, str], str],
    ) -> None:
        for alias, qn in list(bindings.items()):
            if "." not in qn:
                continue
            bindings[alias] = resolve(qn, consumer_file)
