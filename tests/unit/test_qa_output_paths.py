from __future__ import annotations

import pytest

from QA.output_paths import DEFAULT_OUTPUT_BASES, resolve_output_path, sanitize_filename_part


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
