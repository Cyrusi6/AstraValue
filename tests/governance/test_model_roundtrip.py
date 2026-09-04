from __future__ import annotations

import inspect
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from analysis.governance import models
from analysis.governance.canonical import CanonicalizationError, canonical_json_bytes
from analysis.governance.models import (
    AuditOpinionRecord,
    AuditorEngagement,
    CommitmentRecord,
    CompensationRecord,
    ContextualEvidenceItem,
    ContextualFinding,
    ControlRelation,
    CorrectionRecord,
    DeferredResearchItem,
    DirectionKind,
    DiscoveryLead,
    FactualFinding,
    GOVERNANCE_FINDING_ADAPTER,
    GOVERNANCE_RECORD_ADAPTER,
    GOVERNANCE_SNAPSHOT_LINK_ADAPTER,
    GovernanceModel,
    GovernancePolicyVersion,
    IncentiveGrant,
    IncentivePlan,
    InquiryRecord,
    InternalControlRecord,
    LitigationMatter,
    OwnershipPosition,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RESEARCH_RESULT_ITEM_ADAPTER,
    RegulatoryMatter,
    RelatedPartyRelation,
    RelatedPartyTransaction,
    RoleTenure,
    RosterSnapshot,
    SnapshotAnchorLink,
    SnapshotDeltaLink,
    SnapshotRecordLink,
    SourceRole,
    UnresolvedResearchGap,
    VestingCondition,
    parse_governance_record_json,
)
from tests.governance.test_models import (
    COMPANY,
    H,
    H2,
    PERSON,
    QUESTION,
    SPAN_ID,
    T0,
    T1,
    T2,
    build_snapshot,
    record_base,
    valid_claim,
)


def record_samples() -> tuple[GovernanceModel, ...]:
    position = OwnershipPosition(
        position_id="govposition:1",
        company_id=COMPANY,
        holder_entity_id="entity:holder",
        holder_name="holder",
        share_count=Decimal("10"),
        ratio=Decimal("0.1"),
        ratio_basis=Decimal("100"),
        share_class="A",
        capital_basis="total_shares",
        completeness_status=models.CompletenessStatus.COMPLETE,
        claim_ids=("govclaim:1",),
        evidence_span_ids=(SPAN_ID,),
        canonical_hash=H,
    )
    return (
        position,
        RosterSnapshot(
            **record_base("roster"),
            reference_at=T0,
            body_type="board",
            is_complete=True,
            member_ids=(PERSON,),
            source_manifest_id="shared-manifest:1",
            completeness_evidence_span_ids=(SPAN_ID,),
        ),
        RoleTenure(
            **record_base("tenure"),
            person_id=PERSON,
            role_code="director",
            original_role_text="director",
            role_scope="board",
            valid_from=T0,
            appointment_evidence_span_id=SPAN_ID,
        ),
        OwnershipSnapshot(
            **record_base("ownership", question_id="GOV.Q01.OWNERSHIP_CONTROL"),
            reference_at=T0,
            positions=(position,),
            total_share_basis=Decimal("100"),
            capital_basis="total_shares",
            is_complete=True,
            source_manifest_id="shared-manifest:1",
        ),
        ControlRelation(
            **record_base("control", question_id="GOV.Q01.OWNERSHIP_CONTROL"),
            controller_entity_id="entity:a",
            controlled_entity_id="entity:b",
            relation_type="control",
            direction=DirectionKind.DIRECT,
            chain_path=("entity:a", "entity:b"),
        ),
        PledgePositionSnapshot(
            **record_base("pledge", question_id="GOV.Q02.PLEDGE_FREEZE"),
            reference_at=T0,
            pledgor_entity_id="entity:a",
            pledged_shares=Decimal("2"),
            pledged_ratio=Decimal("0.02"),
            ratio_basis=Decimal("100"),
            capital_basis="total_shares",
            is_complete=True,
        ),
        CompensationRecord(
            **record_base("compensation", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
            person_id=PERSON,
            fiscal_year=2022,
            cash_compensation=Decimal("1"),
            currency="CNY",
            scope="issuer",
        ),
        RelatedPartyRelation(
            **record_base("relation", question_id="GOV.Q06.RELATED_PARTIES"),
            related_entity_id="entity:related",
            relation_type="affiliate",
            basis="annual report",
        ),
        RelatedPartyTransaction(
            **record_base("transaction", question_id="GOV.Q06.RELATED_PARTIES"),
            counterparty_entity_id="entity:related",
            transaction_type="purchase",
            amount=Decimal("1"),
            currency="CNY",
            approval_status="approved",
            fiscal_period_start=date(2022, 1, 1),
            fiscal_period_end=date(2022, 12, 31),
        ),
        IncentivePlan(
            **record_base("plan", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
            plan_id="govplan:1",
            instrument="shares",
            grant_pool=Decimal("1"),
            status="active",
        ),
        IncentiveGrant(
            **record_base("grant", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
            plan_id="govplan:1",
            recipient_scope="employees",
            quantity=Decimal("1"),
            grant_at=T1,
        ),
        VestingCondition(
            **record_base("vesting", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
            plan_id="govplan:1",
            period_label="p1",
            metric="revenue",
            threshold="10%",
            status="pending",
        ),
        AuditorEngagement(
            **record_base("auditor", question_id="GOV.Q07.EXTERNAL_AUDIT"),
            audit_firm_entity_id="entity:firm",
            fiscal_period_start=date(2022, 1, 1),
            fiscal_period_end=date(2022, 12, 31),
        ),
        AuditOpinionRecord(
            **record_base("opinion", question_id="GOV.Q07.EXTERNAL_AUDIT"),
            report_period_start=date(2022, 1, 1),
            report_period_end=date(2022, 12, 31),
            opinion_type="unmodified",
        ),
        InternalControlRecord(
            **record_base("internal", question_id="GOV.Q08.INTERNAL_CONTROL_CORRECTIONS"),
            report_period_start=date(2022, 1, 1),
            report_period_end=date(2022, 12, 31),
            opinion="effective",
        ),
        RegulatoryMatter(
            **record_base("regulatory", question_id="GOV.Q09.REGULATORY_DISCLOSURE"),
            authority="SSE",
            measure_type="warning",
            subject_ids=("entity:issuer",),
            decision_date=date(2023, 1, 1),
            disclosed_status="effective",
        ),
        InquiryRecord(
            **record_base("inquiry", question_id="GOV.Q09.REGULATORY_DISCLOSURE"),
            authority="SSE",
            question_categories=("disclosure",),
            issued_at=T0,
            disclosed_status="open",
        ),
        LitigationMatter(
            **record_base("litigation", question_id="GOV.Q10.LITIGATION_COMMITMENTS"),
            disclosed_party_ids=("entity:issuer",),
            disclosed_stage="filed",
            materiality_basis="formal disclosure",
        ),
        CommitmentRecord(
            **record_base("commitment", question_id="GOV.Q10.LITIGATION_COMMITMENTS"),
            promisor_entity_id="entity:controller",
            obligation="avoid competition",
            disclosed_fulfillment_status="performing",
        ),
        GovernancePolicyVersion(
            **record_base("policy", question_id="GOV.Q11.GOVERNANCE_RULES"),
            policy_id="govpolicy:1",
            policy_type="articles",
            effective_date=date(2023, 1, 1),
            version_hash=H2,
        ),
        CorrectionRecord(
            **record_base("correction"),
            corrected_claim_id="govclaim:1",
            old_value="old",
            new_value="new",
            reason="formal correction",
        ),
    )


def test_every_governance_model_is_strict_frozen_extra_forbid_and_has_schema() -> None:
    classes = {
        value
        for value in vars(models).values()
        if inspect.isclass(value)
        and issubclass(value, GovernanceModel)
        and value is not GovernanceModel
    }
    assert classes
    for model_type in classes:
        assert model_type.model_config["frozen"] is True
        assert model_type.model_config["strict"] is True
        assert model_type.model_config["extra"] == "forbid"
        schema = model_type.model_json_schema()
        assert schema.get("type") == "object", model_type.__name__


def test_all_typed_record_union_variants_canonical_round_trip() -> None:
    samples = record_samples()
    assert len(samples) == 21
    for sample in samples:
        payload = canonical_json_bytes(sample)
        parsed = parse_governance_record_json(payload)
        assert type(parsed) is type(sample)
        assert parsed == sample


def test_snapshot_link_union_round_trip_and_discriminator_schema() -> None:
    snapshot = build_snapshot()
    links = (*snapshot.anchor_links, *snapshot.delta_links, *snapshot.record_links)
    for link in links:
        parsed = models.validate_canonical_json(
            GOVERNANCE_SNAPSHOT_LINK_ADAPTER, canonical_json_bytes(link)
        )
        assert parsed == link
    schema = GOVERNANCE_SNAPSHOT_LINK_ADAPTER.json_schema()
    assert schema["discriminator"]["propertyName"] == "kind"


def test_research_and_finding_unions_round_trip() -> None:
    research_items = (
        models.AuthoritativeSourceCandidate(
            item_id="govresearchitem:1",
            source_role=SourceRole.OFFICIAL_DISCLOSURE,
            source_locator="formal:1",
            payload_hash=H,
        ),
        ContextualEvidenceItem(
            item_id="govresearchitem:2",
            source_locator="context:1",
            summary="context",
            payload_hash=H,
        ),
        DiscoveryLead(
            item_id="govresearchitem:3",
            provider="akshare",
            locator="lead:1",
            payload_hash=H,
        ),
        DeferredResearchItem(
            item_id="govresearchitem:4",
            source_family="court",
            reason_code="deferred_v1",
            payload_hash=H,
        ),
        UnresolvedResearchGap(
            item_id="govresearchitem:5",
            gap_id="govgap:1",
            reason_code="not_found",
            detail="not found",
        ),
    )
    for item in research_items:
        parsed = models.validate_canonical_json(
            RESEARCH_RESULT_ITEM_ADAPTER, canonical_json_bytes(item)
        )
        assert parsed == item

    findings = (
        FactualFinding(
            finding_id="govfinding:1", text="fact", citation_ids=("citation:1",)
        ),
        ContextualFinding(
            finding_id="govfinding:2",
            text="context",
            citation_ids=("citation:1",),
        ),
        models.CodexJudgment(
            finding_id="govfinding:3",
            text="judgment",
            citation_ids=("citation:1",),
            uncertainty="limited evidence",
        ),
    )
    for finding in findings:
        parsed = models.validate_canonical_json(
            GOVERNANCE_FINDING_ADAPTER, canonical_json_bytes(finding)
        )
        assert parsed == finding


def test_unknown_kind_extra_fields_and_wrong_namespace_fail_closed() -> None:
    sample = record_samples()[0]
    payload = json.loads(canonical_json_bytes(sample))
    payload["kind"] = "unknown_record"
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        GOVERNANCE_RECORD_ADAPTER.validate_python(payload, strict=True)

    payload = json.loads(canonical_json_bytes(sample))
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GOVERNANCE_RECORD_ADAPTER.validate_python(payload, strict=True)

    person = models.GovernancePerson(
        person_id=f"govp:{COMPANY}:p1",
        company_id=COMPANY,
        stable_local_key="p1",
        canonical_name="person",
        source_claim_ids=("govclaim:1",),
        available_at=T1,
        canonical_hash=H,
    )
    with pytest.raises(ValidationError, match="company_id"):
        models.GovernancePerson(
            **{**person.model_dump(), "person_id": "govp:other-company:p1"}
        )


def test_noncanonical_payload_fails_before_union_validation() -> None:
    sample = record_samples()[0]
    value = json.loads(canonical_json_bytes(sample))
    noncanonical = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    with pytest.raises(CanonicalizationError, match="not canonical"):
        parse_governance_record_json(noncanonical)


def test_fact_layer_schema_has_no_score_or_management_judgment_fields() -> None:
    prohibited = {
        "governance_score",
        "governance_rating",
        "management_integrity",
        "management_ability",
    }
    for sample in record_samples():
        assert prohibited.isdisjoint(type(sample).model_fields)
    assert prohibited.isdisjoint(models.GovernanceClaim.model_fields)
