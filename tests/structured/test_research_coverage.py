from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json

import pytest

from analysis.structured.research_coverage import build_question_coverage, _load_registry
from analysis.structured.research_lite import lite_periods, _question_routes
from analysis.structured.report_semantics import full_coverage, semantic_inputs
from analysis.structured.scope import LITE_PROFILE_ID, load_research_profile


AS_OF = date(2026, 10, 2)
PROFILE = load_research_profile(LITE_PROFILE_ID)
PERIODS = lite_periods(AS_OF, PROFILE)


def build(*, facts=(), records=(), evidence=()):
    return build_question_coverage(ticker="600519", as_of=AS_OF, facts=facts, records=records,
                                   evidence=evidence, profile=PROFILE, periods=PERIODS)


def fact(dataset="income_fields", raw="OPERATE_INCOME", *, value=100, period="2025-12-31",
         available_at="2026-10-02T00:00:00Z", ident="fact-1", period_type="cumulative"):
    return {"fact_id": ident, "ticker": "600519", "metric_id": "operating_income", "value": value,
            "period_end": period, "period_type": period_type, "source_ids": ["source-1"],
            "metadata": {"structured_dataset_id": dataset, "structured_field_path": "$." + raw,
                         "available_at": available_at}}


def record(dataset, fields, *, period="2025-12-31", ident="record-1", **extra):
    return {"company": "600519", "dataset_id": dataset, "record_id": ident,
            "snapshot_id": "snapshot-1", "row_key": "row-1", "period": period,
            "available_at": "2026-10-02T00:00:00Z", "source_ids": ["source-raw"],
            "fields": fields, **extra}


def select(result, *, kind="raw_field", dataset=None, raw=None, period="2025-12-31", route=None):
    rows = [row for row in result["rows"] if row["period"] == period and row["input"]["kind"] == kind
            and (dataset is None or row["input"].get("dataset_id") == dataset)
            and (raw is None or row["input"].get("raw_name") == raw)
            and (route is None or row["input"].get("route_id") == route)]
    assert rows, (kind, dataset, raw, period, route)
    return rows


def test_empty_input_preserves_all_registered_questions_requirements_and_scope():
    result, registry = build(), _load_registry()
    assert {row["question_id"] for row in result["rows"]} == {q["question_id"] for q in registry["questions"]}
    assert {row["requirement_id"] for row in result["rows"]} == {r["requirement_id"] for r in registry["requirements"]}
    assert result["summary"]["question_count"] == 54
    assert result["summary"]["requirement_count"] == 401
    assert result["summary"]["states"].get("ready", 0) == 0
    assert all(row["analysis_status"] == "not_started" for row in result["rows"])
    assert all(row["quality_state"] == "pending" for row in _question_routes(result["rows"], PROFILE, PERIODS))
    excluded = select(result, dataset="company_basic", raw="ORG_CODE")
    assert all(row["reason"] == "outside_research_profile" for row in excluded)
    assert not {row["requirement_id"] for row in excluded} & {w["requirement_id"] for w in result["work_items"]}
    scope_na = [row for row in result["rows"] if row["requirement_id"] == "REQ.ES05.Q01.003"]
    assert scope_na and all(row["state"] == "not_applicable" and row["scope_override"]["basis"] for row in scope_na)


def test_selected_zero_fact_readies_only_matching_field_and_period_with_provenance():
    result = build(facts=[fact(value=0)])
    rows = select(result, dataset="income_fields", raw="OPERATE_INCOME")
    assert all(row["state"] == "ready" and row["fact_ids"] == ["fact-1"] for row in rows)
    assert all(row["source_ids"] == ["source-1"] and row["registry_sha256"] for row in rows)
    prior = select(result, dataset="income_fields", raw="OPERATE_INCOME", period="2024-12-31")
    assert all(row["state"] == "pending" and not row["fact_ids"] for row in prior)
    assert all(row["quality_state"] == "pending" for row in _question_routes(result["rows"], PROFILE, PERIODS))
    assert result["summary"]["required_active_states"]["ready"] > 0
    assert all(not row["required_for_active_coverage"] for row in result["rows"]
               if row["requiredness"] == "optional" or not row["active_in_profile"] or row["scope_route"] != "core")


def test_market_quote_satisfies_point_in_time_market_cap_requirement():
    result = build(facts=[fact(dataset="market_cap", raw="CLOSE_PRICE", value=1258.62,
                               period="2026-09-30", period_type="market_quote", ident="market-quote")])
    rows = [row for row in result["rows"] if row["requirement_id"] == "REQ.ES05.Q01.002"]
    assert len(rows) == 1
    assert rows[0]["state"] == "ready"
    assert rows[0]["fact_ids"] == ["market-quote"]
    assert rows[0]["effective_input_period"] == "2026-09-30"


def test_quarter_and_stock_values_cannot_satisfy_cumulative_field_at_same_year_end():
    for kind in ("single_quarter", "instant", "ttm"):
        result = build(facts=[fact(period_type=kind)])
        rows = select(result, dataset="income_fields", raw="OPERATE_INCOME")
        assert all(row["state"] == "pending" and row["reason"] == "fact_period_type_mismatch" for row in rows)
        assert all(row["period_mismatch_fact_ids"] == ["fact-1"] and not row["fact_ids"] for row in rows)
    result = build(facts=[fact(ident="annual"), fact(ident="q4", period_type="single_quarter", value=25)])
    assert all(row["state"] == "ready" and row["fact_ids"] == ["annual"]
               for row in select(result, dataset="income_fields", raw="OPERATE_INCOME"))


def test_conflicting_projection_values_require_review_and_explicit_revision_selects_leaf():
    original, revised = fact(ident="old"), fact(ident="new", value=200)
    result = build(facts=[original, revised])
    rows = select(result, dataset="income_fields", raw="OPERATE_INCOME")
    assert all(row["state"] == "pending" and row["reason"] == "source_or_revision_conflict" for row in rows)
    assert all(row["conflicting_fact_ids"] == ["new", "old"] and not row["fact_ids"] for row in rows)
    original["metadata"].update(structured_record_version_id="record-old", structured_row_key="row-a",
                                 available_at="2026-10-01T00:00:00Z")
    revised["metadata"].update(structured_record_version_id="record-new", structured_row_key="row-a",
                                supersedes_record_version_id="record-old")
    current = build(facts=[original, revised])
    assert all(row["state"] == "ready" and row["fact_ids"] == ["new"]
               for row in select(current, dataset="income_fields", raw="OPERATE_INCOME"))
    revised["metadata"]["structured_row_key"] = "other-row"
    assert all(row["state"] == "pending" for row in select(build(facts=[original, revised]),
               dataset="income_fields", raw="OPERATE_INCOME"))


def test_distinct_business_dimensions_are_not_revision_conflicts():
    values = [dict(fact("segments", "MAIN_BUSINESS_INCOME", ident="segment-a"),
                   dimension_type="product", dimension_code="A"),
              dict(fact("segments", "MAIN_BUSINESS_INCOME", ident="segment-b", value=200),
                   dimension_type="product", dimension_code="B")]
    assert all(row["state"] == "ready" and row["fact_ids"] == ["segment-a", "segment-b"]
               for row in select(build(facts=values), dataset="segments", raw="MAIN_BUSINESS_INCOME"))


def test_text_numeric_observations_and_missing_data_have_distinct_next_work():
    records = [record("company_basic", {"MAIN_BUSINESS": "白酒生产和销售"}, period=None),
               record("income_fields", {"OPERATE_COST": 12}, ident="cost"),
               record("income_fields", {"RESEARCH_EXPENSE": 3}, period=None, ident="undated")]
    before = deepcopy(records)
    result = build(records=records)
    company = select(result, dataset="company_basic", raw="MAIN_BUSINESS")
    assert all(row["state"] == "source_text_available" and row["effective_input_period"] is None for row in company)
    assert company[0]["observations"][0]["snapshot_id"] == "snapshot-1"
    assert company[0]["record_ids"] == ["record-1"]
    numeric = select(result, dataset="income_fields", raw="OPERATE_COST")
    assert all(row["reason"] == "semantic_definition_or_period_unconfirmed" for row in numeric)
    undated = select(result, dataset="income_fields", raw="RESEARCH_EXPENSE")
    assert all(row["reason"] == "observed_field_period_unconfirmed" for row in undated)
    ids = {row["requirement_id"] for row in company + numeric + undated}
    work = [item for item in result["work_items"] if item["requirement_id"] in ids and item["period"] == "2025-12-31"]
    assert work and all(item["stage"] == "semantic_processing" and not item["acquire_allowed"] for item in work)
    missing = select(result, dataset="income_fields", raw="PARENT_NETPROFIT")
    missing_ids = {row["requirement_id"] for row in missing}
    assert any(item["acquire_allowed"] and item["reason"] == "input_not_acquired"
               for item in result["work_items"] if item["requirement_id"] in missing_ids)
    assert records == before
    assert result == build(records=list(reversed(records)))


def test_current_and_future_availability_use_china_cutoff_and_keep_text_unreviewed():
    good = {"company": "600519", "route_id": "RD01", "period": "2025-12-31", "evidence_id": "e-1"}
    future = dict(good, evidence_id="e-future", available_at="2026-10-02T16:00:00Z")
    result = build(facts=[fact(available_at="2026-10-02T16:00:00Z")],
                   records=[record("company_basic", {"MAIN_BUSINESS": "future"}, period=None,
                                   available_at="2026-10-02T16:00:00Z")], evidence=[good, future])
    assert all(row["state"] == "pending" for row in select(result, dataset="income_fields", raw="OPERATE_INCOME"))
    assert all(not row["record_ids"] for row in select(result, dataset="company_basic", raw="MAIN_BUSINESS"))
    rows = select(result, kind="reading_section", route="RD01")
    assert all(row["state"] == "source_text_available" and row["text_evidence_ids"] == ["e-1"] for row in rows)
    ids = {row["requirement_id"] for row in rows}
    assert all(item["stage"] == "document_reading" and not item["acquire_allowed"]
               for item in result["work_items"] if item["requirement_id"] in ids)


def test_disclosed_empty_field_is_not_zero_or_a_new_acquisition_request():
    result = build(records=[record("income_fields", {"OPERATE_COST": None})])
    rows = select(result, dataset="income_fields", raw="OPERATE_COST")
    assert all(row["state"] == "pending" and row["reason"] == "observed_value_missing" for row in rows)
    assert all(row["observations"][0]["value"] is None and not row["fact_ids"] for row in rows)
    ids = {row["requirement_id"] for row in rows}
    assert all(item["stage"] == "semantic_processing" and not item["acquire_allowed"]
               for item in result["work_items"] if item["requirement_id"] in ids and item["period"] == "2025-12-31")


def test_calculation_inputs_do_not_claim_entire_route_or_research_complete():
    route = next(item for item in _load_registry()["calculation_routes"] if item["route_id"] == "K01")
    facts = [fact(*ref[2:].split(".", 1), ident=f"input-{index}",
                  period_type="single_quarter" if "quarter." in ref else "cumulative")
             for index, ref in enumerate(route["input_refs"]) if ref.startswith("f:")]
    result = build(facts=facts)
    rows = select(result, kind="calculation", route="K01")
    assert all(row["state"] == "pending" for row in rows)
    assert all(row["reason"] == "calculation_route_integration_pending" for row in rows)
    assert all(row["calculation_status"]["missing_verified_inputs"] == [] for row in rows)
    assert all(row["calculation_status"]["complete_route_integrated"] is False for row in rows)


def test_existing_record_set_stays_pending_for_lifecycle_review_without_reacquisition():
    result = build(records=[record("dividend", {"ASSIGN_PROGRESS": "实施分配"})])
    rows = [row for row in result["rows"] if row["input"]["kind"] == "record_set"
            and row["input"].get("dataset_id") == "dividend" and row["record_ids"]]
    assert rows and all(row["state"] == "pending" and row["reason"] == "record_set_available_lifecycle_or_field_semantics_pending" for row in rows)
    ids = {(row["requirement_id"], row["period"]) for row in rows}
    assert all(item["stage"] == "semantic_processing" and not item["acquire_allowed"] for item in result["work_items"]
               if (item["requirement_id"], item["period"]) in ids)


@pytest.mark.parametrize("group,payload", [("facts", fact()),
                                            ("records", record("company_basic", {})),
                                            ("evidence", {"company": "600519"})])
def test_other_company_inputs_are_rejected(group, payload):
    payload["ticker" if group == "facts" else "company"] = "000858"
    with pytest.raises(ValueError, match="company_mismatch"):
        build(**{group: [payload]})


def coverage_row(state="ready", period="2025-12-31"):
    return {"company": "600519", "question_id": "ES02.Q01", "requirement_id": "req-1",
            "period": period, "state": state, "fact_ids": ["f-1"] if state == "ready" else [],
            "record_ids": ["record-1"], "source_ids": ["source-1"], "registry_sha256": "registry-hash"}


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_frozen_pack_coverage_takes_precedence_over_legacy_and_retains_lineage(tmp_path):
    path = tmp_path / "question-coverage.jsonl"
    path.write_text(json.dumps(coverage_row()) + "\n", encoding="utf-8")
    manifest = {"ticker": "600519", "periods": PERIODS,
                "output_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()},
                "source_inputs": [{"files": {path.name: {"path": str(tmp_path / "missing-old")}}}]}
    rows = full_coverage(manifest, read_rows, pack_dir=tmp_path)
    assert rows[0]["state"] == "ready"
    assert rows[0]["record_ids"] == ["record-1"] and rows[0]["registry_sha256"] == "registry-hash"
    with pytest.raises(ValueError, match="pack_dir_required"):
        full_coverage(manifest, read_rows)
    path.write_text(json.dumps(coverage_row("pending")), encoding="utf-8")
    with pytest.raises(ValueError, match="hash_mismatch"):
        full_coverage(manifest, read_rows, pack_dir=tmp_path)
    path.unlink()
    with pytest.raises(FileNotFoundError, match="input_missing"):
        full_coverage(manifest, read_rows, pack_dir=tmp_path)


def test_legacy_nested_current_runs_merge_but_archived_history_is_excluded():
    manifest = {"ticker": "600519", "periods": PERIODS, "source_inputs": [{"files": {
        "runs/current/question-coverage.jsonl": {"path": "current"},
        "runs/other/question-coverage.jsonl": {"path": "other"},
        "history/old/question-coverage.jsonl": {"path": "archive"},
    }}]}
    reads = []

    def rows(path):
        reads.append(str(path))
        return [coverage_row("ready" if str(path) == "current" else "pending")]

    assert full_coverage(manifest, rows)[0]["state"] == "pending"
    assert reads == ["current", "other"]


def test_materialized_event_is_not_replaced_by_observation_time_event(tmp_path):
    core = {"governance": [record("dividend", {"ASSIGN_PROGRESS": "实施分配"})],
            "events": [{"event_id": "formal-event", "metadata": {"structured_record_version_id": "record-1"}}]}
    manifest = {"ticker": "600519", "periods": PERIODS}
    as_of = datetime(2026, 10, 2, 15, 59, 59, tzinfo=timezone.utc)
    result = semantic_inputs(tmp_path, manifest, core, as_of, read_rows)
    assert result["events"] == []
    assert any(claim.category == "capital" for claim in result["claims"])
    core.pop("events")
    legacy = semantic_inputs(tmp_path, manifest, core, as_of, read_rows)
    assert [event.event_id for event in legacy["events"]] == ["report-event-record-1"]
