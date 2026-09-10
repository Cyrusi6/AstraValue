from __future__ import annotations

from datetime import date

from analysis.structured.reading import (
    ReadingCandidate,
    ReadingEvidence,
    ReadingRuleEngine,
    classify_catalog_entry,
    merge_reading_candidates,
    resolve_reading_requirement,
)


def test_catalog_keeps_directory_coverage_separate_from_body_selection() -> None:
    annual = classify_catalog_entry("a", "贵州茅台2025年年度报告")
    summary = classify_catalog_entry("b", "贵州茅台2025年年度报告摘要")
    english = classify_catalog_entry("c", "2025 Annual Report (English)")
    audit = classify_catalog_entry("d", "2025年度内部控制审计报告")

    assert annual.body_status == "selected_mandatory"
    assert {summary.reason, english.reason, audit.reason} == {
        "report_summary",
        "english_report",
        "standalone_audit_report",
    }
    assert all(item.catalog_status == "cataloged" for item in (summary, english, audit))


def test_r01_and_r02_thresholds_use_yoy_and_percentage_points() -> None:
    evaluations = ReadingRuleEngine().evaluate(
        {
            "revenue.current": "120",
            "revenue.prior_year": "100",
            "revenue.previous_quarter_yoy": "0.05",
            "parent_net_profit.current": "10",
            "parent_net_profit.prior_year": "10",
            "net_income_excl.current": "9",
            "net_income_excl.prior_year": "9",
            "gross_margin.current": "0.40",
            "gross_margin.prior_year": "0.37",
            "sales_expense_ratio.current": "0.10",
            "sales_expense_ratio.prior_year": "0.08",
        }
    )
    by_id = {item.rule_id: item for item in evaluations}
    assert by_id["R01"].triggered
    assert by_id["R02"].triggered
    assert by_id["R02"].input_values["gross_margin.change_pp"] == "0.03"


def test_missing_or_nonpositive_denominators_are_not_silent_nontriggers() -> None:
    evaluations = ReadingRuleEngine().evaluate(
        {
            "revenue.current": "10",
            "revenue.prior_year": "0",
            "parent_net_profit.current": "-1",
            "parent_net_profit.prior_year": "-2",
            "operating_cash_flow.current": "1",
            "parent_equity.prior_period": "0",
            "impairment_loss.current": "5",
        }
    )
    by_id = {item.rule_id: item for item in evaluations}
    assert "revenue.positive_yoy_base" in by_id["R01"].missing_inputs
    assert "impairment_loss_and_positive_parent_equity" in by_id["R06"].missing_inputs


def test_incomplete_current_dividend_does_not_compare_with_completed_year() -> None:
    r10 = {item.rule_id: item for item in ReadingRuleEngine().evaluate(
        {
            "dividend.current_completed": False,
            "dividend.prior_completed": True,
            "dividend.current_per_share": "0.1",
            "dividend.prior_per_share": "1.0",
        }
    )}["R10"]
    assert not r10.triggered
    assert "two_completed_profit_year_dividends" in r10.missing_inputs


def test_targeted_correction_and_question_link_are_independent_of_amount() -> None:
    by_id = {item.rule_id: item for item in ReadingRuleEngine().evaluate(
        {
            "correction.linked_used_material": True,
            "correction.open_question": True,
            "event.affects_invalidation": True,
        }
    )}
    assert by_id["R11"].triggered
    assert by_id["R12"].triggered


def test_three_reasons_share_one_existing_parse_without_network_or_mineru() -> None:
    evaluations = ReadingRuleEngine().evaluate(
        {
            "revenue.current": "120",
            "revenue.prior_year": "100",
            "gross_margin.current": "0.40",
            "gross_margin.prior_year": "0.37",
            "parent_net_profit.current": "1",
            "operating_cash_flow.current": "-1",
        }
    )
    triggered = [item for item in evaluations if item.rule_id in {"R01", "R02", "R03"}]
    candidates = [
        ReadingCandidate(
            company_id="company:600519",
            material_id="report:2025q1",
            content_hash="h1",
            report_period="2025-Q1",
            evaluation=item,
            input_fact_ids=(f"fact:{item.rule_id}",),
            unanswered_questions=(f"question:{item.rule_id}",),
        )
        for item in triggered
    ]
    tasks = merge_reading_candidates(
        candidates,
        existing_bodies={"h1": "body:1"},
        existing_parses={"h1": "mineru:1"},
    )
    assert len(tasks) == 1
    assert tasks[0].rule_ids == ("R01", "R02", "R03")
    assert not tasks[0].download_required
    assert not tasks[0].mineru_required
    assert len(tasks[0].unanswered_questions) == 3


def test_parsed_report_without_exact_answer_remains_pending_without_redownload() -> None:
    result = resolve_reading_requirement(
        company_id="company:600519",
        question_id="ES01.Q04",
        requirement_id="REQ.ES01.Q04.production",
        route_id="RD03",
        report_period="Y2025",
        parsed_content_available=True,
    )
    assert result.readiness == "reading_pending"
    assert result.reason_code == "parsed_but_requirement_unanswered"


def test_located_negative_fact_is_ready_only_for_its_bounded_period() -> None:
    evidence = ReadingEvidence(
        evidence_id="evidence:no-incentive-2025",
        company_id="company:600519",
        question_id="ES03.Q05",
        requirement_id="REQ.ES03.Q05.incentive",
        route_id="RD07",
        report_period="Y2025",
        document_id="doc:2025",
        content_hash="a" * 64,
        field_name="equity_incentive_implemented",
        value=None,
        unit=None,
        page=88,
        explicit_absence=True,
        coverage_start=date(2025, 1, 1),
        coverage_end=date(2025, 12, 31),
    )
    current = resolve_reading_requirement(
        company_id="company:600519",
        question_id="ES03.Q05",
        requirement_id="REQ.ES03.Q05.incentive",
        route_id="RD07",
        report_period="Y2025",
        evidence=(evidence,),
    )
    assert current.readiness == "ready"
    assert current.value is False
    assert current.negative_fact is True

    other_period = resolve_reading_requirement(
        company_id="company:600519",
        question_id="ES03.Q05",
        requirement_id="REQ.ES03.Q05.incentive",
        route_id="RD07",
        report_period="Y2024",
        evidence=(evidence,),
    )
    assert other_period.readiness == "reading_pending"
