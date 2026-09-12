from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from analysis.models import VerificationStatus
from analysis.structured.materialization import StructuredFactMaterializer
from analysis.structured.materialization_contracts import LEGACY_CONTRACTS
from analysis.structured.mappings import FIELD_RULES
from analysis.structured.registry import canonical_registry_sha256
from analysis.structured.consumption import is_fact_consumable

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
HASH = "4c8492349c6e490f173f538e7d2f59f380d3f08abaf58de7ae4f29423692e4f4"


class Fixture:
    def __init__(self, *, embedded=False):
        self.context = SimpleNamespace(run_id="run-1", ticker="600519", storage_namespace_id="ns-1",
            field_registry_id=LEGACY_CONTRACTS[HASH]["field_registry_id"],
            field_registry_version="1.1.0", field_registry_hash=HASH,
            frozen_config={"datasets": [], "source_definitions": [], "known_fields": {}})
        self.jobs, self.rows, self.snapshots = [], [], {}
        self.finalized = True
        self.embedded = embedded
        self.rules = deepcopy(LEGACY_CONTRACTS[HASH]["rules"])

    def add(self, dataset="income_fields", raw="OPERATE_INCOME", value="100.5", period="2026-06-30",
            *, row=None, rule=None, available=NOW, supersedes=None, source="structured-eastmoney"):
        i = len(self.rows) + 1
        if rule is None:
            r = asdict(FIELD_RULES[(dataset, raw)])
            rule = {k: v.value if hasattr(v, "value") else str(v) if k == "multiplier" else v for k, v in r.items()}
            rule.update(stored_unit=rule["unit"], original_unit=rule["unit"])
        if self.embedded:
            self.rules[f"{dataset}.{raw}"] = rule
        date_field = "END_DATE" if dataset == "float_holders_history" else "date" if dataset == "baostock_daily" else "REPORT_DATE"
        raw_row = {date_field: period, raw: value, **(row or {})}
        job = {"job_id": f"job-{dataset}-{source}", "run_id": "run-1", "dataset_id": dataset,
               "source_definition_id": source, "source_definition_version": "1.0.0", "plan_item_id": f"plan-{dataset}-{source}"}
        if not any(j["job_id"] == job["job_id"] for j in self.jobs):
            self.jobs.append(job)
        config = self.context.frozen_config
        if not any(d["dataset_id"] == dataset for d in config["datasets"]):
            config["datasets"].append({"dataset_id": dataset, "date_fields": [date_field, "NOTICE_DATE"]})
        if not any(s["source_definition_id"] == source for s in config["source_definitions"]):
            config["source_definitions"].append({"source_definition_id": source, "version": "1.0.0", "upstream_identity": source})
        config["known_fields"].setdefault(dataset, []).append(raw)
        record = {"record_version_id": f"record-{i}", "job_id": job["job_id"], "snapshot_id": f"snapshot-{i}",
            "_committed_attempt_id": f"attempt-{i}", "_committed_attempt_outcome": "success",
            "page_id": f"page-{i}", "row_key": f"row-{dataset}-{period}",
            "available_at": available.isoformat(), "observed_at": available.isoformat(), "raw_row": raw_row,
            "supersedes_record_version_id": supersedes}
        field = {"field_value_id": f"field-{i}", "record_version_id": record["record_version_id"],
            "dataset_id": dataset, "raw_field_name": raw, "field_path": f"$.{raw}",
            "standard_field_id": rule["standard_field"], "definition_version": "1.1.0",
            "nature": rule["nature"], "quality": "passed", "value": value, "unit": rule["stored_unit"], "period_key": period}
        snapshot = SimpleNamespace(snapshot_id=record["snapshot_id"], sha256=f"{i:064x}",
            canonical_url="https://example.invalid/response", canonical_resource_id=None,
            created_at=available, available_at=available, published_at=None,
            source_definition_id=source, source_definition_version="1.0.0", storage_namespace_id="ns-1",
            physical_query_plan_item_id=job["plan_item_id"], policy_decision="allowed")
        self.rows.append((record, [field]))
        self.snapshots[snapshot.snapshot_id] = snapshot
        return record, field, snapshot

    def get_run_context(self, run_id):
        if self.embedded:
            registry = {"registry_id": self.context.field_registry_id, "version": "1.1.0",
                "fields": [{"field_id": key, "definition_status": "confirmed", "unit_status": "confirmed",
                            "formula_eligible": True} for key in sorted(self.rules)]}
            self.context.field_registry_hash = canonical_registry_sha256(registry)
            self.context.frozen_config["materialization_contract"] = {"field_registry": registry, "rules": self.rules}
        return self.context

    def list_jobs(self, run_id, **_):
        return self.jobs

    def iter_committed_record_bundles(self, run_id):
        yield from self.rows

    def get_raw_resource_snapshot(self, snapshot_id):
        return self.snapshots[snapshot_id]

    def list_run_events(self, run_id):
        return [{"event_type": "finalized"}] if self.finalized else []

    def run(self, **kwargs):
        return StructuredFactMaterializer(self, self).materialize("run-1", **kwargs)


def test_materialization_is_deterministic_and_keeps_field_lineage():
    fixture = Fixture()
    fixture.add()
    first, second = fixture.run(), fixture.run()
    assert first == second
    fact = next(f for f in first.facts if not f.derived_from_fact_ids)
    assert fact.value == 100.5
    assert fact.verification_status == VerificationStatus.SUPPLIER_DIRECT
    assert fact.structured_admission.field_path == "$.OPERATE_INCOME"
    assert fact.metadata["structured_row_key"] == "row-income_fields-2026-06-30"
    assert fact.metadata["field_registry_hash"] == HASH
    assert fact.fact_id in first.selected_fact_ids
    # Evidence-table latest selection must not bypass the cutoff manifest.
    assert not is_fact_consumable(fact)
    assert is_fact_consumable(fact, materialization_selected_ids=frozenset(first.selected_fact_ids))


def test_strict_historical_materialization_excludes_late_records():
    fixture = Fixture()
    fixture.add()
    result = fixture.run(as_of=datetime(2026, 9, 9, tzinfo=timezone.utc), strict_historical=True)
    assert result.facts == ()
    assert result.skipped == 1
    with pytest.raises(ValueError, match="requires"):
        fixture.run(strict_historical=True)
    with pytest.raises(ValueError, match="timezone-aware"):
        fixture.run(as_of=datetime(2026, 9, 9))


def test_unfinished_run_and_unrecognized_contract_fail_closed():
    fixture = Fixture()
    fixture.add()
    fixture.finalized = False
    with pytest.raises(ValueError, match="finalized"):
        fixture.run()
    fixture.finalized = True
    fixture.context.field_registry_hash = "0" * 64
    with pytest.raises(ValueError, match="unsupported frozen"):
        fixture.run()


@pytest.mark.parametrize("target,key,value,reason", [
    ("record", "_committed_attempt_outcome", "parse_failed", "page_attempt_not_successful"),
    ("record", "_committed_attempt_outcome", None, "page_attempt_not_successful"),
    ("record", "available_at", None, "timestamp_or_snapshot_missing"),
    ("record", "observed_at", "2026-09-10", "timestamp_or_snapshot_missing"),
    ("field", "definition_version", None, "definition_version_missing_or_mismatched"),
    ("field", "unit", None, "unit_missing_or_conflicting"),
    ("field", "unit", "万元", "unit_missing_or_conflicting"),
    ("field", "field_path", "", "field_locator_missing_or_mismatched"),
    ("field", "standard_field_id", "other", "field_mapping_or_identity_mismatch"),
    ("field", "quality", "failed", "quality_or_nature_not_admissible"),
    ("field", "nature", "forecast", "quality_or_nature_not_admissible"),
    ("field", "value", "200", "original_value_mismatch"),
    ("snapshot", "storage_namespace_id", "other", "snapshot_binding_or_policy_invalid"),
    ("snapshot", "source_definition_version", "9", "snapshot_binding_or_policy_invalid"),
    ("snapshot", "physical_query_plan_item_id", "other", "snapshot_binding_or_policy_invalid"),
    ("snapshot", "policy_decision", "denied", "snapshot_binding_or_policy_invalid"),
])
def test_invalid_evidence_is_retained_as_traceable_gap(target, key, value, reason):
    fixture = Fixture()
    record, field, snapshot = fixture.add()
    obj = {"record": record, "field": field, "snapshot": snapshot}[target]
    if target == "snapshot":
        setattr(obj, key, value)
    else:
        obj[key] = value
    result = fixture.run()
    assert not result.facts
    assert reason in result.gaps
    assert result.field_gaps[0]["record_version_id"] == "record-1"
    assert fixture.rows[0][0]["raw_row"]["OPERATE_INCOME"] == "100.5"


@pytest.mark.parametrize("value", [None, "", "NaN", "Infinity", True, "1e10000"])
def test_missing_and_unusable_numbers_are_not_zero(value):
    fixture = Fixture()
    fixture.add(value=value)
    result = fixture.run()
    assert not result.facts
    assert "value_missing_or_non_numeric" in result.gaps


def test_unknown_definition_does_not_inherit_current_python_mapping(monkeypatch):
    fixture = Fixture()
    fixture.add("segments", "MAIN_BUSINESS_INCOME")
    monkeypatch.setitem(FIELD_RULES, ("income_fields", "OPERATE_INCOME"), None)
    result = fixture.run()
    assert not result.facts and not result.dimensional_facts
    assert "field_definition_unconfirmed" in result.gaps


def test_cumulative_negative_quarters_ttm_and_formula_references():
    fixture = Fixture()
    for period, value in zip(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"], [100, 150, 140, 200]):
        fixture.add(value=value, period=period)
    result = fixture.run()
    quarters = sorted((f for f in result.facts if f.period_type == "single_quarter"), key=lambda f: f.period_end)
    assert [f.value for f in quarters] == [100, 50, -10, 60]
    ttm = [f for f in result.facts if f.period_type == "ttm"]
    assert len(ttm) == 1 and ttm[0].value == 200
    assert ttm[0].derived_from_fact_ids == [f.fact_id for f in quarters]
    assert quarters[2].method_ref == "structured-periods-v1.0.0"
    assert len(quarters[2].derived_from_fact_ids) == 2
    assert result.materialization_hash == fixture.run().materialization_hash
    fixture.rows.reverse()
    assert result.materialization_hash == fixture.run().materialization_hash


def test_missing_quarter_never_becomes_zero_or_ttm():
    fixture = Fixture()
    fixture.add(period="2025-03-31", value=0)
    fixture.add(period="2025-09-30", value=-10)
    result = fixture.run()
    assert [f.value for f in result.facts if f.period_type == "single_quarter"] == [0]
    assert not any(f.period_type == "ttm" for f in result.facts)
    assert any(g.startswith("single_quarter_missing") for g in result.gaps)


@pytest.mark.parametrize("dataset,raw", [("balance_fields", "MONETARYFUNDS"),
    ("income_fields", "BASIC_EPS"), ("baostock_daily", "close"), ("baostock_profit", "epsTTM")])
def test_stocks_ratios_quotes_and_supplier_eps_ttm_are_not_accumulated(dataset, raw):
    fixture = Fixture(embedded=True)
    for period in ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]:
        fixture.add(dataset, raw, 5, period)
    result = fixture.run()
    assert len(result.facts) == 4
    assert not any(f.derived_from_fact_ids for f in result.facts)


def test_explicit_revision_keeps_old_fact_and_cutoff_changes_selection_only():
    fixture = Fixture()
    old, _, _ = fixture.add(value=10, period="2024-06-30", available=datetime(2025, 1, 1, tzinfo=timezone.utc))
    fixture.add(value=12, period="2024-06-30", supersedes=old["record_version_id"])
    current = fixture.run()
    historical = fixture.run(as_of=datetime(2025, 1, 2, tzinfo=timezone.utc), strict_historical=True)
    assert len(current.facts) == 2
    assert current.selected_fact_ids == (next(f.fact_id for f in current.facts if f.value == 12),)
    assert historical.facts[0] == next(f for f in current.facts if f.value == 10)
    assert historical.materialization_hash != current.materialization_hash


@pytest.mark.parametrize("other_source", [False, True])
def test_conflicting_retrievals_or_sources_never_choose_by_hash(other_source):
    fixture = Fixture()
    fixture.add(value=10)
    fixture.add(value=12, source="source-2" if other_source else "structured-eastmoney")
    result = fixture.run()
    assert len(result.facts) == 2 and result.selected_fact_ids == ()
    assert any(g.startswith("source_or_revision_conflict") for g in result.gaps)


def test_different_sources_and_units_cannot_supply_missing_quarter():
    fixture = Fixture(embedded=True)
    fixture.add(value=10, period="2025-03-31")
    fixture.add(value=20, period="2025-06-30", source="source-2")
    result = fixture.run()
    assert len([f for f in result.facts if f.period_type == "single_quarter"]) == 1
    assert not any(f.period_type == "ttm" for f in result.facts)


def test_segments_keep_type_hierarchy_and_holder_identity():
    fixture = Fixture(embedded=True)
    for kind, code, name, parent in [("产品", "A", "酒", "ROOT"), ("产品", "B", "其它", "ROOT"), ("地区", "A", "中国", "REGION")]:
        fixture.add("segments", "MAIN_BUSINESS_INCOME", 10,
            row={"MAINOP_TYPE": kind, "ITEM_CODE": code, "ITEM_NAME": name, "ITEM_PARENT_CODE": parent, "ITEM_LEVEL": 2})
    for holder in ["甲", "乙"]:
        fixture.add("float_holders_history", "FREE_HOLDNUM_RATIO", 5, row={"HOLDER_NAME": holder})
    result = fixture.run()
    assert not result.facts
    assert len(result.dimensional_facts) == len(result.selected_dimensional_fact_ids) == 5
    assert len({f.dimensional_fact_id for f in result.dimensional_facts}) == 5
    assert result == fixture.run()


def test_plan_is_an_event_without_an_executed_cash_amount():
    fixture = Fixture()
    fixture.add("dividend", "PRETAX_BONUS_RMB", 10,
        row={"NOTICE_DATE": "2026-09-09", "ASSIGN_PROGRESS": "实施完成"})
    result = fixture.run()
    assert not result.facts
    assert len(result.events) == 1
    event = result.events[0]
    assert event.lifecycle_state == "announced_plan"
    assert event.amount is None and event.effective_at is None
    assert event.event_terms["cash_dividend_per_share_plan"] == 1
    assert event.metadata["original_value"] == 10
    assert event.metadata["original_unit"] == "CNY_per_10_shares"
    assert event == fixture.run().events[0]


def test_snapshot_observation_cannot_be_backdated_by_record():
    fixture = Fixture()
    record, _, _ = fixture.add()
    record["available_at"] = "2000-01-01T00:00:00+00:00"
    result = fixture.run(as_of=datetime(2025, 1, 1, tzinfo=timezone.utc), strict_historical=True)
    assert not result.facts


@pytest.mark.parametrize("revenue,cost,expected", [(0, 10, None), (-100, -120, -0.2), (100, 120, -0.2), (100, 0, 1)])
def test_gross_margin_checks_real_denominator_and_preserves_losses(revenue, cost, expected):
    fixture = Fixture()
    fixture.add(value=revenue)
    fixture.add(raw="OPERATE_COST", value=cost)
    result = fixture.run()
    ratios = [f for f in result.facts if f.metric_id == "gross_margin"]
    if expected is None:
        assert not ratios
        assert any("zero_denominator" in g for g in result.gaps)
    else:
        assert len(ratios) == 1 and ratios[0].value == pytest.approx(expected)
        assert len(ratios[0].derived_from_fact_ids) == 2
        assert ratios[0].metadata["value_kind"] == "ratio"


def test_ratio_inputs_from_different_sources_or_periods_are_not_mixed():
    fixture = Fixture()
    fixture.add(value=100)
    fixture.add(raw="OPERATE_COST", value=80, source="source-2")
    fixture.add(raw="OPERATE_COST", value=70, period="2025-12-31")
    result = fixture.run()
    assert not any(f.metric_id == "gross_margin" for f in result.facts)


def test_direct_single_quarter_is_preferred_and_ttm_reuses_it():
    fixture = Fixture(embedded=True)
    for period, value in zip(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"], [100, 150, 140, 200]):
        fixture.add(value=value, period=period)
    fixture.add("income_quarter", "OPERATE_INCOME", 50, "2025-06-30")
    result = fixture.run()
    quarters = [f for f in result.facts if f.period_type == "single_quarter"]
    assert len(quarters) == 4
    q2 = next(f for f in quarters if f.period_end.month == 6)
    assert not q2.derived_from_fact_ids
    ttm = next(f for f in result.facts if f.period_type == "ttm")
    assert ttm.value == 200 and q2.fact_id in ttm.derived_from_fact_ids


def test_revisions_with_equal_timestamp_and_future_observations_fail_closed():
    fixture = Fixture()
    old, _, _ = fixture.add(value=10)
    fixture.add(value=12, supersedes=old["record_version_id"])
    assert not fixture.run().selected_fact_ids
    future = Fixture()
    future.add(period="2027-06-30")
    assert "period_after_observation" in future.run().gaps


def test_percent_and_money_units_use_exact_frozen_multiplier():
    fixture = Fixture(embedded=True)
    fixture.add("baostock_daily", "turn", 5, "2025-12-31")
    fixture.add(value=2, rule={**deepcopy(fixture.rules["income_fields.OPERATE_INCOME"]),
        "stored_unit": "万元", "original_unit": "万元", "multiplier": "10000"})
    result = fixture.run()
    assert next(f.value for f in result.facts if f.metric_id == "turnover_rate") == 0.05
    amount = next(f for f in result.facts if f.metric_id == "operating_income" and f.period_type == "cumulative")
    assert amount.value == 20000 and amount.structured_admission.original_unit == "万元"


def test_unit_conflicts_and_scope_differences_do_not_deduplicate():
    fixture = Fixture(embedded=True)
    fixture.add(value=10)
    fixture.add(value=10)
    # Simulate two validated immutable definitions with contradictory units in a
    # selection input, without altering either raw record in storage.
    from analysis.structured.materialization import _select
    facts = list(fixture.run().facts)
    facts[1] = facts[1].model_copy(update={"unit": "shares"})
    selected, gaps = _select(facts)
    assert not selected and any("conflict" in g for g in gaps)
    facts[1] = facts[1].model_copy(update={"unit": "CNY", "scope": "parent"})
    assert len(_select(facts)[0]) == 2


def test_direct_quarter_disagreement_blocks_derivation_without_losing_evidence():
    fixture = Fixture(embedded=True)
    fixture.add(value=100, period="2025-03-31")
    fixture.add(value=150, period="2025-06-30")
    fixture.add("income_quarter", "OPERATE_INCOME", 60, "2025-06-30")
    result = fixture.run()
    assert len(result.facts) == 3
    assert not result.selected_fact_ids
    assert "quarter_cumulative_conflict:operating_income:2025-06-30" in result.gaps


def test_overflowing_inputs_and_derived_ttm_are_gaps():
    fixture = Fixture()
    fixture.add(value="1e1000000")
    assert "value_missing_or_non_numeric" in fixture.run().gaps
    quarters = Fixture(embedded=True)
    for period in ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]:
        quarters.add("income_quarter", "OPERATE_INCOME", "1e308", period)
    result = quarters.run()
    assert len(result.facts) == 4
    assert not any(f.period_type == "ttm" for f in result.facts)


def test_registered_same_source_ttm_is_preferred():
    fixture = Fixture(embedded=True)
    for period, value in zip(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"], [100, 150, 140, 200]):
        fixture.add(value=value, period=period)
    rule = {**fixture.rules["income_fields.OPERATE_INCOME"], "raw_field": "REVENUE_TTM",
            "period_kind": "ttm", "definition_id": "fixture:revenue_ttm"}
    fixture.add("income_fields", "REVENUE_TTM", 200, "2025-12-31", rule=rule)
    result = fixture.run()
    ttm = [f for f in result.facts if f.period_type == "ttm"]
    assert len(ttm) == 1 and not ttm[0].derived_from_fact_ids
