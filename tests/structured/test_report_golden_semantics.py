from copy import deepcopy
from datetime import datetime, timezone

import pytest

from analysis.models import ReportCreateRequest, VerificationStatus
from analysis.reporting import ReportBuilder
from analysis.structured.research_lite import _format_number, _markdown_table


def entry(value, kind, ref):
    return {"metric_id":"operating_income", "period":"2025-12-31", "period_type":kind,
            "label":"营业收入", "state":"ready", "fact_ref":ref,
            "fact":{"value":value,"unit":"CNY","period_type":kind}}


def test_same_year_end_keeps_annual_and_q4_in_either_input_order():
    annual=entry("16000000000", "cumulative", "annual-id")
    q4=entry("3000000000", "single_quarter", "q4-id")
    for rows in [[annual,q4],[q4,annual]]:
        year=_markdown_table(rows,["2025-12-31"])
        quarter=_markdown_table(rows,["2025-12-31"],annual=False)
        assert "160.00亿元" in year and "annual-id" in year and "q4-id" not in year
        assert "30.00亿元" in quarter and "q4-id" in quarter and "annual-id" not in quarter
    assert "待补" in _markdown_table([q4],["2025-12-31"])


def test_conflicting_display_key_fails_instead_of_last_write_wins():
    a=entry("16000000000","cumulative","a")
    b=deepcopy(a);b["fact"]["value"]="17000000000"
    with pytest.raises(ValueError,match="conflicting_display_metric"):
        _markdown_table([a,b],["2025-12-31"])


@pytest.mark.parametrize("metric",["eastmoney_pe_ttm","eastmoney_pb_mrq","eastmoney_ps_ttm"])
def test_valuation_multiples_are_not_percentages(metric):
    assert _format_number("21.12","ratio",metric)=="21.12倍"
    assert _format_number("0.9123","ratio","gross_margin")=="91.23%"


def test_empty_claims_and_pending_coverage_cannot_be_complete():
    request=ReportCreateRequest(ticker="600519",company_name="贵州茅台",industry="消费",
        as_of=datetime(2026,9,13,tzinfo=timezone.utc),
        research_coverage={"questions":[{"question_id":f"ES01.Q{i:02d}","state":"pending",
            "missing_requirement_ids":[f"REQ{i}"]} for i in range(1,55)]})
    report=ReportBuilder().build(request)
    assert report.conclusion.evidence_completeness==0
    assert len(report.audit.missing_items)>=54
    assert any("ClaimRecord" in item for item in report.audit.missing_items)


def test_full_coverage_does_not_promote_partial_period_or_cross_source_conflict(tmp_path):
    from analysis.structured.report_semantics import full_coverage
    rows=[{"company":"600519","question_id":"ES02.Q01","requirement_id":"r",
           "period":"2025-12-31","state":state,"reason":state} for state in ["ready","pending"]]
    manifest={"ticker":"600519","periods":{"annual":["2025-12-31"],"quarters":[],"current":"2026-09-13"},
        "source_inputs":[{"files":{"question-coverage.jsonl":{"path":str(tmp_path/'rows')}}}]}
    assert full_coverage(manifest,lambda _:rows)[0]["state"]=="pending"


def test_bridge_upgrade_preserves_existing_derived_fact_projection_contract():
    from analysis.structured.reporting_bridge import _fact_from_core, DERIVED_FACT_PROJECTION_VERSION
    payload={"fact_id":"lite-derived-stable","metric_id":"gross_margin","value":"0.91","unit":"ratio",
        "available_at":"2026-09-13T00:00:00Z","period_end":"2025-12-31","period_type":"cumulative",
        "source_ids":["source-a"],"derived_from_fact_ids":["input-a","input-b"],"method_ref":"formula:v1","verification_status":VerificationStatus.DERIVED.value}
    fact=_fact_from_core(payload,"600519")
    assert fact.fact_id=="lite-derived-stable"
    assert fact.metadata["report_bridge_version"]==DERIVED_FACT_PROJECTION_VERSION=="eight-step-lite-report-bridge-v1.0.0"
