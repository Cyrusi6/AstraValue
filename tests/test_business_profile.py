from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis.business_evidence.models import Citation, FactInput, ReviewBatch
from analysis.business_evidence.profile import ProfileInput, build_profile
from analysis.business_evidence.store import FactStore


class Corpus:
    db = None
    selection_policy = "current-policy"
    manifest = SimpleNamespace(manifest_id="m1", manifest_hash="a" * 64, storage_namespace_id="n")
    tamper = False
    excluded = False

    def validate(self):
        pass

    def citation(self, c, company):
        if self.excluded:
            raise ValueError("citation_excluded_by_current_selection")
        return {**c.model_dump(), "manifest_id": "m1", "title": "更正公告" if c.snapshot_id == "new" else "年报",
                "published_at": "2024-03-01T00:00:00Z", "source_url": "https://example.test/report.pdf",
                "page_sha256": ("b" if self.tamper else "a") * 64,
                "available_at": "2024-04-01T00:00:00+00:00" if c.snapshot_id == "new" else "2024-03-01T00:00:00+00:00"}

    def document(self, *_):
        return {"company_id": "600519", "exclusion": None, "title": "年报",
                "snapshot": SimpleNamespace(available_at=datetime(2024, 3, 1, tzinfo=timezone.utc)),
                "artifact": SimpleNamespace(output_sha256="a" * 64), "pages": {1: "渠道与产品数据在下一页。", 2: "收入数据。"}}


def record(name="a", value="100", **changes):
    data = dict(record_id=name, company_id="600519", subject="示例公司", subject_role="company", period="2023",
        metric="operating_revenue", dimensions={"business": "酒类", "channel": "直销"}, basis="合并主营业务",
        unit="CNY", event_stage="reported", value_type="decimal", value=value, question_ids=["Q02"],
        citations=[dict(snapshot_id="old", derived_artifact_id="text", page_number=2, section="收入表",
                        quote="本期收入 100 元、200 元和销售量 10 吨，更正披露 0 元。")])
    data.update(changes)
    return FactInput(**data)


def selector(f):
    return f.model_dump(include={"subject", "subject_role", "metric", "dimensions", "basis", "unit", "event_stage"})


def spec(f):
    return dict(schema_version="1.0.0", title="示例业务画像", company_id="600519", as_of="2026-09-08T00:00:00Z",
        periods=["2023", "2024", "2026H1"], series=[dict(series_id="revenue", label="收入", selector=selector(f), note="明确口径")],
        statements=[dict(statement_id="s", question_id="Q02", kind="disclosed_fact", text="收入为{{cell:revenue:2023}}。")],
        gaps=[dict(gap_id="remaining", question_ids=[f"Q{i:02d}" for i in range(1, 11) if i != 2],
                   kind="external_evidence_needed", description="需补充证据", next_action="定向查阅已存材料")])


def store_records(tmp_path, *records):
    store = FactStore(tmp_path / "facts.db", "n", create=True)
    corpus = Corpus()
    store.import_batch(ReviewBatch(schema_version="1.0.0", reviewer="test", records=records), corpus)
    return store, corpus


def test_profile_real_store_replay_periods_missing_and_stable_identity(tmp_path):
    f = record()
    store, corpus = store_records(tmp_path, f)
    data = spec(f)
    data["statements"][0]["text"] = "披露期间为{{period:" + f.fact()["fact_id"] + "}}，收入为{{cell:revenue:2023}}。"
    request = ProfileInput(**data)
    result = build_profile(request, store, corpus, tmp_path)
    assert result["acceptance"]["human_profile_acceptance"] == "pending"
    assert result["series"][0]["cells"]["2024"]["status"] == "missing"
    assert len(result["topics"]) == 10
    assert [p["kind"] for p in result["periods"]] == ["full_year", "full_year", "half_year"]
    assert result["facts"][0]["citations"][0]["page_number"] == 2
    assert result == build_profile(request, store, corpus, tmp_path)
    assert result["profile_id"].startswith("profile-")


@pytest.mark.parametrize("change", [{"dimensions": {"business": "其他"}}, {"unit": "CNY_10000"},
    {"basis": "母公司"}, {"subject": "另一家公司"}, {"event_stage": "planned"}, {"period": "2024"}])
def test_exact_identity_prevents_silent_series_fill(tmp_path, change):
    wanted, other = record(), record(**change)
    store, corpus = store_records(tmp_path, other)
    request = spec(wanted)
    request["statements"] = []
    request["gaps"][0]["question_ids"].append("Q02")
    result = build_profile(ProfileInput(**request), store, corpus, tmp_path)
    assert result["series"][0]["cells"]["2023"]["status"] == "missing"


def test_conflicts_are_not_numeric_and_explicit_correction_retains_history(tmp_path):
    old = record()
    new = record("new", "200", revision="corrected", citations=[dict(snapshot_id="new", derived_artifact_id="text",
        page_number=2, section="收入更正", quote="更正披露收入从 100 元调整为 200 元。")])
    store, corpus = store_records(tmp_path, old, new)
    request = spec(old)
    with pytest.raises(ValueError, match="not_current"):
        build_profile(ProfileInput(**request), store, corpus, tmp_path)
    request["statements"] = []
    request["gaps"][0]["question_ids"].append("Q02")
    result = build_profile(ProfileInput(**request), store, corpus, tmp_path)
    assert result["series"][0]["cells"]["2023"]["status"] == "conflict"
    store.import_batch(ReviewBatch(schema_version="1.0.0", reviewer="test", records=[new], corrections=[dict(
        previous_record_id="a", replacement_record_id="new", reason="公告更正", citation=new.citations[0])]), corpus)
    result = build_profile(ProfileInput(**request), store, corpus, tmp_path)
    assert result["series"][0]["cells"]["2023"]["value"] == "200"
    assert {f["status"] for f in result["facts"]} == {"current", "superseded"}


@pytest.mark.parametrize("failure", ["future", "tamper", "excluded", "foreign"])
def test_current_evidence_cannot_bypass_cutoff_provenance_selection_or_company(tmp_path, failure):
    f = record()
    store, corpus = store_records(tmp_path, f)
    request = spec(f)
    if failure == "future":
        request.update(as_of="2024-01-01T00:00:00Z", periods=["2023"])
    elif failure == "tamper":
        corpus.tamper = True
    elif failure == "excluded":
        corpus.excluded = True
    else:
        request["company_id"] = "000001"
    with pytest.raises(ValueError):
        build_profile(ProfileInput(**request), store, corpus, tmp_path)


def test_company_claim_label_and_unbound_numbers_are_rejected(tmp_path):
    claim = record(event_stage="company_claim")
    store, corpus = store_records(tmp_path, claim)
    request = spec(record())
    request["statements"][0].update(text="公司陈述。", fact_ids=[claim.fact()["fact_id"]])
    with pytest.raises(ValueError, match="company_claim"):
        build_profile(ProfileInput(**request), store, corpus, tmp_path)
    request["statements"][0].update(kind="company_statement", limitations=["仍需外部验证"])
    assert build_profile(ProfileInput(**request), store, corpus, tmp_path)["statements"][0]["kind"] == "company_statement"
    request["statements"][0]["text"] = "收入增长50%。"
    with pytest.raises(ValueError, match="numeric_narrative"):
        ProfileInput(**request)


def test_decimal_ratio_explicit_units_and_invalid_scope(tmp_path):
    revenue, total = record(), record("total", "200", dimensions={"business": "酒类"})
    volume = record("volume", "10", metric="sales_volume", unit="tonne", basis="商品酒销量")
    store, corpus = store_records(tmp_path, revenue, total, volume)
    request = spec(revenue)
    request["series"] += [dict(series_id=k, label=k, selector=selector(f), note="显式口径") for k, f in [("total", total), ("volume", volume)]]
    request["calculations"] = [dict(series_id="share", label="占比", operation="share_pct", numerator="revenue", denominator="total", note="主营业务占比"),
        dict(series_id="unit_revenue", label="混合单位收入", operation="blended_unit_revenue", numerator="revenue", denominator="volume", note="不是产品价格")]
    result = build_profile(ProfileInput(**request), store, corpus, tmp_path)
    assert result["series"][-2]["cells"]["2023"]["value"] == "50"
    assert result["series"][-1]["cells"]["2023"]["value"] == "10"
    assert result["series"][-1]["cells"]["2024"]["status"] == "missing"
    assert len(result["series"][-1]["cells"]["2023"]["input_fact_ids"]) == 2
    request["series"][2]["selector"]["dimensions"] = {"business": "酒类", "channel": "批发"}
    with pytest.raises(ValueError, match="scope_or_unit"):
        build_profile(ProfileInput(**request), store, corpus, tmp_path)


def test_explicit_period_override_and_gross_margin_with_zero_revenue(tmp_path):
    revenue = record()
    later = record("h1", "0", period="2026H1", unit="CNY_10000")
    cost = record("cost", "10", metric="operating_cost")
    later_cost = record("cost_h1", "10", period="2026H1", metric="operating_cost")
    store, corpus = store_records(tmp_path, revenue, later, cost, later_cost)
    request = spec(revenue)
    request["series"][0]["selector_overrides"] = {"2026H1": selector(later)}
    request["series"].append(dict(series_id="cost", label="成本", selector=selector(cost), note="合并成本"))
    request["calculations"] = [dict(series_id="margin", label="毛利率", operation="gross_margin_pct", numerator="revenue", denominator="cost", note="显式换算")]
    result = build_profile(ProfileInput(**request), store, corpus, tmp_path)
    assert result["series"][-1]["cells"]["2023"]["value"] == "90"
    assert result["series"][-1]["cells"]["2026H1"]["status"] == "zero_denominator"
    assert result["series"][-1]["cells"]["2026H1"]["operand_scales"] == ["10000", "1"]
    request["series"][0]["selector_overrides"]["2026H1"]["subject"] = "子公司"
    with pytest.raises(ValueError, match="changes_subject"):
        ProfileInput(**request)


def test_missing_topics_and_search_without_scope_are_not_accepted(tmp_path):
    request = spec(record())
    request["gaps"] = []
    with pytest.raises(ValueError, match="ten_topics"):
        ProfileInput(**request)
    request = spec(record())
    request["gaps"][0]["kind"] = "not_found_in_reviewed_materials"
    with pytest.raises(ValueError, match="document_scope"):
        ProfileInput(**request)
    request["gaps"][0]["searches"] = [dict(snapshot_id="old", derived_artifact_id="text", pages=[9], keywords=["渠道"])]
    store, corpus = store_records(tmp_path, record())
    with pytest.raises(ValueError, match="page_missing"):
        build_profile(ProfileInput(**request), store, corpus, tmp_path)
