"""Independent follow-up contracts for complete paths, changes and safe identities."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from test_core_service import api, approved, build, workspace


def clone_method(catalog, mid, qids, source_id="source-a"):
    original = catalog["methods"][0]
    other = copy.deepcopy(original)
    other.update(method_id=mid, question_ids=qids)
    other["case_ids"] = [cid + "." + mid for cid in original["case_ids"]]
    other["rules"][0]["rule_id"] += "." + mid
    other["rules"][0]["source_refs"][0]["source_id"] = source_id
    for cid in original["case_ids"]:
        case = copy.deepcopy(next(c for c in catalog["cases"] if c["case_id"] == cid))
        case.update(case_id=cid + "." + mid, method_ids=[mid])
        case["source_refs"][0]["source_id"] = source_id
        catalog["cases"].append(case)
    catalog["methods"].append(other)
    return other


def test_partial_question_keeps_useful_method_without_claiming_full_coverage(workspace):
    def add(c):
        c["methods"].append({"method_id": "knowledge.roe.pending", "version": "1.0.0", "title": "杜邦待补", "content_status": "skeleton", "question_ids": ["ES02.Q04"], "dependencies": []})
    service, bundle, _ = build(workspace, add)
    result = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    assert result["status"] == "available" and len(result["methods"]) == 1
    assert result["question_complete"] is False
    assert result["question_gaps"][0]["method_id"] == "knowledge.roe.pending"
    assert service.coverage(bundle["bundle_id"])["available_count"] == 0


def test_full54_drops_to53_and_unaffected_identity_survives(workspace):
    questions = json.loads((workspace[0] / "config/structured_data/research_requirements.v1.json").read_text(encoding="utf-8"))["questions"]
    def add(c):
        c["sources"].append({**c["sources"][0], "source_id": "source-other"})
        for q in questions:
            if q["question_id"] != "ES02.Q04":
                clone_method(c, "k." + q["question_id"], [q["question_id"]], "source-other")
    service, bundle, _ = build(workspace, add)
    assert service.coverage(bundle["bundle_id"])["available_count"] == 54
    service.record_change("source", "source-a", "Correction affecting exactly one path")
    cov = service.coverage(bundle["bundle_id"])
    assert (cov["available_count"], cov["historical_published_count"], cov["total"]) == (53, 54, 54)
    assert cov["pending_question_ids"] == ["ES02.Q04"]


def test_alternative_outside_mapping_still_is_available_and_described(workspace):
    def add(c):
        c["sources"].append({**c["sources"][0], "source_id": "source-b"})
        alternative = clone_method(c, "alternative", ["ES01.Q10"], "source-b")
        c["methods"][0]["alternatives"] = [alternative["method_id"]]
    service, bundle, _ = build(workspace, add)
    service.record_change("source", "source-a", "Correction")
    result = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    assert result["status"] == "available"
    assert result["question_complete"] is True
    assert result["replacement_paths"] == [{"method_id": "knowledge.roic", "replacement_method_ids": ["alternative"]}]


def test_dependency_content_change_cannot_keep_parent_review_identity(workspace):
    def add(c):
        parent = clone_method(c, "parent", ["ES01.Q10"])
        parent["dependencies"] = ["knowledge.roic"]
    service, catalog = approved(workspace, add)
    before = service.method_identity("parent")
    catalog["methods"][0]["rules"][0]["statement"] = "Reverse the economic interpretation"
    assert service.method_identity("parent", catalog) != before


def test_declared_unresolved_conflict_blocks_publication_even_with_new_review(workspace):
    service, _ = approved(workspace, lambda c: c["methods"][0].update(unresolved_conflicts=["No basis to choose capital definition"]))
    assert service.validate_candidate()["valid"] is False


def test_plain_homepage_locator_cannot_pass_structural_source_check(workspace):
    def damage(c):
        c["methods"][0]["rules"][0]["source_refs"][0]["locator"] = "homepage"
    service, _ = approved(workspace, damage)
    assert service.validate_candidate()["valid"] is False


def test_new_published_method_missing_input_structure_is_rejected(workspace):
    def damage(c):
        c["methods"][0]["required_inputs"] = [{"input_id": "roe", "kind": "metric", "binding_status": "matched", "metric_id": "roe"}]
    service, _ = approved(workspace, damage)
    assert service.validate_candidate()["valid"] is False


def test_cli_exposes_json_candidate_and_unknown_question(workspace):
    service, bundle, _ = build(workspace)
    prefix = [sys.executable, "-m", "analysis.knowledge", "--catalog", str(workspace[1]), "--store", str(workspace[0] / "store")]
    import os
    from pathlib import Path
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"), "PYTHONIOENCODING": "utf-8"}
    good = subprocess.run(prefix + ["read", "ES02.Q04", "--bundle-id", bundle["bundle_id"]], capture_output=True, encoding="utf-8", env=env)
    assert good.returncode == 0 and json.loads(good.stdout)["status"] == "available"
    unknown = subprocess.run(prefix + ["read", "unknown"], capture_output=True, encoding="utf-8", env=env)
    assert unknown.returncode == 1 and json.loads(unknown.stdout)["status"] == "unknown_question"


def test_publish_gate_uses_frozen_reviews_not_external_replacements(workspace, monkeypatch):
    service, bundle, _ = build(workspace)
    captured = {}
    def gate(coverage, evidence, question_ids):
        captured.update(evidence)
        return {"passed": False, "failed_checks": ["test-only"]}
    monkeypatch.setattr("analysis.knowledge_release.evaluate_release", gate)
    with pytest.raises(ValueError):
        service.publish_default(bundle["bundle_id"], {"source_checks": [{"method_id": "forged-external-review"}], "case_checks": []})
    assert captured["source_checks"][0]["method_id"] == "knowledge.roic"
    assert captured["case_checks"][0]["content_sha256"] == service.get_bundle(bundle["bundle_id"])["method_identities"]["knowledge.roic"]


def test_top_level_distinguishes_content_and_current_context(workspace):
    service, bundle, _ = build(workspace)
    unknown = service.read("ES02.Q04", bundle_id=bundle["bundle_id"])
    bank = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], context={"industry": "bank"})
    industrial = service.read("ES02.Q04", bundle_id=bundle["bundle_id"], context={"industry": "industrial"})
    assert unknown["context_status"] == "needs_context" and unknown["executable"] is False
    assert bank["context_status"] == "not_applicable" and bank["executable"] is False
    assert industrial["context_status"] == "applicable" and industrial["executable"] is True
    assert bank["question_complete"] is True and bank["status"] == "available"
    assert bank["context_explanation"]


def test_cli_accepts_business_type_needed_by_content(workspace):
    service, bundle, _ = build(workspace, lambda c: c["methods"][0]["applicability"].update(requires_context=["industry", "business_type"]))
    import os
    from pathlib import Path
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"), "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, "-m", "analysis.knowledge", "--catalog", str(workspace[1]), "--store", str(workspace[0] / "store"), "read", "ES02.Q04", "--bundle-id", bundle["bundle_id"], "--industry", "industrial", "--business-type", "manufacturing"]
    result = subprocess.run(args, capture_output=True, encoding="utf-8", env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["context_status"] == "applicable"


def test_candidate_rechecks_actual_frozen_body_against_reviews(workspace, monkeypatch):
    service, _ = approved(workspace)
    original_freeze = service._freeze
    def changed_during_snapshot(catalog):
        (workspace[0] / "docs/methodology/knowledge/example.md").write_text("Rule reversed after initial validation", encoding="utf-8")
        return original_freeze(catalog)
    monkeypatch.setattr(service, "_freeze", changed_during_snapshot)
    with pytest.raises(ValueError, match="frozen|identity"):
        service.build_candidate(bundle_id="racing-content")
    assert not (workspace[0] / "store/bundles/racing-content.json").exists()


def test_required_dependency_applicability_blocks_parent_execution(workspace):
    def add(c):
        parent = clone_method(c, "parent", ["ES01.Q10"])
        parent["dependencies"] = ["knowledge.roic"]
        parent["applicability"]["exclude_industries"] = []
    service, bundle, _ = build(workspace, add)
    result = service.read("ES01.Q10", bundle_id=bundle["bundle_id"], context={"industry": "bank"})
    assert result["question_complete"] is True
    assert result["context_status"] == "not_applicable"
    assert result["executable"] is False
