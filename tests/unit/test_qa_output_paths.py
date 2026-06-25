from __future__ import annotations

import pytest

from QA.output_paths import (
    DEFAULT_OUTPUT_BASES,
    default_report_basename,
    lookup_allowed_repo_checkout,
    resolve_benchmark_workspace,
    resolve_output_directory,
    resolve_output_path,
    resolve_repo_checkout,
    sanitize_filename_part,
    write_json_report,
)


def test_default_report_lives_under_tmp():
    path = resolve_output_path(None, default_name="proxy_audit_static_django.json")
    assert path.parent == DEFAULT_OUTPUT_BASES[0]
    assert path.name == "proxy_audit_static_django.json"


def test_explicit_tmp_path_allowed():
    path = resolve_output_path(
        "/tmp/proxy_audit_static_pydantic.json",
        default_name="ignored.json",
    )
    assert path == DEFAULT_OUTPUT_BASES[0] / "proxy_audit_static_pydantic.json"


def test_path_traversal_rejected():
    with pytest.raises(SystemExit, match="allowed directory"):
        resolve_output_path("/etc/passwd", default_name="report.json")


def test_relative_escape_rejected(tmp_path):
    allowed = (tmp_path / "reports").resolve()
    allowed.mkdir()
    outside = (tmp_path / "outside.json").resolve()
    with pytest.raises(SystemExit, match="allowed directory"):
        resolve_output_path(str(outside), default_name="report.json", allowed_bases=(allowed,))


def test_sanitize_filename_part():
    assert sanitize_filename_part("django") == "django"
    assert sanitize_filename_part("../evil") == ".._evil"


def test_resolve_repo_checkout_stays_under_repos_base(tmp_path):
    qa_dir = tmp_path / "QA"
    repos = qa_dir / "repos" / "django"
    repos.mkdir(parents=True)
    checkout = resolve_repo_checkout(qa_dir, "django", frozenset({"django"}))
    assert checkout == repos.resolve()


def test_resolve_repo_checkout_rejects_unknown_repo(tmp_path):
    qa_dir = tmp_path / "QA"
    (qa_dir / "repos").mkdir(parents=True)
    with pytest.raises(SystemExit, match="unknown repo"):
        resolve_repo_checkout(qa_dir, "../etc/passwd", frozenset({"django"}))


def test_default_report_basename_uses_allowlist():
    name = default_report_basename("proxy_audit_static", "django", frozenset({"django"}))
    assert name == "proxy_audit_static_django.json"


def test_resolve_benchmark_workspace_is_under_tmp():
    path = resolve_benchmark_workspace("fastapi", frozenset({"fastapi"}))
    assert path.parent == DEFAULT_OUTPUT_BASES[0]
    assert path.name == "axis_benchmark_fastapi"


def test_resolve_output_directory_creates_under_base(tmp_path):
    base = (tmp_path / "reports").resolve()
    path = resolve_output_directory("axis_benchmark_test", allowed_bases=(base,))
    assert path.is_dir()
    assert path.parent == base


def test_lookup_allowed_repo_checkout_uses_predefined_mapping(tmp_path):
    qa_dir = tmp_path / "QA"
    checkouts = {
        "django": (qa_dir / "repos" / "django").resolve(),
    }
    checkout = lookup_allowed_repo_checkout("django", allowed=frozenset({"django"}), checkouts=checkouts)
    assert checkout == checkouts["django"]


def test_write_json_report_uses_allowed_base(tmp_path, monkeypatch):
    allowed = (tmp_path / "reports").resolve()
    allowed.mkdir()
    monkeypatch.setattr(
        "QA.output_paths.DEFAULT_OUTPUT_BASES",
        (allowed,),
    )
    out = write_json_report({"ok": True}, None, default_name="proxy_audit_static_django.json")
    assert out.read_text(encoding="utf-8").startswith("{")
    assert out.parent == allowed
