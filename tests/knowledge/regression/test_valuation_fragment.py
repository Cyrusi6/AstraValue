"""Valuation scenario expectations fixed before writing the method fragment."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FRAGMENT = ROOT / "config/methods/knowledge/catalog.v1.json"


def fragment():
    assert FRAGMENT.exists(), "估值和情景11题来源/方法片段尚未实现"
    catalog = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    catalog["methods"] = [m for m in catalog["methods"] if any(q.startswith(("ES05.", "ES08.")) for q in m["question_ids"])]
    return catalog


def test_valuation_fragment_has_exact_eleven_questions_and_local_bodies():
    data = fragment()
    expected = {f"ES05.Q{i:02}" for i in range(1, 7)} | {f"ES08.Q{i:02}" for i in range(1, 6)}
    assert {q for method in data["methods"] for q in method["question_ids"]} == expected
    for method in data["methods"]:
        assert (ROOT / method["body_path"]).exists()
        assert len(method["steps"]) >= 3
        assert method["required_inputs"] and method["counterexamples"] and method["industry_gaps"]


def test_valuation_scenario_boundary_expectations_are_not_reversed():
    cases = {c["case_id"]: c for c in fragment()["cases"]}
    assert "same_industry_not_comparable" in cases["ES05.Q03.counterexample"]["judgment_codes"]
    assert "ev_ebitda_not_universal" in cases["ES05.Q05.counterexample"]["judgment_codes"]
    case = cases["ES08.Q02.counterexample"]
    assert "scenario_weights_not_risk_adjustment" in case["judgment_codes"]
    assert sum(p * cf for p, cf in zip(case["facts"]["probabilities"], case["facts"]["cash_flows"])) == pytest.approx(95)
    assert case["expected_values"]["expected_cash_flow"] == 95


def test_cashflow_and_discount_sensitivity_examples_reproduce_independently():
    cases = {c["case_id"]: c for c in fragment()["cases"]}
    cash = cases["ES08.Q03.normal"]
    f = cash["facts"]
    assert f["ebit"] * (1-f["tax_rate"]) - (f["capex"]-f["depreciation"]) - f["working_capital_change"] == cash["expected_values"]["fcff"] == 65
    assert f["net_income"] / f["constant_shares"] == cash["expected_values"]["illustrative_eps"] == 2
    sensitivity = cases["ES05.Q06.normal"]
    f = sensitivity["facts"]
    assert f["next_fcff"] / (f["wacc_low"]-f["growth"]) == pytest.approx(sensitivity["expected_values"]["value_low_wacc"])
    assert f["next_fcff"] / (f["wacc_high"]-f["growth"]) == pytest.approx(sensitivity["expected_values"]["value_high_wacc"])


def test_every_valuation_method_has_independent_case_boundaries_and_source_reviews():
    data = fragment(); cases = {c["case_id"]: c for c in data["cases"]}
    for method in data["methods"]:
        selected = [cases[cid] for cid in method["case_ids"]]
        assert {"normal", "counterexample", "missing"} <= {c["kind"] for c in selected}
        for case in selected:
            assert case["facts"]["synthetic"] is True
            assert case["expected_factors"] and case["supported_conclusions"] and case["forbidden_conclusions"]
            assert case["supported_conclusions"] != case["forbidden_conclusions"]
            assert case["source_refs"] and case["reason"]
        reviews = [r for r in data["reviews"] if r["method_id"] == method["method_id"]]
        assert {r["kind"] for r in reviews} >= {"source", "case"}
        assert all(r["reasons"] and r["reviewer"] for r in reviews)
        assert not any(r["kind"] == "human" for r in reviews)
