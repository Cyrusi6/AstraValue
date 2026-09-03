from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis.governance.canonical import canonical_json_bytes, canonical_sha256
from analysis.governance.entity_resolution import (
    CompanyPersonResolver,
    approved_link_groups,
    build_person_link_candidate,
    build_person_link_decision,
    select_visible_link_decisions,
)
from analysis.governance.models import (
    CompensationRecord,
    CompletenessStatus,
    ConflictRecord,
    ControlRelation,
    DecisionValue,
    DirectionKind,
    GapRecord,
    GovernancePerspective,
    OwnershipPosition,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RecordResolutionStatus,
    RoleTenure,
    RosterSnapshot,
    TimePrecision,
)
from analysis.governance.reconstruction import (
    AnchorCandidate,
    CoverageState,
    DeltaCandidate,
    QueryInstant,
    ReconstructionValidationError,
    VersionGraphError,
    VersionedItem,
    normalize_governance_query,
    reconstruct_annual_records,
    reconstruct_governance_state,
    reconstruct_interval_records,
    reconstruct_numeric_balance,
    reconstruct_role_tenures,
    reconstruct_state,
    resolve_visible_versions,
    select_anchor,
)
from analysis.governance.reducers import (
    NumericBalanceDelta,
    NumericDeltaKind,
    OwnershipPositionDelta,
    PledgePositionDelta,
    ReducerIntegrityError,
    RoleAction,
    RoleDelta,
    project_current_roles,
    project_versioned_records,
)


UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
H = "a" * 64
COMPANY = "cn-600519"
QUESTION_ROLE = "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND"
QUESTION_CONTROL = "GOV.Q01.CONTROL_STRUCTURE"
CLAIM = "govclaim:1"
SPAN = "govspan:1"
TIMELINE_FIXTURE = Path(__file__).parent / "fixtures" / "bitemporal_timeline_v1.json"


def dt(month: int, day: int = 1, hour: int = 0) -> datetime:
    return datetime(2023, month, day, hour, tzinfo=UTC)


def query(
    state_at: object = dt(6),
    known_at: object | None = None,
    perspective: GovernancePerspective | str | None = None,
):
    return normalize_governance_query(state_at, known_at, perspective)


def anchor(
    stable_id: str,
    payload: object,
    *,
    reference_at: object = dt(1),
    available_at: object = dt(2),
    complete: bool = True,
    supersedes_id: str | None = None,
) -> AnchorCandidate[object]:
    return AnchorCandidate(
        stable_id=stable_id,
        payload=payload,
        reference_at=reference_at,
        available_at=available_at,
        complete=complete,
        supersedes_id=supersedes_id,
    )


def delta(
    stable_id: str,
    payload: object | None = None,
    *,
    effective_at: object = dt(3),
    announced_at: object = dt(3),
    available_at: object = dt(3),
    supersedes_id: str | None = None,
    withdrawn: bool = False,
) -> DeltaCandidate[object]:
    return DeltaCandidate(
        stable_id=stable_id,
        payload=stable_id if payload is None else payload,
        effective_at=effective_at,
        announced_at=announced_at,
        available_at=available_at,
        supersedes_id=supersedes_id,
        withdrawn=withdrawn,
    )


def passthrough(anchor_value: object, values: tuple[object, ...], _query: object):
    return (anchor_value, values)


def tenure(
    suffix: str,
    person: str,
    role: str,
    valid_from: datetime,
    *,
    acting: bool = False,
) -> RoleTenure:
    return RoleTenure(
        record_id=f"govrec:{suffix}",
        company_id=COMPANY,
        question_id=QUESTION_ROLE,
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        available_at=dt(2),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        person_id=f"govp:{COMPANY}:{person}",
        role_code=role,
        original_role_text=role,
        role_scope="company",
        acting=acting,
        valid_from=valid_from,
        # The reducer binds the surrounding delta; leaving this optional field
        # unset avoids manufacturing a second event identity in the fixture.
        appointment_event_id=None,
        appointment_evidence_span_id=SPAN,
    )


def roster_snapshot(
    suffix: str,
    member_ids: tuple[str, ...],
    *,
    is_complete: bool = True,
) -> RosterSnapshot:
    return RosterSnapshot(
        record_id=f"govrec:roster-{suffix}",
        company_id=COMPANY,
        question_id=QUESTION_ROLE,
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        available_at=dt(2),
        completeness_status=(
            CompletenessStatus.COMPLETE
            if is_complete
            else CompletenessStatus.INCOMPLETE
        ),
        canonical_hash=H,
        body_type="executives",
        is_complete=is_complete,
        member_ids=member_ids,
        source_manifest_id=f"manifest:{suffix}",
        completeness_evidence_span_ids=(SPAN,) if is_complete else (),
    )


def gap_record(
    suffix: str,
    *,
    question_id: str = QUESTION_CONTROL,
    available_at: object = dt(2),
    status: RecordResolutionStatus = RecordResolutionStatus.ACTIVE,
    supersedes_gap_id: str | None = None,
) -> GapRecord:
    return GapRecord(
        gap_id=f"govgap:{suffix}",
        company_id=COMPANY,
        question_id=question_id,
        status=status,
        reason_code="coverage_gap",
        detail="冻结测试中的覆盖缺口",
        available_at=available_at,
        supersedes_gap_id=supersedes_gap_id,
        canonical_hash=H,
    )


def conflict_record(suffix: str) -> ConflictRecord:
    return ConflictRecord(
        conflict_id=f"govconflict:{suffix}",
        company_id=COMPANY,
        question_id=QUESTION_CONTROL,
        subject_id="entity:controller",
        predicate="control_relation",
        claim_ids=("govclaim:left", "govclaim:right"),
        reason="两个正式来源无法裁决",
        available_at=dt(2),
        canonical_hash=H,
    )


def role_delta(
    suffix: str,
    action: RoleAction,
    person: str,
    role_codes: tuple[str, ...],
    effective_at: datetime,
    *,
    new_tenures: tuple[RoleTenure, ...] = (),
    terminates: tuple[str, ...] = (),
) -> RoleDelta:
    return RoleDelta(
        delta_id=f"delta:{suffix}",
        action=action,
        effective_at=effective_at,
        announced_at=effective_at,
        available_at=effective_at,
        person_id=f"govp:{COMPANY}:{person}",
        role_codes=role_codes,
        evidence_span_id=SPAN,
        event_id=f"govevent:{suffix}",
        new_tenures=new_tenures,
        explicitly_terminates_tenure_ids=terminates,
    )


def role_candidate(value: RoleDelta, *, supersedes_id: str | None = None):
    return DeltaCandidate(
        stable_id=value.delta_id,
        payload=value,
        effective_at=value.effective_at,
        announced_at=value.announced_at,
        available_at=value.available_at,
        supersedes_id=supersedes_id,
    )


def numeric_candidate(value: NumericBalanceDelta):
    return DeltaCandidate(
        stable_id=value.delta_id,
        payload=value,
        effective_at=value.effective_at,
        announced_at=value.announced_at,
        available_at=value.available_at,
    )


def test_default_strict_query_is_canonical() -> None:
    default = normalize_governance_query("2023-06-01T00:00:00Z")
    explicit = normalize_governance_query(
        "2023-06-01T00:00:00Z",
        "2023-06-01T00:00:00Z",
        GovernancePerspective.STRICT,
    )
    assert default == explicit
    assert default.future_knowledge_used is False


def test_explicit_reconstructed_sets_future_knowledge_used() -> None:
    value = query(dt(2), dt(6), GovernancePerspective.RECONSTRUCTED)
    assert value.perspective == GovernancePerspective.RECONSTRUCTED
    assert value.future_knowledge_used is True


@pytest.mark.parametrize(
    ("state_at", "known_at", "perspective"),
    [
        (dt(2), dt(6), None),
        (dt(2), dt(6), GovernancePerspective.STRICT),
        (dt(6), dt(2), GovernancePerspective.RECONSTRUCTED),
        (datetime(2023, 1, 1), None, None),
        ("2023-01-01T12:00:00", None, None),
    ],
)
def test_invalid_time_rejects_implicit_hindsight_and_known_before_state(
    state_at: object,
    known_at: object | None,
    perspective: object | None,
) -> None:
    with pytest.raises(ReconstructionValidationError):
        normalize_governance_query(state_at, known_at, perspective)


def test_direct_governance_query_construction_rejects_invalid_types() -> None:
    instant = QueryInstant.parse("2023-01-01")
    with pytest.raises(ReconstructionValidationError, match="perspective"):
        type(query())(instant, instant, "bogus", False)
    with pytest.raises(ReconstructionValidationError, match="QueryInstant"):
        type(query())("2023-01-01", instant, GovernancePerspective.STRICT, False)


def test_date_only_uses_shanghai_exclusive_boundary() -> None:
    value = QueryInstant.parse(date(2023, 3, 10))
    assert value.cutoff == datetime(2023, 3, 10, 16, tzinfo=UTC)
    assert value.precision == TimePrecision.DATE
    assert value.original_timezone == "Asia/Shanghai"


def test_intraday_cutoff_excludes_date_only_evidence() -> None:
    item = VersionedItem("v1", "payload", date(2023, 3, 10))
    intraday = query("2023-03-10T12:00:00+08:00")
    end_of_day = query("2023-03-10")
    assert resolve_visible_versions((item,), intraday).selected_ids == ()
    assert resolve_visible_versions((item,), end_of_day).selected_ids == ("v1",)


def test_timezone_rfc3339_normalizes_to_utc() -> None:
    value = QueryInstant.parse("2023-03-10T20:30:00+08:00")
    assert value.cutoff == datetime(2023, 3, 10, 12, 30, tzinfo=UTC)
    assert value.original_input == "2023-03-10T20:30:00+08:00"
    assert value.original_timezone == "UTC+08:00"


@pytest.mark.parametrize(
    "raw",
    [
        "2023-01-01 12:00:00+00:00",
        "20230101T120000+00:00",
        "2023-01-01T12:00:00+00",
        "2023-01-01T12:00+00:00",
        "2023-01-01T12:00:00,5+00:00",
        "2023-W01-1",
    ],
)
def test_non_rfc3339_time_forms_are_rejected(raw: str) -> None:
    with pytest.raises(ReconstructionValidationError):
        QueryInstant.parse(raw)


def test_conservative_boundary_for_available_date() -> None:
    item = VersionedItem("dated", "payload", "2023-03-10")
    assert item.available_at_upper_bound == datetime(2023, 3, 10, 16, tzinfo=UTC)


def test_date_query_excludes_exact_next_shanghai_midnight() -> None:
    dated = VersionedItem("dated", "known-on-date", "2023-03-10")
    next_midnight = VersionedItem(
        "next-midnight",
        "must-not-leak",
        "2023-03-11T00:00:00+08:00",
    )
    selection = resolve_visible_versions((dated, next_midnight), query("2023-03-10"))
    assert selection.selected_ids == ("dated",)


def test_same_cutoff_mixed_precision_requires_explicit_reconstructed() -> None:
    with pytest.raises(ReconstructionValidationError, match="explicit reconstructed"):
        normalize_governance_query(
            "2023-03-10", known_at="2023-03-11T00:00:00+08:00"
        )
    reconstructed = normalize_governance_query(
        "2023-03-10",
        known_at="2023-03-11T00:00:00+08:00",
        perspective=GovernancePerspective.RECONSTRUCTED,
    )
    assert reconstructed.future_knowledge_used is True


def test_availability_first_hides_late_correction_graph() -> None:
    old = VersionedItem("old", "old payload", dt(2))
    late = VersionedItem(
        "late", object(), dt(8), supersedes_id="not-visible-and-missing"
    )
    selection = resolve_visible_versions((late, old), query(dt(6)))
    assert selection.selected_ids == ("old",)


def test_supersedes_selects_visible_head() -> None:
    old = VersionedItem("old", "old payload", dt(2))
    corrected = VersionedItem("new", "new payload", dt(3), supersedes_id="old")
    selection = resolve_visible_versions((corrected, old), query())
    assert selection.selected_ids == ("new",)
    assert {item.stable_id: item.reason for item in selection.decisions}["old"] == (
        "superseded_by_visible_version:new"
    )


def test_withdrawal_removes_only_visible_version() -> None:
    old = VersionedItem("old", "payload", dt(2))
    withdrawal = VersionedItem(
        "withdrawal", None, dt(3), supersedes_id="old", withdrawn=True
    )
    selection = resolve_visible_versions((old, withdrawal), query())
    assert selection.selected == ()
    assert {item.reason for item in selection.decisions} == {
        "superseded_by_visible_version:withdrawal",
        "visible_withdrawal",
    }


def test_late_correction_does_not_leak_content() -> None:
    old = VersionedItem("old", {"value": "then-known"}, dt(2))
    corrected = VersionedItem(
        "new", {"secret_future_value": 99}, dt(8), supersedes_id="old"
    )
    selection = resolve_visible_versions((corrected, old), query(dt(6)))
    assert tuple(item.payload for item in selection.selected) == ({"value": "then-known"},)


@pytest.mark.parametrize(
    "items",
    [
        (
            VersionedItem("new", 1, dt(2), supersedes_id="missing"),
        ),
        (
            VersionedItem("a", 1, dt(2), supersedes_id="b"),
            VersionedItem("b", 2, dt(2), supersedes_id="a"),
        ),
        (
            VersionedItem("root", 1, dt(2)),
            VersionedItem("left", 2, dt(2), supersedes_id="root"),
            VersionedItem("right", 3, dt(2), supersedes_id="root"),
        ),
    ],
)
def test_supersedes_graph_integrity_fails_closed(items: tuple[VersionedItem[int], ...]) -> None:
    with pytest.raises(VersionGraphError):
        resolve_visible_versions(items, query())


def test_supersedes_version_must_be_append_only() -> None:
    target = VersionedItem("target", "old", dt(3))
    impossible = VersionedItem("replacement", "new", dt(2), supersedes_id="target")
    with pytest.raises(VersionGraphError, match="predates superseded"):
        resolve_visible_versions((target, impossible), query())


def test_anchor_selects_latest_complete_visible_candidate() -> None:
    values = (
        anchor("old-complete", "old", reference_at=dt(1), available_at=dt(2)),
        anchor(
            "new-incomplete",
            "partial",
            reference_at=dt(4),
            available_at=dt(4),
            complete=False,
        ),
        anchor("future", "future", reference_at=dt(5), available_at=dt(8)),
    )
    selected = select_anchor(reversed(values), query())
    assert selected.selected is not None
    assert selected.selected.stable_id == "old-complete"


def test_delta_order_ignores_retrieval_order_and_out_of_order_input() -> None:
    values = (
        delta("late", effective_at=dt(5), announced_at=dt(2), available_at=dt(2)),
        delta("early-b", effective_at=dt(3), announced_at=dt(2), available_at=dt(5)),
        delta("early-a", effective_at=dt(3), announced_at=dt(2), available_at=dt(4)),
    )
    result_a = reconstruct_state(
        query(), deltas=values, reducer=passthrough, anchor_required=False
    )
    result_b = reconstruct_state(
        query(), deltas=reversed(values), reducer=passthrough, anchor_required=False
    )
    assert result_a.ordered_delta_ids == ("early-a", "early-b", "late")
    assert result_a.ordered_delta_ids == result_b.ordered_delta_ids
    assert result_a.state == result_b.state


def test_excluded_reason_is_stable() -> None:
    values = (
        delta("before", effective_at=dt(1)),
        delta(
            "future-effective",
            effective_at=dt(8),
            announced_at=dt(2),
            available_at=dt(2),
        ),
        delta("future-known", effective_at=dt(4), available_at=dt(8)),
        delta("old", effective_at=dt(4), available_at=dt(3)),
        delta(
            "corrected",
            effective_at=dt(4),
            available_at=dt(5),
            supersedes_id="old",
        ),
    )
    result = reconstruct_state(
        query(),
        anchors=(anchor("a", (), reference_at=dt(2)),),
        deltas=reversed(values),
        reducer=passthrough,
    )
    reasons = {item.delta_id: item.reason for item in result.delta_outcomes}
    assert reasons == {
        "before": "not_after_anchor_boundary",
        "corrected": "applied",
        "future-effective": "effective_after_state_at",
        "future-known": "available_after_known_at",
        "old": "superseded_by_visible_version:corrected",
    }


def test_roster_timeline_applies_visible_events() -> None:
    alice = tenure("alice-director", "alice", "director", dt(1))
    bob = tenure("bob-director", "bob", "director", dt(3))
    appoint = role_delta(
        "appoint-bob",
        RoleAction.APPOINT,
        "bob",
        ("director",),
        dt(3),
        new_tenures=(bob,),
    )
    resign = role_delta(
        "resign-alice", RoleAction.RESIGN, "alice", ("director",), dt(4)
    )
    result = reconstruct_role_tenures(
        query(),
        anchors=(anchor("roster", (alice,), reference_at=dt(2)),),
        deltas=(role_candidate(resign), role_candidate(appoint)),
        coverage=(CoverageState(QUESTION_ROLE),),
    )
    by_person = {item.person_id: item for item in result.state.tenures}
    assert by_person[alice.person_id].valid_to == dt(4)
    assert by_person[bob.person_id].valid_to is None
    assert result.applied_delta_ids == (appoint.delta_id, resign.delta_id)
    assert result.completeness_status == CompletenessStatus.COMPLETE


def test_real_roster_snapshot_anchor_reaches_role_reducer() -> None:
    alice = tenure("roster-alice", "alice", "director", dt(1))
    roster = roster_snapshot("typed-anchor", (alice.person_id,))
    result = reconstruct_role_tenures(
        query(),
        anchors=(
            anchor(
                "typed-roster-anchor",
                (roster, alice),
                reference_at=dt(1),
                available_at=dt(2),
            ),
        ),
        coverage=(CoverageState(QUESTION_ROLE, available_at=dt(2)),),
    )
    assert result.anchor_id == "typed-roster-anchor"
    assert result.state.tenures == (alice,)
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert "anchor_missing" not in {item.issue_code for item in result.issues}


def test_no_anchor_returns_partial_facts_incomplete() -> None:
    bob = tenure("bob-director", "bob", "director", dt(3))
    appoint = role_delta(
        "appoint-bob",
        RoleAction.APPOINT,
        "bob",
        ("director",),
        dt(3),
        new_tenures=(bob,),
    )
    result = reconstruct_role_tenures(query(), deltas=(role_candidate(appoint),))
    assert tuple(item.record_id for item in result.state.tenures) == (bob.record_id,)
    assert result.anchor_id is None
    assert result.completeness_status == CompletenessStatus.INCOMPLETE


def test_appointment_resignation_acting_renewal_and_correction_are_preserved_in_order() -> None:
    acting = tenure(
        "acting-cfo", "carol", "chief_financial_officer", dt(3), acting=True
    )
    permanent = tenure(
        "permanent-cfo", "carol", "chief_financial_officer", dt(4)
    )
    renewed = tenure(
        "renewed-cfo", "carol", "chief_financial_officer", dt(5)
    )
    wrong = tenure("wrong-cfo", "wrong", "chief_financial_officer", dt(3))
    wrong_delta = role_delta(
        "acting-wrong",
        RoleAction.ACTING,
        "wrong",
        ("chief_financial_officer",),
        dt(3),
        new_tenures=(wrong,),
    )
    corrected = role_delta(
        "acting-corrected",
        RoleAction.ACTING,
        "carol",
        ("chief_financial_officer",),
        dt(3),
        new_tenures=(acting,),
    )
    appointment = role_delta(
        "appoint-carol",
        RoleAction.APPOINT,
        "carol",
        ("chief_financial_officer",),
        dt(4),
        new_tenures=(permanent,),
        terminates=(acting.record_id,),
    )
    renewal = role_delta(
        "renew-carol",
        RoleAction.RENEW,
        "carol",
        ("chief_financial_officer",),
        dt(5),
        new_tenures=(renewed,),
    )
    result = reconstruct_role_tenures(
        query(),
        anchors=(anchor("empty-roster", (), reference_at=dt(2)),),
        deltas=(
            role_candidate(renewal),
            role_candidate(appointment),
            role_candidate(corrected, supersedes_id=wrong_delta.delta_id),
            role_candidate(wrong_delta),
        ),
    )
    assert result.applied_delta_ids == (
        corrected.delta_id,
        appointment.delta_id,
        renewal.delta_id,
    )
    assert wrong_delta.delta_id in result.excluded_delta_ids
    assert {item.acting for item in result.state.tenures} == {False, True}
    assert any(item.record_id == renewed.record_id and item.valid_to is None for item in result.state.tenures)


def numeric_delta(
    suffix: str,
    *,
    kind: NumericDeltaKind = NumericDeltaKind.INCREASE,
    quantity: Decimal | None = Decimal("2"),
) -> NumericBalanceDelta:
    return NumericBalanceDelta(
        delta_id=f"delta:{suffix}",
        kind=kind,
        effective_at=dt(3),
        announced_at=dt(3),
        available_at=dt(3),
        quantity=quantity,
        unit="share" if quantity is not None else None,
        calculation_basis="total shares" if quantity is not None else None,
    )


def control_relation() -> ControlRelation:
    return ControlRelation(
        record_id="govrec:control",
        company_id=COMPANY,
        question_id=QUESTION_CONTROL,
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        effective_at=dt(1),
        valid_from=dt(1),
        available_at=dt(2),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        controller_entity_id="entity:controller",
        controlled_entity_id="entity:company",
        relation_type="actual_control",
        direction=DirectionKind.DIRECT,
        chain_path=("entity:controller", "entity:company"),
    )


def test_ownership_control_pledge_dispatch() -> None:
    position = OwnershipPosition(
        position_id="govposition:dispatch-holder",
        company_id=COMPANY,
        holder_entity_id="entity:holder",
        holder_name="股东甲",
        share_count=Decimal("10"),
        share_class="A",
        capital_basis="issued-a-shares",
        completeness_status=CompletenessStatus.COMPLETE,
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        canonical_hash=H,
    )
    ownership_anchor = OwnershipSnapshot(
        record_id="govrec:dispatch-ownership-anchor",
        company_id=COMPANY,
        question_id="GOV.Q01.OWNERSHIP_CONTROL",
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        available_at=dt(2),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        positions=(position,),
        total_share_basis=Decimal("100"),
        capital_basis="issued-a-shares",
        is_complete=True,
        source_manifest_id="manifest:dispatch",
    )
    ownership_delta = OwnershipPositionDelta(
        delta_id="delta:dispatch-ownership-increase",
        holder_entity_id="entity:holder",
        share_class="A",
        kind=NumericDeltaKind.INCREASE,
        effective_at=dt(3),
        announced_at=dt(3),
        available_at=dt(3),
        quantity=Decimal("2"),
        unit="shares",
        capital_basis="issued-a-shares",
        event_id="govevent:dispatch-ownership-increase",
    )
    ownership = reconstruct_governance_state(
        "ownership",
        query(),
        anchors=(anchor("ownership-anchor", ownership_anchor),),
        deltas=(
            DeltaCandidate(
                stable_id=ownership_delta.delta_id,
                payload=ownership_delta,
                effective_at=ownership_delta.effective_at,
                announced_at=ownership_delta.announced_at,
                available_at=ownership_delta.available_at,
            ),
        ),
        coverage=(CoverageState("GOV.Q01.OWNERSHIP_CONTROL"),),
    )
    pledge_anchor = PledgePositionSnapshot(
        record_id="govrec:dispatch-pledge-anchor",
        company_id=COMPANY,
        question_id="GOV.Q02.PLEDGE_FREEZE",
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        available_at=dt(2),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        pledgor_entity_id="entity:holder",
        pledgee_entity_id="entity:bank",
        pledged_shares=Decimal("10"),
        ratio_basis=Decimal("100"),
        capital_basis="issued-a-shares",
        is_complete=True,
    )
    pledge_delta = PledgePositionDelta(
        delta_id="delta:dispatch-pledge-increase",
        pledgor_entity_id="entity:holder",
        pledgee_entity_id="entity:bank",
        kind=NumericDeltaKind.PLEDGE,
        effective_at=dt(3),
        announced_at=dt(3),
        available_at=dt(3),
        quantity=Decimal("2"),
        unit="shares",
        capital_basis="issued-a-shares",
        event_id="govevent:dispatch-pledge-increase",
    )
    pledge = reconstruct_governance_state(
        "pledge",
        query(),
        anchors=(anchor("pledge-anchor", (pledge_anchor,)),),
        deltas=(
            DeltaCandidate(
                stable_id=pledge_delta.delta_id,
                payload=pledge_delta,
                effective_at=pledge_delta.effective_at,
                announced_at=pledge_delta.announced_at,
                available_at=pledge_delta.available_at,
            ),
        ),
        coverage=(CoverageState("GOV.Q02.PLEDGE_FREEZE"),),
    )
    control = reconstruct_governance_state(
        "control",
        query(),
        anchors=(anchor("control-anchor", (control_relation(),)),),
        coverage=(CoverageState("GOV.Q01.OWNERSHIP_CONTROL"),),
    )
    assert ownership.state.positions[0].balance == Decimal("12")
    assert pledge.state.positions[0].balance == Decimal("12")
    assert tuple(item.record_id for item in control.state.records) == (
        "govrec:control",
    )


def test_incomplete_balance_retains_event_without_inventing_number() -> None:
    release = numeric_delta(
        "partial-release", kind=NumericDeltaKind.RELEASE, quantity=None
    )
    result = reconstruct_numeric_balance(
        query(),
        anchors=(anchor("balance", Decimal("10")),),
        deltas=(numeric_candidate(release),),
    )
    assert result.state.balance is None
    assert result.state.retained_event_ids == (release.delta_id,)
    assert result.completeness_status == CompletenessStatus.INCOMPLETE
    assert {item.delta_id: item.reason for item in result.delta_outcomes}[
        release.delta_id
    ].startswith("retained_without_state_update:")


def test_incomplete_pledge_anchor_cannot_be_promoted_by_wrapper() -> None:
    partial_anchor = PledgePositionSnapshot(
        record_id="govrec:partial-pledge-anchor",
        company_id=COMPANY,
        question_id="GOV.Q02.PLEDGE_FREEZE",
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        available_at=dt(2),
        completeness_status=CompletenessStatus.INCOMPLETE,
        canonical_hash=H,
        pledgor_entity_id="entity:holder",
        pledged_shares=Decimal("10"),
        capital_basis="issued-a-shares",
        is_complete=False,
    )
    result = reconstruct_governance_state(
        "pledge",
        query(),
        anchors=(
            anchor(
                "wrapper-claims-complete",
                (partial_anchor,),
                complete=True,
            ),
        ),
        coverage=(CoverageState("GOV.Q02.PLEDGE_FREEZE", available_at=dt(2)),),
    )
    assert result.state.positions[0].balance == Decimal("10")
    assert result.state.completeness_status == CompletenessStatus.INCOMPLETE
    assert result.completeness_status == CompletenessStatus.INCOMPLETE


def test_coverage_gap_marks_incomplete_without_failure() -> None:
    gap = gap_record("1")
    coverage = CoverageState(
        "GOV.Q01.CONTROL_STRUCTURE",
        completeness_status=CompletenessStatus.INCOMPLETE,
        gap_ids=(gap.gap_id,),
        available_at=dt(2),
    )
    result = reconstruct_interval_records(query(), coverage=(coverage,), gaps=(gap,))
    assert result.completeness_status == CompletenessStatus.INCOMPLETE
    assert result.coverage[0].gap_ids == (gap.gap_id,)
    assert result.gaps == (gap,)


def test_conflicted_business_state_is_returned() -> None:
    conflict = conflict_record("1")
    coverage = CoverageState(
        "GOV.Q02.OWNERSHIP",
        completeness_status=CompletenessStatus.CONFLICTED,
        conflict_ids=(conflict.conflict_id,),
        available_at=dt(2),
    )
    result = reconstruct_interval_records(
        query(), coverage=(coverage,), conflicts=(conflict,)
    )
    assert result.completeness_status == CompletenessStatus.CONFLICTED
    assert result.conflicts == (conflict,)


@pytest.mark.parametrize(
    "side_records",
    ({"gaps": ({"title": "future correction", "amount": 999},)}, {"conflicts": ("legacy",)}),
)
def test_untyped_gap_or_conflict_cannot_bypass_strict_time_gate(
    side_records: dict[str, tuple[object, ...]],
) -> None:
    with pytest.raises(ReconstructionValidationError, match="must be typed"):
        reconstruct_interval_records(query(), **side_records)


def test_no_data_is_not_negative_fact() -> None:
    coverage = CoverageState(
        "GOV.Q09.REGULATORY",
        completeness_status=CompletenessStatus.COMPLETE,
        coverage_entry_ids=("coverage:1",),
        no_data=True,
    )
    result = reconstruct_interval_records(query(), coverage=(coverage,))
    assert result.state.records == ()
    assert result.no_data_questions == ("GOV.Q09.REGULATORY",)
    assert result.negative_fact_inferred is False


def compensation_record(suffix: str, fiscal_year: int) -> CompensationRecord:
    return CompensationRecord(
        record_id=f"govrec:compensation-{suffix}",
        company_id=COMPANY,
        question_id="GOV.Q05.REMUNERATION_INCENTIVES",
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        available_at=dt(5),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        person_id=f"govp:{COMPANY}:alice",
        fiscal_year=fiscal_year,
        cash_compensation=Decimal("1"),
        currency="CNY",
        scope="company",
    )


def test_annual_projection_excludes_period_after_state_at() -> None:
    current = compensation_record("2023", 2023)
    future = compensation_record("2024", 2024)
    result = reconstruct_annual_records(
        query("2023-12-31", "2025-01-01", GovernancePerspective.RECONSTRUCTED),
        deltas=(
            delta(
                future.record_id,
                future,
                effective_at=dt(3),
                available_at=dt(5),
            ),
            delta(
                current.record_id,
                current,
                effective_at=dt(3),
                available_at=dt(5),
            ),
        ),
        fiscal_year=2023,
    )
    assert result.state.records == (current,)
    reasons = {item.delta_id: item.reason for item in result.delta_outcomes}
    assert reasons[future.record_id] == "annual_period_after_state_at"
    assert reasons[current.record_id] == "applied"


def _control_version(
    record_id: str,
    *,
    available_at: datetime,
    supersedes_record_id: str | None = None,
) -> ControlRelation:
    payload = control_relation().model_dump(mode="python")
    payload.update(
        record_id=record_id,
        available_at=available_at,
        supersedes_record_id=supersedes_record_id,
    )
    return ControlRelation.model_validate(payload, strict=True)


def _record_candidate(record: ControlRelation) -> DeltaCandidate[ControlRelation]:
    return DeltaCandidate(
        stable_id=record.record_id,
        payload=record,
        effective_at=record.effective_at or dt(1),
        announced_at=dt(1),
        available_at=record.available_at,
    )


def test_mixed_typed_and_untyped_interval_payload_fails_closed() -> None:
    typed = control_relation()
    with pytest.raises(ReconstructionValidationError, match="only typed"):
        reconstruct_interval_records(
            query(),
            deltas=(
                _record_candidate(typed),
                delta(
                    "malformed",
                    "UNVALIDATED_PAYLOAD",
                    effective_at=dt(1),
                    announced_at=dt(1),
                    available_at=dt(2),
                ),
            ),
        )


def test_record_supersedes_chain_must_be_append_only() -> None:
    target = _control_version("govrec:control-target", available_at=dt(4))
    impossible = _control_version(
        "govrec:control-impossible",
        available_at=dt(3),
        supersedes_record_id=target.record_id,
    )
    with pytest.raises(ReducerIntegrityError, match="not append-only"):
        reconstruct_interval_records(
            query(),
            deltas=tuple(_record_candidate(item) for item in (target, impossible)),
            coverage=(CoverageState(QUESTION_CONTROL, available_at=dt(2)),),
        )


def test_record_supersedes_chain_rejects_parallel_heads() -> None:
    target = _control_version("govrec:control-root", available_at=dt(2))
    left = _control_version(
        "govrec:control-left",
        available_at=dt(3),
        supersedes_record_id=target.record_id,
    )
    right = _control_version(
        "govrec:control-right",
        available_at=dt(4),
        supersedes_record_id=target.record_id,
    )
    with pytest.raises(ReducerIntegrityError, match="parallel heads"):
        reconstruct_interval_records(
            query(),
            deltas=tuple(
                _record_candidate(item) for item in (target, left, right)
            ),
            coverage=(CoverageState(QUESTION_CONTROL, available_at=dt(2)),),
        )


def timeline_utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def timeline_tenure(
    suffix: str,
    person: str,
    role: str,
    valid_from: datetime,
    available_at: datetime,
    *,
    acting: bool = False,
) -> RoleTenure:
    return RoleTenure(
        record_id=f"govrec:timeline-{suffix}",
        company_id=COMPANY,
        question_id=QUESTION_ROLE,
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        available_at=available_at,
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        person_id=f"govp:{COMPANY}:{person}",
        role_code=role,
        original_role_text=role,
        role_scope="company",
        acting=acting,
        valid_from=valid_from,
        appointment_event_id=None,
        appointment_evidence_span_id=SPAN,
    )


def synthetic_governance_timeline() -> tuple[
    AnchorCandidate[tuple[RoleTenure, ...]], tuple[DeltaCandidate[RoleDelta], ...]
]:
    anchor_available = timeline_utc(2023, 4, 20, 16)
    alice = timeline_tenure(
        "alice-director",
        "alice",
        "director",
        timeline_utc(2020, 6, 1),
        anchor_available,
    )
    roster_anchor = AnchorCandidate(
        stable_id="anchor:2022-complete-roster",
        payload=(alice,),
        reference_at="2022-12-31",
        available_at="2023-04-20",
        complete=True,
    )

    appointment_at = timeline_utc(2023, 6, 1)
    appointment_available = timeline_utc(2023, 6, 1, 16)
    bob = timeline_tenure(
        "bob-director",
        "bob",
        "director",
        appointment_at,
        appointment_available,
    )
    carol = timeline_tenure(
        "carol-acting-cfo",
        "carol",
        "chief_financial_officer",
        appointment_at,
        appointment_available,
        acting=True,
    )
    appoint_bob = RoleDelta(
        delta_id="delta:appoint-bob",
        action=RoleAction.APPOINT,
        effective_at=appointment_at,
        announced_at=appointment_at,
        available_at=appointment_available,
        person_id=bob.person_id,
        role_codes=(bob.role_code,),
        evidence_span_id=SPAN,
        event_id="govevent:appoint-bob",
        new_tenures=(bob,),
    )
    acting_carol = RoleDelta(
        delta_id="delta:acting-carol-original",
        action=RoleAction.ACTING,
        effective_at=appointment_at,
        announced_at=appointment_at,
        available_at=appointment_available,
        person_id=carol.person_id,
        role_codes=(carol.role_code,),
        evidence_span_id=SPAN,
        event_id="govevent:acting-carol-original",
        new_tenures=(carol,),
    )

    resignation_at = timeline_utc(2023, 8, 1)
    ambiguous_resignation = RoleDelta(
        delta_id="delta:ambiguous-resign-alice",
        action=RoleAction.RESIGN,
        effective_at=resignation_at,
        announced_at=resignation_at,
        available_at=timeline_utc(2023, 8, 1, 16),
        person_id=alice.person_id,
        role_codes=(),
        evidence_span_id=SPAN,
        event_id="govevent:ambiguous-resign-alice",
        scope_is_explicit=False,
    )

    correction_at = timeline_utc(2024, 2, 1)
    correction_available = timeline_utc(2024, 2, 1, 15)
    dana = timeline_tenure(
        "dana-acting-cfo",
        "dana",
        "chief_financial_officer",
        appointment_at,
        correction_available,
        acting=True,
    )
    corrected_acting = RoleDelta(
        delta_id="delta:acting-dana-correction",
        action=RoleAction.CORRECT,
        effective_at=appointment_at,
        announced_at=correction_at,
        available_at=correction_available,
        person_id=dana.person_id,
        role_codes=(dana.role_code,),
        evidence_span_id=SPAN,
        event_id="govevent:acting-dana-correction",
        new_tenures=(dana,),
        supersedes_delta_id=acting_carol.delta_id,
    )

    def candidate(
        value: RoleDelta,
        *,
        supersedes_id: str | None = None,
        announced_at: object | None = None,
        available_at: object | None = None,
    ) -> DeltaCandidate[RoleDelta]:
        return DeltaCandidate(
            stable_id=value.delta_id,
            payload=value,
            effective_at=value.effective_at,
            announced_at=value.announced_at if announced_at is None else announced_at,
            available_at=value.available_at if available_at is None else available_at,
            supersedes_id=supersedes_id,
        )

    # Intentionally not chronological: stable reconstruction must ignore input order.
    return roster_anchor, (
        candidate(ambiguous_resignation),
        candidate(
            corrected_acting,
            supersedes_id=acting_carol.delta_id,
        ),
        candidate(appoint_bob),
        candidate(acting_carol),
    )


def load_timeline_fixture() -> dict[str, object]:
    with TIMELINE_FIXTURE.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def timeline_result(
    result_name: str, *, reverse_inputs: bool = False
):
    fixture = load_timeline_fixture()
    query_payload = fixture["queries"][result_name]
    assert isinstance(query_payload, dict)
    current_query = normalize_governance_query(
        query_payload["state_at"],
        query_payload["known_at"],
        query_payload["perspective"],
    )
    roster_anchor, deltas = synthetic_governance_timeline()
    anchors = () if result_name == "no_anchor_incomplete" else (roster_anchor,)
    if reverse_inputs:
        anchors = tuple(reversed(anchors))
        deltas = tuple(reversed(deltas))
    return reconstruct_role_tenures(
        current_query,
        anchors=anchors,
        deltas=deltas,
    )


def project_timeline_result(result: object) -> dict[str, object]:
    state = result.state
    return {
        "query": {
            "state_cutoff": result.query.state_cutoff.isoformat().replace("+00:00", "Z"),
            "known_cutoff": result.query.known_cutoff.isoformat().replace("+00:00", "Z"),
            "perspective": result.query.perspective.value,
            "future_knowledge_used": result.future_knowledge_used,
        },
        "anchor_id": result.anchor_id,
        "ordered_delta_ids": list(result.ordered_delta_ids),
        "applied_delta_ids": list(result.applied_delta_ids),
        "excluded": {
            item.delta_id: item.reason
            for item in result.delta_outcomes
            if item.disposition.value == "excluded"
        },
        "tenures": [
            {
                "record_id": item.record_id,
                "person_id": item.person_id,
                "role_code": item.role_code,
                "acting": item.acting,
                "valid_from": item.valid_from.isoformat().replace("+00:00", "Z"),
                "valid_to": (
                    item.valid_to.isoformat().replace("+00:00", "Z")
                    if item.valid_to is not None
                    else None
                ),
            }
            for item in state.tenures
        ],
        "issue_codes": sorted(item.issue_code for item in result.issues),
        "completeness_status": result.completeness_status.value,
    }


def test_synthetic_timeline_dates_are_frozen() -> None:
    fixture = load_timeline_fixture()
    assert fixture["timeline"] == {
        "complete_roster_reference_at": "2022-12-31",
        "complete_roster_available_at": "2023-04-20",
        "appointment_and_acting_effective_at": "2023-06-01",
        "ambiguous_resignation_effective_at": "2023-08-01",
        "retrospective_correction_available_at": "2024-02-01",
    }


def test_complete_anchor_uses_conservative_publication_boundary() -> None:
    roster_anchor, _ = synthetic_governance_timeline()
    intraday = normalize_governance_query("2023-04-20T15:59:59Z")
    date_cutoff = normalize_governance_query("2023-04-20")
    assert select_anchor((roster_anchor,), intraday).selected is None
    assert select_anchor((roster_anchor,), date_cutoff).selected == roster_anchor


def test_pre_correction_strict_matches_frozen_golden() -> None:
    fixture = load_timeline_fixture()
    result = timeline_result("pre_correction_strict")
    assert project_timeline_result(result) == fixture["golden_results"][
        "pre_correction_strict"
    ]


def test_pre_correction_strict_quarantines_later_correction() -> None:
    result = timeline_result("pre_correction_strict")
    reasons = {item.delta_id: item.reason for item in result.delta_outcomes}
    assert reasons["delta:acting-dana-correction"] == "available_after_known_at"
    assert all("dana" not in item.person_id for item in result.state.tenures)


def test_pre_correction_applies_appointment_and_acting() -> None:
    result = timeline_result("pre_correction_strict")
    assert result.applied_delta_ids == (
        "delta:acting-carol-original",
        "delta:appoint-bob",
    )
    assert {item.acting for item in result.state.tenures} == {False, True}


def test_ambiguous_resignation_never_closes_anchor_tenure() -> None:
    result = timeline_result("pre_correction_strict")
    alice = next(item for item in result.state.tenures if item.person_id.endswith(":alice"))
    assert alice.valid_to is None
    assert "ambiguous_role_termination" in {item.issue_code for item in result.issues}
    assert "delta:ambiguous-resign-alice" in result.excluded_delta_ids


def test_post_correction_strict_matches_frozen_golden() -> None:
    fixture = load_timeline_fixture()
    result = timeline_result("post_correction_strict")
    assert project_timeline_result(result) == fixture["golden_results"][
        "post_correction_strict"
    ]


def test_visible_correction_replaces_original_version() -> None:
    result = timeline_result("post_correction_strict")
    reasons = {item.delta_id: item.reason for item in result.delta_outcomes}
    assert reasons["delta:acting-carol-original"] == (
        "superseded_by_visible_version:delta:acting-dana-correction"
    )
    people = {item.person_id.rsplit(":", 1)[-1] for item in result.state.tenures}
    assert "dana" in people
    assert "carol" not in people


def test_same_state_reconstructed_matches_frozen_golden() -> None:
    fixture = load_timeline_fixture()
    result = timeline_result("same_state_reconstructed")
    assert project_timeline_result(result) == fixture["golden_results"][
        "same_state_reconstructed"
    ]


def test_reconstructed_is_explicit_and_marks_future_knowledge() -> None:
    strict = timeline_result("pre_correction_strict")
    reconstructed = timeline_result("same_state_reconstructed")
    assert strict.query.state_cutoff == reconstructed.query.state_cutoff
    assert strict.future_knowledge_used is False
    assert reconstructed.future_knowledge_used is True
    assert project_timeline_result(strict)["tenures"] != project_timeline_result(
        reconstructed
    )["tenures"]


def test_no_anchor_returns_local_facts_and_frozen_incomplete_golden() -> None:
    fixture = load_timeline_fixture()
    result = timeline_result("no_anchor_incomplete")
    assert project_timeline_result(result) == fixture["golden_results"][
        "no_anchor_incomplete"
    ]
    assert result.anchor_id is None
    assert result.state.tenures
    assert result.completeness_status == CompletenessStatus.INCOMPLETE


def test_timeline_reconstruction_is_input_order_independent() -> None:
    fixture = load_timeline_fixture()
    for result_name in fixture["golden_results"]:
        baseline = project_timeline_result(timeline_result(result_name))
        reversed_result = project_timeline_result(
            timeline_result(result_name, reverse_inputs=True)
        )
        assert reversed_result == baseline == fixture["golden_results"][result_name]


def test_future_duplicate_stable_id_fails_closed_before_reducer() -> None:
    visible = delta(
        "duplicate", "visible", announced_at=dt(2), available_at=dt(2)
    )
    future = delta("duplicate", "FUTURE_SECRET", available_at=dt(8))
    with pytest.raises(VersionGraphError, match="before availability filtering"):
        reconstruct_state(
            query(),
            deltas=(visible, future),
            reducer=passthrough,
            anchor_required=False,
        )


def test_payload_and_wrapper_supersedes_metadata_are_resolved_once() -> None:
    result = timeline_result("post_correction_strict")
    assert "delta:acting-dana-correction" in result.applied_delta_ids
    assert "delta:acting-carol-original" in result.excluded_delta_ids


def test_wrapper_and_typed_delta_ids_must_match() -> None:
    payload = numeric_delta("payload")
    mismatched = DeltaCandidate(
        stable_id="delta:wrapper",
        payload=payload,
        effective_at=payload.effective_at,
        announced_at=payload.announced_at,
        available_at=payload.available_at,
    )
    with pytest.raises(VersionGraphError, match="disagrees with payload ID"):
        reconstruct_numeric_balance(
            query(),
            anchors=(anchor("numeric-anchor", Decimal("10")),),
            deltas=(mismatched,),
        )


@pytest.mark.parametrize(
    ("field_name", "wrapper_value"),
    (
        ("effective_at", dt(2)),
        ("announced_at", dt(2)),
        ("available_at", dt(4)),
    ),
)
def test_typed_delta_wrapper_times_must_match_payload(
    field_name: str, wrapper_value: datetime
) -> None:
    payload = numeric_delta(f"time-mismatch-{field_name}")
    wrapper = {
        "effective_at": payload.effective_at,
        "announced_at": payload.announced_at,
        "available_at": payload.available_at,
    }
    wrapper[field_name] = wrapper_value
    candidate = DeltaCandidate(
        stable_id=payload.delta_id,
        payload=payload,
        **wrapper,
    )
    with pytest.raises(VersionGraphError, match=field_name):
        reconstruct_numeric_balance(
            query(),
            anchors=(anchor("numeric-anchor", Decimal("10")),),
            deltas=(candidate,),
        )


def test_delta_announcement_cannot_follow_availability() -> None:
    with pytest.raises(ReconstructionValidationError, match="announced_at"):
        delta(
            "announcement-after-availability",
            effective_at=dt(2),
            announced_at=dt(4),
            available_at=dt(3),
        )


def test_numeric_payload_only_supersedes_is_resolved_before_reduction() -> None:
    old = NumericBalanceDelta(
        delta_id="delta:numeric-old",
        kind=NumericDeltaKind.INCREASE,
        effective_at=dt(3),
        announced_at=dt(3),
        available_at=dt(3),
        quantity=Decimal("2"),
        unit="share",
        calculation_basis="total shares",
    )
    corrected = NumericBalanceDelta(
        delta_id="delta:numeric-corrected",
        kind=NumericDeltaKind.INCREASE,
        effective_at=dt(3),
        announced_at=dt(4),
        available_at=dt(4),
        quantity=Decimal("3"),
        unit="share",
        calculation_basis="total shares",
        supersedes_delta_id=old.delta_id,
    )
    result = reconstruct_numeric_balance(
        query(),
        anchors=(anchor("numeric-anchor", Decimal("10")),),
        deltas=(numeric_candidate(old), numeric_candidate(corrected)),
    )
    assert result.state.balance == Decimal("13")
    assert result.applied_delta_ids == (corrected.delta_id,)
    assert old.delta_id in result.excluded_delta_ids


def test_A01_state_at_only_is_byte_equivalent_to_explicit_strict() -> None:
    implicit = normalize_governance_query("2023-12-31")
    explicit = normalize_governance_query(
        "2023-12-31", "2023-12-31", GovernancePerspective.STRICT
    )
    assert canonical_json_bytes(implicit) == canonical_json_bytes(explicit)


def test_A02_hindsight_requires_explicit_reconstructed_perspective() -> None:
    with pytest.raises(ReconstructionValidationError, match="explicit reconstructed"):
        normalize_governance_query("2023-12-31", "2024-02-01")
    explicit = normalize_governance_query(
        "2023-12-31", "2024-02-01", GovernancePerspective.RECONSTRUCTED
    )
    assert explicit.future_knowledge_used is True


def test_A03_known_at_before_state_at_is_invalid() -> None:
    with pytest.raises(ReconstructionValidationError, match="must not precede"):
        normalize_governance_query(
            "2024-02-01", "2023-12-31", GovernancePerspective.RECONSTRUCTED
        )


def test_A04_late_available_appointment_requires_later_known_at() -> None:
    effective_at = timeline_utc(2023, 6, 1)
    available_at = timeline_utc(2023, 7, 1)
    late_tenure = timeline_tenure(
        "late-director",
        "late-person",
        "director",
        effective_at,
        available_at,
    )
    late_delta = RoleDelta(
        delta_id="delta:late-director",
        action=RoleAction.APPOINT,
        effective_at=effective_at,
        announced_at=available_at,
        available_at=available_at,
        person_id=late_tenure.person_id,
        role_codes=(late_tenure.role_code,),
        evidence_span_id=SPAN,
        event_id="govevent:late-director",
        new_tenures=(late_tenure,),
    )
    wrapped = role_candidate(late_delta)
    coverage = (CoverageState(QUESTION_ROLE, available_at=effective_at),)
    strict = reconstruct_role_tenures(
        normalize_governance_query(effective_at),
        deltas=(wrapped,),
        coverage=coverage,
    )
    reconstructed = reconstruct_role_tenures(
        normalize_governance_query(
            effective_at, available_at, GovernancePerspective.RECONSTRUCTED
        ),
        deltas=(wrapped,),
        coverage=coverage,
    )
    assert strict.state.tenures == ()
    assert tuple(item.record_id for item in reconstructed.state.tenures) == (
        late_tenure.record_id,
    )


def test_A05_late_correction_changes_only_later_reconstruction() -> None:
    before = project_timeline_result(timeline_result("pre_correction_strict"))
    before_hash = canonical_sha256(
        before, schema_name="governance-timeline-golden", schema_version="1"
    )
    after = project_timeline_result(timeline_result("post_correction_strict"))
    hindsight = project_timeline_result(timeline_result("same_state_reconstructed"))
    before_again = project_timeline_result(timeline_result("pre_correction_strict"))
    assert any("carol" in item["person_id"] for item in before["tenures"])
    assert any("dana" in item["person_id"] for item in after["tenures"])
    assert hindsight["query"]["future_knowledge_used"] is True
    assert canonical_sha256(
        before_again, schema_name="governance-timeline-golden", schema_version="1"
    ) == before_hash


def test_A06_china_date_precision_never_admits_next_midnight() -> None:
    dated = VersionedItem("dated", "available-on-D", "2023-03-10")
    exact_next_midnight = VersionedItem(
        "next-midnight", "future", "2023-03-11T00:00:00+08:00"
    )
    intraday = resolve_visible_versions(
        (dated, exact_next_midnight), query("2023-03-10T15:00:00+08:00")
    )
    end_of_date = resolve_visible_versions(
        (dated, exact_next_midnight), query("2023-03-10")
    )
    assert intraday.selected_ids == ()
    assert end_of_date.selected_ids == ("dated",)


def test_A06_typed_record_cannot_relabel_exact_next_midnight_as_prior_date() -> None:
    exact_next_midnight = datetime(2023, 3, 11, tzinfo=SHANGHAI)
    payload = control_relation().model_dump(mode="python")
    payload.update(
        record_id="govrec:exact-next-midnight",
        available_at=exact_next_midnight,
        available_time_precision=TimePrecision.DATETIME,
    )
    record = ControlRelation.model_validate(payload, strict=True)
    disguised = DeltaCandidate(
        stable_id=record.record_id,
        payload=record,
        effective_at=record.effective_at,
        announced_at=dt(2),
        available_at="2023-03-10",
    )
    with pytest.raises(VersionGraphError, match="available_at"):
        reconstruct_interval_records(
            normalize_governance_query("2023-03-10"),
            deltas=(disguised,),
            coverage=(CoverageState(QUESTION_CONTROL),),
        )


def test_A06_date_precision_typed_record_is_visible_only_at_date_boundary() -> None:
    boundary = datetime(2023, 3, 11, tzinfo=SHANGHAI)
    payload = control_relation().model_dump(mode="python")
    payload.update(
        record_id="govrec:date-precision",
        available_at=boundary,
        available_time_precision=TimePrecision.DATE,
    )
    record = ControlRelation.model_validate_with_hash(payload)
    wrapped = DeltaCandidate(
        stable_id=record.record_id,
        payload=record,
        effective_at=record.effective_at,
        announced_at=dt(2),
        available_at="2023-03-10",
    )
    intraday = reconstruct_interval_records(
        normalize_governance_query("2023-03-10T12:00:00+08:00"),
        deltas=(wrapped,),
        coverage=(CoverageState(QUESTION_CONTROL),),
    )
    end_of_date = reconstruct_interval_records(
        normalize_governance_query("2023-03-10"),
        deltas=(wrapped,),
        coverage=(CoverageState(QUESTION_CONTROL),),
    )
    assert intraday.state.records == ()
    assert end_of_date.state.records == (record,)

    # The typed reducer remains fail-closed when called without the outer
    # DeltaCandidate visibility gate.
    direct = project_versioned_records(
        (record,),
        state_at=boundary,
        known_at=boundary,
        known_at_precision=TimePrecision.DATE,
    )
    assert direct.records == (record,)

    exact_payload = record.model_dump(mode="python")
    exact_payload["available_time_precision"] = TimePrecision.DATETIME
    exact_record = ControlRelation.model_validate_with_hash(exact_payload)
    direct_exact = project_versioned_records(
        (exact_record,),
        state_at=boundary,
        known_at=boundary,
        known_at_precision=TimePrecision.DATE,
    )
    assert direct_exact.records == ()
    assert exact_record.canonical_hash != record.canonical_hash


def test_A07_anchor_appointments_acting_resignation_and_correction_are_golden() -> None:
    fixture = load_timeline_fixture()
    for result_name in (
        "pre_correction_strict",
        "post_correction_strict",
        "same_state_reconstructed",
    ):
        result = timeline_result(result_name)
        assert project_timeline_result(result) == fixture["golden_results"][result_name]
    assert "ambiguous_role_termination" in {
        item.issue_code
        for item in timeline_result("post_correction_strict").issues
    }


def test_A08_no_anchor_or_coverage_gap_returns_partial_incomplete_state() -> None:
    roster_anchor, deltas = synthetic_governance_timeline()
    del roster_anchor
    gap = gap_record(
        "no-complete-roster",
        question_id=QUESTION_ROLE,
        available_at=timeline_utc(2023, 8, 2),
    )
    result = reconstruct_role_tenures(
        normalize_governance_query("2023-12-31"),
        deltas=deltas,
        coverage=(
            CoverageState(
                QUESTION_ROLE,
                completeness_status=CompletenessStatus.INCOMPLETE,
                gap_ids=(gap.gap_id,),
                available_at="2023-12-31",
            ),
        ),
        gaps=(gap,),
    )
    assert result.state.tenures
    assert result.gaps == (gap,)
    assert result.completeness_status == CompletenessStatus.INCOMPLETE


def test_A09_open_tenure_multi_role_and_single_role_overlap_are_distinct() -> None:
    first_vp = tenure("vp-one", "vp-one", "vice_general_manager", dt(1))
    second_vp = tenure("vp-two", "vp-two", "vice_general_manager", dt(1))
    multi = reconstruct_role_tenures(
        query(),
        anchors=(anchor("multi-roster", (first_vp, second_vp), reference_at=dt(2)),),
        coverage=(CoverageState(QUESTION_ROLE),),
    )
    assert len(multi.state.tenures) == 2
    current = project_current_roles(
        multi.state.tenures,
        state_at=query().state_cutoff,
        coverage_complete=False,
    )
    assert all(item.active_at_state for item in current)
    assert not any(item.confirmed_current for item in current)

    old_manager = tenure("old-manager", "old-manager", "general_manager", dt(1))
    new_manager = tenure("new-manager", "new-manager", "general_manager", dt(3))
    appoint_new = role_delta(
        "appoint-new-manager",
        RoleAction.APPOINT,
        "new-manager",
        ("general_manager",),
        dt(3),
        new_tenures=(new_manager,),
    )
    overlap = reconstruct_role_tenures(
        query(),
        anchors=(anchor("manager-roster", (old_manager,), reference_at=dt(2)),),
        deltas=(role_candidate(appoint_new),),
        coverage=(CoverageState(QUESTION_ROLE),),
    )
    assert overlap.completeness_status == CompletenessStatus.CONFLICTED
    assert "single_occupancy_overlap" in {
        item.issue_code for item in overlap.issues
    }


def test_A10_typed_ownership_and_pledge_update_only_with_complete_numbers() -> None:
    position = OwnershipPosition(
        position_id="govposition:A10-holder",
        company_id=COMPANY,
        holder_entity_id="entity:A10-holder",
        holder_name="股东甲",
        share_count=Decimal("100"),
        share_class="A",
        capital_basis="issued-a-shares",
        completeness_status=CompletenessStatus.COMPLETE,
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        canonical_hash=H,
    )
    ownership_anchor = OwnershipSnapshot(
        record_id="govrec:A10-ownership-anchor",
        company_id=COMPANY,
        question_id="GOV.Q01.OWNERSHIP_CONTROL",
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        available_at=dt(2),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        positions=(position,),
        total_share_basis=Decimal("1000"),
        capital_basis="issued-a-shares",
        is_complete=True,
        source_manifest_id="manifest:A10",
    )
    ownership_deltas = (
        OwnershipPositionDelta(
            "delta:A10-increase",
            "entity:A10-holder",
            "A",
            NumericDeltaKind.INCREASE,
            dt(3),
            dt(3),
            dt(3),
            Decimal("20"),
            "shares",
            "issued-a-shares",
            "govevent:A10-increase",
        ),
        OwnershipPositionDelta(
            "delta:A10-decrease",
            "entity:A10-holder",
            "A",
            NumericDeltaKind.DECREASE,
            dt(4),
            dt(4),
            dt(4),
            Decimal("5"),
            "shares",
            "issued-a-shares",
            "govevent:A10-decrease",
        ),
    )
    ownership = reconstruct_governance_state(
        "ownership",
        query(),
        anchors=(anchor("A10-ownership", ownership_anchor),),
        deltas=tuple(
            DeltaCandidate(
                item.delta_id,
                item,
                item.effective_at,
                item.announced_at,
                item.available_at,
            )
            for item in reversed(ownership_deltas)
        ),
        coverage=(CoverageState("GOV.Q01.OWNERSHIP_CONTROL"),),
    )
    assert ownership.state.positions[0].balance == Decimal("115")

    pledge_anchor = PledgePositionSnapshot(
        record_id="govrec:A10-pledge-anchor",
        company_id=COMPANY,
        question_id="GOV.Q02.PLEDGE_FREEZE",
        claim_ids=(CLAIM,),
        evidence_span_ids=(SPAN,),
        reference_at=dt(1),
        available_at=dt(2),
        completeness_status=CompletenessStatus.COMPLETE,
        canonical_hash=H,
        pledgor_entity_id="entity:A10-holder",
        pledgee_entity_id="entity:A10-bank",
        pledged_shares=Decimal("30"),
        ratio_basis=Decimal("1000"),
        capital_basis="issued-a-shares",
        is_complete=True,
    )
    pledge_deltas = (
        PledgePositionDelta(
            "delta:A10-pledge",
            "entity:A10-holder",
            "entity:A10-bank",
            NumericDeltaKind.PLEDGE,
            dt(3),
            dt(3),
            dt(3),
            Decimal("10"),
            "shares",
            "issued-a-shares",
            "govevent:A10-pledge",
        ),
        PledgePositionDelta(
            "delta:A10-release-unknown",
            "entity:A10-holder",
            "entity:A10-bank",
            NumericDeltaKind.RELEASE,
            dt(4),
            dt(4),
            dt(4),
            None,
            "shares",
            "issued-a-shares",
            "govevent:A10-release-unknown",
        ),
    )
    pledge = reconstruct_governance_state(
        "pledge",
        query(),
        anchors=(anchor("A10-pledge", (pledge_anchor,)),),
        deltas=tuple(
            DeltaCandidate(
                item.delta_id,
                item,
                item.effective_at,
                item.announced_at,
                item.available_at,
            )
            for item in pledge_deltas
        ),
        coverage=(CoverageState("GOV.Q02.PLEDGE_FREEZE"),),
    )
    assert pledge.state.positions[0].balance is None
    assert pledge.state.retained_event_ids == (
        "govevent:A10-pledge",
        "govevent:A10-release-unknown",
    )
    assert pledge.completeness_status == CompletenessStatus.INCOMPLETE


def same_name_link_fixture():
    resolver = CompanyPersonResolver()
    available_at = timeline_utc(2023, 4, 20)
    first = resolver.resolve(
        company_id="cn-600519",
        canonical_name="张三",
        source_claim_ids=("govclaim:A11-left",),
        available_at=available_at,
        stable_evidence_key="roster-row-1",
    )
    second = resolver.resolve(
        company_id="cn-300750",
        canonical_name="张三",
        source_claim_ids=("govclaim:A11-right",),
        available_at=available_at,
        stable_evidence_key="roster-row-1",
    )
    candidate = build_person_link_candidate(
        company_ids=(first.company_id, second.company_id),
        person_ids=(first.person_id, second.person_id),
        basis="same name is only a candidate",
        producer="deterministic-test",
        evidence_span_ids=("govspan:A11-left", "govspan:A11-right"),
        available_at=available_at,
    )
    return first, second, candidate


def test_A11_same_name_people_remain_company_local_candidates() -> None:
    first, second, candidate = same_name_link_fixture()
    assert first.person_id != second.person_id
    assert first.person_id.startswith(f"govp:{first.company_id}:")
    assert second.person_id.startswith(f"govp:{second.company_id}:")
    assert set(candidate.person_ids) == {first.person_id, second.person_id}
    assert approved_link_groups((), known_at=timeline_utc(2023, 12, 31)) == ()


def test_A12_only_visible_approved_link_decision_affects_aggregation() -> None:
    first, second, candidate = same_name_link_fixture()
    approved_at = timeline_utc(2023, 6, 1)
    rejected_at = timeline_utc(2024, 2, 1)
    approved = build_person_link_decision(
        candidate=candidate,
        decision=DecisionValue.APPROVED,
        decision_source="manual-maintenance",
        producer="reviewer-1",
        rationale="official biographies establish the same person",
        decided_at=approved_at,
        available_at=approved_at,
    )
    rejected = build_person_link_decision(
        candidate=candidate,
        decision=DecisionValue.REJECTED,
        decision_source="manual-maintenance",
        producer="reviewer-2",
        rationale="later official evidence distinguishes the two people",
        decided_at=rejected_at,
        available_at=rejected_at,
        supersedes_decision_id=approved.person_link_decision_id,
    )
    original_ids = (first.person_id, second.person_id)
    assert select_visible_link_decisions(
        (approved, rejected), known_at=approved_at, candidates=(candidate,)
    ) == (approved,)
    assert approved_link_groups(
        (approved, rejected), known_at=approved_at
    ) == (tuple(sorted(original_ids)),)
    assert select_visible_link_decisions(
        (approved, rejected), known_at=rejected_at, candidates=(candidate,)
    ) == ()
    assert approved_link_groups(
        (approved, rejected), known_at=rejected_at
    ) == ()
    assert (first.person_id, second.person_id) == original_ids
