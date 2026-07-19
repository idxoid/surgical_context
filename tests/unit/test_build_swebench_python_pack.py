"""Unit tests for SWE-bench → question-pack conversion."""

from __future__ import annotations

from QA.build_swebench_python_pack import parse_patch_gold, parse_patch_spans, row_to_question


def test_parse_patch_spans_old_side_coordinates():
    patch = """\
diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -10,7 +10,8 @@ def foo():
     a = 1
     b = 2
-    c = 3
+    c = 30
+    d = 4
     return c
"""
    files, spans = parse_patch_spans(patch)
    assert files == ["pkg/mod.py"]
    assert spans == [
        {
            "file_path": "pkg/mod.py",
            "symbol": "",
            "start_line": 10,
            "end_line": 16,
        }
    ]

    _files, hunk_spans, edit_spans = parse_patch_gold(patch)
    assert hunk_spans == spans
    assert edit_spans == [
        {
            "file_path": "pkg/mod.py",
            "symbol": "",
            "start_line": 12,
            "end_line": 12,
        }
    ]


def test_parse_patch_pure_insertion_pins_locus():
    patch = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -5,0 +6,2 @@
+new_line_1
+new_line_2
"""
    files, spans = parse_patch_spans(patch)
    assert files == ["a.py"]
    assert spans[0]["start_line"] == 5
    assert spans[0]["end_line"] == 5

    _files, _hunk_spans, edit_spans = parse_patch_gold(patch)
    assert edit_spans == [
        {
            "file_path": "a.py",
            "symbol": "",
            "start_line": 5,
            "end_line": 5,
        }
    ]


def test_row_to_question_maps_django():
    row = {
        "repo": "django/django",
        "instance_id": "django__django-10914",
        "base_commit": "e7fd69d051eaa67cb17f172a39b57253e9cb831a",
        "version": "3.0",
        "problem_statement": "Set default FILE_UPLOAD_PERMISSION to 0o644.",
        "patch": """\
diff --git a/django/conf/global_settings.py b/django/conf/global_settings.py
--- a/django/conf/global_settings.py
+++ b/django/conf/global_settings.py
@@ -304,7 +304,7 @@ def gettext_noop(s):
 
 # comment
-FILE_UPLOAD_PERMISSIONS = None
+FILE_UPLOAD_PERMISSIONS = 0o644
 
 # more
""",
        "FAIL_TO_PASS": ["test_override_file_upload_permissions"],
    }
    q = row_to_question(row)
    assert q is not None
    assert q["repo"] == "django"
    assert q["instance_id"] == "django__django-10914"
    assert q["expected_files"] == ["django/conf/global_settings.py"]
    assert q["expected_spans"] == q["expected_edit_spans"]
    assert q["expected_spans"][0]["start_line"] == 306
    assert q["expected_hunk_spans"][0]["start_line"] == 304
    assert q["intent"] == "bug_fix"
    assert q["expected_mode"] == "workspace"
