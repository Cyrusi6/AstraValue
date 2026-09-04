from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from analysis.governance.codex_tools import (
    READ_ONLY_TOOL_NAMES,
    CodexInputPackBuilder,
    GovernanceToolRegistry,
    ToolPayload,
    make_mapping_tool,
)
from analysis.governance.models import (
    BudgetUsage,
    CompletenessStatus,
    GovernancePerspective,
    GovernanceQuestionCoverageLink,
    GovernanceSnapshot,
    ResearchBudget,
    ResearchResultBundle,
    ResearchTaskStatus,
    TimePrecision,
)
from analysis.governance.registry import EXACT_QUESTION_IDS
from analysis.governance.research_gate import seal_research_result_bundle


H1 = "1" * 64
H2 = "2" * 64
NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_snapshot(
    *,
    snapshot_id: str = "govsnapshot:one",
    snapshot_hash: str = H1,
    completeness: CompletenessStatus = CompletenessStatus.COMPLETE,
    gap_ids: tuple[str, ...] = (),
    conflict_ids: tuple[str, ...] = (),
    candidate_ids: tuple[str, ...] = (),
    perspective: GovernancePerspective = GovernancePerspective.STRICT,
    state_at: datetime = NOW,
    known_at: datetime = NOW,
    supersedes_snapshot_id: str | None = None,
) -> GovernanceSnapshot:
    coverage_status = (
        CompletenessStatus.INCOMPLETE
        if completeness != CompletenessStatus.COMPLETE
        else CompletenessStatus.COMPLETE
    )
    return GovernanceSnapshot(
        governance_snapshot_id=snapshot_id,
        company_id="cn-600519",
        state_at=state_at,
        known_at=known_at,
        state_time_precision=TimePrecision.DATETIME,
        known_time_precision=TimePrecision.DATETIME,
        perspective=perspective,
        question_set_version="1.0.0",
        source_registry_version="sources:1",
        query_pack_version="queries:1",
        extractor_versions=("tables:1",),
        reconstruction_version="reconstruction:1",
        evidence_manifest_id="shared-manifest:one",
        evidence_manifest_hash=H2,
        canonical_record_ids=(),
        active_gap_ids=gap_ids,
        active_conflict_ids=conflict_ids,
        pending_candidate_ids=candidate_ids,
        question_level_coverage=tuple(
            GovernanceQuestionCoverageLink(
                question_id=question_id,
                coverage_entry_ids=(f"shared-coverage:{index:02d}",),
                completeness_status=coverage_status,
            )
            for index, question_id in enumerate(EXACT_QUESTION_IDS, 1)
        ),
        completeness_status=completeness,
        future_knowledge_used=(
            perspective == GovernancePerspective.RECONSTRUCTED
            and known_at > state_at
        ),
        supersedes_snapshot_id=supersedes_snapshot_id,
        canonical_snapshot_hash=snapshot_hash,
        created_at=NOW,
    )


def make_tool_registry() -> GovernanceToolRegistry:
    def handler(snapshot_id: str, parameters: dict[str, Any]) -> ToolPayload:
        return ToolPayload(
            payload={
                "kind": "test_governance_payload",
                "object_id": "govrec:one",
                "schema_version": "1.0.0",
                "canonical_hash": H1,
                "requested": dict(parameters),
            },
            actual_record_ids=("govrec:one",),
            actual_claim_ids=("govclaim:one",),
            actual_evidence_span_ids=("govspan:one",),
            actual_raw_snapshot_ids=("shared-raw:one",),
            citation_ids=("citation:one",),
        )

    return GovernanceToolRegistry(
        [make_mapping_tool(name, handler) for name in READ_ONLY_TOOL_NAMES]
    )


def make_budget() -> ResearchBudget:
    return ResearchBudget(
        max_rounds=2,
        max_child_tasks=2,
        max_network_requests=4,
        max_parallelism=1,
        wall_clock_seconds=30,
        max_output_bytes=100_000,
    )


def make_input_pack(snapshot: GovernanceSnapshot, registry: GovernanceToolRegistry):
    return CodexInputPackBuilder(registry).build(
        snapshot=snapshot,
        question_descriptions={
            question_id: f"{question_id} compact status" for question_id in EXACT_QUESTION_IDS
        },
        important_records=(),
        important_event_ids=(),
        research_budget=make_budget(),
        report_output_schema_hash=H1,
        created_at=NOW,
    )


def make_terminal_bundle(
    task_id: str,
    *,
    status: ResearchTaskStatus = ResearchTaskStatus.COMPLETED,
    failure_reason: str | None = None,
) -> ResearchResultBundle:
    bundle = ResearchResultBundle(
        research_result_bundle_id="govresearchbundle:one",
        research_task_id=task_id,
        task_status=status,
        budget_used=BudgetUsage(
            rounds=1,
            child_tasks=1,
            network_requests=1,
            wall_clock_milliseconds=10,
            output_bytes=100,
        ),
        failure_reason=failure_reason,
        created_at=NOW,
        canonical_hash="0" * 64,
    )
    return seal_research_result_bundle(bundle)
