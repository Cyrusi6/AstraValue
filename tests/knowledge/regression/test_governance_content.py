"""Governance outcomes are evidence states, never inferred from a proposal."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def fragment():
    path = ROOT / "config/methods/knowledge/catalog.v1.json"
    assert path.is_file(), "governance and capital guidance has not been written"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["methods"] = [m for m in catalog["methods"] if any(q.startswith(("ES03.", "ES04.")) for q in m["question_ids"])]
    return catalog


def test_all_twelve_have_distinct_evidence_and_source_backed_cases(fragment):
    assert {q for m in fragment["methods"] for q in m["question_ids"]} == {
        f"ES{s:02d}.Q{q:02d}" for s in (3, 4) for q in range(1, 7)
    }
    assert len({tuple(m["steps"]) for m in fragment["methods"]}) == 12
    cases = {c["case_id"]: c for c in fragment["cases"]}
    for m in fragment["methods"]:
        assert (ROOT / m["body_path"]).is_file()
        assert {cases[c]["kind"] for c in m["case_ids"]} >= {"normal", "counterexample", "missing"}
        assert m["rules"] and m["evidence_requirements"] and m["limitations"]


def test_commitments_and_repurchases_require_actual_performance(fragment):
    cases = {c["case_id"]: c for c in fragment["cases"]}
    commitment = cases["ES03.Q05.counterexample"]
    assert commitment["facts"]["due_amount"] > commitment["facts"]["received_amount"]
    assert "approval_not_performance" in commitment["judgment_codes"]
    repurchase = cases["ES04.Q02.counterexample"]
    assert repurchase["facts"]["announced_upper_limit"] > 0
    assert repurchase["facts"]["executed_amount"] == 0
    assert "repurchase_plan_not_completion" in repurchase["judgment_codes"]


def test_completed_purchase_and_completed_cancellation_are_not_identical(fragment):
    cases = {c["case_id"]: c for c in fragment["cases"]}
    case = cases["ES04.Q02.normal"]
    assert case["facts"]["repurchased_shares"] == 10
    assert case["facts"]["cancelled_shares"] == 0
    assert "execution_not_cancellation" in case["judgment_codes"]
    dilution = cases["ES03.Q04.normal"]
    assert dilution["facts"]["new_shares"] / (dilution["facts"]["old_shares"] + dilution["facts"]["new_shares"]) == pytest.approx(0.1)
    assert dilution["expected_values"]["post_issue_new_share_fraction"] == pytest.approx(0.1)
