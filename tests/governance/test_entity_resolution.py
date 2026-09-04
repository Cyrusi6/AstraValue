from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from analysis.governance.entity_resolution import (
    CompanyPersonResolver,
    IdentityResolutionError,
    approved_link_groups,
    build_person_alias,
    build_person_link_candidate,
    build_person_link_decision,
    select_visible_link_decisions,
)
from analysis.governance.models import (
    CompletenessStatus,
    CompensationRecord,
    ControlRelation,
    DecisionValue,
    DirectionKind,
    OwnershipPosition,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RoleTenure,
)
from analysis.governance.reducers import (
    NumericBalanceDelta,
    NumericDeltaKind,
    OwnershipPositionDelta,
    PledgePositionDelta,
    ReducerIntegrityError,
    RoleAction,
    RoleDelta,
    project_annual_records,
    project_current_roles,
    project_versioned_records,
    reduce_numeric_balance,
    reduce_ownership_state,
    reduce_pledge_state,
    reduce_role_tenures,
    validate_control_graph,
)


T0 = datetime(2022, 12, 31, 8, tzinfo=timezone.utc)
T1 = datetime(2023, 6, 1, 8, tzinfo=timezone.utc)
T2 = datetime(2024, 2, 1, 8, tzinfo=timezone.utc)
COMPANY = "company:600519"
QUESTION = "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND"
H = "0" * 64


def tenure(
    suffix: str,
    person: str,
    role: str,
    *,
    start: datetime = T0,
    scope: str = "listed_company",
    acting: bool = False,
    available_at: datetime = T0,
    event_id: str | None = None,
    supersedes: str | None = None,
) -> RoleTenure:
    event = event_id or f"event:{suffix}"
    return RoleTenure(
        record_id=f"govrec:{suffix}",
        company_id=COMPANY,
        question_id=QUESTION,
        claim_ids=(f"govclaim:{suffix}",),
        evidence_span_ids=(f"govspan:{suffix}",),
        source_event_ids=(event,),
        effective_at=start,
        valid_from=start,
        available_at=available_at,
        completeness_status=CompletenessStatus.COMPLETE,
        supersedes_record_id=supersedes,
        canonical_hash=H,
        person_id=person,
        role_code=role,
        original_role_text=role,
        role_scope=scope,
        acting=acting,
        appointment_event_id=event,
        appointment_evidence_span_id=f"govspan:{suffix}",
    )


def compensation(
    suffix: str,
    *,
    year: int,
    amount: str,
    effective_at: datetime,
    supersedes: str | None = None,
) -> CompensationRecord:
    return CompensationRecord(
        record_id=f"govrec:{suffix}",
        company_id=COMPANY,
        question_id="GOV.Q05.REMUNERATION_INCENTIVES",
        claim_ids=(f"govclaim:{suffix}",),
        evidence_span_ids=(f"govspan:{suffix}",),
        reference_at=effective_at,
        effective_at=effective_at,
        available_at=effective_at,
        completeness_status=CompletenessStatus.COMPLETE,
        supersedes_record_id=supersedes,
        canonical_hash=H,
        person_id=f"govp:{COMPANY}:p1",
        fiscal_year=year,
        cash_compensation=Decimal(amount),
        currency="CNY",
        scope="total",
    )


def control(
    suffix: str,
    left: str,
    right: str,
    *,
    direction: DirectionKind = DirectionKind.DIRECT,
    path: tuple[str, ...] | None = None,
) -> ControlRelation:
    return ControlRelation(
        record_id=f"govrec:{suffix}",
        company_id=COMPANY,
        question_id="GOV.Q01.OWNERSHIP_CONTROL",
        claim_ids=(f"govclaim:{suffix}",),
        evidence_span_ids=(f"govspan:{suffix}",),
        effective_at=T0,
        valid_from=T0,
        available_at=T0,
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        controller_entity_id=left,
        controlled_entity_id=right,
        relation_type="control",
        direction=direction,
        chain_path=path or (left, right),
    )


def test_company_local_same_name_and_stable_id_collision() -> None:
    resolver = CompanyPersonResolver(id_factory=lambda: "stable-fallback")
    first = resolver.resolve(
        company_id="company:A",
        canonical_name="张三",
        source_claim_ids=("govclaim:a",),
        available_at=T0,
        stable_evidence_key="roster-row-1",
    )
    other_company = resolver.resolve(
        company_id="company:B",
        canonical_name="张三",
        source_claim_ids=("govclaim:b",),
        available_at=T0,
        stable_evidence_key="roster-row-1",
    )
    assert first.person_id != other_company.person_id
    assert first.person_id.startswith("govp:company:A:")
    assert other_company.person_id.startswith("govp:company:B:")

    resolver.resolve(
        company_id="company:C",
        canonical_name="Alice",
        source_claim_ids=("govclaim:c",),
        available_at=T0,
    )
    with pytest.raises(IdentityResolutionError, match="repeatedly"):
        resolver.resolve(
            company_id="company:C",
            canonical_name="Bob",
            source_claim_ids=("govclaim:d",),
            available_at=T0,
        )


def test_alias_visibility_and_same_evidence_key_conflict() -> None:
    resolver = CompanyPersonResolver()
    person = resolver.resolve(
        company_id=COMPANY,
        canonical_name="Zhang San",
        source_claim_ids=("govclaim:person",),
        available_at=T0,
        stable_evidence_key="director-seat-1",
    )
    alias = build_person_alias(
        company_id=COMPANY,
        person_id=person.person_id,
        alias="张三",
        alias_type="chinese_name",
        raw_snapshot_id="raw:1",
        evidence_span_id="govspan:alias",
        extractor_version="v1",
        available_at=T1,
    )
    resolver.register_alias(alias)
    assert resolver.candidates_for_alias(
        company_id=COMPANY, alias="张三", known_at=T1 - timedelta(seconds=1)
    ) == ()
    assert resolver.candidates_for_alias(
        company_id=COMPANY, alias=" 张 三 ", known_at=T1
    ) == (person,)
    with pytest.raises(IdentityResolutionError, match="different name"):
        resolver.resolve(
            company_id=COMPANY,
            canonical_name="李四",
            source_claim_ids=("govclaim:other",),
            available_at=T1,
            stable_evidence_key="director-seat-1",
        )


def test_link_candidate_order_approved_rejected_and_decision_time() -> None:
    candidate = build_person_link_candidate(
        company_ids=("company:B", "company:A"),
        person_ids=("govp:company:B:b", "govp:company:A:a"),
        basis="formal biography",
        producer="maintenance",
        evidence_span_ids=("govspan:b", "govspan:a"),
        available_at=T0,
    )
    reversed_candidate = build_person_link_candidate(
        company_ids=("company:A", "company:B"),
        person_ids=("govp:company:A:a", "govp:company:B:b"),
        basis="formal biography",
        producer="maintenance",
        evidence_span_ids=("govspan:a", "govspan:b"),
        available_at=T0,
    )
    assert candidate.person_link_candidate_id == reversed_candidate.person_link_candidate_id
    approved = build_person_link_decision(
        candidate=candidate,
        decision=DecisionValue.APPROVED,
        decision_source="formal maintenance",
        producer="reviewer",
        rationale="same disclosed identity",
        decided_at=T1,
        available_at=T1,
    )
    rejected = build_person_link_decision(
        candidate=candidate,
        decision=DecisionValue.REJECTED,
        decision_source="formal maintenance",
        producer="reviewer",
        rationale="later evidence disproves link",
        decided_at=T2,
        available_at=T2,
        supersedes_decision_id=approved.person_link_decision_id,
    )
    assert select_visible_link_decisions(
        (approved, rejected), known_at=T1, candidates=(candidate,)
    ) == (approved,)
    assert select_visible_link_decisions(
        (approved, rejected), known_at=T2, candidates=(candidate,)
    ) == ()


def test_approved_link_groups_are_transitive() -> None:
    ab = build_person_link_candidate(
        company_ids=("company:A", "company:B"),
        person_ids=("govp:company:A:a", "govp:company:B:b"),
        basis="evidence",
        producer="reviewer",
        evidence_span_ids=("govspan:ab",),
        available_at=T0,
    )
    bc = build_person_link_candidate(
        company_ids=("company:B", "company:C"),
        person_ids=("govp:company:B:b", "govp:company:C:c"),
        basis="evidence",
        producer="reviewer",
        evidence_span_ids=("govspan:bc",),
        available_at=T0,
    )
    decisions = tuple(
        build_person_link_decision(
            candidate=item,
            decision=DecisionValue.APPROVED,
            decision_source="review",
            producer="reviewer",
            rationale="verified",
            decided_at=T1,
            available_at=T1,
        )
        for item in (ab, bc)
    )
    assert approved_link_groups(decisions, known_at=T1) == (
        ("govp:company:A:a", "govp:company:B:b", "govp:company:C:c"),
    )


def test_roster_role_tenure_multi_occupancy_and_valid_to_projection() -> None:
    p1 = f"govp:{COMPANY}:p1"
    p2 = f"govp:{COMPANY}:p2"
    result = reduce_role_tenures(
        (tenure("vice1", p1, "vice_general_manager"), tenure("vice2", p2, "vice_general_manager")),
        (),
        state_at=T1,
        known_at=T1,
        anchor_complete=True,
    )
    assert len(result.tenures) == 2
    assert result.completeness_status == CompletenessStatus.COMPLETE
    projected = project_current_roles(
        result.tenures, state_at=T1, coverage_complete=False
    )
    assert all(item.active_at_state and not item.confirmed_current for item in projected)
    no_anchor = reduce_role_tenures((), (), state_at=T1, known_at=T1)
    assert no_anchor.completeness_status == CompletenessStatus.INCOMPLETE
    assert no_anchor.issues[0].issue_code == "anchor_missing"


def test_appointment_resignation_scope_acting_renewal_correction_and_overlap() -> None:
    p1 = f"govp:{COMPANY}:p1"
    p2 = f"govp:{COMPANY}:p2"
    acting = tenure("acting", p1, "general_manager", acting=True)
    formal = tenure(
        "formal", p1, "general_manager", start=T1, event_id="event:formal"
    )
    appoint = RoleDelta(
        delta_id="delta:formal",
        action=RoleAction.APPOINT,
        effective_at=T1,
        announced_at=T1,
        available_at=T1,
        person_id=p1,
        role_codes=("general_manager",),
        evidence_span_id="govspan:formal",
        event_id="event:formal",
        new_tenures=(formal,),
        claim_ids=("govclaim:formal",),
    )
    result = reduce_role_tenures(
        (acting,), (appoint,), state_at=T2, known_at=T2, anchor_complete=True
    )
    assert len(result.tenures) == 2
    closed_acting = next(item for item in result.tenures if item.acting)
    assert closed_acting.valid_to == T1
    assert closed_acting.record_id != acting.record_id
    assert closed_acting.supersedes_record_id == acting.record_id
    assert "govspan:formal" in closed_acting.evidence_span_ids
    assert "event:formal" in closed_acting.source_event_ids

    renewed = tenure(
        "renewed", p1, "general_manager", start=T2, event_id="event:renewed"
    )
    renewal = RoleDelta(
        delta_id="delta:renewal",
        action=RoleAction.RENEW,
        effective_at=T2,
        announced_at=T2,
        available_at=T2,
        person_id=p1,
        role_codes=("general_manager",),
        evidence_span_id="govspan:renewed",
        event_id="event:renewed",
        new_tenures=(renewed,),
    )
    renewed_result = reduce_role_tenures(
        (formal,), (renewal,), state_at=T2, known_at=T2, anchor_complete=True
    )
    assert len(renewed_result.tenures) == 2
    assert sum(item.valid_to is None for item in renewed_result.tenures) == 1

    board = tenure("board", p1, "director", scope="board")
    subsidiary = tenure("sub", p1, "director", scope="subsidiary")
    ambiguous = RoleDelta(
        delta_id="delta:ambiguous",
        action=RoleAction.RESIGN,
        effective_at=T1,
        announced_at=T1,
        available_at=T1,
        person_id=p1,
        role_codes=("director",),
        evidence_span_id="govspan:resign",
        event_id="event:resign",
        scope_is_explicit=False,
    )
    unchanged = reduce_role_tenures(
        (board, subsidiary),
        (ambiguous,),
        state_at=T2,
        known_at=T2,
        anchor_complete=True,
    )
    assert {item.record_id for item in unchanged.tenures} == {
        board.record_id,
        subsidiary.record_id,
    }

    newcomer = tenure(
        "new-chair", p2, "chairperson", start=T1, event_id="event:new-chair"
    )
    overlap = RoleDelta(
        delta_id="delta:new-chair",
        action=RoleAction.APPOINT,
        effective_at=T1,
        announced_at=T1,
        available_at=T1,
        person_id=p2,
        role_codes=("chairperson",),
        evidence_span_id="govspan:new-chair",
        event_id="event:new-chair",
        new_tenures=(newcomer,),
    )
    conflicted = reduce_role_tenures(
        (tenure("old-chair", p1, "chairperson"),),
        (overlap,),
        state_at=T2,
        known_at=T2,
        anchor_complete=True,
    )
    assert conflicted.completeness_status == CompletenessStatus.CONFLICTED
    assert len(conflicted.tenures) == 2

    wrong = tenure(
        "wrong-date", p1, "director", start=T1, event_id="event:wrong"
    )
    corrected = tenure(
        "correct-date", p1, "director", start=T0, event_id="event:correct"
    )
    original_delta = RoleDelta(
        delta_id="delta:wrong",
        action=RoleAction.APPOINT,
        effective_at=T1,
        announced_at=T1,
        available_at=T1,
        person_id=p1,
        role_codes=("director",),
        evidence_span_id="govspan:wrong-date",
        event_id="event:wrong",
        new_tenures=(wrong,),
    )
    correction_delta = RoleDelta(
        delta_id="delta:correct",
        action=RoleAction.CORRECT,
        effective_at=T0,
        announced_at=T2,
        available_at=T2,
        person_id=p1,
        role_codes=("director",),
        evidence_span_id="govspan:correct-date",
        event_id="event:correct",
        new_tenures=(corrected,),
        supersedes_delta_id=original_delta.delta_id,
    )
    before_correction = reduce_role_tenures(
        (),
        (original_delta, correction_delta),
        state_at=T1,
        known_at=T1,
        anchor_complete=True,
    )
    assert before_correction.tenures == (wrong,)
    after_correction = reduce_role_tenures(
        (),
        (original_delta, correction_delta),
        state_at=T2,
        known_at=T2,
        anchor_complete=True,
    )
    assert after_correction.tenures == (corrected,)
    assert original_delta.delta_id in after_correction.excluded_delta_ids


def test_rotation_can_explicitly_close_a_different_persons_predecessor() -> None:
    previous_person = f"govp:{COMPANY}:previous-chair"
    next_person = f"govp:{COMPANY}:next-chair"
    previous = tenure("previous-chair", previous_person, "chairperson")
    successor = tenure(
        "chair-rotation",
        next_person,
        "chairperson",
        start=T1,
        event_id="event:chair-rotation",
    )
    rotation = RoleDelta(
        delta_id="delta:chair-rotation",
        action=RoleAction.ROTATE,
        effective_at=T1,
        announced_at=T1,
        available_at=T1,
        person_id=next_person,
        role_codes=("chairperson",),
        evidence_span_id="govspan:chair-rotation",
        event_id="event:chair-rotation",
        new_tenures=(successor,),
        explicitly_terminates_tenure_ids=(previous.record_id,),
    )

    result = reduce_role_tenures(
        (previous,),
        (rotation,),
        state_at=T2,
        known_at=T2,
        anchor_complete=True,
    )

    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.applied_delta_ids == (rotation.delta_id,)
    assert len(result.tenures) == 2
    closed_previous = next(
        item for item in result.tenures if item.person_id == previous_person
    )
    assert closed_previous.valid_to == T1
    assert closed_previous.supersedes_record_id == previous.record_id
    assert next(item for item in result.tenures if item.person_id == next_person) == successor


def test_ownership_state_pledge_partial_release_and_conservation() -> None:
    deltas = (
        NumericBalanceDelta(
            "delta:release-unknown",
            NumericDeltaKind.RELEASE,
            T1,
            T1,
            T1,
            None,
            "shares",
            "total-shares-v1",
            event_id="event:partial-release",
        ),
        NumericBalanceDelta(
            "delta:later",
            NumericDeltaKind.PLEDGE,
            T2,
            T2,
            T2,
            Decimal("2"),
            "shares",
            "total-shares-v1",
            event_id="event:later",
        ),
    )
    result = reduce_numeric_balance(
        Decimal("10"),
        deltas,
        state_at=T2,
        known_at=T2,
        anchor_unit="shares",
        anchor_calculation_basis="total-shares-v1",
    )
    assert result.balance is None
    assert result.applied_delta_ids == ()
    assert result.retained_delta_ids == ("delta:release-unknown", "delta:later")
    assert result.retained_event_ids == ("event:partial-release", "event:later")
    assert result.completeness_status == CompletenessStatus.INCOMPLETE

    negative = NumericBalanceDelta(
        "delta:negative",
        NumericDeltaKind.DECREASE,
        T1,
        T1,
        T1,
        Decimal("-2"),
        "shares",
        "total-shares-v1",
    )
    with pytest.raises(ReducerIntegrityError, match="cannot be negative"):
        reduce_numeric_balance(
            Decimal("10"), (negative,), state_at=T2, known_at=T2
        )

    position = OwnershipPosition(
        position_id="govposition:p1",
        company_id=COMPANY,
        holder_entity_id="entity:holder",
        holder_name="股东甲",
        share_count=Decimal("10"),
        share_class="A",
        capital_basis="issued-a-shares",
        completeness_status=CompletenessStatus.COMPLETE,
        claim_ids=("govclaim:position",),
        evidence_span_ids=("govspan:position",),
        canonical_hash=H,
    )
    ownership_anchor = OwnershipSnapshot(
        record_id="govrec:ownership-anchor",
        company_id=COMPANY,
        question_id="GOV.Q01.OWNERSHIP_CONTROL",
        claim_ids=("govclaim:ownership",),
        evidence_span_ids=("govspan:ownership",),
        reference_at=T0,
        available_at=T0,
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        positions=(position,),
        total_share_basis=Decimal("100"),
        capital_basis="issued-a-shares",
        is_complete=True,
        source_manifest_id="manifest:1",
    )
    ownership = reduce_ownership_state(
        ownership_anchor,
        (
            OwnershipPositionDelta(
                delta_id="delta:holder-increase",
                holder_entity_id="entity:holder",
                share_class="A",
                kind=NumericDeltaKind.INCREASE,
                effective_at=T1,
                announced_at=T1,
                available_at=T1,
                quantity=Decimal("2"),
                unit="shares",
                capital_basis="issued-a-shares",
                event_id="event:holder-increase",
            ),
        ),
        state_at=T2,
        known_at=T2,
    )
    assert ownership.positions[0].balance == Decimal("12")
    assert ownership.retained_delta_ids == ("delta:holder-increase",)

    pledge_anchor = PledgePositionSnapshot(
        record_id="govrec:pledge-anchor",
        company_id=COMPANY,
        question_id="GOV.Q02.PLEDGE_FREEZE",
        claim_ids=("govclaim:pledge",),
        evidence_span_ids=("govspan:pledge",),
        reference_at=T0,
        available_at=T0,
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        pledgor_entity_id="entity:holder",
        pledgee_entity_id="entity:bank",
        pledged_shares=Decimal("5"),
        ratio_basis=Decimal("100"),
        capital_basis="issued-a-shares",
        is_complete=True,
    )
    pledge = reduce_pledge_state(
        (pledge_anchor,),
        (
            PledgePositionDelta(
                delta_id="delta:partial-release",
                pledgor_entity_id="entity:holder",
                pledgee_entity_id="entity:bank",
                kind=NumericDeltaKind.RELEASE,
                effective_at=T1,
                announced_at=T1,
                available_at=T1,
                quantity=None,
                unit="shares",
                capital_basis="issued-a-shares",
                event_id="event:partial-release",
            ),
        ),
        state_at=T2,
        known_at=T2,
    )
    assert pledge.positions[0].balance is None
    assert pledge.retained_delta_ids == ("delta:partial-release",)
    assert pledge.retained_event_ids == ("event:partial-release",)


def test_control_chain_requires_every_segment() -> None:
    direct_ab = control("ab", "entity:A", "entity:B")
    direct_bc = control("bc", "entity:B", "entity:C")
    indirect = control(
        "ac",
        "entity:A",
        "entity:C",
        direction=DirectionKind.INDIRECT,
        path=("entity:A", "entity:B", "entity:C"),
    )
    assert validate_control_graph((direct_ab, direct_bc, indirect))[-1].record_id
    with pytest.raises(ReducerIntegrityError, match="unproven segment"):
        validate_control_graph((direct_ab, indirect))


def test_annual_record_interval_record_conflict_and_supersedes() -> None:
    old = compensation("pay-old", year=2023, amount="10", effective_at=T0)
    future = compensation(
        "pay-new",
        year=2023,
        amount="12",
        effective_at=T2,
        supersedes=old.record_id,
    )
    before = project_versioned_records(
        (old, future), state_at=T1, known_at=T2
    )
    assert before.records == (old,)
    after = project_annual_records(
        (old, future), fiscal_year=2023, state_at=T2, known_at=T2
    )
    assert after.records == (future,)

    conflicting = compensation(
        "pay-conflict", year=2023, amount="99", effective_at=T0
    )
    conflict_result = project_annual_records(
        (old, conflicting), fiscal_year=2023, state_at=T1, known_at=T1
    )
    assert conflict_result.records == ()
    assert conflict_result.completeness_status == CompletenessStatus.CONFLICTED
    assert {item.record_ids for item in conflict_result.conflicts} == {
        ("govrec:pay-conflict", "govrec:pay-old")
    }
