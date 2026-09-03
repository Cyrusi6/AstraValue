from __future__ import annotations

from dataclasses import fields
from datetime import timedelta

import pytest

from analysis.governance.codex_tools import (
    READ_ONLY_TOOL_NAMES,
    CodexToolError,
    CodexToolService,
    GovernanceTool,
    GovernanceToolRegistry,
    InMemoryCodexSessionRecorder,
    ToolPayload,
    canonical_session_manifest_hash,
    make_mapping_tool,
)
from analysis.governance.models import CompletenessStatus, ReportGenerationStatus

from .codex_test_support import H1, H2, NOW, make_input_pack, make_snapshot, make_tool_registry


def _start(recorder: InMemoryCodexSessionRecorder, input_pack) -> None:
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="report-run:one",
        model_profile="offline-test",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=input_pack,
        started_at=NOW,
    )


def test_A17_compact_pack_contains_all_active_ids_and_no_fulltext() -> None:
    snapshot = make_snapshot(
        completeness=CompletenessStatus.CONFLICTED,
        gap_ids=("govgap:one", "govgap:two"),
        conflict_ids=("govconflict:one",),
        candidate_ids=("govclaim:candidate",),
    )
    registry = make_tool_registry()
    pack = make_input_pack(snapshot, registry)
    assert len(pack.question_summaries) == 11
    assert pack.active_gap_ids == ("govgap:one", "govgap:two")
    assert pack.active_conflict_ids == ("govconflict:one",)
    assert pack.pending_candidate_ids == ("govclaim:candidate",)
    assert tuple(item.tool_name for item in pack.tool_schemas) == READ_ONLY_TOOL_NAMES
    serialized = pack.canonical_bytes().decode("utf-8")
    assert "fulltext" not in serialized.lower()
    assert "chain_of_thought" not in serialized.lower()
    assert pack == make_input_pack(snapshot, registry)


def test_readonly_registry_rejects_unapproved_or_mutating_tools() -> None:
    registry = make_tool_registry()
    assert registry.names == READ_ONLY_TOOL_NAMES
    assert all(item.read_only for item in registry.schema_references())
    with pytest.raises(ValueError, match="unapproved"):
        make_mapping_tool("approve_candidate", lambda *_: ToolPayload(payload={}))


def test_snapshot_bound_tool_records_actual_reads_before_return() -> None:
    snapshot = make_snapshot()
    registry = make_tool_registry()
    pack = make_input_pack(snapshot, registry)
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, pack)
    service = CodexToolService(registry, recorder)
    response = service.invoke(
        session_id="govsession:one",
        tool_name="query_roles_and_people",
        parameters={"role_code": "director"},
        occurred_at=NOW,
    )
    assert response["governance_snapshot_id"] == snapshot.governance_snapshot_id
    assert response["governance_snapshot_hash"] == snapshot.canonical_snapshot_hash
    reads = recorder.tool_reads("govsession:one")
    assert len(reads) == 1
    assert reads[0].actual_record_ids == ("govrec:one",)
    assert reads[0].actual_claim_ids == ("govclaim:one",)
    assert reads[0].actual_evidence_span_ids == ("govspan:one",)
    assert reads[0].citation_ids == ("citation:one",)
    assert reads[0].response_hash is not None


def test_tool_cannot_switch_snapshot_or_accept_secrets() -> None:
    registry = make_tool_registry()
    pack = make_input_pack(make_snapshot(), registry)
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, pack)
    service = CodexToolService(registry, recorder)
    with pytest.raises(CodexToolError) as mismatch:
        service.invoke(
            session_id="govsession:one",
            tool_name="get_governance_overview",
            parameters={"governance_snapshot_id": "govsnapshot:other"},
            occurred_at=NOW,
        )
    assert mismatch.value.code == "snapshot_binding_mismatch"
    with pytest.raises(CodexToolError) as sensitive:
        service.invoke(
            session_id="govsession:one",
            tool_name="get_governance_overview",
            parameters={"token": "should-never-be-recorded"},
            occurred_at=NOW,
        )
    assert sensitive.value.code == "sensitive_parameter"
    reads = recorder.tool_reads("govsession:one")
    assert [item.status.value for item in reads] == ["rejected", "rejected"]
    assert [item.error_code for item in reads] == [
        "snapshot_binding_mismatch",
        "sensitive_parameter",
    ]
    assert "should-never-be-recorded" not in "".join(
        item.canonical_parameters_json for item in reads
    )


def test_persist_before_return_failure_prevents_payload_disclosure() -> None:
    class FailingRecorder(InMemoryCodexSessionRecorder):
        def append_tool_read(self, read):
            raise OSError("simulated persistence failure")

    registry = make_tool_registry()
    pack = make_input_pack(make_snapshot(), registry)
    recorder = FailingRecorder()
    _start(recorder, pack)
    service = CodexToolService(registry, recorder)
    with pytest.raises(OSError, match="persistence"):
        service.invoke(
            session_id="govsession:one",
            tool_name="get_record_lineage",
            parameters={"record_id": "govrec:one"},
            occurred_at=NOW,
        )


def test_A21_session_manifest_actual_reads_and_redaction_not_hidden_reasoning() -> None:
    registry = make_tool_registry()
    pack = make_input_pack(make_snapshot(), registry)
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, pack)
    CodexToolService(registry, recorder).invoke(
        session_id="govsession:one",
        tool_name="get_evidence_excerpt",
        parameters={"evidence_span_id": "govspan:one"},
        occurred_at=NOW,
    )
    manifest = recorder.manifest(
        "govsession:one",
        generation_status=ReportGenerationStatus.RUNNING,
    )
    assert manifest.tool_read_ids
    assert "chain_of_thought" not in type(manifest).model_fields
    assert "credentials" not in type(manifest).model_fields
    assert "browser_profile" not in type(manifest).model_fields


def test_tool_input_and_output_json_schemas_fail_closed() -> None:
    output_schema = {
        "type": "object",
        "required": ["kind", "object_id", "schema_version", "canonical_hash"],
        "properties": {
            "kind": {"const": "test_payload"},
            "object_id": {"type": "string"},
            "schema_version": {"type": "string"},
            "canonical_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "additionalProperties": False,
    }
    tool = GovernanceTool(
        name="get_governance_overview",
        version="1.0.0",
        input_schema={
            "type": "object",
            "required": ["question_id"],
            "properties": {"question_id": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema=output_schema,
        handler=lambda *_: ToolPayload(
            payload={
                "kind": "wrong_kind",
                "object_id": "govrec:one",
                "schema_version": "1.0.0",
                "canonical_hash": H1,
            },
            actual_record_ids=("govrec:one",),
        ),
    )
    registry = GovernanceToolRegistry((tool,))
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, make_input_pack(make_snapshot(), registry))
    service = CodexToolService(registry, recorder)
    with pytest.raises(CodexToolError) as bad_input:
        service.invoke(
            session_id="govsession:one",
            tool_name=tool.name,
            parameters={"unexpected": True},
            occurred_at=NOW,
        )
    assert bad_input.value.code == "invalid_parameters"
    with pytest.raises(CodexToolError) as bad_output:
        service.invoke(
            session_id="govsession:one",
            tool_name=tool.name,
            parameters={"question_id": "GOV.Q01"},
            occurred_at=NOW,
        )
    assert bad_output.value.code == "tool_output_schema_invalid"
    assert [item.status.value for item in recorder.tool_reads("govsession:one")] == [
        "rejected",
        "failed",
    ]


def test_future_or_unproven_substantive_payload_never_returns_to_codex() -> None:
    def future_handler(_snapshot_id, _parameters):
        return ToolPayload(
            payload={
                "kind": "test_payload",
                "object_id": "govrec:future",
                "schema_version": "1.0.0",
                "canonical_hash": H1,
                "text": "future correction changes the conclusion",
                "available_at": NOW + timedelta(days=1),
                "llm_allowed": True,
                "integrity_verified": True,
                "lineage_complete": True,
                "evidence_span_id": "govspan:future",
            },
            actual_record_ids=("govrec:future",),
            actual_evidence_span_ids=("govspan:future",),
            citation_ids=("citation:future",),
        )

    registry = GovernanceToolRegistry(
        (make_mapping_tool("get_governance_overview", future_handler),)
    )
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, make_input_pack(make_snapshot(), registry))
    with pytest.raises(CodexToolError) as error:
        CodexToolService(registry, recorder).invoke(
            session_id="govsession:one",
            tool_name="get_governance_overview",
            parameters={},
            occurred_at=NOW,
        )
    assert error.value.code == "future_leakage"
    read = recorder.tool_reads("govsession:one")[0]
    assert read.status.value == "failed"
    assert read.response_payload_json is None
    assert "future correction" not in read.canonical_parameters_json


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    (
        ({"available_at": None}, "invalid_time"),
        ({"llm_allowed": False}, "llm_policy_denied"),
        ({"integrity_verified": False}, "hash_mismatch"),
        ({"lineage_complete": False}, "broken_lineage"),
        ({"evidence_span_id": "govspan:not-read"}, "broken_lineage"),
        ({"governance_snapshot_id": "govsnapshot:other"}, "snapshot_binding_mismatch"),
        ({"governance_snapshot_hash": H2}, "snapshot_hash_mismatch"),
    ),
)
def test_substantive_tool_payload_requires_policy_time_hash_span_and_snapshot_binding(
    overrides, expected_code
) -> None:
    def handler(_snapshot_id, _parameters):
        body = {
            "kind": "test_payload",
            "object_id": "govrec:one",
            "schema_version": "1.0.0",
            "canonical_hash": H1,
            "text": "authorized excerpt",
            "available_at": NOW,
            "llm_allowed": True,
            "integrity_verified": True,
            "lineage_complete": True,
            "evidence_span_id": "govspan:one",
        }
        body.update(overrides)
        return ToolPayload(
            payload=body,
            actual_record_ids=("govrec:one",),
            actual_evidence_span_ids=("govspan:one",),
            citation_ids=("citation:one",),
        )

    registry = GovernanceToolRegistry(
        (make_mapping_tool("get_governance_overview", handler),)
    )
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, make_input_pack(make_snapshot(), registry))
    with pytest.raises(CodexToolError) as error:
        CodexToolService(registry, recorder).invoke(
            session_id="govsession:one",
            tool_name="get_governance_overview",
            parameters={},
            occurred_at=NOW,
        )
    assert error.value.code == expected_code
    assert recorder.tool_reads("govsession:one")[0].response_payload_json is None


def test_evidence_excerpt_is_span_only() -> None:
    registry = make_tool_registry()
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, make_input_pack(make_snapshot(), registry))
    with pytest.raises(CodexToolError) as error:
        CodexToolService(registry, recorder).invoke(
            session_id="govsession:one",
            tool_name="get_evidence_excerpt",
            parameters={
                "evidence_span_id": "govspan:one",
                "record_id": "govrec:one",
            },
            occurred_at=NOW,
        )
    assert error.value.code == "span_only_violation"


def test_finalized_session_manifest_is_content_hashed_and_rejects_append() -> None:
    registry = make_tool_registry()
    pack = make_input_pack(make_snapshot(), registry)
    recorder = InMemoryCodexSessionRecorder()
    _start(recorder, pack)
    final = recorder.finalize_session(
        "govsession:one",
        generation_status=ReportGenerationStatus.COMPLETED,
        final_citation_ids=(),
        report_hash=H1,
        output_schema_validated=True,
        failure_code=None,
        completed_at=NOW,
    )
    assert final.canonical_hash == canonical_session_manifest_hash(final)
    assert recorder.manifest("govsession:one") is final
    with pytest.raises(CodexToolError) as error:
        CodexToolService(registry, recorder).invoke(
            session_id="govsession:one",
            tool_name="get_governance_overview",
            parameters={},
            occurred_at=NOW,
        )
    assert error.value.code == "session_finalized"
    with pytest.raises(CodexToolError, match="immutable"):
        recorder.append_research_result(
            "govsession:one", "govresearchtask:late", "govresearchbundle:late"
        )
