"""Python language adapter using tree-sitter."""

import re
from pathlib import Path

from tree_sitter import Query

from sidecar.parser.adapters.treesitter_base import TreeSitterAdapter
from sidecar.parser.protocol import ImportEdge, InheritanceEdge
from sidecar.parser.uid import (
    compute_uid,
    module_name_from_path,
    qualified_name_for,
    signature_from_node,
)


class PythonAdapter(TreeSitterAdapter):
    """Python parser adapter."""

    @property
    def language_name(self) -> str:
        return "python"

    @property
    def file_extensions(self) -> set[str]:
        return {".py", ".pyi"}

    @property
    def ts_language_name(self) -> str:
        return "python"

    @property
    def symbol_query(self) -> str:
        return """
            (function_definition name: (identifier) @func.name) @func.def
            (class_definition name: (identifier) @class.name) @class.def
            (module (expression_statement (assignment left: (identifier) @var.name) @var.def))
        """

    @property
    def call_query(self) -> str:
        return "(call) @call"

    @property
    def parent_types(self) -> set[str]:
        return {"function_definition", "class_definition"}

    @property
    def import_query(self) -> str:
        return """
            (import_statement name: (dotted_name) @import.name)
            (import_statement name: (identifier) @import.name)
        """

    # Known stdlib and third-party top-level packages to skip
    _EXTERNAL_PREFIXES = {
        "os",
        "sys",
        "re",
        "io",
        "abc",
        "ast",
        "math",
        "time",
        "json",
        "uuid",
        "enum",
        "copy",
        "typing",
        "hashlib",
        "pathlib",
        "logging",
        "dataclasses",
        "collections",
        "functools",
        "itertools",
        "contextlib",
        "threading",
        "multiprocessing",
        "subprocess",
        "shutil",
        "tempfile",
        "unittest",
        "argparse",
        "heapq",
        "struct",
        "string",
        "textwrap",
        # third-party
        "neo4j",
        "lancedb",
        "fastapi",
        "pydantic",
        "uvicorn",
        "ollama",
        "yaml",
        "tiktoken",
        "numpy",
        "pandas",
        "sentence_transformers",
        "tree_sitter",
        "tree_sitter_languages",
        "pathspec",
    }

    def extract_imports(self, source_code: str, file_path: str, *, tree=None) -> list[ImportEdge]:
        """Extract only intra-project import statements (skips stdlib and third-party).

        Imports are line-based regex; ``tree`` is unused but accepted for
        ``extract_all`` parity.
        """
        imports = []
        for line in source_code.split("\n"):
            line = line.strip()
            if line.startswith("import "):
                parts = line[7:].split(",")
                for part in parts:
                    module = part.strip().split(" as ")[0].strip()
                    if module and not self._is_external(module, file_path=file_path):
                        imports.append(ImportEdge(file_path, module, "direct"))
            elif line.startswith("from "):
                match = line.split(" import ")
                if len(match) == 2:
                    module = match[0][5:].strip()
                    if (
                        module
                        and module != "."
                        and not self._is_external(module.lstrip("."), file_path=file_path)
                    ):
                        imports.append(ImportEdge(file_path, module, "from_package"))
        return imports

    def _is_external(self, module: str, *, file_path: str | None = None) -> bool:
        top = module.split(".")[0]
        if file_path and top:
            parent_dirs = {parent.name for parent in Path(file_path).parents if parent.name}
            if top in parent_dirs:
                return False
        return top in self._EXTERNAL_PREFIXES

    def extract_inheritance(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[InheritanceEdge]:
        """Extract class inheritance from Python source.

        Line-based scan; ``tree`` is accepted for ``extract_all`` parity.
        """
        edges = []
        lines = source_code.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("class "):
                match = line[6:].split(":")[0].strip()
                if "(" in match:
                    class_name = match.split("(")[0].strip()
                    bases_str = match.split("(")[1].rstrip(")")
                    for base in bases_str.split(","):
                        base_name = base.strip()
                        if base_name:
                            subclass_uid = self._uid(file_path, class_name)
                            edges.append(InheritanceEdge(subclass_uid, base_name, False))
        return edges

    def _positional_identifier_arguments(
        self, call_node, source_code: str, *, limit: int = 8
    ) -> list[str]:
        """Leading positional arguments that are bare identifiers (for DI-style hints)."""
        arg_list = call_node.child_by_field_name("arguments")
        if arg_list is None:
            return []
        out: list[str] = []
        for child in arg_list.named_children:
            if child.type == "keyword_argument":
                break
            if child.type == "identifier":
                out.append(source_code[child.start_byte : child.end_byte])
                if len(out) >= limit:
                    break
                continue
            break
        return out

    def extract_calls_from_source(
        self, source_code: str, file_path: str, *, tree=None
    ) -> list[dict]:
        """Extract function calls and attach resolver metadata when statically resolvable."""
        if tree is None:
            tree = self._parse(source_code)
        query = Query(self.language, self.call_query)

        # Flatten captures from matches into (node, tag) tuples
        captures = []
        for _match_id, captures_dict in query.matches(tree.root_node):
            for tag, nodes in captures_dict.items():
                for node in nodes:
                    captures.append((node, tag))

        symbols = self.extract_symbols(source_code, file_path, tree=tree)
        by_name: dict[str, list] = {}
        for symbol in symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        import_bindings = self._extract_import_bindings(source_code, file_path)

        calls = []
        for node, tag in captures:
            if tag != "call":
                continue

            func_node = node.child_by_field_name("function")
            if not func_node:
                continue

            parent = node.parent
            while parent and parent.type not in self.parent_types:
                parent = parent.parent
            if not parent:
                continue

            caller_uid = self._uid_for_node(parent, source_code, file_path)
            call_name = ""
            callee_uid = None
            callee_qualified_name = None
            rel_type = "CALLS_GUESS"
            tier = "guess"
            confidence = 0.4

            if func_node.type == "identifier":
                call_name = source_code[func_node.start_byte : func_node.end_byte]
                rel_type = self._classify_direct_call(call_name)
                tier = "direct" if rel_type == "CALLS_DIRECT" else "guess"
                confidence = 1.0 if rel_type == "CALLS_DIRECT" else 0.4

                if call_name in import_bindings:
                    callee_qualified_name = import_bindings[call_name]
                    rel_type = "CALLS_IMPORTED"
                    tier = "imported"
                    confidence = 0.85
                elif len(by_name.get(call_name, [])) == 1:
                    callee_uid = by_name[call_name][0].uid
                    rel_type = "CALLS_SCOPED"
                    tier = "scoped"
                    confidence = 0.9
                elif rel_type != "CALLS_INFERRED":
                    rel_type = "CALLS_GUESS"
                    tier = "guess"
                    confidence = 0.4

            elif func_node.type == "attribute":
                children = [c for c in func_node.children if c.type == "identifier"]
                if len(children) < 2:
                    continue
                receiver_node = children[0]
                method_node = children[-1]
                receiver_text = source_code[receiver_node.start_byte : receiver_node.end_byte]
                call_name = source_code[method_node.start_byte : method_node.end_byte]

                if receiver_text == "self":
                    rel_type = "CALLS_DYNAMIC"
                    tier = "dynamic"
                    confidence = 0.7
                    callee_uid = self._resolve_method_uid(parent, call_name, by_name)
                else:
                    rel_type = "CALLS_DYNAMIC"
                    tier = "dynamic"
                    confidence = 0.7
                    if receiver_text in import_bindings:
                        base = import_bindings[receiver_text]
                        callee_qualified_name = f"{base}.{call_name}"
            else:
                continue

            if callee_uid == caller_uid:
                continue

            call = {
                "caller_uid": caller_uid,
                "callee_name": call_name,
                "rel_type": rel_type,
                "tier": tier,
                "confidence": confidence,
                "resolver": "py-scope-v1",
                "call_site_line": node.start_point[0] + 1,
            }
            if callee_uid:
                call["callee_uid"] = callee_uid
            if callee_qualified_name:
                call["callee_qualified_name"] = callee_qualified_name
            pos_args = self._positional_identifier_arguments(node, source_code)
            if pos_args:
                call["arguments"] = pos_args
            calls.append(call)

        return calls

    def _classify_direct_call(self, call_name: str) -> str:
        """Classify a direct identifier call as DIRECT or INFERRED based on known patterns."""
        inferred_patterns = {
            "getattr",
            "setattr",
            "hasattr",
            "getattr_static",
            "operator.methodcaller",
            "methodcaller",
            "exec",
            "eval",
            "compile",
            "__import__",
            "importlib.import_module",
        }

        if call_name in inferred_patterns or call_name.startswith("globals()["):
            return "CALLS_INFERRED"

        if call_name in ("__init__", "__call__", "__getattr__", "__setattr__"):
            return "CALLS_DIRECT"

        return "CALLS_DIRECT"

    def _uid(self, file_path: str, name: str) -> str:
        qualified_name = f"{module_name_from_path(file_path)}.{name}"
        return compute_uid(qualified_name, f"{name}()->_", self.language_name)

    def _uid_for_node(self, node, source_code: str, file_path: str) -> str:
        qualified_name = qualified_name_for(node, source_code, file_path)
        raw_signature, _ = signature_from_node(node, source_code, self.language_name)
        return compute_uid(qualified_name, raw_signature, self.language_name)

    def _resolve_method_uid(
        self, caller_node, method_name: str, by_name: dict[str, list]
    ) -> str | None:
        candidates = by_name.get(method_name, [])
        if not candidates:
            return None

        class_node = caller_node
        while class_node and class_node.type != "class_definition":
            class_node = class_node.parent
        if not class_node:
            return candidates[0].uid if len(candidates) == 1 else None

        class_name_node = class_node.child_by_field_name("name")
        if not class_name_node:
            return None
        class_name = class_name_node.text.decode("utf-8")
        for candidate in candidates:
            if f".{class_name}.{method_name}" in candidate.qualified_name:
                return str(candidate.uid)
        return str(candidates[0].uid) if len(candidates) == 1 else None

    def _extract_import_bindings(self, source_code: str, file_path: str) -> dict[str, str]:
        """Return local import alias -> best-effort target qualified name."""
        module = module_name_from_path(file_path)
        package = module.rsplit(".", 1)[0] if "." in module else ""
        bindings: dict[str, str] = {}
        for line in source_code.splitlines():
            stripped = line.strip()
            from_match = re.match(r"from\s+([.\w]+)\s+import\s+(.+)$", stripped)
            if from_match:
                import_module, names = from_match.groups()
                target_module = self._resolve_import_module(import_module, package)
                for item in names.split(","):
                    item = item.strip()
                    if not item or item == "*":
                        continue
                    original, _, alias = item.partition(" as ")
                    local_name = alias.strip() or original.strip()
                    bindings[local_name] = f"{target_module}.{original.strip()}"
                continue

            import_match = re.match(r"import\s+(.+)$", stripped)
            if import_match:
                for item in import_match.group(1).split(","):
                    item = item.strip()
                    original, _, alias = item.partition(" as ")
                    target_module = original.strip()
                    if not target_module:
                        continue
                    local_name = alias.strip() or target_module.split(".")[0]
                    bindings[local_name] = target_module
        return bindings

    def _resolve_import_module(self, import_module: str, package: str) -> str:
        if not import_module.startswith("."):
            return import_module
        dots = len(import_module) - len(import_module.lstrip("."))
        remainder = import_module.lstrip(".")
        parts = package.split(".") if package else []
        prefix = parts[: max(0, len(parts) - dots + 1)]
        if remainder:
            prefix.append(remainder)
        return ".".join(p for p in prefix if p)


def make_adapter() -> PythonAdapter:
    """Factory function for adapter discovery."""
    return PythonAdapter()
