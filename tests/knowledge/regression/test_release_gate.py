"""Final-release accounting stays separate from daily behaviour tests."""
from copy import deepcopy
from importlib import import_module
from importlib.util import find_spec

import pytest


def evaluate(coverage, evidence, expected):
    assert find_spec("analysis.knowledge_release") is not None, "release gate is not implemented"
    return import_module("analysis.knowledge_release").evaluate_release(coverage, evidence, expected)


@pytest.fixture
def complete():
    expected = [f"ES{step:02d}.Q01" for step in range(1, 9)]
    methods = [{"method_id": "M1", "content_sha256": "a" * 64}]
    coverage = {"bundle_id": "b1", "ready_question_ids": list(expected), "methods": methods}
    checks = [{"method_id": "M1", "content_sha256": "a" * 64, "outcome": "passed", "reasons": ["case and source checked"]}]
    evidence = {
        "bundle_id": "b1",
        "source_checks": deepcopy(checks),
        "case_checks": deepcopy(checks),
        "agent_samples": [
            {"step_id": f"ES{s:02d}", "bundle_id": "b1", "outcome": "passed", "method_ids": ["M1"], "reasons": ["criteria verified"]}
            for s in range(1, 9)
        ],
        "human_review": {"bundle_id": "b1", "reviewer_kind": "human", "reviewer": "fixture reviewer", "outcome": "passed", "sampled_steps": [f"ES{s:02d}" for s in range(1, 9)], "important_issues": [], "reasons": ["manual sample fixture"]},
    }
    return coverage, evidence, expected


def test_complete_evidence_passes_without_changing_inputs(complete):
    before = deepcopy(complete)
    result = evaluate(*complete)
    assert result["passed"] is True
    assert result["ready"] == result["total"] == 8
    assert complete == before


def test_candidate_content_hash_mismatch_is_rejected(complete):
    coverage, evidence, expected = complete
    coverage["content_sha256"] = "c" * 64
    evidence["candidate_content_sha256"] = "d" * 64

    result = evaluate(coverage, evidence, expected)

    assert not result["passed"]
    assert result["checks"]["candidate_content_identity"] is False
    assert "candidate_content_identity" in result["failed_checks"]


def test_matching_candidate_content_hash_is_accepted(complete):
    coverage, evidence, expected = complete
    coverage["content_sha256"] = "c" * 64
    evidence["candidate_content_sha256"] = "c" * 64

    result = evaluate(coverage, evidence, expected)

    assert result["passed"] is True
    assert result["checks"]["candidate_content_identity"] is True


def test_partial_coverage_is_not_masked_by_passing_regression(complete):
    coverage, evidence, expected = complete
    coverage["ready_question_ids"] = expected[:3]
    result = evaluate(coverage, evidence, expected)
    assert not result["passed"]
    assert result["ready"] == 3
    assert result["total"] == 8
    assert result["missing_question_ids"] == expected[3:]


@pytest.mark.parametrize("change", ["missing", "agent_as_human", "failed", "old_bundle", "unresolved", "missing_step"])
def test_human_acceptance_cannot_be_inferred(complete, change):
    coverage, evidence, expected = complete
    if change == "missing":
        evidence.pop("human_review")
    elif change == "agent_as_human":
        evidence["human_review"]["reviewer_kind"] = "agent"
    elif change == "failed":
        evidence["human_review"]["outcome"] = "failed"
    elif change == "old_bundle":
        evidence["human_review"]["bundle_id"] = "old"
    elif change == "unresolved":
        evidence["human_review"]["important_issues"] = ["incorrect interpretation"]
    else:
        evidence["human_review"]["sampled_steps"].pop()
    assert not evaluate(coverage, evidence, expected)["passed"]


@pytest.mark.parametrize("group", ["source_checks", "case_checks"])
def test_stale_or_unreasoned_method_review_is_rejected(complete, group):
    coverage, evidence, expected = complete
    evidence[group][0]["content_sha256"] = "b" * 64
    assert not evaluate(coverage, evidence, expected)["passed"]
    evidence[group][0]["content_sha256"] = "a" * 64
    evidence[group][0]["reasons"] = []
    assert not evaluate(coverage, evidence, expected)["passed"]


def test_same_count_different_questions_does_not_pass(complete):
    coverage, evidence, expected = complete
    coverage["ready_question_ids"] = expected[:-1] + ["ES99.Q01"]
    result = evaluate(coverage, evidence, expected)
    assert not result["passed"]
    assert result["missing_question_ids"] == [expected[-1]]


def test_duplicate_questions_and_empty_methods_do_not_pass(complete):
    coverage, evidence, expected = complete
    coverage["ready_question_ids"] += [expected[0]]
    assert not evaluate(coverage, evidence, expected)["passed"]
    coverage["ready_question_ids"] = expected
    coverage["methods"] = []
    assert not evaluate(coverage, evidence, expected)["passed"]


def test_agent_samples_must_use_current_bundle_and_known_methods(complete):
    coverage, evidence, expected = complete
    evidence["agent_samples"][0]["bundle_id"] = "old"
    assert not evaluate(coverage, evidence, expected)["passed"]
    evidence["agent_samples"][0]["bundle_id"] = "b1"
    evidence["agent_samples"][0]["method_ids"] = ["unknown"]
    assert not evaluate(coverage, evidence, expected)["passed"]


def test_no_evidence_returns_failures_instead_of_acceptance(complete):
    coverage, _, expected = complete
    result = evaluate(coverage, {}, expected)
    assert not result["passed"]
    assert result["checks"]["human_review"] is False


def prepare(bundle_id, methods, reviews, acceptance=None):
    module = import_module("analysis.knowledge_release")
    assert hasattr(module, "prepare_evidence"), "frozen review adapter is not implemented"
    return module.prepare_evidence(bundle_id, methods, reviews, acceptance)


def test_review_adapter_uses_frozen_identity_and_ignores_external_source_claims(complete):
    coverage, evidence, _ = complete
    reviews = [dict(evidence["source_checks"][0], kind="source"), dict(evidence["case_checks"][0], kind="case")]
    records = {"bundle_id": "b1", "source_checks": [{"forged": True}], "human_review": evidence["human_review"]}
    result = prepare("b1", coverage["methods"], reviews, records)
    assert result["source_checks"] == [reviews[0]]
    assert result["case_checks"] == [reviews[1]]
    assert result["human_review"] == evidence["human_review"]


def test_review_adapter_does_not_upgrade_old_acceptance_or_stale_reviews(complete):
    coverage, evidence, expected = complete
    rows = [dict(evidence["source_checks"][0], kind="source", content_sha256="b" * 64)]
    result = prepare("b1", coverage["methods"], rows, {"bundle_id": "old"})
    assert result["bundle_id"] == "old"
    assert result["source_checks"] == []
    assert not evaluate(coverage, result, expected)["passed"]
