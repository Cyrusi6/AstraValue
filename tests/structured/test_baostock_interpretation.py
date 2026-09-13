from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.structured.consumption import is_fact_consumable
from analysis.structured.interpretation import load_interpretation, validate_interpretation
from analysis.structured.materialization import StructuredFactMaterializer
from analysis.structured.materialization_replay import replay
from analysis.structured.registry import PROJECT_ROOT, StructuredRegistryLoader
from analysis.structured.runtime import _build_sources
from analysis.structured.service import StructuredDataService
from analysis.structured.storage import canonical_sha256
from test_materialization import Fixture
from test_materialization_projection import NoWaitGate
from test_protocols import FakeResult

ID = "baostock-interpretation-v1.0.0"
EFFECTIVE = datetime(2026, 9, 12, 6, 20, tzinfo=timezone.utc)
OLD_HASH = "dc7e9581868f0331fa6856c3fa4658d66d0fbc3f41e0af2955360ce80cfbb618"


def fixture(version="1.0.0"):
    f = Fixture()
    f.context.field_registry_version = version
    if version == "1.0.0":
        f.context.field_registry_hash = OLD_HASH
    source = next(s for s in _build_sources(StructuredRegistryLoader().load()).values()
                  if s.source_definition_id == "structured-baostock").to_mapping()
    if version == "1.0.0":
        source["version"] = "1.0.0"
        source["content_hash"] = canonical_sha256({k:v for k,v in source.items() if k != "content_hash"})
    f.context.frozen_config["source_definitions"] = [source]
    f.interpretation = load_interpretation(ID, f.context)
    return f


def add(f, dataset="daily", raw="turn", value="56.827692", period="2025-06-30", **kwargs):
    dataset = "baostock_" + dataset
    rule = f.interpretation["rules"][dataset+"."+raw]
    record, field, snapshot = f.add(dataset=dataset, raw=raw, value=value, period=period,
        rule=rule, source="structured-baostock", **kwargs)
    source = f.context.frozen_config["source_definitions"][0]
    snapshot.source_definition_version = source["version"]
    for job in f.jobs:
        job["source_definition_version"] = source["version"]
    field.update(rule["input_descriptors"][f.context.field_registry_version])
    field["definition_version"] = f.context.field_registry_version
    if dataset == "baostock_daily":
        record["raw_row"].setdefault("adjustflag", "3")
        record["raw_row"].setdefault("tradestatus", "1")
        record["raw_row"].setdefault("preclose", "10")
        date_field = "date"
    elif dataset == "baostock_basic":
        date_field = "ipoDate"
    elif dataset == "baostock_adjust":
        date_field = "__retrieved_at" if f.context.field_registry_version == "1.0.0" else "dividOperateDate"
        record["raw_row"]["dividOperateDate"] = period
        if date_field == "__retrieved_at":
            field["period_key"] = record["observed_at"]
    else:
        date_field = "statDate"
    record["raw_row"][date_field] = field["period_key"]
    for d in f.context.frozen_config["datasets"]:
        if d["dataset_id"] == dataset:
            d["date_fields"] = [date_field]
    return record, field, snapshot


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
@pytest.mark.parametrize("dataset,raw,value,expected,unit,period_kind", [
    ("daily", "turn", "56.827692", "0.56827692", "ratio", "market_quote"),
    ("daily", "pctChg", "-13.252630", "-0.13252630", "ratio", "market_quote"),
    ("daily", "volume", "40631800", "40631800", "shares", "market_quote"),
    ("daily", "amount", "1410347179.0000", "1410347179.0000", "CNY", "market_quote"),
    ("profit", "npMargin", "0.353655", "0.353655", "ratio", "reported_period"),
    ("profit", "epsTTM", "1.746599", "1.746599", "CNY_per_share", "ttm"),
    ("profit", "netProfit", "-561440706.10", "-561440706.10", "CNY", "reported_period"),
    ("profit", "MBRevenue", "83354000000", "83354000000", "CNY", "reported_period"),
    ("profit", "totalShare", "943800000.00", "943800000.00", "shares", "instant"),
    ("operation", "NRTurnDays", "4.880525", "4.880525", "days", "cumulative"),
    ("balance", "assetToEquity", "1.678443", "1.678443", "ratio", "instant"),
    ("adjust", "foreAdjustFactor", "0.130391", "0.130391", "ratio", "instant"),
])
def test_units_periods_and_original_versions(version, dataset, raw, value, expected, unit, period_kind):
    f = fixture(version)
    record, field, _ = add(f, dataset, raw, value)
    original = deepcopy((f.context, f.rows))
    result = f.run(interpretation_contract=ID)
    assert len(result.facts) == 1, result.gaps
    fact = result.facts[0]
    assert Decimal(fact.metadata["decimal_value"]) == Decimal(expected)
    assert fact.value == float(expected) and fact.unit == unit and fact.period_type == period_kind
    assert fact.as_of == EFFECTIVE
    assert fact.metadata["field_definition_version"] == version
    assert fact.structured_admission.field_definition_version == ID
    assert fact.metadata["original_field_descriptor"]["unit"] == field["unit"]
    assert fact.metadata["snapshot_sha256"] == fact.structured_admission.snapshot_sha256
    assert (f.context, f.rows) == original
    assert result.interpretation_contract == f.interpretation
    assert result == f.run(interpretation_contract=ID)
    assert is_fact_consumable(fact, materialization_selected_ids=frozenset(result.selected_fact_ids))
    assert not fact.derived_from_fact_ids
    if period_kind == "reported_period":
        assert fact.period_start is None
        assert fact.metadata["semantic_gaps"] == ["period_window_unconfirmed"]


def test_definition_cutoff_and_default_legacy_are_separate():
    f = fixture()
    add(f)
    before = f.run()
    assert not before.facts
    assert "interpretation_contract" not in before.to_mapping()
    early = f.run(interpretation_contract=ID, strict_historical=True, as_of=EFFECTIVE-timedelta(microseconds=1))
    assert not early.facts
    assert early.gaps == ("interpretation_not_effective_at_cutoff",)
    current = f.run(interpretation_contract=ID, strict_historical=True, as_of=EFFECTIVE)
    assert len(current.facts) == 1
    assert f.run() == before


@pytest.mark.parametrize("target,key,value", [
    ("field", "quality", "failed"), ("field", "nature", "forecast"),
    ("field", "nature", "provider_estimate"), ("field", "unit", "万元"),
    ("field", "definition_version", None), ("field", "field_path", "$.other"),
    ("field", "value", "wrong"), ("snapshot", "policy_decision", "denied"),
    ("snapshot", "storage_namespace_id", "other"), ("record", "observed_at", None),
    ("record", "_committed_attempt_outcome", "parse_failed"),
])
def test_reinterpretation_does_not_override_evidence_or_quality_failure(target, key, value):
    f = fixture()
    record, field, snapshot = add(f)
    if target == "snapshot":
        setattr(snapshot, key, value)
    else:
        {"record":record, "field":field}[target][key] = value
    result = f.run(interpretation_contract=ID)
    assert not result.facts and result.field_gaps


@pytest.mark.parametrize("raw,value,row,reason", [
    ("turn", "", {"tradestatus":"0"}, "value_missing_or_non_numeric"),
    ("close", "NaN", {}, "value_missing_or_non_numeric"),
    ("amount", "-1", {}, "market_value_negative"),
    ("close", "11", {"tradestatus":"0", "preclose":"10"}, "suspended_price_not_previous_close"),
    ("volume", "1", {"tradestatus":"0"}, "suspended_volume_or_amount_nonzero"),
    ("pctChg", "0", {"preclose":"0"}, "price_change_zero_denominator"),
    ("close", "10", {"adjustflag":"2"}, "market_frequency_or_adjustment_mismatch"),
    ("tradestatus", "3", {}, "categorical_value_outside_contract"),
])
def test_market_semantics_fail_with_explicit_gaps(raw, value, row, reason):
    f = fixture()
    add(f, raw=raw, value=value, row=row)
    result = f.run(interpretation_contract=ID)
    assert not result.facts and reason in result.gaps


def test_suspended_zero_is_preserved_and_basic_status_not_backdated_to_ipo():
    f = fixture()
    add(f, raw="volume", value="0", row={"tradestatus":"0"})
    add(f, "basic", "status", "1", period="2001-08-27")
    result = f.run(interpretation_contract=ID)
    by_metric = {x.metric_id:x for x in result.facts}
    assert by_metric["market_volume"].value == 0
    status = by_metric["baostock_listing_status"]
    assert status.period_end.isoformat() == "2026-09-10"
    assert status.metadata["value_kind"] == "label"


def test_contract_source_and_evidence_tampering_fail_closed():
    f = fixture()
    with pytest.raises(ValueError, match="unknown interpretation"):
        load_interpretation("../unregistered", f.context)
    contract = deepcopy(f.interpretation)
    contract["rules"]["baostock_daily.turn"]["multiplier"] = "100"
    with pytest.raises(ValueError, match="hash/schema"):
        validate_interpretation(contract, f.context)
    f.context.frozen_config["source_definitions"][0]["queries"][0]["endpoint"] = "other"
    with pytest.raises(ValueError, match="incompatible"):
        load_interpretation(ID, f.context)


class Sdk:
    calls = 0
    def login(self):
        return SimpleNamespace(error_code="0", error_msg="")
    def logout(self):
        return SimpleNamespace(error_code="0", error_msg="")
    def query_stock_basic(self, **kwargs):
        self.calls += 1
        return FakeResult(fields=("code", "code_name", "ipoDate", "outDate", "type", "status"),
            rows=[("sh.600519", "贵州茅台", "2001-08-27", "", "1", "1")])


def test_normal_bound_service_freezes_contract_and_readonly_cli_exports(tmp_path, monkeypatch, capsys):
    from analysis import cli
    from analysis.structured import materialization_replay
    sdk = Sdk()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP")))
    db, data = tmp_path/"bound.db", tmp_path/"data"
    with AcquisitionRuntime.create(db, data, http_client=client, sleeper=lambda _:None) as runtime:
        runtime.source_gate = NoWaitGate()
        service = StructuredDataService.from_runtime(runtime, sdk=sdk)
        run = service.plan("600519", mode="baseline", company_scope="company-only", datasets=("baostock_basic",),
            as_of=datetime(2026,9,1,tzinfo=timezone.utc))["run_ids"][0]
        assert service.run(run)["status"]["succeeded"] == 1
        context_before = service.storage.get_run_context(run)
        result = service.materialize(run, research_scope=False, interpretation_contract=ID)
        assert result["fact_count"] == 2 and result["persisted"]
        stored = runtime.report_storage.get_materialization(result["materialization_hash"])
        assert stored["interpretation_contract"]["contract_id"] == ID
        assert result == service.materialize(run, research_scope=False, interpretation_contract=ID)
        assert context_before == service.storage.get_run_context(run)
        for f in result["facts"]:
            lineage = runtime.report_storage.fact_lineage(f["fact_id"])
            assert lineage["structured_evidence"]["field"]["definition_version"] == "1.1.0"
            assert f["structured_admission"]["field_definition_version"] == ID
        monkeypatch.setattr(cli, "_create_structured_service", lambda *_: service)
        assert cli.main(["structured", "materialize", run, "--db", str(db), "--data-root", str(data),
            "--interpretation-contract", ID, "--legacy-contract-replay", "--no-persist", "--summary", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["fact_count"] == 2
        assert sdk.calls == 1
    client.close()
    output = tmp_path/"replay"
    monkeypatch.setattr("sys.argv", ["replay", run, "--db", str(db), "--data-root", str(data),
        "--interpretation-contract", ID, "--output-dir", str(output)])
    materialization_replay.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["fact_count"] == 2 and summary["repeat_hash_equal"]
    assert summary["new_acquisition_attempts"] == 0
    assert len((output/"facts.jsonl").read_text(encoding="utf8").splitlines()) == 2
    assert json.loads((output/"manifest.json").read_text(encoding="utf8"))["projection_namespace_id"] is None
    from analysis.structured.materialization_audit import audit
    checked = audit(db, data, output)
    assert checked["fact_count_verified"] == 2 and checked["all_mapped_numeric_inputs_accounted_for"]
    with (output/"facts.jsonl").open("a", encoding="utf8") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="export file hash"):
        audit(db, data, output)
    with pytest.raises(ValueError, match="separate"):
        replay(db, data, run, output_dir=data/"unsafe")
