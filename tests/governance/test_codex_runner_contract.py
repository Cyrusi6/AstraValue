from __future__ import annotations

from analysis.governance.codex_runner import (
    CodexReportDraft,
    CodexRuntimeTools,
    DeterministicCodexRunner,
    FakeResearchTaskBroker,
)
from analysis.governance.codex_tools import CodexToolService, InMemoryCodexSessionRecorder
from analysis.governance.models import (
    CodexJudgment,
    FactualFinding,
    GovernanceReportSection,
)

from .codex_test_support import NOW, make_input_pack, make_snapshot, make_tool_registry


def test_deterministic_codex_runner_proves_protocol_only() -> None:
    registry = make_tool_registry()
    pack = make_input_pack(make_snapshot(), registry)
    recorder = InMemoryCodexSessionRecorder()
    recorder.start(
        session_id="govsession:one",
        parent_report_run_id="run:one",
        model_profile="offline",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack=pack,
        started_at=NOW,
    )

    def draft_factory(input_pack, tools: CodexRuntimeTools):
        response = tools.query("get_governance_overview", {})
        assert response["governance_snapshot_id"] == input_pack.governance_snapshot_id
        return CodexReportDraft(
            sections=(
                GovernanceReportSection(
                    section_id="governance",
                    title="治理事实与判断",
                    findings=(
                        FactualFinding(
                            finding_id="govfinding:fact",
                            text="存在一项正式披露记录",
                            citation_ids=("citation:one",),
                        ),
                        CodexJudgment(
                            finding_id="govfinding:judgment",
                            text="Codex 可独立形成定性判断",
                            citation_ids=("citation:one",),
                            uncertainty="仅使用当前证据范围",
                        ),
                    ),
                ),
            ),
            citation_ids=("citation:one",),
        )

    runner = DeterministicCodexRunner(draft_factory)
    outcome = runner.run(
        input_pack=pack,
        tool_service=CodexToolService(registry, recorder),
        session_id="govsession:one",
    )
    assert outcome.protocol_status == "protocol_passed"
    assert outcome.real_codex_verified is False
    assert outcome.network_verified is False
    assert outcome.draft.sections[0].findings[1].kind == "judgment"
    assert not hasattr(outcome.draft, "governance_score")


def test_runtime_tools_expose_no_candidate_approval_or_fact_write() -> None:
    tools = CodexRuntimeTools(query_callback=lambda *_: {})
    assert not hasattr(tools, "approve_candidate")
    assert not hasattr(tools, "write_governance_claim")
    assert not hasattr(tools, "adopt_snapshot")


def test_fake_research_broker_never_claims_real_codex_or_network() -> None:
    broker = FakeResearchTaskBroker({})
    assert broker.protocol_status == "protocol_passed"
    assert broker.real_codex_verified is False
    assert broker.network_verified is False
