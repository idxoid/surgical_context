"""Structural ``proxy_object`` propagation helpers."""

from __future__ import annotations

from sidecar.indexer.fast.proxy_object_propagation import (
    method_facts_show_proxy_delegation,
)


def test_method_facts_show_proxy_delegation_matches_delegate_body():
    facts = [
        {"axis": "dfg", "bit": "attr_read", "payload": {}},
        {"axis": "cfg", "bit": "value_call", "payload": {}},
        {"axis": "dfg", "bit": "return_output", "payload": {}},
    ]
    assert method_facts_show_proxy_delegation(facts) is True


def test_method_facts_show_proxy_delegation_rejects_registry_writes():
    facts = [
        {"axis": "dfg", "bit": "attr_read", "payload": {}},
        {"axis": "cfg", "bit": "value_call", "payload": {}},
        {"axis": "dfg", "bit": "return_output", "payload": {}},
        {"axis": "dfg", "bit": "keyed_write", "payload": {}},
    ]
    assert method_facts_show_proxy_delegation(facts) is False


def test_method_facts_show_proxy_delegation_rejects_incomplete_body():
    facts = [
        {"axis": "dfg", "bit": "attr_read", "payload": {}},
        {"axis": "dfg", "bit": "return_output", "payload": {}},
    ]
    assert method_facts_show_proxy_delegation(facts) is False
