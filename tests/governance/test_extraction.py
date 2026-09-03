from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count

import pytest

from analysis.governance.admission import canonical_eligible
from analysis.governance.extraction import (
    CandidateExtractor,
    DeterministicTableExtractor,
    GovernanceExtractionOrchestrator,
    GovernanceFieldValidator,
    InMemoryExtractionSink,
    ManifestBoundExtractionLoader,
    ManifestBoundLoadError,
    OrchestrationResult,
    independent_source_count,
    validate_period_order,
    validate_total_relationship,
)
from analysis.governance.models import (
    AuditOpinionRecord,
    AuditorEngagement,
    BiographyClaim,
    CommitmentRecord,
    CompletenessStatus,
    CompensationRecord,
    ControlRelation,
    CorrectionRecord,
    ExtractorKind,
    GovernancePerson,
    GovernancePerspective,
    GovernancePolicyVersion,
    GovernanceQuestionCoverageLink,
    IncentiveGrant,
    IncentivePlan,
    InquiryRecord,
    InternalControlRecord,
    LitigationMatter,
    OwnershipPosition,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RegulatoryMatter,
    RelatedPartyRelation,
    RelatedPartyTransaction,
    ReviewStatus,
    RoleTenure,
    RosterSnapshot,
    SourceRole,
    VerificationStatus,
    VestingCondition,
)
from analysis.governance.snapshot_service import (
    GovernanceSnapshotService,
    SnapshotBuildRequest,
)
from tests.governance.fakes import FakeGovernanceAcquisitionPort
from tests.governance.test_models import valid_claim


H2 = "b" * 64
MANIFEST = "shared-manifest:governance-1"
ARTIFACT = "shared-artifact:tables-1"
RAW = "shared-raw:governance-1"
COMPANY = "cn-600519"
T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2023, 2, 1, tzinfo=timezone.utc)
T2 = datetime(2023, 3, 1, tzinfo=timezone.utc)


def table(
    kind: str,
    values: dict[str, object],
    *,
    question_id: str | None = None,
    columns: bool = True,
    row_key: str = "row-1",
    lineage: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": kind,
        "schema_version": "1.0",
        "table_id": f"table:{kind}",
        "page": 10,
        "rows": [
            {
                "row_key": row_key,
                "values": values,
                "columns": ({key: key for key in values} if columns else {}),
                **({"lineage": lineage} if lineage else {}),
            }
        ],
    }
    if question_id is not None:
        payload["question_id"] = question_id
    return payload


def port_for(
    content: object,
    *,
    llm_allowed: bool = True,
    integrity_verified: bool = True,
    lineage_complete: bool = True,
    source_role: str = "official_disclosure",
) -> FakeGovernanceAcquisitionPort:
    content_bytes = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    descriptor = {
        "artifact_id": ARTIFACT,
        "artifact_kind": "normalized_table",
        "raw_snapshot_id": RAW,
        "content_hash": content_hash,
        "source_role": source_role,
        "upstream_material_id": "upstream:annual-report-2022",
        "independence_group": "upstream:annual-report-2022",
        "integrity_verified": integrity_verified,
        "lineage_complete": lineage_complete,
        "llm_allowed": llm_allowed,
        "announced_at": T0,
        "available_at": T1,
        "retrieved_at": T2,
    }
    return FakeGovernanceAcquisitionPort(
        manifests={MANIFEST: {"manifest_id": MANIFEST, "artifacts": [descriptor]}},
        artifacts={
            (MANIFEST, ARTIFACT): {
                "artifact_id": ARTIFACT,
                "raw_snapshot_id": RAW,
                "content_hash": content_hash,
                "content_bytes": content_bytes,
                "content": content,
            }
        },
    )


def orchestrate(content: object, **port_kwargs: object):
    fake = port_for(content, **port_kwargs)
    result = GovernanceExtractionOrchestrator(
        ManifestBoundExtractionLoader(fake),
        clock=lambda: T2,
        id_factory=lambda: "test-run",
    ).execute(
        manifest_id=MANIFEST,
        artifact_id=ARTIFACT,
        company_id=COMPANY,
    )
    return result, fake


def assert_extraction_hash_contract(result: OrchestrationResult) -> None:
    assert result.run.run_hash == result.run.calculate_canonical_hash(
        hash_field="run_hash"
    )
    for claim in result.claims:
        assert claim.canonical_hash == claim.calculate_canonical_hash()
    for validation in result.validation_results:
        assert validation.canonical_hash == validation.calculate_canonical_hash()
    for record in result.canonical_records:
        assert record.canonical_hash == record.calculate_canonical_hash()
        if isinstance(record, OwnershipSnapshot):
            for position in record.positions:
                assert position.canonical_hash == position.calculate_canonical_hash()


def test_manifest_only_loader_rejects_arbitrary_path_before_port_read() -> None:
    fake = port_for({"tables": []})
    loader = ManifestBoundExtractionLoader(fake)

    for arbitrary_path in (
        r"D:\reports\annual.pdf",
        "../annual.pdf",
        "https://example.test/annual.pdf",
    ):
        with pytest.raises(ManifestBoundLoadError) as caught:
            loader.load(MANIFEST, arbitrary_path)
        assert caught.value.reason_code == "arbitrary_location_rejected"
    assert fake.artifact_reads == []

    with pytest.raises(ManifestBoundLoadError) as missing:
        loader.load(MANIFEST, "shared-artifact:not-in-manifest")
    assert missing.value.reason_code == "artifact_not_in_manifest"
    assert fake.artifact_reads == []


def test_manifest_policy_and_lineage_fail_closed_before_or_after_bound_read() -> None:
    denied = port_for({"tables": []}, llm_allowed=False)
    with pytest.raises(ManifestBoundLoadError) as policy:
        ManifestBoundExtractionLoader(denied).load(
            MANIFEST, ARTIFACT, for_llm=True
        )
    assert policy.value.reason_code == "llm_policy_denied"
    assert denied.artifact_reads == []

    corrupt = port_for({"tables": []}, integrity_verified=False)
    with pytest.raises(ManifestBoundLoadError) as integrity:
        ManifestBoundExtractionLoader(corrupt).load(MANIFEST, ARTIFACT)
    assert integrity.value.reason_code == "integrity_not_verified"
    assert corrupt.artifact_reads == []

    incomplete = port_for({"tables": []}, lineage_complete=False)
    with pytest.raises(ManifestBoundLoadError) as lineage:
        ManifestBoundExtractionLoader(incomplete).load(MANIFEST, ARTIFACT)
    assert lineage.value.reason_code == "lineage_incomplete"
    assert incomplete.artifact_reads == []


@pytest.mark.parametrize("parsed_field", ("content", "payload", "data"))
def test_loader_rejects_parsed_payload_without_authoritative_bytes(
    parsed_field: str,
) -> None:
    fake = port_for({"tables": []})
    loaded = dict(fake.artifacts[(MANIFEST, ARTIFACT)])
    parsed = loaded.pop("content")
    loaded.pop("content_bytes")
    loaded[parsed_field] = parsed
    fake.artifacts[(MANIFEST, ARTIFACT)] = loaded

    with pytest.raises(ManifestBoundLoadError) as error:
        ManifestBoundExtractionLoader(fake).load(MANIFEST, ARTIFACT)

    assert error.value.reason_code == "content_bytes_missing"
    assert fake.artifact_reads == [(MANIFEST, ARTIFACT, False)]


def test_loader_recomputes_authoritative_bytes_even_when_metadata_claims_integrity() -> None:
    fake = port_for({"tables": []})
    loaded = dict(fake.artifacts[(MANIFEST, ARTIFACT)])
    loaded["content_bytes"] = b"tampered bytes"
    fake.artifacts[(MANIFEST, ARTIFACT)] = loaded

    with pytest.raises(ManifestBoundLoadError) as error:
        ManifestBoundExtractionLoader(fake).load(MANIFEST, ARTIFACT)

    assert error.value.reason_code == "content_hash_mismatch"


def test_loader_ignores_unbound_parsed_sidecar_after_verifying_bytes() -> None:
    fake = port_for({"tables": []})
    loaded = dict(fake.artifacts[(MANIFEST, ARTIFACT)])
    loaded["content"] = {"tables": [{"kind": "unbound-sidecar"}]}
    fake.artifacts[(MANIFEST, ARTIFACT)] = loaded

    artifact = ManifestBoundExtractionLoader(fake).load(MANIFEST, ARTIFACT)

    assert artifact.content["tables"] == ()  # type: ignore[index]


def test_validator_unit_total_date_basis_and_evidence_rules() -> None:
    assert validate_total_relationship(
        {"reported_total": "3.0", "components": ["1.0", "2.0"]}
    ) == (True, None)
    total_ok, total_detail = validate_total_relationship(
        {"reported_total": "4.0", "components": ["1.0", "2.0"]}
    )
    assert not total_ok and "differs" in str(total_detail)
    date_ok, date_detail = validate_period_order(
        {
            "fiscal_period_start": datetime(2023, 2, 1).date(),
            "fiscal_period_end": datetime(2023, 1, 1).date(),
        }
    )
    assert not date_ok and "precedes" in str(date_detail)

    bad = table(
        "ownership_position",
        {
            "holder_entity_id": "entity:holder",
            "holder_name": "股东甲",
            "share_count": "100",
            "ratio": "0.10",
            "share_class": "A",
        },
    )
    result, _ = orchestrate({"tables": [bad]})
    assert not result.canonical_records
    codes = {
        check.check_code
        for validation in result.validation_results
        for check in validation.checks
        if not check.passed
    }
    assert "ratio_basis_present" in codes
    assert all(claim.verification_status == VerificationStatus.FAILED for claim in result.claims)


def test_roster_biography_multiple_people_multiple_roles_acting_and_renewal() -> None:
    roster = {
        "kind": "roster",
        "schema_version": "1.0",
        "table_id": "table:management-roster",
        "page": 20,
        "body_type": "executive",
        "is_complete": True,
        "reference_at": "2023-01-01T00:00:00+00:00",
        "columns": {
            "canonical_name": "姓名",
            "biography_text": "履历",
            "role_code": "职务",
            "valid_from": "任期开始",
            "acting": "代理",
        },
        "rows": [
            {
                "row_key": "p1",
                "values": {
                    "name": "张三",
                    "biography": "历任财务负责人。",
                    "valid_from": "2023-01-01T00:00:00+00:00",
                    "roles": [
                        {"role_code": "director"},
                        {"role_code": "chief_financial_officer", "acting": True},
                    ],
                },
                "columns": {
                    "canonical_name": "姓名",
                    "biography_text": "履历",
                    "role_code": "职务",
                    "valid_from": "任期开始",
                    "acting": "代理",
                },
            },
            {
                "row_key": "p2",
                "values": {
                    "name": "李四",
                    "valid_from": "2023-01-01T00:00:00+00:00",
                    "roles": [{"role_code": "general_manager"}],
                },
                "columns": {
                    "canonical_name": "姓名",
                    "role_code": "职务",
                    "valid_from": "任期开始",
                    "acting": "代理",
                },
            },
        ],
    }
    result, _ = orchestrate({"tables": [roster]})
    assert_extraction_hash_contract(result)
    assert sum(isinstance(item, GovernancePerson) for item in result.canonical_records) == 2
    assert sum(isinstance(item, BiographyClaim) for item in result.canonical_records) == 1
    tenures = [item for item in result.canonical_records if isinstance(item, RoleTenure)]
    assert len(tenures) == 3
    assert any(item.acting for item in tenures)
    snapshots = [item for item in result.canonical_records if isinstance(item, RosterSnapshot)]
    assert len(snapshots) == 1 and len(snapshots[0].member_ids) == 2
    # A second extraction is a new immutable renewal run, not an in-place edit.
    another, _ = orchestrate({"tables": [roster]})
    assert another.run is not result.run


@pytest.mark.parametrize(
    ("kind", "values", "expected"),
    [
        (
            "ownership",
            {
                "holder_entity_id": "entity:holder",
                "holder_name": "股东甲",
                "share_count": "100",
                "ratio": "0.10",
                "ratio_basis": "1000",
                "share_class": "A",
                "capital_basis": "total_shares",
            },
            OwnershipPosition,
        ),
        (
            "ownership_snapshot",
            {
                "positions": [
                    {
                        "holder_entity_id": "entity:holder",
                        "holder_name": "股东甲",
                        "share_count": "100",
                        "ratio": "0.10",
                        "ratio_basis": "1000",
                        "share_class": "A",
                        "capital_basis": "total_shares",
                    }
                ],
                "total_share_basis": "1000",
                "capital_basis": "total_shares",
                "is_complete": True,
                "reference_at": "2023-01-01T00:00:00+00:00",
            },
            OwnershipSnapshot,
        ),
        (
            "control",
            {
                "controller_entity_id": "entity:controller",
                "controlled_entity_id": "entity:issuer",
                "relation_type": "voting_control",
                "direction": "direct",
                "chain_path": ["entity:controller", "entity:issuer"],
            },
            ControlRelation,
        ),
        (
            "pledge",
            {
                "pledgor_entity_id": "entity:holder",
                "pledgee_entity_id": "entity:bank",
                "pledged_shares": "10",
                "pledged_ratio": "0.01",
                "ratio_basis": "1000",
                "capital_basis": "total_shares",
                "is_complete": True,
                "reference_at": "2023-01-01T00:00:00+00:00",
            },
            PledgePositionSnapshot,
        ),
        (
            "compensation",
            {
                "person_id": f"govp:{COMPANY}:p1",
                "fiscal_year": 2022,
                "cash_compensation": "1000000.00",
                "currency": "CNY",
                "scope": "issuer_paid",
            },
            CompensationRecord,
        ),
        (
            "related_party_relation",
            {
                "related_entity_id": "entity:related",
                "relation_type": "controlled_by_same_parent",
                "basis": "annual_report",
            },
            RelatedPartyRelation,
        ),
        (
            "related_party_transaction",
            {
                "counterparty_entity_id": "entity:related",
                "transaction_type": "purchase",
                "amount": "100.00",
                "currency": "CNY",
                "approval_status": "approved",
                "fiscal_period_start": "2022-01-01",
                "fiscal_period_end": "2022-12-31",
            },
            RelatedPartyTransaction,
        ),
        (
            "incentive",
            {
                "plan_id": "plan-1",
                "instrument": "restricted_share",
                "grant_pool": "100",
                "status": "active",
                "dilution_basis": "10000",
            },
            IncentivePlan,
        ),
        (
            "grant",
            {
                "plan_id": "plan-1",
                "recipient_scope": "named_executives",
                "recipient_person_ids": [f"govp:{COMPANY}:p1"],
                "quantity": "10",
                "price": "5.00",
                "currency": "CNY",
                "grant_at": "2023-01-01T00:00:00+00:00",
                "vesting_start_at": "2024-01-01T00:00:00+00:00",
            },
            IncentiveGrant,
        ),
        (
            "vesting",
            {
                "plan_id": "plan-1",
                "period_label": "first",
                "metric": "revenue",
                "threshold": ">=100",
                "actual_disclosure": "105",
                "status": "met",
            },
            VestingCondition,
        ),
        (
            "auditor",
            {
                "audit_firm_entity_id": "entity:audit-firm",
                "signing_auditors": ["auditor:a", "auditor:b"],
                "fiscal_period_start": "2022-01-01",
                "fiscal_period_end": "2022-12-31",
                "fee": "100.00",
                "currency": "CNY",
            },
            AuditorEngagement,
        ),
        (
            "audit_opinion",
            {
                "report_period_start": "2022-01-01",
                "report_period_end": "2022-12-31",
                "opinion_type": "unmodified",
                "emphasis_or_key_matter": "none",
            },
            AuditOpinionRecord,
        ),
        (
            "internal_control",
            {
                "report_period_start": "2022-01-01",
                "report_period_end": "2022-12-31",
                "opinion": "effective",
                "defect_category": "none",
                "rectification_status": "not_applicable",
            },
            InternalControlRecord,
        ),
        (
            "regulatory",
            {
                "authority": "SSE",
                "measure_type": "warning",
                "subject_ids": ["entity:issuer"],
                "decision_date": "2023-01-01",
                "disclosed_status": "effective",
            },
            RegulatoryMatter,
        ),
        (
            "inquiry",
            {
                "authority": "SSE",
                "question_categories": ["governance"],
                "issued_at": "2023-01-01T00:00:00+00:00",
                "responded_at": "2023-01-02T00:00:00+00:00",
                "disclosed_status": "responded",
            },
            InquiryRecord,
        ),
        (
            "litigation",
            {
                "disclosed_party_ids": ["entity:issuer", "entity:counterparty"],
                "amount": "100.00",
                "currency": "CNY",
                "disclosed_stage": "first_instance",
                "materiality_basis": "issuer_disclosed_material",
            },
            LitigationMatter,
        ),
        (
            "commitment",
            {
                "promisor_entity_id": "entity:controller",
                "obligation": "avoid competition",
                "deadline": "2025-01-01T00:00:00+00:00",
                "disclosed_fulfillment_status": "ongoing",
            },
            CommitmentRecord,
        ),
        (
            "policy",
            {
                "policy_id": "articles-v2",
                "policy_type": "articles",
                "effective_date": "2023-01-01",
                "version_hash": H2,
            },
            GovernancePolicyVersion,
        ),
        (
            "correction",
            {
                "corrected_claim_id": "govclaim:old",
                "old_value": "1",
                "new_value": "2",
                "reason": "clerical correction",
            },
            CorrectionRecord,
        ),
    ],
)
def test_A15_deterministic_topic_extractors_cover_all_v1_records(
    kind: str, values: dict[str, object], expected: type[object]
) -> None:
    result, _ = orchestrate({"tables": [table(kind, values)]})
    assert_extraction_hash_contract(result)
    assert any(isinstance(record, expected) for record in result.canonical_records), (
        kind,
        result.rejected_records,
    )
    assert result.claims
    span_by_id = {item.evidence_span_id: item for item in result.evidence_spans}
    validations_by_claim = {
        claim.claim_id: tuple(
            item
            for item in result.validation_results
            if item.claim_id == claim.claim_id
        )
        for claim in result.claims
    }
    assert all(not claim.canonical_eligible for claim in result.claims)
    assert all(
        canonical_eligible(
            claim,
            evidence_span=span_by_id[claim.evidence_span_id],
            validation_results=validations_by_claim[claim.claim_id],
        )
        for claim in result.claims
    )


def test_extraction_output_can_publish_directly_to_snapshot_service() -> None:
    result, _ = orchestrate(
        {
            "tables": [
                table(
                    "control",
                    {
                        "controller_entity_id": "entity:controller",
                        "controlled_entity_id": "entity:issuer",
                        "relation_type": "voting_control",
                        "direction": "direct",
                        "chain_path": ["entity:controller", "entity:issuer"],
                    },
                )
            ]
        }
    )
    record = next(
        item for item in result.canonical_records if isinstance(item, ControlRelation)
    )
    request = SnapshotBuildRequest(
        namespace="test-governance",
        company_id=COMPANY,
        state_at=T2,
        known_at=T2,
        perspective=GovernancePerspective.STRICT,
        question_set_version="v1",
        source_registry_version="test-registry-v1",
        query_pack_version="test-query-pack-v1",
        extractor_versions=(result.run.extractor_version,),
        reconstruction_version="test-reconstruction-v1",
        evidence_manifest_id=MANIFEST,
        evidence_manifest_hash=H2,
        actual_manifest_hash=H2,
        canonical_records=(record,),
        claims=result.claims,
        evidence_spans=result.evidence_spans,
        question_level_coverage=(
            GovernanceQuestionCoverageLink(
                question_id=record.question_id,
                coverage_entry_ids=("coverage:control",),
                completeness_status=CompletenessStatus.COMPLETE,
            ),
        ),
        completeness_status=CompletenessStatus.COMPLETE,
        created_at=T2,
    )

    published = GovernanceSnapshotService("test-governance").create(request)

    assert published.snapshot.canonical_record_ids == (record.record_id,)
    assert published.idempotent_reuse is False


def test_pledge_without_quantity_or_basis_is_only_an_incomplete_claim() -> None:
    result, _ = orchestrate(
        {
            "tables": [
                table(
                    "pledge",
                    {
                        "pledgor_entity_id": "entity:holder",
                        "is_complete": False,
                        "reference_at": "2023-01-01T00:00:00+00:00",
                    },
                )
            ]
        }
    )
    assert result.claims
    assert not result.canonical_records
    assert any(
        "pledge_quantity_or_basis_missing" in rejected.reason_codes
        for rejected in result.rejected_records
    )


def test_immutable_run_orchestration_persists_claim_validation_before_record() -> None:
    values = {
        "person_id": f"govp:{COMPANY}:p1",
        "fiscal_year": 2022,
        "cash_compensation": "100.00",
        "currency": "CNY",
        "scope": "issuer_paid",
    }
    fake = port_for({"tables": [table("compensation", values)]})
    sequence = count(1)
    sink = InMemoryExtractionSink()
    orchestrator = GovernanceExtractionOrchestrator(
        ManifestBoundExtractionLoader(fake),
        sink=sink,
        clock=lambda: T2,
        id_factory=lambda: f"run-{next(sequence)}",
    )
    first = orchestrator.execute(
        manifest_id=MANIFEST, artifact_id=ARTIFACT, company_id=COMPANY
    )
    second = GovernanceExtractionOrchestrator(
        ManifestBoundExtractionLoader(fake),
        clock=lambda: T2,
        id_factory=lambda: f"run-{next(sequence)}",
    ).execute(manifest_id=MANIFEST, artifact_id=ARTIFACT, company_id=COMPANY)

    assert first.run.extraction_run_id != second.run.extraction_run_id
    assert first.run.output_claim_ids == tuple(
        sorted(claim.claim_id for claim in first.claims)
    )
    kinds = [kind for kind, _identity in sink.write_order]
    assert kinds.index("claim") < kinds.index("validation") < kinds.index("record")
    assert kinds[-1] == "run"
    assert first.canonical_records  # deterministic_admission


def test_llm_candidate_stays_pending_even_when_validation_passes() -> None:
    values = {
        "person_id": f"govp:{COMPANY}:p1",
        "fiscal_year": 2022,
        "cash_compensation": "100.00",
        "currency": "CNY",
        "scope": "issuer_paid",
    }
    fake = port_for({"tables": [table("compensation", values)]})
    candidate = CandidateExtractor(
        extractor_kind=ExtractorKind.LLM,
        name="llm-governance",
        version="model-1",
    )
    result = GovernanceExtractionOrchestrator(
        ManifestBoundExtractionLoader(fake), clock=lambda: T2
    ).execute(
        manifest_id=MANIFEST,
        artifact_id=ARTIFACT,
        company_id=COMPANY,
        extractor=candidate,
    )
    assert result.claims
    assert all(claim.review_status == ReviewStatus.PENDING for claim in result.claims)
    assert all(claim.verification_status == VerificationStatus.PASSED for claim in result.claims)
    assert not result.canonical_records
    assert set(result.candidate_claim_ids) == {claim.claim_id for claim in result.claims}
    assert fake.artifact_reads[-1] == (MANIFEST, ARTIFACT, True)


def test_title_only_recall_never_creates_a_fact() -> None:
    result, _ = orchestrate(
        {"title": "关于董事及高级管理人员变动的公告", "title_matches": ["管理层变动"]}
    )
    assert result.title_recalls and not result.title_recalls[0].fact_claimed
    assert not result.claims
    assert not result.canonical_records


def test_A14_mirror_independence_group_counts_once() -> None:
    one = valid_claim(independence_group="upstream:same", upstream_material_id="upstream:same")
    two = valid_claim(
        claim_id="govclaim:mirror",
        evidence_span_id="govspan:mirror",
        independence_group="upstream:same",
        upstream_material_id="upstream:same",
    )
    assert independent_source_count((one, two)) == 1


def test_source_conflict_prevents_exact_canonical_record() -> None:
    common = {
        "holder_entity_id": "entity:holder",
        "holder_name": "股东甲",
        "ratio_basis": "1000",
        "share_class": "A",
        "reference_at": "2023-01-01T00:00:00+00:00",
    }
    first = table(
        "ownership",
        {**common, "share_count": "100"},
        row_key="source-a",
        lineage={"independence_group": "upstream:a", "upstream_material_id": "upstream:a"},
    )
    second = table(
        "ownership",
        {**common, "share_count": "120"},
        row_key="source-b",
        lineage={"independence_group": "upstream:b", "upstream_material_id": "upstream:b"},
    )
    result, _ = orchestrate({"tables": [first, second]})
    assert not result.canonical_records
    assert any(claim.has_unresolved_conflict for claim in result.claims)
    assert any(
        validation.verification_status == VerificationStatus.CONFLICTED
        for validation in result.validation_results
    )


def test_broken_span_fails_closed_without_claim_or_record() -> None:
    result, _ = orchestrate(
        {
            "tables": [
                table(
                    "compensation",
                    {
                        "person_id": f"govp:{COMPANY}:p1",
                        "fiscal_year": 2022,
                        "cash_compensation": "100.00",
                        "currency": "CNY",
                        "scope": "issuer_paid",
                    },
                    columns=False,
                )
            ]
        }
    )
    assert not result.claims
    assert not result.canonical_records
    assert any(
        "broken_span_or_lineage" in reason
        for rejected in result.rejected_records
        for reason in rejected.reason_codes
    )


def test_validator_object_is_available_for_direct_field_checks() -> None:
    assert GovernanceFieldValidator().name == "governance-field-validator"
    assert DeterministicTableExtractor().version == "1.0.0"
