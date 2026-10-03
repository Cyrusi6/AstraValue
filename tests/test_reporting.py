from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from analysis.models import ClaimKind, ClaimRecord, EventRecord, FactRecord, PeerSetVersion, ResearchRating, VerificationStatus
from analysis.reporting import ReportBuildError, ReportBuilder


def test_report_has_strict_eight_steps_and_auditable_outputs(demo_request):
    report = ReportBuilder().build(demo_request)
    assert [item.number for item in report.sections] == list(range(1, 9))
    assert report.method_bundle_id == report.audit.method_bundle.method_bundle_id
    assert report.audit.method_bundle.method_hashes
    assert not report.audit.unreferenced_numbers
    assert len(report.sections[7].tables[0]["rows"]) == 3
    assert report.status.value == "草稿"
    assert not report.conclusion.rating_confirmed
    assert report.conclusion.rating == ResearchRating.UNRATED  # 多方法分歧超过配置阈值


def test_point_in_time_excludes_future_fact(demo_request):
    future = deepcopy(demo_request.facts[0])
    future.fact_id = "future-fact"
    future.value = 999999
    future.as_of = demo_request.as_of + timedelta(days=1)
    future.disclosed_at = demo_request.as_of + timedelta(days=1)
    demo_request.facts.append(future)
    report = ReportBuilder().build(demo_request)
    assert "future-fact" not in {item.fact_id for item in report.facts}


def test_point_in_time_rejects_claim_whose_evidence_is_not_yet_available(demo_request):
    future = deepcopy(demo_request.facts[0])
    future.fact_id = "future-claim-evidence"
    future.as_of = demo_request.as_of + timedelta(days=1)
    future.disclosed_at = demo_request.as_of + timedelta(days=1)
    demo_request.facts.append(future)
    demo_request.claims.append(
        ClaimRecord(
            ticker=demo_request.ticker,
            category="risk",
            text="未来事实尚未到达报告时点。",
            claim_kind=ClaimKind.DISCLOSED_FACT,
            evidence_fact_ids=[future.fact_id],
            as_of=demo_request.as_of,
        )
    )
    with pytest.raises(ReportBuildError, match="future-claim-evidence"):
        ReportBuilder().build(demo_request)


def test_point_in_time_rejects_future_assumption_source(demo_request):
    future_source = deepcopy(demo_request.sources[0])
    future_source.source_id = "future-assumption-source"
    future_source.published_at = demo_request.as_of + timedelta(days=1)
    demo_request.sources.append(future_source)
    demo_request.assumptions[0].source_ids = [future_source.source_id]
    with pytest.raises(ReportBuildError, match="截至报告时点假设来源尚不可用"):
        ReportBuilder().build(demo_request)


def test_industry_required_metric_accepts_deterministic_derived_value(demo_request):
    demo_request.industry = "消费"
    report = ReportBuilder().build(demo_request)
    assert not any("gross_margin: 行业模型必需指标缺失" in item for item in report.audit.missing_items)


def test_current_price_requires_point_in_time_timestamp(demo_request):
    demo_request.price_as_of = None
    with pytest.raises(ValueError, match="价格时点"):
        ReportBuilder().build(demo_request)


def test_current_price_timestamp_cannot_be_after_report_cutoff(demo_request):
    demo_request.price_as_of = demo_request.as_of + timedelta(seconds=1)
    with pytest.raises(ValueError, match="价格时点不能晚于"):
        ReportBuilder().build(demo_request)


def test_numeric_claim_without_evidence_is_rejected(demo_request):
    demo_request.claims.append(
        ClaimRecord(
            ticker=demo_request.ticker,
            category="risk",
            text="风险概率为50%",
            claim_kind=ClaimKind.ANALYST_JUDGEMENT,
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    with pytest.raises(ReportBuildError):
        ReportBuilder().build(demo_request)


def test_model_input_without_lineage_is_blocked(demo_request):
    demo_request.model_inputs["VAL.RELATIVE"].pop("_lineage")
    report = ReportBuilder().build(demo_request)
    run = next(item for item in report.audit.model_runs if item.method_ref.startswith("VAL.RELATIVE"))
    assert run.status.value == "待核验"
    assert "数值输入缺少血缘" in run.failure_reason
    assert report.audit.unreferenced_numbers


def test_pending_fact_forces_unrated_and_never_enters_latest(demo_request):
    fact = next(item for item in demo_request.facts if item.metric_id == "revenue" and item.period_type == "annual")
    fact.verification_status = VerificationStatus.PENDING
    report = ReportBuilder().build(demo_request)
    assert report.conclusion.rating == ResearchRating.UNRATED
    assert report.audit.conflicts


def test_peer_metric_sources_and_peer_set_refs_remain_in_audit(demo_request):
    peer_source = demo_request.sources[0].model_copy(update={"source_id": "peer-metric-source"})
    demo_request.sources.append(peer_source)
    demo_request.peer_sets = [PeerSetVersion(
        peer_set_id="peer-set-audit", target_ticker=demo_request.ticker,
        as_of=demo_request.as_of, included_tickers=["000858"],
        inclusion_reason={"000858": "同一行业观察集"},
        industry_taxonomy_version="test-v1", data_snapshot_id="peer-snapshot",
        metadata={"peers": [{"ticker": "000858", "metrics": [{"source_ids": [peer_source.source_id]}]}]},
    )]

    report = ReportBuilder().build(demo_request)

    assert "peer-set-audit" in report.audit.peer_set_refs
    assert peer_source.source_id in {item.source_id for item in report.audit.sources}


def test_pending_event_conflict_identifies_event_and_period(demo_request):
    demo_request.events = [EventRecord(
        event_id="dividend-event-2025", ticker=demo_request.ticker, event_type="dividend",
        announced_at=demo_request.as_of, available_at=demo_request.as_of,
        period_end=date(2025, 12, 31), lifecycle_state="announced_plan",
        summary="每股税前现金分红方案", source_ids=[demo_request.sources[0].source_id],
        data_snapshot_id="event-snapshot",
    )]

    report = ReportBuilder().build(demo_request)

    assert any("event_id=dividend-event-2025" in item and "period_end=2025-12-31" in item
               for item in report.audit.conflicts)


def test_critical_acceptance_window_excludes_prior_year_comparative():
    facts = [
        FactRecord(
            ticker="600519",
            metric_id="revenue",
            value=float(year),
            unit="元",
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
            period_type="annual",
            as_of=datetime(year + 1, 4, 1, tzinfo=timezone.utc),
        )
        for year in range(2021, 2026)
    ]
    facts.extend(
        FactRecord(
            ticker="600519",
            metric_id="total_assets",
            value=float(year),
            unit="元",
            period_end=date(year, 12, 31),
            period_type="instant",
            as_of=datetime(max(year + 1, 2022), 4, 1, tzinfo=timezone.utc),
            is_restated=year == 2020,
        )
        for year in range(2020, 2026)
    )

    critical = ReportBuilder()._critical_facts_in_analysis_window(facts)
    assert date(2020, 12, 31) not in {item.period_end for item in critical}
    assert date(2021, 12, 31) in {item.period_end for item in critical}
