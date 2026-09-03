from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from analysis.acquisition.models import SourceCandidate, SourceCandidateStatus
from analysis.acquisition.registry import (
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


def test_registered_domain_is_not_misclassified_as_candidate():
    registry = SourceRegistryLoader().load_registry()
    with pytest.raises(SourceRegistryError, match="已属于已批准来源"):
        create_source_candidate(
            registry,
            url="https://static.cninfo.com.cn/report.pdf",
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
