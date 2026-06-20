"""Structural ``error_dispatch`` propagation and key resolution."""

from __future__ import annotations

from context_engine.indexer.fast.error_dispatch_propagation import (
    exception_name_keys_from_keyed_writes,
    is_builtin_exception_type_name,
)


def test_builtin_exception_name_is_exception_type():
    assert is_builtin_exception_type_name("ValueError")
    assert is_builtin_exception_type_name("HTTPException") is False


def test_exception_name_keys_from_keyed_writes_filters_name_kind_only():
    facts = [
        {
            "axis": "dfg",
            "bit": "keyed_write",
            "payload": {
                "key": "ValueError",
                "key_kind": "Name",
            },
        },
        {
            "axis": "dfg",
            "bit": "keyed_write",
            "payload": {
                "key": "/users",
                "key_kind": "Constant",
                "key_literal": "/users",
            },
        },
        {
            "axis": "cfg",
            "bit": "value_call",
            "payload": {},
        },
    ]
    assert exception_name_keys_from_keyed_writes(facts) == ["ValueError"]


def test_exception_name_keys_from_keyed_writes_empty_when_no_hits():
    assert exception_name_keys_from_keyed_writes([]) == []
