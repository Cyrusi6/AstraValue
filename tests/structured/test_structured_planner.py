from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis.structured.planner import (
    CompanyPlanTarget,
    DatasetWork,
    HistoryMode,
    PlanDisposition,
    StructuredDatasetPlanner,
    StructuredPlanningError,
)
from analysis.structured.storage import (
    StructuredRunContext,
    StructuredStorage,
    ensure_structured_storage,
)


ROOT = Path(__file__).resolve().parents[2]


def _registry(name: str):
    return json.loads(
        (ROOT / "config" / "structured_data" / name).read_text(encoding="utf-8")
    )


def _seven_company_targets() -> tuple[CompanyPlanTarget, ...]:
    peer_set = _registry("peer_sets.v1.json")["peer_sets"][0]
    return tuple(
        CompanyPlanTarget(
            company_id=f"company:{item['canonical_ticker']}",
            ticker=item["canonical_ticker"],
            listing_date=date(2001, 1, 1),
            role=item["role"],
            selection_reason=item["inclusion_basis"],
        )
        for item in peer_set["companies"]
    )


def _context(store, *, run=None, policy_version: str = "structured-first-v1"):
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
        requirement_set_version="1.0.0",
        requirement_set_hash="c" * 64,
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
        policy_version=policy_version,
        frozen_config={"display_window_years": 5},
        created_at=store.now,
    )


def test_zero_network_registry_plan_covers_seven_by_fifty_five_without_display_truncation():
    datasets = _registry("datasets.v1.json")["datasets"]
    targets = _seven_company_targets()
    as_of = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

    works = StructuredDatasetPlanner().plan_history(
        companies=targets,
        datasets=datasets,
        as_of=as_of,
    )

    assert len(targets) == 7
    assert len(datasets) == 55
    assert len(works) > 7 * 55
    assert {(item.company_id, item.dataset_id) for item in works} == {
        (company.company_id, dataset["dataset_id"])
        for company in targets
        for dataset in datasets
    }

    by_dataset = {item["dataset_id"]: item for item in datasets}
    assert sum(item.purpose == "company_type" for item in works) == 7
    for work in works:
        dataset = by_dataset[work.dataset_id]
        if work.purpose == "company_type":
            assert work.history_mode == HistoryMode.REPORT_CATALOG
            continue
        if dataset["history_mode"] == "snapshot_from_first_retrieval":
            assert work.history_mode == HistoryMode.CURRENT_SNAPSHOT
            assert work.purpose == "current_snapshot"
            assert work.time_start == as_of
        elif dataset["history_mode"] == "on_demand_all_available_history":
            assert work.history_mode == HistoryMode.ON_DEMAND
            assert work.disposition == PlanDisposition.INACTIVE_ON_DEMAND
            assert work.purpose == "no_io"
        elif dataset["history_enumeration"] == "financial_date_catalog":
            assert work.history_mode == HistoryMode.REPORT_CATALOG
            assert work.purpose == "report_catalog"
            assert work.time_start.date() == date(2001, 1, 1)
        elif dataset["history_enumeration"] == "year_quarter_batches":
            assert work.history_mode == HistoryMode.ALL_AVAILABLE
            assert work.purpose == "year_quarter"
            assert set(work.parameters) == {"year", "quarter"}
        else:
            assert work.history_mode == HistoryMode.ALL_AVAILABLE
            assert work.purpose == "full_history"
            assert work.time_start.date() >= date(2001, 1, 1)
    for target in targets:
        for dataset_id in ("baostock_adjust", "baostock_calendar", "baostock_daily"):
            first = min(
                item.time_start
                for item in works
                if item.company_id == target.company_id
                and item.dataset_id == dataset_id
            )
            assert first.date() == date(2001, 1, 1)


def test_catalog_period_batches_snapshot_on_demand_and_explicit_dataset_scope():
    datasets = _registry("datasets.v1.json")["datasets"]
    target = _seven_company_targets()[0]
    planner = StructuredDatasetPlanner()
    as_of = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

    financial = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("balance_fields",),
        known_report_periods={
            "balance_fields": ("2025-12-31", "2026-12-31"),
        },
    )
    assert [(item.purpose, item.parameters) for item in financial] == [
        ("company_type", {}),
        ("report_catalog", {}),
        ("report_period", {"report_period": "2025-12-31"}),
    ]

    current = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("company_basic",),
    )
    assert len(current) == 1
    assert current[0].purpose == "current_snapshot"
    assert current[0].time_end - current[0].time_start == timedelta(microseconds=1)

    inactive = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("tags",),
    )
    active = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("tags",),
        activated_dataset_ids=("tags",),
    )
    assert inactive[0].disposition == PlanDisposition.INACTIVE_ON_DEMAND
    assert active[0].disposition == PlanDisposition.REQUIRED
    assert active[0].purpose == "full_history"
    assert active[0].time_start.date() == target.listing_date

    # Selecting a non-financial dataset must not silently fall back to an old
    # financial/market default list or to a five-year display window.
    explicit = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("litigation",),
    )
    assert [item.dataset_id for item in explicit] == ["litigation"]
    assert explicit[0].time_start.date() == target.listing_date


def test_baostock_history_expands_to_frozen_year_quarter_and_date_batches():
    datasets = _registry("datasets.v1.json")["datasets"]
    target = CompanyPlanTarget(
        "company:recent",
        "600519.SH",
        date(2025, 11, 15),
    )
    planner = StructuredDatasetPlanner()
    as_of = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

    finance = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("baostock_profit",),
    )
    assert [(item.parameters["year"], item.parameters["quarter"]) for item in finance] == [
        (2025, 4),
        (2026, 1),
        (2026, 2),
        (2026, 3),
    ]
    assert all(item.purpose == "year_quarter" for item in finance)

    daily = planner.plan_history(
        companies=(target,),
        datasets=datasets,
        as_of=as_of,
        selected_dataset_ids=("baostock_daily",),
    )
    assert len(daily) == 1
    assert daily[0].purpose == "full_history"
    assert daily[0].time_start.date() == target.listing_date
    assert daily[0].time_end == as_of + timedelta(microseconds=1)


def test_company_context_namespace_and_policy_are_part_of_plan_isolation(acquisition_store):
    store = acquisition_store
    planner = StructuredDatasetPlanner()
    same_ticker = planner.plan_history(
        companies=(
            CompanyPlanTarget("company:a", "600519", date(2001, 1, 1)),
            CompanyPlanTarget("company:b", "600519", date(2001, 1, 1)),
        ),
        datasets=({"dataset_id": "events", "provider": "eastmoney"},),
        as_of=store.now,
    )
    assert len({item.scope_key for item in same_ticker}) == 2

    with pytest.raises(StructuredPlanningError, match="duplicate company"):
        planner.plan_history(
            companies=(
                CompanyPlanTarget("company:a", "600519", date(2001, 1, 1)),
                CompanyPlanTarget("company:a", "000858", date(2001, 1, 1)),
            ),
            datasets=({"dataset_id": "events", "provider": "eastmoney"},),
            as_of=store.now,
        )

    context = _context(store)
    work = DatasetWork(
        company_id=context.company_id,
        ticker=context.ticker,
        dataset_id="periodic_reports",
        source_definition_id=store.definition.source_definition_id,
        source_definition_version=store.definition.version,
        query_id=store.plan_item.query_id,
        purpose="full_history",
        history_mode=HistoryMode.ALL_AVAILABLE,
        disposition=PlanDisposition.REQUIRED,
        time_start=store.plan_item.time_start,
        time_end=store.plan_item.time_end,
        partition_key="company:600519:periodic_reports:all",
        parameters={},
        ordinal=0,
    )
    definitions = {
        (store.definition.source_definition_id, store.definition.version): store.definition
    }
    first = planner.compose_shared_plan(
        run=store.run,
        context=context,
        works=(work,),
        source_definitions=definitions,
    )
    changed_policy = planner.compose_shared_plan(
        run=store.run,
        context=replace(context, policy_version="structured-first-v2"),
        works=(work,),
        source_definitions=definitions,
    )
    assert first.jobs[0]["dedupe_key"] != changed_policy.jobs[0]["dedupe_key"]

    with pytest.raises(StructuredPlanningError, match="namespace"):
        planner.compose_shared_plan(
            run=store.run,
            context=replace(context, storage_namespace_id="another-namespace"),
            works=(work,),
            source_definitions=definitions,
        )
    with pytest.raises(StructuredPlanningError, match="company"):
        planner.compose_shared_plan(
            run=store.run,
            context=replace(context, company_id="company:another"),
            works=(work,),
            source_definitions=definitions,
        )


def test_repeating_the_same_persistent_plan_reuses_the_existing_run(acquisition_store):
    store = acquisition_store
    planner = StructuredDatasetPlanner()
    run = store.run.model_copy(update={"run_id": "run-structured-idempotent"})
    context = _context(store, run=run)
    work = DatasetWork(
        company_id=context.company_id,
        ticker=context.ticker,
        dataset_id="periodic_reports",
        source_definition_id=store.definition.source_definition_id,
        source_definition_version=store.definition.version,
        query_id=store.plan_item.query_id,
        purpose="full_history",
        history_mode=HistoryMode.ALL_AVAILABLE,
        disposition=PlanDisposition.REQUIRED,
        time_start=store.plan_item.time_start,
        time_end=store.plan_item.time_end,
        partition_key="company:600519:periodic_reports:idempotent",
        parameters={},
        ordinal=0,
    )
    plan = planner.compose_shared_plan(
        run=run,
        context=context,
        works=(work,),
        source_definitions={
            (store.definition.source_definition_id, store.definition.version): store.definition
        },
    )
    ensure_structured_storage(
        store.db_path, store.data_root, store.namespace.namespace_id
    )
    storage = StructuredStorage(store.db_path, store.namespace.namespace_id)

    assert planner.persist(
        storage, store.repository, context=context, plan=plan
    ) == (run.run_id, True)
    assert planner.persist(
        storage, store.repository, context=context, plan=plan
    ) == (run.run_id, False)
    assert len(storage.list_jobs(run.run_id, limit=None)) == 1
