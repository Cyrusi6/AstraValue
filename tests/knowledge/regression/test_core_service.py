"""K01-K07 software contracts; synthetic fixtures are not source verification."""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def api():
    try:
        return importlib.import_module("analysis.knowledge")
    except ModuleNotFoundError as exc:
        pytest.fail(f"K01/K07 knowledge API is not implemented: {exc}")


@pytest.fixture
def workspace(tmp_path):
    for relative in ["config/structured_data/research_requirements.v1.json", "config/methods/metrics.json"]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    document = tmp_path / "docs/methodology/knowledge/example.md"
    document.parent.mkdir(parents=True)
    document.write_text("# Synthetic method\nOriginal interpretation, never company facts.\n", encoding="utf-8")
    ref = {"source_id": "source-a", "version": "1", "locator": "p10-11", "support": "Capital timing follows cash-flow convention."}
    method = {
        "method_id": "knowledge.roic", "version": "1.0.0", "legacy_method_id": "roic", "title": "Synthetic ROIC fixture",
        "question_ids": ["ES02.Q04"], "content_status": "published", "body_path": "docs/methodology/knowledge/example.md",
        "steps": ["Align numerator and denominator scope.", "Choose beginning or average capital with rationale."],
        "required_inputs": [{"input_id": "profit", "kind": "metric", "concept": "parent net income", "metric_id": "net_income_parent", "binding_status": "pending", "requirements": {"period": "annual", "unit": "CNY", "scope": "parent"}},
                            {"input_id": "disclosure", "kind": "text", "concept": "reconciliation", "locator_requirement": "filing page and table", "requirements": {}, "binding_status": "pending"}],
        "evidence_requirements": [{"evidence_id": "capital", "importance": "required", "description": "Reconcile capital", "supports": "consistent ROIC", "missing_effect": "Cannot compare ROIC"}],
        "rules": [{"rule_id": "roic.timing", "statement": "Do not mix denominator timing conventions.", "source_refs": [ref]}],
        "counterexamples": ["Same income with a different capital basis is not comparable."],
        "limitations": ["Accounting capital can omit intangible investment."], "industry_gaps": ["Bank-specific returns remain unbuilt."],
        "case_ids": ["normal", "counter", "missing"], "dependencies": [],
        "applicability": {"include_industries": [], "exclude_industries": ["bank", "银行"], "requires_context": ["industry"], "conditions": ["Nonfinancial enterprise"]},
    }
    catalog = {"schema_version": "1.0.0", "catalog_version": "1.0.0", "mapping_version": "1.0.0",
               "sources": [{"source_id": "source-a", "version": "1", "title": "Synthetic source", "author": "Fixture", "url": "https://example.invalid/source.pdf", "locator": "p10-11", "acquired_at": "2026-09-19T08:00:00Z", "verification_status": "verified", "access_status": "public", "redistribution": "not_assessed", "content_sha256": "synthetic-only"}],
               "methods": [method], "cases": [], "reviews": []}
    for case_id, kind in [("normal", "normal"), ("counter", "counterexample"), ("missing", "missing")]:
        catalog["cases"].append({"case_id": case_id, "method_ids": [method["method_id"]], "kind": kind,
                                 "expected_factors": ["timing"], "supported_conclusions": ["comparison conditional on consistent timing"],
                                 "forbidden_conclusions": ["unconditional comparability"], "reason": "Synthetic software fixture", "source_refs": [ref], "evaluation_mode": "content_review"})
    path = tmp_path / "config/methods/knowledge/catalog.v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return tmp_path, path, catalog


def approved(workspace, mutate=None):
    root, path, catalog = workspace
    catalog = copy.deepcopy(catalog)
    if mutate:
        mutate(catalog)
    path.write_text(json.dumps(catalog), encoding="utf-8")
    service = api().KnowledgeService.from_catalog(path, root / "store")
    for method in catalog["methods"]:
        if method["content_status"] != "published":
            continue
        identity = service.method_identity(method["method_id"])
        for kind in ["source", "case"]:
            catalog["reviews"].append({"review_id": method["method_id"] + "-" + kind, "method_id": method["method_id"], "method_version": method["version"], "content_sha256": identity, "kind": kind, "outcome": "passed", "reasons": ["Synthetic fixture only; software behavior checked."], "unresolved_issues": [], "reviewer": "test-fixture"})
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return api().KnowledgeService.from_catalog(path, root / "store"), catalog


def build(workspace, mutate=None):
    service, catalog = approved(workspace, mutate)
    return service, service.build_candidate(bundle_id="candidate-1"), catalog


def test_known_unknown_pending_and_no_default(workspace):
    service, bundle, _ = build(workspace)
    assert service.read("ES02.Q04")["status"] == "no_default_release"
    assert service.read("ES99.Q99", bundle_id=bundle["bundle_id"])["status"] == "unknown_question"
    assert service.read("ES01.Q01", bundle_id=bundle["bundle_id"])["status"] == "content_pending"
    result = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    assert result["status"] == "available"
    assert result["question"]["title"] != "Synthetic ROIC fixture"
    assert result["question_set_version"] == "1.0.0"
    assert result["methods"][0]["steps"] and result["methods"][0]["counterexamples"]


def test_candidates_require_explicit_selection_and_coverage_denominator(workspace):
    service, bundle, _ = build(workspace)
    coverage = service.coverage(bundle["bundle_id"])
    assert (coverage["available_count"], coverage["total"]) == (1, 54)
    assert coverage["bundle_kind"] == "candidate"
    assert coverage["default_published"] is False
    assert coverage["industry_gaps"]["ES02.Q04"]
    assert service.coverage()["available_count"] == 0


@pytest.mark.parametrize("status", ["skeleton", "draft", "reviewed"])
def test_nonpublished_never_counts_but_explicit_preview_keeps_status(workspace, status):
    service, bundle, _ = build(workspace, lambda c: c["methods"][0].update(content_status=status))
    assert service.coverage(bundle["bundle_id"])["available_count"] == 0
    assert service.read("ES02.Q04", bundle_id=bundle["bundle_id"])["status"] == "content_pending"
    preview = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], include_drafts=True)
    assert preview["methods"][0]["content_status"] == status
    assert preview["methods"][0]["available"] is False


def test_context_unknown_and_bank_are_separate_from_maturity(workspace):
    service, bundle, _ = build(workspace)
    missing = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    assert missing["methods"][0]["applicability"]["status"] == "needs_context"
    bank = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], context={"industry": "bank"})
    assert bank["methods"][0]["applicability"]["status"] == "not_applicable"
    assert bank["methods"][0]["content_status"] == "published"
    assert bank["methods"][0]["industry_gaps"]
    with pytest.raises(ValueError, match="context"):
        service.read("ES02.Q04", bundle_id=bundle["bundle_id"], context={"net_income": 123})


def test_metric_semantics_need_evidence_and_text_evidence_remains_text(workspace):
    def wrong(c):
        inp = c["methods"][0]["required_inputs"][0]
        inp.update(metric_id="net_income", binding_status="matched", definition_version="1.0.0", expected_definition={"name": "归母净利润"})
    service, bundle, _ = build(workspace, wrong)
    inputs = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])["methods"][0]["required_inputs"]
    assert inputs[0]["binding_status"] == "mismatch"
    assert inputs[1]["binding_status"] == "text_evidence"
    assert inputs[1]["locator_requirement"]


def test_exact_metric_snapshot_matches_and_unknown_metric_stays_pending(workspace):
    def matched(c):
        inp = c["methods"][0]["required_inputs"][0]
        inp.update(binding_status="matched", definition_version="1.0.0", expected_definition={"name": "归母净利润", "unit": "CNY", "type": "flow"})
    service, bundle, _ = build(workspace, matched)
    inp = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])["methods"][0]["required_inputs"][0]
    assert inp["binding_status"] == "pending", "period/scope absent in registry cannot be assumed matched"
    assert inp["definition"]["name"] == "归母净利润"


@pytest.mark.parametrize("damage", ["counterexample", "source", "review", "failed_review", "empty_reasons", "case"])
def test_publication_rejects_missing_or_failed_evidence(workspace, damage):
    service, catalog = approved(workspace)
    if damage == "counterexample": catalog["methods"][0]["counterexamples"] = []
    if damage == "source": catalog["methods"][0]["rules"][0]["source_refs"][0]["locator"] = ""
    if damage == "review": catalog["reviews"] = []
    if damage == "failed_review": catalog["reviews"][0]["outcome"] = "failed"
    if damage == "empty_reasons": catalog["reviews"][0]["reasons"] = []
    if damage == "case": catalog["cases"] = catalog["cases"][:1]
    result = service.validate_candidate(catalog)
    assert result["valid"] is False
    assert result["errors"]
    with pytest.raises(ValueError): service.build_candidate(catalog, bundle_id="bad")


def test_body_source_case_and_metric_changes_invalidate_review(workspace):
    service, catalog = approved(workspace)
    original = service.method_identity("knowledge.roic")
    for section, field in [("sources", "title"), ("cases", "reason"), ("methods", "title")]:
        changed = copy.deepcopy(catalog)
        changed[section][0][field] = "changed"
        assert service.validate_candidate(changed)["valid"] is False
    (workspace[0] / "docs/methodology/knowledge/example.md").write_text("changed body", encoding="utf-8")
    assert service.method_identity("knowledge.roic") != original
    assert service.validate_candidate()["valid"] is False


def test_version_snapshot_keeps_body_mapping_source_and_metric_definition(workspace):
    service, bundle, _ = build(workspace)
    result = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    ref = result["methods"][0]["detail_ref"]
    old = service.expand(ref)
    (workspace[0] / "docs/methodology/knowledge/example.md").write_text("NEW BODY", encoding="utf-8")
    metrics_path = workspace[0] / "config/methods/metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")); metrics["metrics"]["net_income_parent"]["name"] = "UPDATED"
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    reread = api().KnowledgeService.from_catalog(workspace[1], workspace[0] / "store")
    assert reread.expand(ref)["body"] == old["body"]
    assert reread.expand(ref)["required_inputs"][0]["definition"]["name"] == "归母净利润"
    assert reread.read("ES02.Q04", bundle_id="missing")["status"] == "unknown_version"
    with pytest.raises(ValueError, match="immutable"): service.build_candidate(bundle_id="candidate-1")


def test_missing_historical_metric_does_not_substitute_latest(workspace):
    service, bundle, _ = build(workspace)
    ref = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])["methods"][0]["detail_ref"]
    path = workspace[0] / "store/bundles/candidate-1.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    del snapshot["metrics"]["metrics"]["net_income_parent"]
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity|reproduc"): service.expand(ref)


def test_candidate_content_hash_is_stable_across_rebuilds(workspace):
    service, _, _ = build(workspace)
    second_store = workspace[0] / "store-second"
    reread = api().KnowledgeService.from_catalog(workspace[1], second_store)

    first = service.build_candidate()
    second = reread.build_candidate()

    assert first["bundle_id"] == second["bundle_id"]
    assert first["content_sha256"] == second["content_sha256"]
    assert service.get_bundle(first["bundle_id"])["content_sha256"] == first["content_sha256"]
    assert reread.get_bundle(second["bundle_id"])["content_sha256"] == second["content_sha256"]


def test_short_response_cannot_silently_drop_limits(workspace):
    service, bundle, _ = build(workspace)
    short = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], max_chars=20)
    assert short["complete"] is False
    assert "methods" in short["omitted_sections"]
    full = service.expand(short["continue_ref"])
    assert full["methods"][0]["limitations"]
    assert full["bundle_id"] == bundle["bundle_id"]


def test_shared_methods_deduplicate_and_dependencies_order(workspace):
    def add(c):
        first = c["methods"][0]
        second = copy.deepcopy(first); second.update(method_id="knowledge.second", title="Second", dependencies=["knowledge.roic"])
        second["case_ids"] = ["normal2", "counter2", "missing2"]
        for case in list(c["cases"]):
            new = copy.deepcopy(case); new.update(case_id=case["case_id"] + "2", method_ids=["knowledge.second"]); c["cases"].append(new)
        c["methods"].append(second)
        first["question_ids"].append("ES01.Q10")
    service, bundle, _ = build(workspace, add)
    result = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    assert [m["method_id"] for m in result["methods"]] == ["knowledge.roic", "knowledge.second"]
    assert service.coverage(bundle["bundle_id"])["available_count"] == 2


@pytest.mark.parametrize("damage", ["unknown_question", "missing_dependency", "cycle"])
def test_invalid_mapping_or_dependency_blocks_candidate(workspace, damage):
    service, catalog = approved(workspace)
    method = catalog["methods"][0]
    if damage == "unknown_question": method["question_ids"] = ["ES01.Q99"]
    if damage == "missing_dependency": method["dependencies"] = ["missing"]
    if damage == "cycle": method["dependencies"] = [method["method_id"]]
    assert service.validate_candidate(catalog)["valid"] is False


def test_source_change_propagates_to_cases_questions_and_historical_notice(workspace):
    service, bundle, _ = build(workspace)
    ref = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])["methods"][0]["detail_ref"]
    before = service.expand(ref)["body"]
    impact = service.record_change("source", "source-a", "Source rule corrected", change_id="change-1")
    assert impact["method_ids"] == ["knowledge.roic"]
    assert impact["question_ids"] == ["ES02.Q04"]
    assert set(impact["case_ids"]) == {"normal", "counter", "missing"}
    cov = service.coverage(bundle["bundle_id"])
    assert cov["available_count"] == 0 and cov["historical_published_count"] == 1
    historical = service.expand(ref)
    assert historical["body"] == before and historical["notices"]


def test_unrelated_method_keeps_coverage_after_change(workspace):
    def add(c):
        other = copy.deepcopy(c["methods"][0]); other.update(method_id="other", question_ids=["ES01.Q10"], case_ids=["normal2", "counter2", "missing2"])
        c["sources"].append({**c["sources"][0], "source_id": "source-b"})
        other["rules"][0]["source_refs"][0]["source_id"] = "source-b"
        for case in list(c["cases"]):
            new = copy.deepcopy(case); new.update(case_id=case["case_id"] + "2", method_ids=["other"]); new["source_refs"][0]["source_id"] = "source-b"; c["cases"].append(new)
        c["methods"].append(other)
    service, bundle, _ = build(workspace, add)
    service.record_change("source", "source-a", "correction")
    assert service.coverage(bundle["bundle_id"])["available_question_ids"] == ["ES01.Q10"]


def test_same_source_republication_is_not_independent_support(workspace):
    def duplicate(c):
        c["sources"].append({**c["sources"][0], "source_id": "mirror", "original_source_id": "source-a"})
        c["methods"][0]["rules"][0]["source_refs"].append({**c["methods"][0]["rules"][0]["source_refs"][0], "source_id": "mirror"})
    service, bundle, _ = build(workspace, duplicate)
    detail = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], detail=True)
    assert detail["methods"][0]["independent_source_count"] == 1


def test_versioned_source_alias_resolves_without_reintroducing_duplicate_source(workspace):
    def alias(catalog):
        catalog["source_aliases"] = [{
            "alias_source_id": "SRC-OLD",
            "alias_version": "snapshot-1",
            "canonical_source_id": "source-a",
            "canonical_version": "1",
            "status": "historical_alias",
            "scope": "historical bundle/manifest only",
        }]
        catalog["methods"][0]["rules"][0]["source_refs"][0].update(source_id="SRC-OLD", version="snapshot-1")
        for case in catalog["cases"]:
            case["source_refs"][0].update(source_id="SRC-OLD", version="snapshot-1")

    service, bundle, _ = build(workspace, alias)
    detail = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], detail=True)
    assert detail["methods"][0]["source_refs"] == [{
        "kind": "source", "bundle_id": bundle["bundle_id"],
        "source_id": "source-a", "source_version": "1",
    }]
    resolved = service.expand({
        "kind": "source", "bundle_id": bundle["bundle_id"],
        "source_id": "SRC-OLD", "source_version": "snapshot-1",
    })
    assert resolved["source_id"] == "source-a"


def test_default_publish_needs_full_scope_and_human_evidence(workspace):
    service, bundle, _ = build(workspace)
    with pytest.raises(ValueError, match="54|release"): service.publish_default(bundle["bundle_id"], {})


def test_legacy_registry_remains_usable():
    from analysis.registry import MethodRegistry
    registry = MethodRegistry()
    methods = registry.list_methods()
    assert methods
    assert registry.create_bundle([methods[0].method_id]).methods
