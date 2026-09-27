"""首批内容契约。程序检查引用/案例边界；自然语言依据仍需独立内容审阅。"""
from pathlib import Path
import json
import pytest

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "config/methods/knowledge/catalog.v1.json"


@pytest.fixture
def catalog():
    assert CATALOG.exists(), "首批真实来源、方法及案例目录尚未建设"
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def by_id(records, key):
    return {record[key]: record for record in records}


def test_pilot_has_three_distinct_method_paths_with_honest_remaining_scope(catalog):
    methods = by_id(catalog["methods"], "method_id")
    expected = {
        "knowledge.pricing_power": "ES01.Q10",
        "knowledge.roic": "ES02.Q04",
        "knowledge.working_capital": "ES02.Q08",
    }
    for method_id, question_id in expected.items():
        m = methods[method_id]
        assert question_id in m["question_ids"]
        assert (ROOT / m["body_path"]).is_file()
        assert m["industry_gaps"], "通用样例不可假装包含全部行业能力"
        assert m["limitations"]
    other_roe_paths = [m for m in methods.values() if "ES02.Q04" in m["question_ids"]
                       and m["method_id"] != "knowledge.roic"]
    assert other_roe_paths, "只写ROIC不能代表ROE/杜邦；须另有待补或已验证的实质路径"
    for method in other_roe_paths:
        if method["content_status"] == "published":
            assert method["rules"] and method["case_ids"]


def test_verified_rules_have_versioned_primary_source_locations(catalog):
    sources = by_id(catalog["sources"], "source_id")
    for m in catalog["methods"]:
        if m["content_status"] == "skeleton":
            continue
        assert m["rules"]
        for rule in m["rules"]:
            assert rule["source_refs"]
            for ref in rule["source_refs"]:
                source = sources[ref["source_id"]]
                assert ref["version"] == source["version"]
                assert source["verification_status"] == "verified"
                assert len(source["content_sha256"]) == 64
                assert ref["locator"] and ref["support"]


def test_ifrs3_duplicate_uses_canonical_source_and_historical_alias(catalog):
    sources = [s for s in catalog["sources"] if s["source_id"] in {
        "SRC-IFRS-IFRS3-OVERVIEW", "SRC-IFRS3-OVERVIEW"
    }]
    assert [s["source_id"] for s in sources] == ["SRC-IFRS3-OVERVIEW"]
    aliases = [a for a in catalog["source_aliases"] if a["alias_source_id"] == "SRC-IFRS-IFRS3-OVERVIEW"]
    assert aliases == [{
        "alias_source_id": "SRC-IFRS-IFRS3-OVERVIEW",
        "alias_version": "snapshot-2026-09-19",
        "canonical_source_id": "SRC-IFRS3-OVERVIEW",
        "canonical_version": "2026-09-19",
        "status": "historical_alias",
        "scope": "historical bundle/manifest only",
        "reason": "同一 IFRS Foundation 公开概览 URL 与内容哈希；统一当前知识目录身份，保留旧快照可复核。",
    }]
    refs = [r for m in catalog["methods"] for rule in m.get("rules", []) for r in rule.get("source_refs", [])]
    refs.extend(r for c in catalog["cases"] for r in c.get("source_refs", []))
    assert not any(r["source_id"] == "SRC-IFRS-IFRS3-OVERVIEW" for r in refs)


def test_cases_cover_positive_counterexample_and_missing_for_each_pilot(catalog):
    cases = by_id(catalog["cases"], "case_id")
    for method_id in ("knowledge.pricing_power", "knowledge.roic", "knowledge.working_capital"):
        method = by_id(catalog["methods"], "method_id")[method_id]
        selected = [cases[case_id] for case_id in method["case_ids"]]
        kinds = {case["kind"] for case in selected}
        assert {"normal", "counterexample", "missing"} <= kinds
        assert any(case.get("pair_id") for case in selected)
        for case in selected:
            assert case["expected_factors"] and case["supported_conclusions"]
            assert case["forbidden_conclusions"] and case["reason"] and case["source_refs"]
            assert case["facts"].get("synthetic") is True


def test_pricing_cases_do_not_equate_mix_with_same_product_price(catalog):
    cases = by_id(catalog["cases"], "case_id")
    mixed = cases["pricing.mix_only"]
    facts = mixed["facts"]
    before = sum(p*q for p,q in zip(facts["prices_before"], facts["quantities_before"])) / sum(facts["quantities_before"])
    after = sum(p*q for p,q in zip(facts["prices_after"], facts["quantities_after"])) / sum(facts["quantities_after"])
    assert facts["prices_before"] == facts["prices_after"]
    assert after > before
    assert mixed["judgment_codes"] == ["unit_value_rise", "mix_explanation", "no_same_product_price_claim"]
    sufficient = cases["pricing.profitable_increase"]
    assert sufficient["facts"]["quantity_after"] < sufficient["facts"]["quantity_before"]
    assert "bounded_profitable_increase" in sufficient["judgment_codes"]
    assert "no_permanent_moat_claim" in sufficient["judgment_codes"]
    assert cases["pricing.missing_channel"]["pair_id"] == sufficient["pair_id"]


def test_roic_cases_keep_beginning_and_average_capital_separate(catalog):
    cases = by_id(catalog["cases"], "case_id")
    normal = cases["roic.consistent"]
    facts = normal["facts"]
    nopat = facts["ebit"] * (1-facts["tax_rate"])
    assert nopat / facts["begin_capital"] == pytest.approx(0.12)
    assert nopat / ((facts["begin_capital"]+facts["end_capital"])/2) == pytest.approx(0.10)
    assert normal["expected_values"] == {"beginning_roic": 0.12, "average_roic": 0.10}
    assert "reject_profit_after_interest_as_nopat" in cases["roic.tax_shield_mismatch"]["judgment_codes"]
    assert "do_not_substitute_ending_capital" in cases["roic.missing_opening"]["judgment_codes"]


def test_working_capital_cases_distinguish_reinvestment_and_bank_scope(catalog):
    cases = by_id(catalog["cases"], "case_id")
    normal = cases["working_capital.operating_release"]
    f = normal["facts"]
    assert f["receivables_change"] + f["inventory_change"] - f["payables_change"] == -10
    assert "temporary_cash_release" in normal["judgment_codes"]
    assert "not_operating_improvement" in cases["working_capital.overdue_payables"]["judgment_codes"]
    assert "bank_generic_path_not_applicable" in cases["working_capital.bank"]["judgment_codes"]
    assert "do_not_zero_fill" in cases["working_capital.missing_components"]["judgment_codes"]


def test_working_capital_full_fcff_case_is_not_a_cashflow_proxy(catalog):
    case = by_id(catalog["cases"], "case_id")["working_capital.fcff_bridge"]
    facts = case["facts"]
    assert facts["nopat"] + facts["depreciation"] - facts["capex"] - facts["change_nwc"] == 80
    assert case["expected_values"]["fcff"] == 80
    assert "cash_flow_proxy_not_fcff" in case["judgment_codes"]


def test_inputs_do_not_invent_metric_matches_or_human_acceptance(catalog):
    for method in catalog["methods"]:
        for inp in method["required_inputs"]:
            if inp["kind"] == "text":
                assert inp["locator_requirement"]
            elif inp["binding_status"] == "matched":
                assert all(inp.get(k) for k in ("metric_id", "definition_version", "definition_sha256", "expected_definition"))
    assert not any(r["kind"] == "human" and r["outcome"] == "passed" for r in catalog["reviews"])


def test_expansion_covers_exact_authoritative_54_questions_without_skeletons(catalog):
    authority = json.loads((ROOT / "config/structured_data/research_requirements.v1.json").read_text(encoding="utf-8"))
    expected = {q["question_id"] for q in authority["questions"]}
    mapped = {q for m in catalog["methods"] for q in m["question_ids"]}
    assert len(expected) == 54
    assert mapped == expected, f"未建设: {sorted(expected - mapped)}; 非法编号: {sorted(mapped - expected)}"
    assert not [m["method_id"] for m in catalog["methods"] if m["content_status"] != "published"]
    for m in catalog["methods"]:
        assert len(m["steps"]) >= 3 and m["rules"] and m["required_inputs"]
        assert m["counterexamples"] and m["limitations"] and m["industry_gaps"]


def test_expansion_every_method_has_reasoned_positive_negative_and_missing_cases(catalog):
    cases = by_id(catalog["cases"], "case_id")
    for m in catalog["methods"]:
        selected = [cases[c] for c in m["case_ids"]]
        assert {"normal", "counterexample", "missing"} <= {c["kind"] for c in selected}, m["method_id"]
        for c in selected:
            assert c["facts"]["synthetic"] is True
            assert c["supported_conclusions"] != c["forbidden_conclusions"]
            assert c["source_refs"] and c["reason"] and c["expected_factors"]


def test_expansion_key_counterexamples_preserve_economic_boundaries(catalog):
    cases = by_id(catalog["cases"], "case_id")
    expected = {
        "ES01.Q02.counterexample": "revenue_is_not_segment_profit",
        "ES02.Q01.counterexample": "consolidation_breaks_comparability",
        "ES02.Q09.counterexample": "ending_shares_not_weighted_average",
        "ES03.Q05.counterexample": "approval_not_performance",
        "ES04.Q02.counterexample": "repurchase_plan_not_completion",
        "ES05.Q03.counterexample": "same_industry_not_comparable",
        "ES05.Q05.counterexample": "ev_ebitda_not_universal",
        "ES06.Q03.counterexample": "risk_disclosure_not_occurred_event",
        "ES07.Q04.counterexample": "selected_peers_not_market_total",
        "ES08.Q02.counterexample": "scenario_weights_not_risk_adjustment",
    }
    for case_id, boundary in expected.items():
        assert boundary in cases[case_id]["judgment_codes"]
