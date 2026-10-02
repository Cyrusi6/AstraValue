"""Immutable old packages and stable new identities use separate hash rules."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from test_core_service import approved, build, workspace


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def acceptance_for(service, bundle):
    """Synthetic gate records exercise software; they are not real acceptance."""
    method_id = service.get_bundle(bundle["bundle_id"])["catalog"]["methods"][0]["method_id"]
    steps = sorted({q.split(".")[0] for q in service.question_index})
    return {
        "bundle_id": bundle["bundle_id"],
        "candidate_content_sha256": bundle["content_sha256"],
        "agent_samples": [{"bundle_id": bundle["bundle_id"], "step_id": step,
                           "method_ids": [method_id], "outcome": "passed",
                           "reasons": ["Synthetic gate fixture only."]} for step in steps],
        "human_review": {"bundle_id": bundle["bundle_id"], "reviewer_kind": "human",
                         "reviewer": "synthetic-test-fixture", "sampled_steps": steps,
                         "outcome": "passed", "important_issues": [],
                         "reasons": ["Synthetic gate fixture only, not user acceptance."]},
    }


def full_question_scope(workspace):
    questions = json.loads((workspace[0] / "config/structured_data/research_requirements.v1.json").read_text("utf-8"))
    return lambda catalog: catalog["methods"][0].update(
        question_ids=[q["question_id"] for q in questions["questions"]])


def test_original_unversioned_bundle_is_read_without_rewriting_identity(workspace):
    service, bundle, _ = build(workspace)
    path = service.store_root / "bundles" / f"{bundle['bundle_id']}.json"
    payload = json.loads(path.read_text("utf-8"))
    for field in ("bundle_hash_version", "content_sha256", "integrity_sha256"):
        payload.pop(field)
    payload["created_at"] = "2026-09-19T08:00:00+00:00"
    payload["content_sha256"] = digest(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()

    assert service.get_bundle(bundle["bundle_id"]) == payload
    assert path.read_bytes() == original
    assert service.read("ES02.Q04", bundle_id=bundle["bundle_id"])["status"] == "available"

    payload["created_at"] = "2026-09-20T08:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        service.get_bundle(bundle["bundle_id"])


def test_same_content_rebuild_has_stable_identity_and_distinct_integrity(workspace):
    service, _ = approved(workspace)
    first = service.build_candidate()
    payload1 = service.get_bundle(first["bundle_id"])
    service.store_root = workspace[0] / "rebuilt-store"
    second = service.build_candidate()
    payload2 = service.get_bundle(second["bundle_id"])
    assert first["bundle_id"] == second["bundle_id"]
    assert first["content_sha256"] == second["content_sha256"]
    assert payload1["created_at"] != payload2["created_at"]
    assert payload1["integrity_sha256"] != payload2["integrity_sha256"]


@pytest.mark.parametrize("damage", ["body", "created_at", "content_sha256", "integrity_sha256", "version"])
def test_new_bundle_rejects_content_metadata_or_hash_rule_damage(workspace, damage):
    service, bundle, _ = build(workspace)
    path = service.store_root / "bundles" / f"{bundle['bundle_id']}.json"
    payload = json.loads(path.read_text("utf-8"))
    if damage == "body":
        payload["catalog"]["methods"][0]["body"] = "reversed rule"
    elif damage == "version":
        payload["bundle_hash_version"] = 99
    else:
        payload[damage] = "altered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity|hash version"):
        service.get_bundle(bundle["bundle_id"])


def test_publish_and_release_command_agree_on_candidate_content_pin(workspace):
    service, bundle, _ = build(workspace, full_question_scope(workspace))
    script = Path(__file__).resolve().parents[3] / "scripts/validate_knowledge_release.py"
    spec = importlib.util.spec_from_file_location("knowledge_release_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = acceptance_for(service, bundle)
    bad_evidence = {**evidence, "candidate_content_sha256": "0" * 64}
    failed = module.release_report(service, bundle["bundle_id"], bad_evidence)
    assert failed["failed_checks"] == ["candidate_content_identity"]
    with pytest.raises(ValueError, match="candidate_content_identity"):
        service.publish_default(bundle["bundle_id"], bad_evidence)
    assert not (service.store_root / "default.json").exists()

    checked = module.release_report(service, bundle["bundle_id"], evidence)
    published = service.publish_default(bundle["bundle_id"], evidence)
    assert checked["passed"] and published["gate"]["passed"]
    assert checked["checks"] == published["gate"]["checks"]
