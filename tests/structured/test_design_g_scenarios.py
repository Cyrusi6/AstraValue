"""设计 G 节八个合成验收案例。

这些案例只使用合成输入，串联身份、行业、同行、阅读和覆盖判定合同；
它们不代表任何真实公司的研究结论或人工验收。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal

from analysis.structured.coverage import (
    Applicability,
    CoverageState,
    InputReadiness,
    RequirementInput,
    aggregate_coverage,
    evaluate_question,
)
from analysis.structured.identity import (
    CompanyResolver,
    IndustryProfileRouter,
    IndustrySignals,
    PeerCandidate,
    SecurityIdentity,
    select_peers,
)
from analysis.structured.reading import (
    ReadingCandidate,
    ReadingEvidence,
    RuleEvaluation,
    merge_reading_candidates,
    resolve_reading_requirement,
)


def _input(
    requirement_id: str,
    readiness: InputReadiness,
    *,
    period_key: str = "NOW",
    optional: bool = False,
    evidence_ids: tuple[str, ...] = (),
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id,
        period_key=period_key,
        readiness=readiness,
        optional=optional,
        evidence_ids=evidence_ids,
    )


def _identity(code: str, name: str, *, market: str = "SSE") -> SecurityIdentity:
    suffix = {"SSE": "SH", "SZSE": "SZ"}[market]
    return SecurityIdentity(
        company_id=f"company:{code}",
        security_id=f"security:{code}:{suffix}",
        canonical_ticker=f"{code}.{suffix}",
        security_code=code,
        market=market,
        current_name=name,
        security_type="A_SHARE",
        source_record_ids=(f"B08:{code}",),
    )


def _peer(
    identity: SecurityIdentity,
    *,
    revenue: str = "100",
    profit: str = "10",
    business_model_match: bool | None = True,
    evidence_ids: tuple[str, ...] = ("C02:segment",),
) -> PeerCandidate:
    return PeerCandidate(
        identity=identity,
        profile_ids=("consumer",),
        common_segment_ids=("synthetic-segment",),
        principal_segment_share=Decimal("0.8"),
        business_model_match=business_model_match,
        region_match=True,
        comparison_period="Y2025",
        revenue=Decimal(revenue),
        net_profit=Decimal(profit),
        evidence_ids=evidence_ids,
    )


def test_g01_latest_revenue_ready_but_missing_history_keeps_trend_pending() -> None:
    latest = evaluate_question(
        step_id="ES02",
        question_id="ES02.Q02.latest",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.ES02.Q02.latest_revenue",
                InputReadiness.READY,
                evidence_ids=("fact:revenue:2026Q2",),
            ),
        ),
    )
    trend = evaluate_question(
        step_id="ES02",
        question_id="ES02.Q02.trend",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.ES02.Q02.prior_year_revenue",
                InputReadiness.SOURCE_EMPTY,
                period_key="2025Q2",
            ),
        ),
    )

    summary = aggregate_coverage((latest, trend), all_step_ids=("ES02",))

    assert latest.state is CoverageState.READY
    assert trend.state is CoverageState.PENDING
    assert summary.ready_question_count == 1
    assert summary.steps[0].state is CoverageState.PENDING


def test_g02_parsed_report_without_quantity_reuses_parse_and_queues_only_answer() -> None:
    resolution = resolve_reading_requirement(
        company_id="company:synthetic",
        question_id="ES01.Q04",
        requirement_id="REQ.ES01.Q04.production",
        route_id="RD03",
        report_period="Y2025",
        parsed_content_available=True,
    )
    evaluation = RuleEvaluation(
        rule_id="RD03",
        rule_version="research-requirements-v1",
        triggered=True,
        reason="parsed_but_requirement_unanswered",
        target_sections=("经营情况", "产销存"),
    )
    tasks = merge_reading_candidates(
        (
            ReadingCandidate(
                company_id="company:synthetic",
                material_id="report:2025",
                content_hash="a" * 64,
                report_period="Y2025",
                evaluation=evaluation,
                unanswered_questions=("ES01.Q04", "ES01.Q05"),
            ),
        ),
        existing_bodies={"a" * 64: "body:2025"},
        existing_parses={"a" * 64: "mineru:2025"},
    )

    assert resolution.readiness == "reading_pending"
    assert resolution.reason_code == "parsed_but_requirement_unanswered"
    assert len(tasks) == 1
    assert tasks[0].unanswered_questions == ("ES01.Q04", "ES01.Q05")
    assert tasks[0].download_required is False
    assert tasks[0].mineru_required is False


def test_g03_empty_pledge_response_is_pending_until_bounded_absence_evidence() -> None:
    source_empty = evaluate_question(
        step_id="ES03",
        question_id="ES03.Q02",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.ES03.Q02.pledge_exists",
                InputReadiness.SOURCE_EMPTY,
                period_key="Y2025",
            ),
        ),
    )
    absence = ReadingEvidence(
        evidence_id="evidence:no-pledge:Y2025",
        company_id="company:synthetic",
        question_id="ES03.Q02",
        requirement_id="REQ.ES03.Q02.pledge_exists",
        route_id="RD07",
        report_period="Y2025",
        document_id="report:2025",
        content_hash="b" * 64,
        field_name="pledge_exists",
        value=None,
        unit=None,
        page=77,
        explicit_absence=True,
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 12, 31),
    )
    resolved = resolve_reading_requirement(
        company_id="company:synthetic",
        question_id="ES03.Q02",
        requirement_id="REQ.ES03.Q02.pledge_exists",
        route_id="RD07",
        report_period="Y2025",
        evidence=(absence,),
    )
    terms = evaluate_question(
        step_id="ES03",
        question_id="ES03.Q02.terms",
        applicability=Applicability.FALSE,
        applicability_evidence_ids=(absence.evidence_id,),
        inputs=(
            RequirementInput(
                requirement_id="REQ.ES03.Q02.pledge_terms",
                period_key="Y2025",
                readiness=InputReadiness.NOT_DISCLOSED,
                applicability=Applicability.FALSE,
                evidence_ids=(absence.evidence_id,),
            ),
        ),
    )

    assert source_empty.state is CoverageState.PENDING
    assert resolved.readiness == "ready" and resolved.value is False
    assert terms.state is CoverageState.NOT_APPLICABLE


def test_g04_new_bank_has_common_revenue_but_special_fields_and_fcff_stay_separate() -> None:
    bank = _identity("600036", "合成银行")
    resolution = CompanyResolver((bank,)).resolve("600036", as_of=date(2026, 9, 8))
    profile = IndustryProfileRouter(("general", "bank")).select(
        IndustrySignals(
            company_id=bank.company_id,
            source_profile_ids=("bank",),
            source_evidence_ids=("C01:industry",),
            company_type_profile_id="bank",
            company_type_evidence_ids=("EMF:companyType=3",),
        )
    )
    common_revenue = evaluate_question(
        step_id="ES02",
        question_id="ES02.Q02",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.ES02.Q02.bank_revenue",
                InputReadiness.READY,
                evidence_ids=("fact:operate_income",),
            ),
        ),
    )
    bank_capital = evaluate_question(
        step_id="ES02",
        question_id="ES02.Q06.bank",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.ES02.Q06.capital_adequacy",
                InputReadiness.DEFINITION_UNKNOWN,
                period_key="2026Q2",
            ),
        ),
    )
    fcff = evaluate_question(
        step_id="ES05",
        question_id="ES05.Q04.fcff",
        applicability=Applicability.FALSE,
        applicability_evidence_ids=("industry-profile:bank",),
        inputs=(
            RequirementInput(
                requirement_id="REQ.ES05.Q04.fcff",
                period_key="SCN",
                readiness=InputReadiness.UNSUPPORTED,
                applicability=Applicability.FALSE,
                evidence_ids=("industry-profile:bank",),
            ),
        ),
    )

    assert resolution.identity == bank
    assert profile.profile_ids == ("bank",)
    assert profile.general_fallback_used is False
    assert common_revenue.state is CoverageState.READY
    assert bank_capital.state is CoverageState.PENDING
    assert fcff.state is CoverageState.NOT_APPLICABLE


def test_g05_required_inputs_can_be_ready_while_optional_method_and_research_are_not() -> None:
    question = evaluate_question(
        step_id="ES05",
        question_id="ES05.Q06",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.ES05.Q06.actual_price",
                InputReadiness.READY,
                evidence_ids=("fact:price",),
            ),
            _input(
                "REQ.ES05.Q06.optional_forecast",
                InputReadiness.SOURCE_EMPTY,
                optional=True,
            ),
        ),
        method_status="skeleton",
        assumption_status="pending_confirmation",
        analysis_status="not_started",
    )

    assert question.state is CoverageState.READY
    assert question.optional_missing_ids == ("REQ.ES05.Q06.optional_forecast",)
    assert question.method_status == "skeleton"
    assert question.assumption_status == "pending_confirmation"
    assert question.analysis_status == "not_started"


def test_g06_peer_gaps_do_not_block_target_or_expand_peers_recursively() -> None:
    target = _peer(_identity("600519", "合成目标"), revenue="1000")
    loss_peer = _peer(_identity("000858", "合成亏损同行", market="SZSE"), profit="-1")
    unknown_model = _peer(
        _identity("000568", "合成模式未知同行", market="SZSE"),
        business_model_match=None,
        evidence_ids=(),
    )
    selection = select_peers(
        target=target,
        candidates=(loss_peer, unknown_model),
        company_scope="company-with-peers",
    )

    assert selection.selected_security_ids == (loss_peer.identity.security_id,)
    assert loss_peer.identity.security_id in selection.metric_subsets["operating"]
    assert loss_peer.identity.security_id not in selection.metric_subsets["pe"]
    assert selection.recursive_expansion is False
    assert any("business_model_unconfirmed" in item.reasons for item in selection.decisions)


def test_g07_new_requirement_version_recomputes_without_mutating_old_coverage_or_evidence() -> None:
    shared_evidence = ("snapshot:shared-raw-response",)
    old = evaluate_question(
        step_id="ES01",
        question_id="ES01.Q01",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.v1.identity",
                InputReadiness.READY,
                evidence_ids=shared_evidence,
            ),
        ),
    )
    old_payload = asdict(old)
    current = evaluate_question(
        step_id="ES01",
        question_id="ES01.Q01",
        applicability=Applicability.TRUE,
        inputs=(
            _input(
                "REQ.v1.identity",
                InputReadiness.READY,
                evidence_ids=shared_evidence,
            ),
            _input("REQ.v2.channel_inventory", InputReadiness.UNMAPPED),
        ),
    )

    assert old.state is CoverageState.READY
    assert current.state is CoverageState.PENDING
    assert current.ready_requirement_ids == ("REQ.v1.identity",)
    assert current.missing_requirement_ids == ("REQ.v2.channel_inventory",)
    assert asdict(old) == old_payload


def test_g08_more_than_500_details_keep_final_gap_and_one_snapshot_across_outputs() -> None:
    inputs = tuple(
        _input(
            f"REQ.{index:03d}",
            InputReadiness.READY if index < 500 else InputReadiness.READING_PENDING,
            evidence_ids=(f"fact:{index}",) if index < 500 else (),
        )
        for index in range(501)
    )
    question = evaluate_question(
        step_id="ES08",
        question_id="ES08.Q01",
        applicability=Applicability.TRUE,
        inputs=inputs,
    )
    snapshot_id = "coverage:design-g:synthetic"
    shared_projection = {
        "coverage_snapshot_id": snapshot_id,
        "total": len(question.required_requirement_ids),
        "missing_requirement_ids": list(question.missing_requirement_ids),
    }
    api_payload = dict(shared_projection)
    cli_payload = dict(shared_projection)
    export_payload = dict(shared_projection)

    assert question.state is CoverageState.PENDING
    assert shared_projection["total"] == 501
    assert shared_projection["missing_requirement_ids"][-1] == "REQ.500"
    assert api_payload == cli_payload == export_payload
