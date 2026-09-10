from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pytest

from analysis.acquisition.models import PhysicalQueryCoverageLink
from analysis.acquisition.repository import AcquisitionNotFoundError
from analysis.structured.coverage import (
    Applicability,
    CoverageState,
    GroupMode,
    InputReadiness,
    RequirementInput,
    aggregate_coverage,
    evaluate_question,
    expand_analysis_scope,
)
from analysis.structured.storage import (
    StructuredRunContext,
    StructuredStorage,
    canonical_json,
    ensure_structured_storage,
)


def _context(store, *, run=None, requirement_version="1.0.0", requirement_hash=None):
    run = run or store.run
    return StructuredRunContext(
        run_id=run.run_id,
        storage_namespace_id=store.namespace.namespace_id,
        ticker=run.ticker,
        company_id=f"company:{run.ticker}",
        dataset_registry_id="structured-datasets",
        dataset_registry_version="1.0.0",
        dataset_registry_hash="a" * 64,
        field_registry_id="structured-fields",
        field_registry_version="1.0.0",
        field_registry_hash="b" * 64,
        requirement_set_id="eight-step-requirements",
        requirement_set_version=requirement_version,
        requirement_set_hash=requirement_hash or "c" * 64,
        query_pack_id="structured-query-pack",
        query_pack_version="1.0.0",
        query_pack_hash="d" * 64,
        source_registry_id=run.registry_id,
        source_registry_version=run.registry_version,
        source_registry_hash=run.registry_content_hash,
        peer_set_id="liquor-seven",
        peer_set_version="1.0.0",
        peer_set_hash="e" * 64,
        schedule_id="structured-schedule",
        schedule_version="1.0.0",
        schedule_hash="f" * 64,
        policy_version="structured-first-v1",
        frozen_config={"requirement_version": requirement_version},
        created_at=store.now,
    )


def _persist_context(store, storage, *, run=None, context=None, suffix=""):
    run = run or store.run
    context = context or _context(store, run=run)
    if run.run_id == store.run.run_id:
        plan = store.plan_item
        coverage = store.coverage_entries
        links = store.links
    else:
        plan = store.plan_item.model_copy(
            update={
                "run_id": run.run_id,
                "plan_item_id": f"plan-coverage{suffix}",
            }
        )
        coverage = tuple(
            item.model_copy(
                update={
                    "run_id": run.run_id,
                    "coverage_entry_id": f"{item.coverage_entry_id}-coverage{suffix}",
                }
            )
            for item in store.coverage_entries
        )
        links = tuple(
            PhysicalQueryCoverageLink(
                plan_item_id=plan.plan_item_id,
                coverage_entry_id=item.coverage_entry_id,
            )
            for item in coverage
        )
    job = {
        "job_id": f"job-coverage{suffix}",
        "run_id": run.run_id,
        "plan_item_id": plan.plan_item_id,
        "storage_namespace_id": context.storage_namespace_id,
        "company_id": context.company_id,
        "ticker": context.ticker,
        "dataset_id": "coverage-inputs",
        "source_definition_id": plan.source_definition_id,
        "source_definition_version": plan.source_definition_version,
        "purpose": "full_history",
        "schedule_mode": "all_available",
        "scope_key": f"scope-coverage{suffix}",
        "dedupe_key": hashlib.sha256(f"coverage{suffix}".encode()).hexdigest(),
        "time_start": plan.time_start.isoformat(),
        "time_end": plan.time_end.isoformat(),
        "max_attempts": 2,
        "ordinal": 0,
        "created_at": store.now.isoformat(),
    }
    storage.persist_plan_bundle(
        store.repository,
        run=run,
        context=context,
        plan_items=(plan,),
        coverage_entries=coverage,
        links=links,
        jobs=(job,),
    )
    return context


def _snapshot(context, *, snapshot_id, industry_version="1.0.0", industry_hash=None):
    return {
        "coverage_snapshot_id": snapshot_id,
        "run_id": context.run_id,
        "storage_namespace_id": context.storage_namespace_id,
        "company_id": context.company_id,
        "ticker": context.ticker,
        "requirement_set_id": context.requirement_set_id,
        "requirement_set_version": context.requirement_set_version,
        "requirement_set_hash": context.requirement_set_hash,
        "field_registry_version": context.field_registry_version,
        "industry_profile_id": "general",
        "industry_profile_version": industry_version,
        "industry_profile_hash": industry_hash or "1" * 64,
        "peer_set_id": context.peer_set_id,
        "peer_set_version": context.peer_set_version,
        "method_version": "eight-step-v1",
        "as_of": datetime(2026, 9, 8, 12, tzinfo=timezone.utc).isoformat(),
        "analysis_scope": {"raw_snapshot_ids": ["shared-raw-snapshot"]},
        "evaluated_at": datetime(2026, 9, 8, 13, tzinfo=timezone.utc).isoformat(),
    }


def _evaluation(context, snapshot_id, index, *, readiness="ready"):
    requirement_id = f"REQ.{index:03d}"
    return {
        "evaluation_id": f"evaluation-{snapshot_id}-{index:03d}",
        "coverage_snapshot_id": snapshot_id,
        "company_id": context.company_id,
        "step_id": "ES01",
        "question_id": f"ES01.Q{index:03d}",
        "requirement_id": requirement_id,
        "period_key": "NOW",
        "applicability": "true",
        "readiness": readiness,
        "required_group_id": "default",
        "optional": False,
        "reason_code": None if readiness == "ready" else readiness,
        "evaluated_at": datetime(2026, 9, 8, 13, tzinfo=timezone.utc).isoformat(),
    }


def test_zero_false_and_empty_values_keep_distinct_readiness_semantics():
    zero = RequirementInput(
        "revenue",
        "2025Q4",
        InputReadiness.READY,
        value_present=True,
        value=0,
    )
    false_value = RequirementInput(
        "has_pledge",
        "NOW",
        InputReadiness.READY,
        value_present=True,
        value=False,
    )
    empty = RequirementInput(
        "margin",
        "2025Q4",
        InputReadiness.SOURCE_EMPTY,
        reason_code="valid_empty_response",
    )
    ready = evaluate_question(
        step_id="ES01",
        question_id="ES01.Q01",
        applicability=Applicability.TRUE,
        inputs=(zero, false_value),
    )
    pending = evaluate_question(
        step_id="ES02",
        question_id="ES02.Q01",
        applicability=Applicability.TRUE,
        inputs=(empty,),
    )
    assert ready.state == CoverageState.READY
    assert pending.state == CoverageState.PENDING
    assert pending.reason_codes == ("valid_empty_response",)


def test_three_value_applicability_required_groups_and_optional_inputs():
    absent_by_rule = RequirementInput(
        "lawsuit_item",
        "NOW",
        InputReadiness.NOT_DISCLOSED,
        applicability=Applicability.FALSE,
        evidence_ids=("rule:no-lawsuit",),
    )
    alternative_ready = RequirementInput(
        "route_a",
        "NOW",
        InputReadiness.READY,
        required_group_id="equivalent-route",
        group_mode=GroupMode.ANY_OF,
        value_present=True,
        value=12,
    )
    alternative_missing = RequirementInput(
        "route_b",
        "NOW",
        InputReadiness.UNMAPPED,
        required_group_id="equivalent-route",
        group_mode=GroupMode.ANY_OF,
    )
    optional_missing = RequirementInput(
        "optional_enrichment",
        "NOW",
        InputReadiness.SOURCE_FAILED,
        optional=True,
    )
    unknown = RequirementInput(
        "industry_condition",
        "NOW",
        InputReadiness.DEFINITION_UNKNOWN,
        applicability=Applicability.UNKNOWN,
    )

    ready = evaluate_question(
        step_id="ES03",
        question_id="ES03.Q01",
        applicability=Applicability.TRUE,
        inputs=(absent_by_rule, alternative_ready, alternative_missing, optional_missing),
    )
    uncertain = evaluate_question(
        step_id="ES04",
        question_id="ES04.Q01",
        applicability=Applicability.TRUE,
        inputs=(unknown,),
    )
    not_applicable = evaluate_question(
        step_id="ES05",
        question_id="ES05.Q01",
        applicability=Applicability.FALSE,
        applicability_evidence_ids=("industry-rule:bank-only",),
        inputs=(unknown,),
    )

    assert ready.state == CoverageState.READY
    assert ready.missing_requirement_ids == ()
    assert ready.optional_missing_ids == ("optional_enrichment",)
    assert uncertain.state == CoverageState.PENDING
    assert uncertain.applicability == Applicability.TRUE
    assert uncertain.reason_codes == (InputReadiness.APPLICABILITY_UNKNOWN.value,)
    assert not_applicable.state == CoverageState.NOT_APPLICABLE

    summary = aggregate_coverage(
        (ready, uncertain, not_applicable),
        all_step_ids=("ES03", "ES04", "ES05", "ES06"),
    )
    assert summary.ready_question_count == 1
    assert summary.applicable_or_unknown_question_count == 2
    assert summary.readiness_ratio == 0.5
    assert summary.steps[-1].state == CoverageState.PENDING

    zero_denominator = aggregate_coverage(
        (not_applicable,), all_step_ids=("ES05",)
    )
    assert zero_denominator.readiness_ratio is None


def test_analysis_scope_excludes_unpublished_and_prelisting_but_keeps_history_contract():
    quarters = tuple(
        f"{year}Q{quarter}"
        for year in range(2022, 2027)
        for quarter in range(1, 5)
    )
    scope = expand_analysis_scope(
        as_of=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        published_years=(str(year) for year in range(2018, 2026)),
        published_quarters=quarters,
        published_months=(f"{year}-{month:02d}" for year in range(2021, 2027) for month in range(1, 13)),
        listing_date=date(2024, 7, 1),
        user_start=date(2025, 1, 1),
        user_end=date(2026, 12, 31),
        incomplete_event_ids=("pledge:1", "pledge:1", "buyback:2"),
        scenario_periods=("base", "downside"),
    )

    assert scope.acquisition_history_mode == "all_available"
    assert scope.financial_years == ("Y2025",)
    assert scope.financial_quarters == (
        "2025Q1",
        "2025Q2",
        "2025Q3",
        "2025Q4",
        "2026Q1",
        "2026Q2",
    )
    assert "2026Q3" not in scope.financial_quarters
    assert scope.event_start == date(2025, 1, 1)
    assert scope.incomplete_event_ids == ("pledge:1", "buyback:2")
    assert "2024Q2" in scope.excluded_pre_listing_periods

    missing_history = expand_analysis_scope(
        as_of=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        published_years=("2025",),
        published_quarters=("2025Q4", "2026Q2", "2026Q3"),
    )
    assert missing_history.financial_quarters == ("2025Q4", "2026Q2")
    assert {"2025Q3", "2026Q1"}.issubset(
        set(missing_history.missing_dependency_periods)
    )


def test_coverage_snapshot_is_atomic_untruncated_and_version_recomputation_is_immutable(
    acquisition_store,
):
    store = acquisition_store
    ensure_structured_storage(
        store.db_path, store.data_root, store.namespace.namespace_id
    )
    storage = StructuredStorage(store.db_path, store.namespace.namespace_id)
    context_v1 = _persist_context(store, storage, suffix="-v1")
    snapshot_v1 = _snapshot(context_v1, snapshot_id="coverage-v1")
    evaluations_v1 = tuple(
        _evaluation(
            context_v1,
            "coverage-v1",
            index,
            readiness="source_failed" if index == 500 else "ready",
        )
        for index in range(501)
    )

    bad = list(evaluations_v1)
    bad[-1] = {**bad[-1], "company_id": "company:wrong"}
    with pytest.raises(Exception, match="another company"):
        storage.save_research_coverage_snapshot(snapshot_v1, bad)
    with pytest.raises(AcquisitionNotFoundError):
        storage.get_research_coverage_snapshot("coverage-v1")

    hash_v1 = storage.save_research_coverage_snapshot(snapshot_v1, evaluations_v1)
    assert storage.save_research_coverage_snapshot(snapshot_v1, evaluations_v1) == hash_v1
    before = storage.get_research_coverage_snapshot("coverage-v1")
    before_bytes = canonical_json(before).encode("utf-8")
    loaded = storage.list_requirement_evaluations("coverage-v1", limit=None)
    assert len(loaded) == 501
    assert loaded[-1]["requirement_id"] == "REQ.500"
    assert loaded[-1]["readiness"] == "source_failed"

    run_v2 = store.run.model_copy(update={"run_id": "run-coverage-v2"})
    context_v2 = _context(
        store,
        run=run_v2,
        requirement_version="2.0.0",
        requirement_hash="7" * 64,
    )
    _persist_context(
        store,
        storage,
        run=run_v2,
        context=context_v2,
        suffix="-v2",
    )
    snapshot_v2 = _snapshot(
        context_v2,
        snapshot_id="coverage-v2",
        industry_version="2.0.0",
        industry_hash="8" * 64,
    )
    evaluation_v2 = (_evaluation(context_v2, "coverage-v2", 0),)
    hash_v2 = storage.save_research_coverage_snapshot(snapshot_v2, evaluation_v2)

    assert hash_v2 != hash_v1
    assert snapshot_v2["analysis_scope"] == snapshot_v1["analysis_scope"]
    assert canonical_json(
        storage.get_research_coverage_snapshot("coverage-v1")
    ).encode("utf-8") == before_bytes
