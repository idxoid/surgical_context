from context_engine.parser.uid import (
    _python_return_annotation_from_header,
    _split_signature,
    module_name_from_path,
    normalize_signature,
    project_root_scope,
)


def test_module_name_from_path_with_project_root_preserves_external_repo_structure():
    file_path = "/tmp/repos/express/lib/application.js"
    module = module_name_from_path(file_path, project_root="/tmp/repos/express")
    assert module == "lib.application"


def test_module_name_from_path_uses_scoped_project_root():
    file_path = "/tmp/repos/vue/packages/runtime-core/src/apiWatch.ts"
    with project_root_scope("/tmp/repos/vue"):
        module = module_name_from_path(file_path)
    assert module == "packages.runtime-core.src.apiWatch"


def test_split_signature_parses_nested_parameter_lists():
    name, params, returns = _split_signature("foo(a, (b, c)) -> list[str]")
    assert name == "foo"
    assert params == "a, (b, c)"
    assert returns == "list[str]"


def test_split_signature_handles_typescript_return_suffix():
    name, params, returns = _split_signature("bar(x: number): string")
    assert name == "bar"
    assert params == "x: number"
    assert returns == "string"


def test_split_signature_rejects_unclosed_parens_without_redos():
    pathological = "foo(" + "(" * 20_000
    assert _split_signature(pathological) == ("foo", "", "_")


def test_python_return_annotation_from_header():
    header = "def foo(x: int) -> Optional[str]:"
    assert _python_return_annotation_from_header(header) == "Optional[str]"


def test_normalize_signature_strips_param_names():
    assert normalize_signature("fetch(url: str, timeout: int = 5) -> bytes") == (
        "fetch(str,int)->bytes"
    )
