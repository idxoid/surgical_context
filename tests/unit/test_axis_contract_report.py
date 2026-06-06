import json

from QA.axis_contract_report import (
    axis_profile_from_lance_row,
    build_axis_contract_report,
    compile_contract_report_row,
    write_axis_contract_report,
)
from sidecar.axis.container_kind import ContainerKindMatch
from sidecar.axis.schema import AxisFact


def _kind_json(match: ContainerKindMatch) -> str:
    return json.dumps([match.to_dict()], sort_keys=True)


def _fact_json(*facts: AxisFact) -> str:
    return json.dumps([fact.to_dict() for fact in facts], sort_keys=True)


def _fact(axis: str, bit: str, *, payload=None) -> AxisFact:
    return AxisFact(
        symbol_uid="u:c",
        qualified_name="pkg.chain",
        symbol_kind="function",
        axis=axis,
        bit=bit,
        line=1,
        evidence=f"<{bit}>",
        ast_kind="Synthetic",
        payload=payload or {},
    )


def test_axis_profile_from_lance_row_uses_persisted_bits_and_kind_qualified_name():
    row = {
        "uid": "u:x",
        "name": "x",
        "cfg_bits": ["value_call"],
        "dfg_bits": ["callable_value"],
        "struct_bits": ["function_def"],
        "axis_container_kinds_json": _kind_json(
            ContainerKindMatch(
                kind="proxy_object",
                symbol_uid="u:x",
                qualified_name="pkg.proxy",
                evidence_bits=(),
                evidence_probes=("library_marker:proxy_object",),
                payload={},
            )
        ),
    }

    profile = axis_profile_from_lance_row(row)

    assert profile.symbol_uid == "u:x"
    assert profile.qualified_name == "pkg.proxy"
    assert profile.cfg_bits == {"value_call"}
    assert profile.dfg_bits == {"callable_value"}
    assert profile.struct_bits == {"function_def"}


def test_compile_contract_report_row_includes_contract_and_query_plan():
    row = {
        "uid": "u:m",
        "name": "registry",
        "file_path": "/repo/registry.py",
        "cfg_bits": [],
        "dfg_bits": ["keyed_write", "keyed_read"],
        "struct_bits": ["literal_key"],
        "axis_container_kinds_json": _kind_json(
            ContainerKindMatch(
                kind="metadata_carrier",
                symbol_uid="u:m",
                qualified_name="pkg.registry",
                evidence_bits=(
                    ("dfg", "keyed_write"),
                    ("dfg", "keyed_read"),
                    ("struct", "literal_key"),
                ),
                evidence_probes=(),
                payload={"shared_key_count": 1},
            )
        ),
    }

    report_row = compile_contract_report_row(row, workspace_id="ws")

    assert report_row.container_kinds == ("metadata_carrier",)
    assert [contract.contract for contract in report_row.contracts] == [
        "metadata_key_roundtrip"
    ]
    assert report_row.persisted_contracts == ()
    assert report_row.contract_drift is False
    assert report_row.plans[0]["traversal_mode"] == "deferred_binding_flow"
    assert report_row.plans[0]["lance_predicate"] == (
        "workspace_id = 'ws' "
        "AND array_has(dfg_bits, 'keyed_read') "
        "AND array_has(dfg_bits, 'keyed_write') "
        "AND array_has(struct_bits, 'literal_key') "
        "AND axis_container_kinds_json LIKE '%\"kind\": \"metadata_carrier\"%'"
    )


def test_contract_report_uses_axis_evidence_payload_for_dispatch_identity():
    kind = ContainerKindMatch(
        kind="middleware_chain",
        symbol_uid="u:c",
        qualified_name="pkg.chain",
        evidence_bits=(
            ("dfg", "callable_value"),
            ("dfg", "container_write_value"),
            ("dfg", "iteration_source"),
            ("cfg", "value_call"),
        ),
        evidence_probes=(),
        payload={},
    )
    base = {
        "uid": "u:c",
        "name": "chain",
        "file_path": "/repo/chain.py",
        "cfg_bits": ["value_call"],
        "dfg_bits": ["callable_value", "container_write_value", "iteration_source"],
        "struct_bits": [],
        "axis_container_kinds_json": _kind_json(kind),
    }

    without_facts = compile_contract_report_row(base, workspace_id="ws")
    with_facts = compile_contract_report_row(
        {
            **base,
            "axis_evidence_json": _fact_json(
                _fact("dfg", "callable_value"),
                _fact("dfg", "container_write_value", payload={"container": "self.chain"}),
                _fact("dfg", "iteration_source", payload={"iterable": "self.chain"}),
                _fact("cfg", "value_call"),
            ),
        },
        workspace_id="ws",
    )

    assert without_facts.contracts == ()
    assert [contract.contract for contract in with_facts.contracts] == [
        "callable_container_dispatch"
    ]
    assert with_facts.contracts[0].payload["container"] == "self.chain"


def test_contract_report_marks_persisted_contract_drift():
    row = {
        "uid": "u:m",
        "name": "registry",
        "file_path": "/repo/registry.py",
        "cfg_bits": [],
        "dfg_bits": ["keyed_write", "keyed_read"],
        "struct_bits": ["literal_key"],
        "axis_contracts_json": "[{\"contract\": \"stale_contract\"}]",
        "axis_container_kinds_json": _kind_json(
            ContainerKindMatch(
                kind="metadata_carrier",
                symbol_uid="u:m",
                qualified_name="pkg.registry",
                evidence_bits=(
                    ("dfg", "keyed_write"),
                    ("dfg", "keyed_read"),
                    ("struct", "literal_key"),
                ),
                evidence_probes=(),
                payload={},
            )
        ),
    }

    report_row = compile_contract_report_row(row, workspace_id="ws")

    assert [contract.contract for contract in report_row.contracts] == [
        "metadata_key_roundtrip"
    ]
    assert report_row.persisted_contracts == ("stale_contract",)
    assert report_row.contract_drift is True


def test_build_report_filters_plain_rows_without_kinds_or_contracts():
    rows = [
        {
            "uid": "plain",
            "name": "plain",
            "cfg_bits": ["call_site"],
            "dfg_bits": [],
            "struct_bits": ["function_def"],
            "axis_container_kinds_json": "[]",
        },
        {
            "uid": "proxy",
            "name": "proxy",
            "cfg_bits": [],
            "dfg_bits": [],
            "struct_bits": [],
            "axis_container_kinds_json": _kind_json(
                ContainerKindMatch(
                    kind="proxy_object",
                    symbol_uid="proxy",
                    qualified_name="pkg.proxy",
                    evidence_bits=(),
                    evidence_probes=("library_marker:proxy_object",),
                    payload={},
                )
            ),
        },
    ]

    report = build_axis_contract_report(rows, workspace_id="ws")

    assert [row.uid for row in report] == ["proxy"]
    assert [contract.contract for contract in report[0].contracts] == ["proxy_indirection"]


def test_write_axis_contract_report_outputs_jsonl_and_markdown(tmp_path):
    row = compile_contract_report_row(
        {
            "uid": "proxy",
            "name": "proxy",
            "file_path": "/repo/proxy.py",
            "cfg_bits": [],
            "dfg_bits": [],
            "struct_bits": [],
            "axis_container_kinds_json": _kind_json(
                ContainerKindMatch(
                    kind="proxy_object",
                    symbol_uid="proxy",
                    qualified_name="pkg.proxy",
                    evidence_bits=(),
                    evidence_probes=("library_marker:proxy_object",),
                    payload={},
                )
            ),
        },
        workspace_id="ws",
    )

    jsonl_path, md_path = write_axis_contract_report([row], tmp_path)

    assert json.loads(jsonl_path.read_text(encoding="utf-8"))["uid"] == "proxy"
    markdown = md_path.read_text(encoding="utf-8")
    assert "proxy_object" in markdown
    assert "proxy_indirection" in markdown
    assert "| proxy | /repo/proxy.py | proxy_object | proxy_indirection | - | no |" in markdown
