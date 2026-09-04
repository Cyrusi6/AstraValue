from __future__ import annotations

from datetime import timedelta

import pytest

from analysis.governance.codex_runner import CodexReportDraft, DeterministicCodexRunner
from analysis.governance.codex_tools import (
    READ_ONLY_TOOL_NAMES,
    CodexToolService,
    GovernanceToolRegistry,
    InMemoryCodexSessionRecorder,
    ToolPayload,
    canonical_session_manifest_hash,
    make_mapping_tool,
)
from analysis.governance.models import (
    CodexJudgment,
    CompletenessStatus,
    FactualFinding,
    GovernanceReportSection,
    ReportGenerationStatus,
    SnapshotAnchorLink,
)
from analysis.governance.report_service import (
    GovernanceReportService,
    canonical_governance_report_hash,
)

from .codex_test_support import H1, NOW, make_input_pack, make_snapshot, make_tool_registry


def _draft_factory(
    citation_id: str = "citation:one",
    limitation: str | tuple[str, ...] | None = None,
):
    def factory(_pack, tools):
        tools.query("get_governance_overview", {})
        return CodexReportDraft(
            sections=(
                GovernanceReportSection(
                    section_id="management",
                    title="管理层与治理",
                    findings=(
                        FactualFinding(
                            finding_id="govfinding:fact",
                            text="正式披露支持一项任职事实",
                            citation_ids=(citation_id,),
                        ),
                        CodexJudgment(
                            finding_id="govfinding:integrity-judgment",
                            text="Codex 对管理层诚信和能力形成定性判断",
                            citation_ids=(citation_id,),
                            uncertainty="存在数据范围限制",
                        ),
                    ),
                ),
            ),
            data_limitations=(
                ()
                if limitation is None
                else ((limitation,) if isinstance(limitation, str) else limitation)
            ),
            citation_ids=(citation_id,),
        )

    return factory


def _run(snapshot, *, factory=None, registry=None, initial_citation_ids=()):
    registry = registry or make_tool_registry()
    pack = make_input_pack(snapshot, registry)
    recorder = InMemoryCodexSessionRecorder()
    runner = DeterministicCodexRunner(factory or _draft_factory())
    catalog = {snapshot.governance_snapshot_id: snapshot}
    service = GovernanceReportService(
        runner=runner,
        tool_service=CodexToolService(registry, recorder),
        recorder=recorder,
        snapshot_resolver=catalog.__getitem__,
    )
    result = service.generate(
        report_run_id="report-run:one",
        session_id="govsession:report",
        input_pack=pack,
        model_profile="offline-test",
        tool_protocol_version="1",
        started_at=NOW,
        completed_at=NOW,
        initial_citation_ids=initial_citation_ids,
    )
    return result, runner


@pytest.mark.parametrize(
    ("snapshot", "expected_limitation"),
    (
        (make_snapshot(), None),
        (
            make_snapshot(
                completeness=CompletenessStatus.INCOMPLETE,
                gap_ids=("govgap:one",),
            ),
            "snapshot_incomplete",
        ),
        (
            make_snapshot(
                completeness=CompletenessStatus.CONFLICTED,
                conflict_ids=("govconflict:one",),
            ),
            "snapshot_conflicted",
        ),
    ),
)
def test_A22_complete_incomplete_and_conflicted_all_finish_without_human_gate(
    snapshot, expected_limitation
) -> None:
    result, runner = _run(snapshot)
    assert result.report.generation_status == ReportGenerationStatus.COMPLETED
    assert result.report.decision_author == "codex"
    assert result.report.technical_validation.passed
    assert result.session_manifest.output_schema_validated
    assert result.report.canonical_report_hash != result.session_manifest.canonical_hash
    assert result.report.canonical_report_hash == canonical_governance_report_hash(
        result.report
    )
    assert result.session_manifest.canonical_hash == canonical_session_manifest_hash(
        result.session_manifest
    )
    assert result.report.session_manifest_hash == result.session_manifest.canonical_hash
    assert result.session_manifest.report_hash == result.report.canonical_report_hash
    assert runner.calls == 1
    if expected_limitation:
        assert expected_limitation in result.report.data_limitations


def test_A24_budget_exhausted_remains_a_soft_report_limitation() -> None:
    result, _ = _run(
        make_snapshot(
            completeness=CompletenessStatus.INCOMPLETE,
            gap_ids=("govgap:one",),
        ),
        factory=_draft_factory(
            limitation=("budget_exhausted", "coverage_no_data", "source_restricted_429")
        ),
    )
    assert result.report.generation_status == ReportGenerationStatus.COMPLETED
    assert "budget_exhausted" in result.report.data_limitations
    assert "coverage_no_data" in result.report.data_limitations
    assert "source_restricted_429" in result.report.data_limitations


def test_layering_judgment_no_fact_write_allows_qualitative_judgment_no_score() -> None:
    result, _ = _run(make_snapshot())
    kinds = [
        finding.kind
        for section in result.report.sections
        for finding in section.findings
    ]
    assert kinds == ["fact", "judgment"]
    assert not hasattr(result.report, "governance_score")
    assert not hasattr(result.report, "management_integrity")
    assert not hasattr(result.report, "management_ability")


def test_A23_future_leakage_blocks_completed_report_and_preserves_failure_manifest() -> None:
    snapshot = make_snapshot()
    future_anchor = SnapshotAnchorLink(
        anchor_link_id="govanchor:future",
        governance_snapshot_id=snapshot.governance_snapshot_id,
        question_id="GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",
        state_kind="roster_snapshot",
        anchor_record_id="govrec:future",
        reference_at=NOW,
        available_at=NOW + timedelta(days=1),
        canonical_hash=H1,
    )
    snapshot = snapshot.model_copy(update={"anchor_links": (future_anchor,)})
    result, runner = _run(snapshot)
    assert result.report.generation_status == ReportGenerationStatus.FAILED
    assert result.report.failure_code == "input_integrity_failure"
    assert "future_firewall" in result.report.technical_validation.hard_failure_codes
    assert result.session_manifest.generation_status == ReportGenerationStatus.FAILED
    assert runner.calls == 0


def test_unknown_citation_is_a_hard_lineage_failure() -> None:
    result, runner = _run(
        make_snapshot(), factory=_draft_factory(citation_id="citation:not-read")
    )
    assert runner.calls == 1
    assert result.report.generation_status == ReportGenerationStatus.FAILED
    assert "citation_lineage" in result.report.technical_validation.hard_failure_codes
    assert result.report.sections == ()
    assert result.report.citation_ids == ()


def test_caller_supplied_initial_citation_cannot_bypass_actual_read_lineage() -> None:
    injected = "citation:not-read"
    result, _ = _run(
        make_snapshot(),
        factory=_draft_factory(citation_id=injected),
        initial_citation_ids=(injected,),
    )
    assert result.report.generation_status == ReportGenerationStatus.FAILED
    assert "citation_lineage" in result.report.technical_validation.hard_failure_codes
    assert result.report.sections == ()


def test_future_tool_payload_is_a_hard_failure_and_never_enters_report() -> None:
    def future_handler(_snapshot_id, _parameters):
        return ToolPayload(
            payload={
                "kind": "test_governance_payload",
                "object_id": "govrec:future",
                "schema_version": "1.0.0",
                "canonical_hash": H1,
                "text": "later correction changes the result",
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
        tuple(make_mapping_tool(name, future_handler) for name in READ_ONLY_TOOL_NAMES)
    )
    result, runner = _run(make_snapshot(), registry=registry)
    assert runner.calls == 1
    assert result.report.generation_status == ReportGenerationStatus.FAILED
    assert result.report.failure_code == "tool_integrity_failure"
    assert "future_leakage" in result.report.technical_validation.hard_failure_codes
    assert result.report.sections == ()
    assert result.report.citation_ids == ()
