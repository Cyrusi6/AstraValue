from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from analysis.governance.models import (
    AuditOpinionRecord,
    AuditorEngagement,
    AuthoritativeSourceCandidate,
    BiographyClaim,
    BudgetUsage,
    ClaimObjectType,
    CodexInputPack,
    CodexJudgment,
    CodexSessionManifest,
    CodexToolRead,
    CommitmentRecord,
    CompletenessStatus,
    CompensationRecord,
    ConflictRecord,
    ContextualEvidenceItem,
    ControlRelation,
    CorrectionRecord,
    DecisionValue,
    DeferredResearchItem,
    DeltaDisposition,
    DirectionKind,
    DiscoveryLead,
    ExtractionStatus,
    ExtractorKind,
    FactualFinding,
    GapRecord,
    GovernanceClaim,
    GovernanceEvidenceSpan,
    GovernanceExtractionRun,
    GovernancePerson,
    GovernancePerspective,
    GovernancePolicyVersion,
    GovernanceQuestionCoverageLink,
    GovernanceReport,
    GovernanceReportSection,
    GovernanceSnapshot,
    IncentiveGrant,
    IncentivePlan,
    InquiryRecord,
    InternalControlRecord,
    LitigationMatter,
    ObjectReference,
    OwnershipPosition,
    OwnershipSnapshot,
    PersonAlias,
    PersonLinkCandidate,
    PersonLinkDecision,
    PledgePositionSnapshot,
    QuestionSummary,
    QuarantinedResearchItem,
    QuarantineReason,
    RecordResolutionStatus,
    RegulatoryMatter,
    RelatedPartyRelation,
    RelatedPartyTransaction,
    ReportGenerationStatus,
    ReportTechnicalValidation,
    ResearchBudget,
    ResearchResultBundle,
    ResearchTask,
    ResearchTaskStatus,
    ReviewDecision,
    ReviewStatus,
    RoleTenure,
    RosterSnapshot,
    SnapshotAdoption,
    SnapshotAnchorLink,
    SnapshotDeltaLink,
    SnapshotRecordLink,
    SourceRole,
    TimePrecision,
    ToolReadStatus,
    ToolSchemaReference,
    UnresolvedResearchGap,
    ValidationCheck,
    ValidationResult,
    VerificationStatus,
    VestingCondition,
)


H = "a" * 64
H2 = "b" * 64
T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2023, 2, 1, tzinfo=timezone.utc)
T2 = datetime(2023, 3, 1, tzinfo=timezone.utc)
COMPANY = "cn-600519"
PERSON = f"govp:{COMPANY}:p1"
CLAIM_ID = "govclaim:1"
SPAN_ID = "govspan:1"
QUESTION = "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND"


def record_base(
    suffix: str,
    *,
    question_id: str = QUESTION,
    completeness: CompletenessStatus = CompletenessStatus.COMPLETE,
) -> dict[str, object]:
    return {
        "record_id": f"govrec:{suffix}",
        "company_id": COMPANY,
        "question_id": question_id,
        "claim_ids": (CLAIM_ID,),
        "evidence_span_ids": (SPAN_ID,),
        "available_at": T1,
        "completeness_status": completeness,
        "canonical_hash": H,
    }


def valid_claim(**overrides: object) -> GovernanceClaim:
    values: dict[str, object] = {
        "claim_id": CLAIM_ID,
        "extraction_run_id": "govxrun:1",
        "company_id": COMPANY,
        "question_id": QUESTION,
        "subject_type": "person",
        "subject_id": PERSON,
        "predicate": "role_code",
        "object_type": ClaimObjectType.TEXT,
        "object_value": "director",
        "record_type": "role_tenure",
        "announced_at": T0,
        "available_at": T1,
        "retrieved_at": T2,
        "time_precision": TimePrecision.DATETIME,
        "source_role": SourceRole.OFFICIAL_DISCLOSURE,
        "evidence_manifest_id": "shared-manifest:1",
        "raw_snapshot_id": "shared-raw:1",
        "content_hash": H,
        "evidence_span_id": SPAN_ID,
        "upstream_material_id": "upstream:1",
        "independence_group": "upstream:1",
        "extractor_kind": ExtractorKind.DETERMINISTIC,
        "extractor_version": "roster-table.v1",
        "extraction_status": ExtractionStatus.COMPLETE,
        "verification_status": VerificationStatus.PASSED,
        "review_status": ReviewStatus.NOT_REQUIRED,
        "raw_hash_verified": True,
        "lineage_complete": True,
        "evidence_locator_complete": True,
        "canonical_hash": H,
    }
    values.update(overrides)
    return GovernanceClaim(**values)


def test_enum_values_are_closed_and_stable() -> None:
    assert {item.value for item in SourceRole} == {
        "official_disclosure",
        "regulator_exchange",
        "discovery_only",
        "contextual_evidence",
        "deferred",
    }
    assert {item.value for item in ExtractorKind} == {"deterministic", "regex", "llm"}
    assert {item.value for item in ExtractionStatus} == {"complete", "partial", "failed"}
    assert {item.value for item in VerificationStatus} == {
        "passed",
        "failed",
        "conflicted",
        "not_applicable",
    }
    assert {item.value for item in ReviewStatus} == {
        "not_required",
        "pending",
        "approved",
        "rejected",
    }
    assert {item.value for item in CompletenessStatus} == {
        "complete",
        "incomplete",
        "conflicted",
    }
    assert {item.value for item in GovernancePerspective} == {"strict", "reconstructed"}
    assert {item.value for item in TimePrecision} == {"date", "datetime"}
    with pytest.raises(ValueError):
        SourceRole("trusted_blog")


def test_extraction_run_is_frozen_and_rejects_naive_timezone() -> None:
    run = GovernanceExtractionRun(
        extraction_run_id="govxrun:1",
        evidence_manifest_id="shared-manifest:1",
        raw_snapshot_id="shared-raw:1",
        content_hash=H,
        question_ids=(QUESTION,),
        extractor_kind=ExtractorKind.DETERMINISTIC,
        extractor_name="roster_table",
        extractor_version="1",
        target_schema_version="1",
        started_at=T0,
        completed_at=T1,
        output_claim_ids=(CLAIM_ID,),
        validation_result_ids=("govvalidation:1",),
        run_hash=H2,
    )
    assert run.started_at.tzinfo == timezone.utc
    with pytest.raises(ValidationError, match="frozen"):
        run.extractor_version = "2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        GovernanceExtractionRun(
            **{**run.model_dump(), "started_at": datetime(2023, 1, 1)}
        )


def test_typed_hash_factory_normalizes_defaults_and_excludes_its_own_hash() -> None:
    payload = {
        "extraction_run_id": "govxrun:hash-contract",
        "evidence_manifest_id": "shared-manifest:1",
        "raw_snapshot_id": "shared-raw:1",
        "content_hash": H,
        "question_ids": (QUESTION,),
        "extractor_kind": ExtractorKind.DETERMINISTIC,
        "extractor_name": " roster_table ",
        "extractor_version": "1",
        "target_schema_version": "1",
        "started_at": T0,
    }

    run = GovernanceExtractionRun.model_validate_with_hash(
        payload,
        hash_field="run_hash",
    )

    assert run.kind == "governance_extraction_run"
    assert run.schema_version == "1.0.0"
    assert run.extractor_name == "roster_table"
    assert run.output_claim_ids == ()
    assert run.run_hash == run.calculate_canonical_hash(hash_field="run_hash")
    poisoned = run.model_copy(update={"run_hash": H2})
    assert poisoned.calculate_canonical_hash(hash_field="run_hash") == run.run_hash


def test_evidence_span_requires_stable_field_locator_and_hashes() -> None:
    span = GovernanceEvidenceSpan(
        evidence_span_id=SPAN_ID,
        raw_snapshot_id="shared-raw:1",
        content_hash=H,
        page=2,
        table_id="table:directors",
        row_label="张三",
        column_label="职务",
        excerpt_hash=H2,
    )
    assert span.locator_complete
    with pytest.raises(ValidationError, match="stable field-level locator"):
        GovernanceEvidenceSpan(
            evidence_span_id="govspan:2",
            raw_snapshot_id="shared-raw:1",
            content_hash=H,
            excerpt_hash=H2,
        )
    with pytest.raises(ValidationError, match="supplied together"):
        GovernanceEvidenceSpan(
            evidence_span_id="govspan:2",
            raw_snapshot_id="shared-raw:1",
            content_hash=H,
            char_start=1,
            excerpt_hash=H2,
        )


def test_claim_typed_decimal_round_trip_and_claim_only_admission_fails_closed() -> None:
    claim = valid_claim(
        object_type=ClaimObjectType.DECIMAL,
        object_value=Decimal("123.4500"),
        predicate="cash_compensation",
        unit="yuan",
        currency="CNY",
    )
    assert claim.object_value == Decimal("123.4500")
    assert claim.canonical_eligible is False
    assert '"object_value":"123.45"' in claim.canonical_bytes().decode("utf-8")
    assert GovernanceClaim.model_validate_canonical_json(claim.canonical_bytes()) == claim
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GovernanceClaim(**{**claim.model_dump(), "canonical_eligible": True})
    with pytest.raises(ValidationError, match="boolean is not an integer"):
        valid_claim(object_type=ClaimObjectType.INTEGER, object_value=True)


def test_validation_result_preserves_independent_verification_axis() -> None:
    result = ValidationResult(
        validation_result_id="govvalidation:1",
        claim_id=CLAIM_ID,
        validator_name="field-contract",
        validator_version="1",
        verification_status=VerificationStatus.PASSED,
        checks=(ValidationCheck(check_code="type", passed=True),),
        validated_at=T2,
        canonical_hash=H,
    )
    assert result.verification_status == VerificationStatus.PASSED
    with pytest.raises(ValidationError, match="cannot contain failed"):
        ValidationResult(
            **{
                **result.model_dump(),
                "checks": (ValidationCheck(check_code="unit", passed=False),),
            }
        )


def test_review_gap_conflict_and_correction_are_append_only_values() -> None:
    review = ReviewDecision(
        review_decision_id="govreview:1",
        candidate_claim_id=CLAIM_ID,
        decision=DecisionValue.APPROVED,
        decided_by="maintainer:1",
        rationale="matched formal source",
        evidence_span_ids=(SPAN_ID,),
        decided_at=T1,
        available_at=T2,
        canonical_hash=H,
    )
    gap = GapRecord(
        gap_id="govgap:1",
        company_id=COMPANY,
        question_id=QUESTION,
        status=RecordResolutionStatus.ACTIVE,
        reason_code="anchor_missing",
        detail="no complete roster anchor",
        available_at=T1,
        canonical_hash=H,
    )
    conflict = ConflictRecord(
        conflict_id="govconflict:1",
        company_id=COMPANY,
        question_id=QUESTION,
        subject_id=PERSON,
        predicate="role_start",
        claim_ids=(CLAIM_ID, "govclaim:2"),
        reason="formal sources disagree",
        available_at=T2,
        canonical_hash=H,
    )
    correction = CorrectionRecord(
        **record_base("correction"),
        corrected_claim_id=CLAIM_ID,
        old_value="2023-01-01",
        new_value="2023-02-01",
        reason="formal correction",
    )
    assert review.candidate_claim_id == CLAIM_ID
    assert gap.status == RecordResolutionStatus.ACTIVE
    assert conflict.resolution_claim_id is None
    assert correction.corrected_record_id is None
    with pytest.raises(ValidationError, match="exactly one"):
        CorrectionRecord(
            **record_base("bad-correction"),
            corrected_claim_id=CLAIM_ID,
            corrected_record_id="govrec:old",
            old_value="a",
            new_value="b",
            reason="bad target",
        )


def test_person_alias_biography_and_cross_company_links_keep_local_identity() -> None:
    person = GovernancePerson(
        person_id=PERSON,
        company_id=COMPANY,
        stable_local_key="p1",
        canonical_name="张三",
        source_claim_ids=(CLAIM_ID,),
        available_at=T1,
        canonical_hash=H,
    )
    alias = PersonAlias(
        alias_id="govalias:1",
        company_id=COMPANY,
        person_id=PERSON,
        alias="ZHANG SAN",
        alias_type="english_name",
        raw_snapshot_id="shared-raw:1",
        evidence_span_id=SPAN_ID,
        extractor_version="1",
        available_at=T1,
        canonical_hash=H,
    )
    bio = BiographyClaim(
        biography_claim_id="govbio:1",
        company_id=COMPANY,
        person_id=PERSON,
        claim_id=CLAIM_ID,
        biography_text="曾任某职务",
        source_role=SourceRole.OFFICIAL_DISCLOSURE,
        raw_snapshot_id="shared-raw:1",
        evidence_span_id=SPAN_ID,
        available_at=T1,
        extractor_version="1",
        canonical_hash=H,
    )
    other_company = "cn-000001"
    other_person = f"govp:{other_company}:p9"
    candidate = PersonLinkCandidate(
        person_link_candidate_id="govlinkcandidate:1",
        company_ids=(COMPANY, other_company),
        person_ids=(PERSON, other_person),
        basis="same name and disclosed career",
        producer="resolver.v1",
        extractor_kind=ExtractorKind.DETERMINISTIC,
        evidence_span_ids=(SPAN_ID,),
        available_at=T2,
        canonical_hash=H,
    )
    decision = PersonLinkDecision(
        person_link_decision_id="govlinkdecision:1",
        person_link_candidate_id=candidate.person_link_candidate_id,
        company_ids=candidate.company_ids,
        person_ids=candidate.person_ids,
        decision=DecisionValue.REJECTED,
        decision_source="maintainer",
        producer="identity-review.v1",
        rationale="different birth dates",
        evidence_span_ids=(SPAN_ID,),
        decided_at=T2,
        available_at=T2,
        canonical_hash=H,
    )
    assert person.person_id == alias.person_id == bio.person_id
    assert decision.decision == DecisionValue.REJECTED
    with pytest.raises(ValidationError, match="company_id"):
        GovernancePerson(
            **{**person.model_dump(), "person_id": f"govp:{other_company}:p1"}
        )


def test_roster_and_role_tenure_preserve_multi_role_and_termination_evidence() -> None:
    roster = RosterSnapshot(
        **record_base("roster"),
        reference_at=T0,
        body_type="board",
        is_complete=True,
        member_ids=(PERSON,),
        source_manifest_id="shared-manifest:1",
        completeness_evidence_span_ids=(SPAN_ID,),
    )
    tenure = RoleTenure(
        **record_base("tenure"),
        person_id=PERSON,
        role_code="director",
        original_role_text="董事",
        role_scope="board",
        valid_from=T0,
        appointment_event_id="event:1",
        appointment_evidence_span_id=SPAN_ID,
    )
    assert roster.member_ids == (PERSON,)
    assert tenure.valid_to is None
    with pytest.raises(ValidationError, match="termination evidence"):
        RoleTenure(
            **{
                **tenure.model_dump(),
                "valid_to": T2,
                "termination_evidence_span_id": None,
            }
        )


def test_ownership_control_and_pledge_preserve_numeric_basis_and_completeness() -> None:
    position = OwnershipPosition(
        position_id="govposition:1",
        company_id=COMPANY,
        holder_entity_id="entity:holder",
        holder_name="控股股东",
        share_count=Decimal("100"),
        ratio=Decimal("0.5"),
        ratio_basis=Decimal("200"),
        share_class="A",
        capital_basis="total_issued_shares",
        completeness_status=CompletenessStatus.COMPLETE,
        claim_ids=(CLAIM_ID,),
        evidence_span_ids=(SPAN_ID,),
        canonical_hash=H,
    )
    ownership = OwnershipSnapshot(
        **record_base("ownership", question_id="GOV.Q01.OWNERSHIP_CONTROL"),
        reference_at=T0,
        positions=(position,),
        total_share_basis=Decimal("200"),
        capital_basis="total_issued_shares",
        is_complete=True,
        source_manifest_id="shared-manifest:1",
    )
    control = ControlRelation(
        **record_base("control", question_id="GOV.Q01.OWNERSHIP_CONTROL"),
        controller_entity_id="entity:controller",
        controlled_entity_id="entity:issuer",
        relation_type="actual_control",
        direction=DirectionKind.DIRECT,
        chain_path=("entity:controller", "entity:issuer"),
        valid_from=T0,
    )
    pledge = PledgePositionSnapshot(
        **record_base("pledge", question_id="GOV.Q02.PLEDGE_FREEZE"),
        reference_at=T1,
        pledgor_entity_id="entity:holder",
        pledged_shares=Decimal("10"),
        pledged_ratio=Decimal("0.05"),
        ratio_basis=Decimal("200"),
        capital_basis="total_issued_shares",
        is_complete=True,
    )
    assert ownership.positions[0].share_count == Decimal("100")
    assert control.chain_path[-1] == "entity:issuer"
    assert pledge.ratio_basis == Decimal("200")
    with pytest.raises(ValidationError, match="calculation basis"):
        OwnershipPosition(
            **{**position.model_dump(), "ratio_basis": None}
        )


def test_compensation_related_party_incentive_audit_and_internal_control_models() -> None:
    compensation = CompensationRecord(
        **record_base("compensation", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
        person_id=PERSON,
        fiscal_year=2022,
        cash_compensation=Decimal("1000000"),
        currency="CNY",
        scope="issuer",
    )
    relation = RelatedPartyRelation(
        **record_base("related-relation", question_id="GOV.Q06.RELATED_PARTIES"),
        related_entity_id="entity:related",
        relation_type="controller_affiliate",
        basis="formal annual report",
        valid_from=T0,
    )
    transaction = RelatedPartyTransaction(
        **record_base("related-transaction", question_id="GOV.Q06.RELATED_PARTIES"),
        counterparty_entity_id="entity:related",
        transaction_type="purchase",
        amount=Decimal("12.3"),
        currency="CNY",
        approval_status="approved",
        fiscal_period_start=date(2022, 1, 1),
        fiscal_period_end=date(2022, 12, 31),
    )
    plan = IncentivePlan(
        **record_base("plan", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
        plan_id="govplan:1",
        instrument="restricted_stock",
        grant_pool=Decimal("100"),
        status="active",
        valid_from=T0,
        dilution_basis=Decimal("10000"),
    )
    grant = IncentiveGrant(
        **record_base("grant", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
        plan_id=plan.plan_id,
        recipient_scope="named_executives",
        recipient_person_ids=(PERSON,),
        quantity=Decimal("10"),
        price=Decimal("20"),
        currency="CNY",
        grant_at=T1,
        vesting_start_at=T2,
    )
    condition = VestingCondition(
        **record_base("vesting", question_id="GOV.Q05.REMUNERATION_INCENTIVES"),
        plan_id=plan.plan_id,
        period_label="first_vesting_period",
        metric="revenue_growth",
        threshold=">=10%",
        actual_disclosure="12%",
        status="met",
    )
    auditor = AuditorEngagement(
        **record_base("auditor", question_id="GOV.Q07.EXTERNAL_AUDIT"),
        audit_firm_entity_id="entity:audit-firm",
        signing_auditors=("person:a", "person:b"),
        fiscal_period_start=date(2022, 1, 1),
        fiscal_period_end=date(2022, 12, 31),
        fee=Decimal("100"),
        currency="CNY",
    )
    opinion = AuditOpinionRecord(
        **record_base("opinion", question_id="GOV.Q07.EXTERNAL_AUDIT"),
        report_period_start=date(2022, 1, 1),
        report_period_end=date(2022, 12, 31),
        opinion_type="unmodified",
    )
    internal = InternalControlRecord(
        **record_base("internal", question_id="GOV.Q08.INTERNAL_CONTROL_CORRECTIONS"),
        report_period_start=date(2022, 1, 1),
        report_period_end=date(2022, 12, 31),
        opinion="effective",
    )
    assert compensation.cash_compensation == Decimal("1000000")
    assert relation.valid_to is None
    assert transaction.amount == Decimal("12.3")
    assert grant.plan_id == condition.plan_id == plan.plan_id
    assert auditor.fee == Decimal("100")
    assert opinion.opinion_type == "unmodified"
    assert internal.opinion == "effective"


def test_regulatory_inquiry_litigation_commitment_and_policy_models() -> None:
    regulatory = RegulatoryMatter(
        **record_base("regulatory", question_id="GOV.Q09.REGULATORY_DISCLOSURE"),
        authority="SSE",
        measure_type="disciplinary_action",
        subject_ids=("entity:issuer",),
        decision_date=date(2023, 1, 2),
        disclosed_status="effective",
    )
    inquiry = InquiryRecord(
        **record_base("inquiry", question_id="GOV.Q09.REGULATORY_DISCLOSURE"),
        authority="SSE",
        question_categories=("disclosure",),
        issued_at=T0,
        responded_at=T1,
        disclosed_status="responded",
    )
    litigation = LitigationMatter(
        **record_base("litigation", question_id="GOV.Q10.LITIGATION_COMMITMENTS"),
        disclosed_party_ids=("entity:issuer",),
        amount=Decimal("100"),
        currency="CNY",
        disclosed_stage="first_instance",
        materiality_basis="issuer disclosure",
    )
    commitment = CommitmentRecord(
        **record_base("commitment", question_id="GOV.Q10.LITIGATION_COMMITMENTS"),
        promisor_entity_id="entity:controller",
        obligation="avoid competition",
        deadline=T2,
        disclosed_fulfillment_status="performing",
    )
    policy = GovernancePolicyVersion(
        **record_base("policy", question_id="GOV.Q11.GOVERNANCE_RULES"),
        policy_id="govpolicy:articles-v2",
        policy_type="articles_of_association",
        effective_date=date(2023, 1, 1),
        version_hash=H2,
    )
    assert regulatory.authority == inquiry.authority
    assert litigation.source_scope == "formal_disclosures_only"
    assert commitment.valid_to is None
    assert policy.version_hash == H2


def build_snapshot() -> GovernanceSnapshot:
    snapshot_id = "govsnapshot:1"
    anchor = SnapshotAnchorLink(
        anchor_link_id="govanchor:1",
        governance_snapshot_id=snapshot_id,
        question_id=QUESTION,
        state_kind="roster",
        anchor_record_id="govrec:roster",
        reference_at=T0,
        available_at=T1,
        canonical_hash=H,
    )
    delta = SnapshotDeltaLink(
        delta_link_id="govdelta:1",
        governance_snapshot_id=snapshot_id,
        question_id=QUESTION,
        delta_record_id="govrec:tenure",
        disposition=DeltaDisposition.APPLIED,
        sequence=1,
        effective_at=T1,
        available_at=T1,
        canonical_hash=H,
    )
    record_link = SnapshotRecordLink(
        record_link_id="govrecordlink:1",
        governance_snapshot_id=snapshot_id,
        record_id="govrec:tenure",
        record_kind="role_tenure",
        canonical_hash=H,
    )
    return GovernanceSnapshot(
        governance_snapshot_id=snapshot_id,
        company_id=COMPANY,
        state_at=T2,
        known_at=T2,
        state_time_precision=TimePrecision.DATETIME,
        known_time_precision=TimePrecision.DATETIME,
        perspective=GovernancePerspective.STRICT,
        question_set_version="1.0.0",
        source_registry_version="1.0.0",
        query_pack_version="1.0.0",
        extractor_versions=("roster-table.v1",),
        reconstruction_version="1.0.0",
        evidence_manifest_id="shared-manifest:1",
        evidence_manifest_hash=H,
        anchor_links=(anchor,),
        delta_links=(delta,),
        record_links=(record_link,),
        canonical_record_ids=("govrec:tenure",),
        question_level_coverage=(
            GovernanceQuestionCoverageLink(
                question_id=QUESTION,
                coverage_entry_ids=("shared-coverage:1",),
                completeness_status=CompletenessStatus.COMPLETE,
            ),
        ),
        completeness_status=CompletenessStatus.COMPLETE,
        future_knowledge_used=False,
        canonical_snapshot_hash=H2,
        created_at=T2,
    )


def test_snapshot_links_versions_coverage_and_bitemporal_identity() -> None:
    snapshot = build_snapshot()
    assert snapshot.canonical_record_ids == ("govrec:tenure",)
    assert snapshot.perspective == GovernancePerspective.STRICT
    with pytest.raises(ValidationError, match="strict perspective"):
        GovernanceSnapshot(
            **{**snapshot.model_dump(), "known_at": T2 + timedelta(days=1)}
        )
    with pytest.raises(ValidationError, match="match canonical record links"):
        GovernanceSnapshot(
            **{**snapshot.model_dump(), "canonical_record_ids": ("govrec:other",)}
        )


def test_codex_research_quarantine_adoption_and_report_contracts() -> None:
    snapshot = build_snapshot()
    budget = ResearchBudget(
        max_rounds=2,
        max_child_tasks=2,
        max_network_requests=10,
        max_parallelism=1,
        wall_clock_seconds=60,
        max_output_bytes=10000,
    )
    question_summary = QuestionSummary(
        question_id=QUESTION,
        completeness_status=CompletenessStatus.COMPLETE,
        coverage_entry_ids=("shared-coverage:1",),
        anchor_record_ids=("govrec:roster",),
        summary="complete roster and one appointment",
    )
    tool_schema = ToolSchemaReference(
        tool_name="query_roles_and_people",
        tool_version="1",
        input_schema_hash=H,
        output_schema_hash=H2,
    )
    input_pack = CodexInputPack(
        input_pack_id="govinput:1",
        company_id=COMPANY,
        state_at=T2,
        known_at=T2,
        perspective=GovernancePerspective.STRICT,
        governance_snapshot_id=snapshot.governance_snapshot_id,
        governance_snapshot_hash=snapshot.canonical_snapshot_hash,
        evidence_manifest_id=snapshot.evidence_manifest_id,
        evidence_manifest_hash=snapshot.evidence_manifest_hash,
        question_set_version="1.0.0",
        question_summaries=(question_summary,),
        important_records=(
            ObjectReference(
                object_id="govrec:tenure",
                object_kind="role_tenure",
                schema_version="1.0.0",
                canonical_hash=H,
            ),
        ),
        tool_schemas=(tool_schema,),
        research_budget=budget,
        temporal_rules=("available_at_lte_known_at",),
        report_output_schema_hash=H,
        canonical_hash=H2,
        created_at=T2,
    )
    tool_read = CodexToolRead(
        tool_read_id="govtoolread:1",
        session_id="govsession:1",
        governance_snapshot_id=snapshot.governance_snapshot_id,
        sequence=1,
        tool_name=tool_schema.tool_name,
        tool_version="1",
        canonical_parameters_json='{"person_id":"govp:cn-600519:p1"}',
        parameters_hash=H,
        response_payload_json='{"record_id":"govrec:tenure"}',
        response_hash=H2,
        actual_record_ids=("govrec:tenure",),
        actual_claim_ids=(CLAIM_ID,),
        actual_evidence_span_ids=(SPAN_ID,),
        actual_raw_snapshot_ids=("shared-raw:1",),
        citation_ids=("citation:1",),
        status=ToolReadStatus.SUCCEEDED,
        occurred_at=T2,
        canonical_hash=H,
    )
    task = ResearchTask(
        research_task_id="govresearchtask:1",
        parent_session_id="govsession:1",
        company_id=COMPANY,
        question_ids=(QUESTION,),
        gap_ids=("govgap:1",),
        question="find formal roster anchor",
        state_at=T2,
        known_at=T2,
        perspective=GovernancePerspective.STRICT,
        allowed_source_roles=(
            SourceRole.OFFICIAL_DISCLOSURE,
            SourceRole.REGULATOR_EXCHANGE,
        ),
        budget=budget,
        recursion_depth=1,
        result_schema_name="research-result-bundle",
        result_schema_version="1",
        result_schema_hash=H,
        created_at=T2,
        canonical_hash=H2,
    )
    authoritative = AuthoritativeSourceCandidate(
        item_id="govresearchitem:1",
        source_role=SourceRole.OFFICIAL_DISCLOSURE,
        source_locator="https://example.invalid/formal.pdf",
        available_at=T1,
        time_precision=TimePrecision.DATETIME,
        payload_hash=H,
    )
    contextual = ContextualEvidenceItem(
        item_id="govresearchitem:2",
        source_locator="https://example.invalid/news",
        summary="context only",
        available_at=T1,
        time_precision=TimePrecision.DATETIME,
        payload_hash=H,
    )
    discovery = DiscoveryLead(
        item_id="govresearchitem:3",
        provider="akshare",
        locator="lead:1",
        payload_hash=H,
    )
    deferred = DeferredResearchItem(
        item_id="govresearchitem:4",
        source_family="court",
        reason_code="deferred_v1",
        payload_hash=H,
    )
    unresolved = UnresolvedResearchGap(
        item_id="govresearchitem:5",
        gap_id="govgap:1",
        reason_code="not_found",
        detail="formal material not located",
    )
    bundle = ResearchResultBundle(
        research_result_bundle_id="govresearchbundle:1",
        research_task_id=task.research_task_id,
        task_status=ResearchTaskStatus.COMPLETED,
        budget_used=BudgetUsage(
            rounds=1,
            child_tasks=1,
            network_requests=2,
            wall_clock_milliseconds=100,
            output_bytes=200,
        ),
        authoritative_source_candidates=(authoritative,),
        contextual_evidence=(contextual,),
        discovery_leads=(discovery,),
        deferred_items=(deferred,),
        unresolved_gaps=(unresolved,),
        created_at=T2,
        canonical_hash=H,
    )
    quarantined = QuarantinedResearchItem(
        quarantine_id="govquarantine:1",
        research_task_id=task.research_task_id,
        research_result_bundle_id=bundle.research_result_bundle_id,
        source_item_id=authoritative.item_id,
        reason_code=QuarantineReason.FUTURE_INFORMATION,
        quarantined_payload_artifact_id="artifact:quarantine:1",
        quarantined_payload_hash=H,
        known_at=T2,
        quarantined_at=T2,
        canonical_hash=H2,
    )
    adoption = SnapshotAdoption(
        snapshot_adoption_id="govadoption:1",
        session_id="govsession:1",
        expected_session_revision=0,
        resulting_session_revision=1,
        old_snapshot_id="govsnapshot:1",
        old_snapshot_hash=H,
        new_snapshot_id="govsnapshot:2",
        new_snapshot_hash=H2,
        adopted_at_sequence=2,
        adopted_at=T2,
        canonical_hash=H,
    )
    session = CodexSessionManifest(
        session_manifest_id="govsession:1",
        parent_report_run_id="report-run:1",
        model_profile="codex-governance-v1",
        runner_protocol_version="1",
        tool_protocol_version="1",
        input_pack_id=input_pack.input_pack_id,
        input_pack_hash=input_pack.canonical_hash,
        initial_snapshot_id="govsnapshot:1",
        final_snapshot_id="govsnapshot:2",
        tool_read_ids=(tool_read.tool_read_id,),
        research_task_ids=(task.research_task_id,),
        research_result_bundle_ids=(bundle.research_result_bundle_id,),
        snapshot_adoption_ids=(adoption.snapshot_adoption_id,),
        final_citation_ids=("citation:1",),
        report_hash=H,
        generation_status=ReportGenerationStatus.COMPLETED,
        output_schema_validated=True,
        started_at=T1,
        completed_at=T2,
        canonical_hash=H2,
    )
    finding = FactualFinding(
        finding_id="govfinding:1",
        text="formal roster contains the named director",
        citation_ids=("citation:1",),
    )
    judgment = CodexJudgment(
        finding_id="govfinding:2",
        text="Codex qualitative assessment",
        citation_ids=("citation:1",),
        uncertainty="limited history",
    )
    report = GovernanceReport(
        governance_report_id="govreport:1",
        company_id=COMPANY,
        state_at=T2,
        known_at=T2,
        perspective=GovernancePerspective.STRICT,
        governance_snapshot_id="govsnapshot:2",
        governance_snapshot_hash=H2,
        evidence_manifest_id="shared-manifest:2",
        evidence_manifest_hash=H,
        session_manifest_id=session.session_manifest_id,
        session_manifest_hash=session.canonical_hash,
        generation_status=ReportGenerationStatus.COMPLETED,
        sections=(
            GovernanceReportSection(
                section_id="people",
                title="Board and management",
                findings=(finding, judgment),
            ),
        ),
        data_limitations=("limited history",),
        citation_ids=("citation:1",),
        future_knowledge_used=False,
        technical_validation=ReportTechnicalValidation(
            passed=True,
            checks=(ValidationCheck(check_code="schema", passed=True),),
        ),
        created_at=T2,
        canonical_report_hash=H,
    )
    assert input_pack.governance_snapshot_id == snapshot.governance_snapshot_id
    assert tool_read.sequence == 1
    assert bundle.discovery_leads[0].provider == "akshare"
    assert quarantined.model_dump().keys().isdisjoint(
        {"title", "summary", "url", "value", "conclusion"}
    )
    assert session.output_schema_validated
    assert report.decision_author == "codex"
    assert not hasattr(report, "governance_score")
