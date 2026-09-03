from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone

import pytest

from analysis.acquisition.models import (
    AcquisitionMode,
    AnchorEvidence,
    CompanyAcquisitionProfile,
    CoveragePlanDisposition,
    PhysicalQueryCoverageLink,
)
from analysis.acquisition.planner import AcquisitionPlanner, AcquisitionPlanningError
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH, SourceRegistryLoader


AS_OF = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _profile() -> CompanyAcquisitionProfile:
    return CompanyAcquisitionProfile(
        ticker="600519",
        company_name="贵州茅台",
        market="SSE",
        listing_date=date(2001, 8, 27),
    )


def _planner() -> AcquisitionPlanner:
    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    registry = loader.load_registry(question_set=questions)
    return AcquisitionPlanner(registry, questions)


def test_plan_traceability_has_all_ten_questions_and_static_dispositions():
    plan = _planner().plan(
        _profile(), mode=AcquisitionMode.BASELINE, as_of=AS_OF, run_id="run-plan"
    )
    assert {item.question_id for item in plan.coverage_entries} == {
        f"BM.Q{index:02d}.{suffix}"
        for index, suffix in enumerate(
            [
                "IDENTITY_BUSINESS_MODEL",
                "PRODUCT_REGION_ECONOMICS",
                "COUNTERPARTY_CHANNEL",
                "UNIT_ECONOMICS_PRICING",
                "CAPACITY_OPERATIONS",
                "CAPEX_CYCLE",
                "RD_INPUT_EFFICIENCY",
                "VALUE_CHAIN",
                "STRATEGY_CHANGES",
                "COMPETITIVE_CLAIMS_EVIDENCE",
            ],
            start=1,
        )
    }
    static = {
        (item.source_definition_id, item.static_reason_code)
        for item in plan.coverage_entries
        if item.plan_disposition == CoveragePlanDisposition.STATIC_POLICY_SKIPPED
    }
    assert ("szse.disclosures", "market_not_applicable") in static
    assert ("moutai.ir", "pending_policy") in static


def test_applicability_schedules_cninfo_and_sse_but_not_szse_or_pending_ir():
    plan = _planner().plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-applicability"
    )
    planned_sources = {
        item.source_definition_id for item in plan.physical_query_plan_items
    }
    assert {"cninfo.disclosures", "sse.disclosures"} <= planned_sources
    assert "szse.disclosures" not in planned_sources
    assert "moutai.ir" not in planned_sources


def test_shared_execution_m2m_maps_periodic_request_to_multiple_questions_once():
    plan = _planner().plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-shared"
    )
    periodic = next(
        item
        for item in plan.physical_query_plan_items
        if item.source_definition_id == "cninfo.disclosures"
        and item.query_id == "cninfo.periodic_report"
    )
    linked = [item for item in plan.coverage_links if item.plan_item_id == periodic.plan_item_id]
    assert len(linked) == 10
    coverage_by_id = {item.coverage_entry_id: item for item in plan.coverage_entries}
    assert len({coverage_by_id[item.coverage_entry_id].question_id for item in linked}) == 10


def test_no_duplicate_io_for_identical_execution_key():
    plan = _planner().plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-no-duplicate"
    )
    keys = [item.execution_key for item in plan.physical_query_plan_items]
    assert len(keys) == len(set(keys))
    assert len(plan.coverage_links) > len(plan.physical_query_plan_items)


def test_enabled_ir_is_applicable_only_after_new_registry_version(tmp_path):
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["registry_version"] = "1.1.0"
    ir = next(
        item for item in payload["definitions"] if item["source_definition_id"] == "moutai.ir"
    )
    ir["version"] = "1.1.0"
    ir["policy_status"] = "enabled"
    ir["enabled"] = True
    ir["access_method"] = "https_api"
    ir["initial_request_allowlist"] = [
        {"scheme": "https", "host": "ir.example.test", "port": 443, "path_prefix": "/public/"}
    ]
    ir["redirect_allowlist"] = list(ir["initial_request_allowlist"])
    ir["license_policy"]["automated_access"] = "allowed"
    ir["license_policy"]["checked_at"] = "2026-09-03T01:00:00Z"
    ir["queries"][0]["endpoint"] = "https://ir.example.test/public/list"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    registry = loader.load_registry(path, question_set=questions)
    plan = AcquisitionPlanner(registry, questions).plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-ir-enabled"
    )
    assert any(
        item.source_definition_id == "moutai.ir"
        for item in plan.physical_query_plan_items
    )
    assert not any(
        item.source_definition_id == "moutai.ir"
        and item.static_reason_code == "pending_policy"
        for item in plan.coverage_entries
    )


def test_plan_can_be_explained_before_any_attempt_exists():
    plan = _planner().plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-plan-only"
    )
    assert plan.physical_query_plan_items
    assert plan.coverage_entries
    assert plan.coverage_links
    assert set(PhysicalQueryCoverageLink.model_fields) == {
        "plan_item_id",
        "coverage_entry_id",
    }
    assert Counter(item.plan_disposition for item in plan.coverage_entries)[
        CoveragePlanDisposition.STATIC_POLICY_SKIPPED
    ] > 0


def test_company_anchor_prefers_earlier_prospectus_with_frozen_evidence():
    profile = CompanyAcquisitionProfile(
        ticker="600519",
        company_name="贵州茅台",
        market="SSE",
        listing_date=date(2001, 8, 27),
        listing_evidence=AnchorEvidence(
            value_date=date(2001, 8, 27),
            source_definition_id="sse.disclosures",
            proof_id="proof-listing",
        ),
        prospectus_date=date(2001, 7, 31),
        prospectus_evidence=AnchorEvidence(
            value_date=date(2001, 7, 31),
            source_definition_id="cninfo.disclosures",
            snapshot_id="snapshot-prospectus",
        ),
    )

    plan = _planner().plan(
        profile, mode="baseline", as_of=AS_OF, run_id="run-company-anchor"
    )

    assert plan.run.company_anchor_date == date(2001, 7, 31)
    assert plan.run.company_anchor_quality == "prospectus_or_listing"


def test_company_anchor_uses_listing_then_explicit_source_fallback():
    listing_only = _planner().plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-listing-only"
    )
    assert listing_only.run.company_anchor_date == date(2001, 8, 27)

    fallback = CompanyAcquisitionProfile(
        ticker="600519",
        company_name="贵州茅台",
        market="SSE",
        fallback_earliest_date=date(1990, 12, 19),
        fallback_reason="固定注册表中适用来源的最早可得日期",
    )
    fallback_plan = _planner().plan(
        fallback, mode="baseline", as_of=AS_OF, run_id="run-anchor-fallback"
    )
    assert fallback_plan.run.company_anchor_date == date(1990, 12, 19)
    assert fallback_plan.run.company_anchor_quality == "source_earliest_fallback"


def test_baseline_full_history_uses_contiguous_half_open_source_windows():
    plan = _planner().plan(
        _profile(), mode="baseline", as_of=AS_OF, run_id="run-full-history"
    )
    windows = sorted(
        (
            item.time_start,
            item.time_end,
        )
        for item in plan.physical_query_plan_items
        if item.source_definition_id == "cninfo.disclosures"
        and item.query_id == "cninfo.periodic_report"
    )
    assert windows[0][0] == datetime(2001, 8, 26, 16, tzinfo=timezone.utc)
    assert windows[-1][1] == AS_OF
    assert len(windows) > 20  # proves the former five/ten-year cap is absent
    assert all(left[1] == right[0] for left, right in zip(windows, windows[1:]))


def test_baseline_records_source_not_available_before_earliest_boundary():
    early_profile = CompanyAcquisitionProfile(
        ticker="600519",
        company_name="贵州茅台",
        market="SSE",
        listing_date=date(1980, 1, 1),
    )
    cutoff = AS_OF
    plan = _planner().plan(
        early_profile,
        mode="baseline",
        as_of=cutoff,
        run_id="run-source-earliest",
    )
    skipped = [
        item
        for item in plan.coverage_entries
        if item.source_definition_id == "cninfo.disclosures"
        and item.query_id == "cninfo.periodic_report"
        and item.static_reason_code == "source_not_available"
    ]
    required = [
        item
        for item in plan.coverage_entries
        if item.source_definition_id == "cninfo.disclosures"
        and item.query_id == "cninfo.periodic_report"
        and item.plan_disposition == CoveragePlanDisposition.REQUIRED
    ]
    assert skipped
    assert required
    assert {item.time_end for item in skipped} == {
        datetime(1990, 12, 19, tzinfo=timezone.utc)
    }
    assert min(item.time_start for item in required) == datetime(
        1990, 12, 19, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("effective_at", "expires_at", "message"),
    (
        ("2026-09-03T00:00:01Z", None, "尚未生效"),
        ("2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z", "已经失效"),
    ),
)
def test_planner_rejects_source_definition_outside_run_as_of_window(
    tmp_path, effective_at, expires_at, message
):
    payload = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    definition = payload["definitions"][0]
    definition["effective_at"] = effective_at
    definition["expires_at"] = expires_at
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loader = SourceRegistryLoader()
    questions = loader.load_questions()
    registry = loader.load_registry(path, question_set=questions)

    with pytest.raises(AcquisitionPlanningError, match=message):
        AcquisitionPlanner(registry, questions).plan(
            _profile(), mode="baseline", as_of=AS_OF
        )
