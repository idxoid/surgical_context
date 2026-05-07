from sidecar.parser.uid import module_name_from_path, project_root_scope


def test_module_name_from_path_with_project_root_preserves_external_repo_structure():
    file_path = "/tmp/repos/express/lib/application.js"
    module = module_name_from_path(file_path, project_root="/tmp/repos/express")
    assert module == "lib.application"


def test_module_name_from_path_uses_scoped_project_root():
    file_path = "/tmp/repos/vue/packages/runtime-core/src/apiWatch.ts"
    with project_root_scope("/tmp/repos/vue"):
        module = module_name_from_path(file_path)
    assert module == "packages.runtime-core.src.apiWatch"
