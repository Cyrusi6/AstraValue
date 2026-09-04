from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from analysis.acquisition.models import (
    LiveAccessReviewCheck,
    SourceCandidate,
    SourceCandidateStatus,
)
from analysis.acquisition.registry import (
    INITIAL_REGISTRY_PATH,
    SourceRegistryError,
    SourceRegistryLoader,
    create_source_candidate,
    review_source_candidate,
)


def test_unregistered_domain_only_creates_pending_candidate():
    registry = SourceRegistryLoader().load_registry()
    candidate = create_source_candidate(
        registry,
        url="https://new-ir.example.test/publications",
        discovery_context={"run_id": "run-1", "from_url": "approved-page"},
        discovered_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    assert candidate.status == SourceCandidateStatus.PENDING_REVIEW
    assert "source_definition_id" not in SourceCandidate.model_fields
    assert "snapshot_id" not in SourceCandidate.model_fields


def test_pending_review_registered_domain_is_not_a_formal_source():
    registry = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH)
    url = "https://static.cninfo.com.cn/report.pdf"

    assert SourceRegistryLoader.find_registered_definition_for_url(
        registry.registry,
        url,
    ) is None
    candidate = create_source_candidate(
        registry,
        url=url,
        discovery_context={"run_id": "run-pending-review"},
    )
    assert candidate.status == SourceCandidateStatus.PENDING_REVIEW


def test_approved_registered_domain_is_not_misclassified_as_candidate(tmp_path):
    payload = json.loads(INITIAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["registry_version"] = "1.2.0"
    definition = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "cninfo.disclosures"
    )
    definition["version"] = "1.2.0"
    definition["live_access_review"].update(
        {
            "status": "approved",
            "completed_checks": [item.value for item in LiveAccessReviewCheck],
            "reviewed_at": "2026-09-03T01:00:00Z",
            "reviewed_by": "fixture-reviewer",
            "evidence_reference": "fixture:policy-review",
        }
    )
    path = tmp_path / "approved-registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    registry = SourceRegistryLoader().load_registry(path)
    url = "https://static.cninfo.com.cn/report.pdf"

    registered = SourceRegistryLoader.find_registered_definition_for_url(
        registry.registry,
        url,
    )
    assert registered is not None
    assert registered.source_definition_id == "cninfo.disclosures"
    with pytest.raises(SourceRegistryError, match="已属于已批准来源"):
        create_source_candidate(
            registry,
            url=url,
            discovery_context={"run_id": "run-1"},
        )


def test_approval_requires_new_registry_version_and_does_not_enable_current_registry():
    registry = SourceRegistryLoader().load_registry()
    candidate = create_source_candidate(
        registry,
        url="https://new-ir.example.test/publications",
        discovery_context={"run_id": "run-1"},
    )
    with pytest.raises(ValidationError, match="新registry version"):
        SourceCandidate(
            **candidate.model_dump(
                exclude={
                    "status",
                    "reviewed_at",
                    "reviewed_by",
                    "decision_reason",
                    "approved_registry_version",
                }
            ),
            status="approved",
            reviewed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
            reviewed_by="reviewer",
            decision_reason="policy reviewed",
        )
    approved = review_source_candidate(
        candidate,
        status=SourceCandidateStatus.APPROVED,
        reviewed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        reviewed_by="reviewer",
        decision_reason="policy reviewed",
        approved_registry_version="1.1.0",
    )
    assert approved.status == SourceCandidateStatus.APPROVED
    assert approved.approved_registry_version == "1.1.0"
    assert SourceRegistryLoader.find_registered_definition_for_url(
        registry.registry, approved.candidate_url
    ) is None


def test_rejected_candidate_cannot_reference_registry_version():
    registry = SourceRegistryLoader().load_registry()
    candidate = create_source_candidate(
        registry,
        url="https://untrusted.example.test/",
        discovery_context={"reason": "redirect"},
    )
    rejected = review_source_candidate(
        candidate,
        status=SourceCandidateStatus.REJECTED,
        reviewed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        reviewed_by="reviewer",
        decision_reason="license denied",
    )
    assert rejected.status == SourceCandidateStatus.REJECTED
    assert rejected.approved_registry_version is None
