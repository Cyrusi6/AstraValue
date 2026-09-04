from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from .canonical import SHANGHAI_TIMEZONE, canonical_sha256
from .models import (
    CompletenessStatus,
    ConflictRecord,
    ControlRelation,
    CorrectionRecord,
    DirectionKind,
    GovernanceRecord,
    OwnershipPosition,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RecordResolutionStatus,
    RoleTenure,
    RosterSnapshot,
    TimePrecision,
)


class ReducerIntegrityError(RuntimeError):
    pass


class RoleOccupancy(str, Enum):
    SINGLE = "single"
    MULTIPLE = "multiple"


class RoleAction(str, Enum):
    APPOINT = "appointment"
    RESIGN = "resignation"
    DISMISS = "dismissal"
    ACTING = "acting"
    ROTATE = "rotation"
    RENEW = "renewal"
    CORRECT = "correction"


@dataclass(frozen=True)
class RoleRule:
    role_code: str
    occupancy: RoleOccupancy


DEFAULT_ROLE_TAXONOMY: dict[str, RoleRule] = {
    "chairperson": RoleRule("chairperson", RoleOccupancy.SINGLE),
    "general_manager": RoleRule("general_manager", RoleOccupancy.SINGLE),
    "chief_financial_officer": RoleRule(
        "chief_financial_officer", RoleOccupancy.SINGLE
    ),
    "board_secretary": RoleRule("board_secretary", RoleOccupancy.SINGLE),
    "director": RoleRule("director", RoleOccupancy.MULTIPLE),
    "independent_director": RoleRule("independent_director", RoleOccupancy.MULTIPLE),
    "supervisor": RoleRule("supervisor", RoleOccupancy.MULTIPLE),
    "vice_general_manager": RoleRule(
        "vice_general_manager", RoleOccupancy.MULTIPLE
    ),
    "committee_member": RoleRule("committee_member", RoleOccupancy.MULTIPLE),
}


@dataclass(frozen=True)
class RoleDelta:
    delta_id: str
    action: RoleAction
    effective_at: datetime
    announced_at: datetime
    available_at: datetime
    person_id: str
    role_codes: tuple[str, ...]
    evidence_span_id: str
    event_id: str
    new_tenures: tuple[RoleTenure, ...] = ()
    explicitly_terminates_tenure_ids: tuple[str, ...] = ()
    scope_is_explicit: bool = True
    role_scope: str | None = None
    claim_ids: tuple[str, ...] = ()
    supersedes_delta_id: str | None = None
    eligible: bool = True


@dataclass(frozen=True)
class ReductionIssue:
    issue_code: str
    object_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class DeltaExclusion:
    delta_id: str
    reason_code: str


@dataclass(frozen=True)
class RoleReductionResult:
    tenures: tuple[RoleTenure, ...]
    applied_delta_ids: tuple[str, ...]
    excluded_delta_ids: tuple[str, ...]
    excluded_reasons: tuple[DeltaExclusion, ...]
    issues: tuple[ReductionIssue, ...]
    completeness_status: CompletenessStatus


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _precision_rank(value: TimePrecision | str, *, field_name: str) -> int:
    try:
        precision = TimePrecision(value)
    except (TypeError, ValueError) as exc:
        raise ReducerIntegrityError(
            f"{field_name} has an unsupported time precision"
        ) from exc
    return 0 if precision == TimePrecision.DATE else 1


def _available_at_or_before(
    value: datetime,
    value_precision: TimePrecision | str,
    cutoff: datetime,
    cutoff_precision: TimePrecision | str,
) -> bool:
    return (
        _aware_utc(value, field_name="record.available_at"),
        _precision_rank(value_precision, field_name="record.available_time_precision"),
    ) <= (
        cutoff,
        _precision_rank(cutoff_precision, field_name="known_at_precision"),
    )


def _derived_record_version(record: GovernanceRecord, **updates: Any) -> GovernanceRecord:
    """Create an append-only derived version without mutating/reusing the old ID."""

    payload = record.model_dump(mode="python")
    payload.update(updates)
    payload.pop("canonical_hash", None)
    previous_record_id = record.record_id
    payload["supersedes_record_id"] = previous_record_id
    id_payload = dict(payload)
    id_payload.pop("record_id", None)
    id_digest = canonical_sha256(
        id_payload,
        schema_name="governance-derived-record-id",
        schema_version="1",
    )
    payload["record_id"] = f"govrec:{id_digest}"
    payload["canonical_hash"] = canonical_sha256(
        payload,
        schema_name=record.schema_name,
        schema_version=str(payload.get("schema_version", "1.0.0")),
    )
    return type(record).model_validate(payload, strict=True)


def _closed_tenure(tenure: RoleTenure, delta: RoleDelta) -> RoleTenure:
    return _derived_record_version(
        tenure,
        valid_to=delta.effective_at,
        termination_event_id=delta.event_id,
        termination_evidence_span_id=delta.evidence_span_id,
        claim_ids=tuple(sorted({*tenure.claim_ids, *delta.claim_ids})),
        evidence_span_ids=tuple(
            sorted({*tenure.evidence_span_ids, delta.evidence_span_id})
        ),
        source_event_ids=tuple(sorted({*tenure.source_event_ids, delta.event_id})),
    )


def _resolve_role_delta_versions(
    deltas: Iterable[RoleDelta],
    *,
    state_at: datetime,
    known_at: datetime,
) -> tuple[tuple[RoleDelta, ...], tuple[DeltaExclusion, ...]]:
    by_id: dict[str, RoleDelta] = {}
    visible: list[RoleDelta] = []
    exclusions: list[DeltaExclusion] = []
    for delta in deltas:
        available_at = _aware_utc(
            delta.available_at, field_name="delta.available_at"
        )
        if available_at > known_at:
            exclusions.append(DeltaExclusion(delta.delta_id, "available_after_known_at"))
            continue
        # Only visible deltas may have their business content or version links
        # inspected. This is the reducer-level counterpart of the future firewall.
        _aware_utc(delta.effective_at, field_name="delta.effective_at")
        _aware_utc(delta.announced_at, field_name="delta.announced_at")
        if delta.available_at < delta.announced_at:
            raise ReducerIntegrityError("role delta is available before announcement")
        existing = by_id.get(delta.delta_id)
        if existing is not None and existing != delta:
            raise ReducerIntegrityError("one role delta ID has multiple payloads")
        by_id[delta.delta_id] = delta
        if delta.effective_at > state_at:
            exclusions.append(DeltaExclusion(delta.delta_id, "effective_after_state_at"))
        elif not delta.eligible:
            exclusions.append(DeltaExclusion(delta.delta_id, "not_canonical_eligible"))
        else:
            visible.append(delta)

    applicable_by_id = {item.delta_id: item for item in visible}
    superseded: set[str] = set()
    superseders: dict[str, str] = {}
    for item in visible:
        target_id = item.supersedes_delta_id
        if target_id is None:
            continue
        target = applicable_by_id.get(target_id)
        if target is None:
            raise ReducerIntegrityError("role delta has a dangling supersedes reference")
        if item.delta_id == target_id:
            raise ReducerIntegrityError("role delta cannot supersede itself")
        if item.available_at < target.available_at:
            raise ReducerIntegrityError("role delta supersedes chain is not append-only")
        if target_id in superseders:
            raise ReducerIntegrityError("role delta supersedes chain has parallel heads")
        superseders[target_id] = item.delta_id
        superseded.add(target_id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(delta_id: str) -> None:
        if delta_id in visiting:
            raise ReducerIntegrityError("role delta supersedes chain contains a cycle")
        if delta_id in visited:
            return
        visiting.add(delta_id)
        parent_id = applicable_by_id[delta_id].supersedes_delta_id
        if parent_id is not None:
            visit(parent_id)
        visiting.remove(delta_id)
        visited.add(delta_id)

    for delta_id in sorted(applicable_by_id):
        visit(delta_id)
    for delta_id in sorted(superseded):
        exclusions.append(DeltaExclusion(delta_id, "superseded_by_visible_delta"))
    heads = tuple(item for item in visible if item.delta_id not in superseded)
    return heads, tuple(exclusions)


def reduce_role_tenures(
    anchor_tenures: Iterable[RoleTenure],
    deltas: Iterable[RoleDelta],
    *,
    state_at: datetime,
    known_at: datetime,
    taxonomy: dict[str, RoleRule] | None = None,
    anchor: RosterSnapshot | None = None,
    anchor_complete: bool | None = None,
    coverage_complete: bool = True,
) -> RoleReductionResult:
    state_at = _aware_utc(state_at, field_name="state_at")
    known_at = _aware_utc(known_at, field_name="known_at")
    rules = taxonomy or DEFAULT_ROLE_TAXONOMY
    anchor_values = tuple(anchor_tenures)
    state: dict[str, RoleTenure] = {}
    roster_members_without_tenure: tuple[str, ...] = ()
    for item in anchor_values:
        if item.available_at > known_at or item.valid_from is None or item.valid_from > state_at:
            continue
        existing = state.get(item.record_id)
        if existing is not None and existing != item:
            raise ReducerIntegrityError("one tenure record ID has multiple payloads")
        state[item.record_id] = item
    if anchor is not None:
        if anchor.available_at > known_at or (
            anchor.reference_at is not None and anchor.reference_at > state_at
        ):
            anchor_is_complete = False
        else:
            anchor_is_complete = anchor.is_complete
            companies = {item.company_id for item in state.values()}
            if companies and companies != {anchor.company_id}:
                raise ReducerIntegrityError("roster anchor and tenures cross company namespace")
            if not set(item.person_id for item in state.values()).issubset(
                set(anchor.member_ids)
            ):
                raise ReducerIntegrityError("anchor tenures contain a person outside the roster")
            roster_members_without_tenure = tuple(
                sorted(
                    set(anchor.member_ids)
                    - {item.person_id for item in state.values()}
                )
            )
    elif anchor_complete is not None:
        anchor_is_complete = anchor_complete
    else:
        anchor_is_complete = bool(state)
    applied: list[str] = []
    issues: list[ReductionIssue] = []
    if roster_members_without_tenure:
        issues.append(
            ReductionIssue(
                "roster_member_without_tenure",
                roster_members_without_tenure,
                "完整名册成员缺少对应的类型化任期",
            )
        )
    if not anchor_is_complete:
        issues.append(
            ReductionIssue(
                "anchor_missing",
                tuple(sorted(state)),
                "没有可见的完整名册锚点；仅返回可证明的局部任期",
            )
        )
    ordered_heads, initial_exclusions = _resolve_role_delta_versions(
        deltas, state_at=state_at, known_at=known_at
    )
    exclusion_items: list[DeltaExclusion] = list(initial_exclusions)
    ordered = sorted(
        ordered_heads,
        key=lambda item: (
            item.effective_at,
            item.announced_at,
            item.delta_id,
        ),
    )
    for delta in ordered:
        if delta.action in {RoleAction.RESIGN, RoleAction.DISMISS}:
            if not delta.scope_is_explicit or not delta.role_codes:
                exclusion_items.append(
                    DeltaExclusion(delta.delta_id, "ambiguous_role_termination")
                )
                issues.append(
                    ReductionIssue(
                        "ambiguous_role_termination",
                        (delta.delta_id,),
                        "辞任或免职没有明确职务范围，未关闭任何任期",
                    )
                )
                continue
            matching = [
                tenure
                for tenure in state.values()
                if tenure.person_id == delta.person_id
                and tenure.role_code in delta.role_codes
                and tenure.valid_to is None
                and (delta.role_scope is None or tenure.role_scope == delta.role_scope)
            ]
            matching_scopes = {item.role_scope for item in matching}
            if delta.role_scope is None and len(matching_scopes) > 1:
                exclusion_items.append(
                    DeltaExclusion(delta.delta_id, "ambiguous_role_scope")
                )
                issues.append(
                    ReductionIssue(
                        "ambiguous_role_scope",
                        (delta.delta_id,),
                        "相同职务存在多个机构范围，终止证据未明确目标范围",
                    )
                )
                continue
            if not matching:
                exclusion_items.append(
                    DeltaExclusion(delta.delta_id, "termination_without_open_tenure")
                )
                issues.append(
                    ReductionIssue(
                        "termination_without_open_tenure",
                        (delta.delta_id,),
                        "没有找到可由该明确终止证据关闭的任期",
                    )
                )
                continue
            for tenure in matching:
                closed = _closed_tenure(tenure, delta)
                del state[tenure.record_id]
                state[closed.record_id] = closed
            applied.append(delta.delta_id)
            continue

        # Validate the entire appointment-like delta before changing any state.
        if not delta.new_tenures:
            exclusion_items.append(
                DeltaExclusion(delta.delta_id, "appointment_without_typed_tenure")
            )
            issues.append(
                ReductionIssue(
                    "appointment_without_typed_tenure",
                    (delta.delta_id,),
                    "任命增量没有合格的类型化任期",
                )
            )
            continue
        for tenure in delta.new_tenures:
            if tenure.person_id != delta.person_id or tenure.role_code not in delta.role_codes:
                raise ReducerIntegrityError("delta and typed tenure identities disagree")
            if tenure.valid_from != delta.effective_at:
                raise ReducerIntegrityError("typed tenure start differs from delta effective_at")
            if tenure.available_at > known_at or tenure.available_at > delta.available_at:
                raise ReducerIntegrityError("typed tenure is not visible with its delta")
            if tenure.appointment_event_id not in {None, delta.event_id}:
                raise ReducerIntegrityError("typed tenure event differs from its delta")
            if tenure.appointment_evidence_span_id != delta.evidence_span_id:
                raise ReducerIntegrityError("typed tenure evidence differs from its delta")
            if delta.action == RoleAction.ACTING and not tenure.acting:
                raise ReducerIntegrityError("acting delta requires an acting tenure")
            if delta.action in {
                RoleAction.APPOINT,
                RoleAction.ROTATE,
                RoleAction.RENEW,
            } and tenure.acting:
                raise ReducerIntegrityError("formal appointment/renewal cannot create acting tenure")
            if tenure.role_code not in rules:
                raise ReducerIntegrityError(
                    f"role taxonomy does not define {tenure.role_code}"
                )

        staged = dict(state)
        explicitly_closed: list[RoleTenure] = []
        for tenure_id in delta.explicitly_terminates_tenure_ids:
            tenure = staged.get(tenure_id)
            if tenure is None or tenure.valid_to is not None:
                raise ReducerIntegrityError(
                    f"explicit termination references unknown/closed tenure {tenure_id}"
                )
            matching_new = any(
                item.role_code == tenure.role_code
                and item.role_scope == tenure.role_scope
                for item in delta.new_tenures
            )
            if not matching_new:
                raise ReducerIntegrityError(
                    "explicitly terminated tenure differs in role or scope"
                )
            if delta.action == RoleAction.RENEW and tenure.person_id != delta.person_id:
                raise ReducerIntegrityError("renewal cannot close another person's tenure")
            closed = _closed_tenure(tenure, delta)
            del staged[tenure_id]
            staged[closed.record_id] = closed
            explicitly_closed.append(tenure)

        for tenure in delta.new_tenures:
            if delta.action in {
                RoleAction.APPOINT,
                RoleAction.ROTATE,
                RoleAction.RENEW,
            }:
                phase_matches = [
                    item
                    for item in staged.values()
                    if item.person_id == tenure.person_id
                    and item.role_code == tenure.role_code
                    and item.role_scope == tenure.role_scope
                    and item.valid_to is None
                    and (
                        item.acting
                        if delta.action == RoleAction.APPOINT
                        else delta.action == RoleAction.RENEW and not item.acting
                    )
                ]
                explicit_renewal_matches = [
                    item
                    for item in explicitly_closed
                    if item.person_id == tenure.person_id
                    and item.role_code == tenure.role_code
                    and item.role_scope == tenure.role_scope
                    and not item.acting
                ]
                if delta.action == RoleAction.RENEW and (
                    len(phase_matches) + len(explicit_renewal_matches) != 1
                ):
                    exclusion_items.append(
                        DeltaExclusion(delta.delta_id, "renewal_without_exact_predecessor")
                    )
                    issues.append(
                        ReductionIssue(
                            "renewal_without_exact_predecessor",
                            (delta.delta_id,),
                            "续任没有唯一的同人同职同范围前序任期",
                        )
                    )
                    staged = state
                    break
                for existing in phase_matches:
                    closed = _closed_tenure(existing, delta)
                    del staged[existing.record_id]
                    staged[closed.record_id] = closed

            rule = rules[tenure.role_code]
            same_identity_overlaps = [
                existing
                for existing in staged.values()
                if existing.person_id == tenure.person_id
                and existing.role_code == tenure.role_code
                and existing.role_scope == tenure.role_scope
                and existing.valid_from is not None
                and (existing.valid_to is None or existing.valid_to > tenure.valid_from)
            ]
            if same_identity_overlaps:
                issues.append(
                    ReductionIssue(
                        "same_role_phase_overlap",
                        tuple(
                            sorted(
                                [
                                    tenure.record_id,
                                    *(item.record_id for item in same_identity_overlaps),
                                ]
                            )
                        ),
                        "同一人员的同职同范围任期阶段发生无依据重叠",
                    )
                )
            if rule.occupancy == RoleOccupancy.SINGLE:
                overlaps = [
                    existing
                    for existing in staged.values()
                    if existing.role_code == tenure.role_code
                    and existing.role_scope == tenure.role_scope
                    and existing.valid_from is not None
                    and existing.valid_from <= state_at
                    and (existing.valid_to is None or existing.valid_to > tenure.valid_from)
                ]
                if overlaps:
                    issues.append(
                        ReductionIssue(
                            "single_occupancy_overlap",
                            tuple(sorted([tenure.record_id, *(item.record_id for item in overlaps)])),
                            "通常单一职务出现无明确终止证据的重叠，未凭常识关闭前任",
                        )
                    )
            staged[tenure.record_id] = tenure
        else:
            state = staged
            applied.append(delta.delta_id)

    sorted_tenures = tuple(sorted(state.values(), key=lambda item: item.record_id))
    status = (
        CompletenessStatus.CONFLICTED
        if any(
            item.issue_code in {"single_occupancy_overlap", "same_role_phase_overlap"}
            for item in issues
        )
        else CompletenessStatus.INCOMPLETE
        if issues or not anchor_is_complete or not coverage_complete
        else CompletenessStatus.COMPLETE
    )
    exclusion_items = sorted(
        {item.delta_id: item for item in exclusion_items}.values(),
        key=lambda item: item.delta_id,
    )
    return RoleReductionResult(
        tenures=sorted_tenures,
        applied_delta_ids=tuple(applied),
        excluded_delta_ids=tuple(item.delta_id for item in exclusion_items),
        excluded_reasons=tuple(exclusion_items),
        issues=tuple(issues),
        completeness_status=status,
    )


def reduce_role_state(
    anchor: RosterSnapshot | None,
    anchor_tenures: Iterable[RoleTenure],
    deltas: Iterable[RoleDelta],
    *,
    state_at: datetime,
    known_at: datetime,
    taxonomy: dict[str, RoleRule] | None = None,
    anchor_complete: bool | None = None,
    coverage_complete: bool = True,
) -> RoleReductionResult:
    """Roster-aware facade used by the bitemporal reconstruction layer."""

    return reduce_role_tenures(
        anchor_tenures,
        deltas,
        state_at=state_at,
        known_at=known_at,
        taxonomy=taxonomy,
        anchor=anchor,
        anchor_complete=anchor_complete,
        coverage_complete=coverage_complete,
    )


@dataclass(frozen=True)
class CurrentRoleProjection:
    tenure: RoleTenure
    active_at_state: bool
    confirmed_current: bool


def project_current_roles(
    tenures: Iterable[RoleTenure],
    *,
    state_at: datetime,
    coverage_complete: bool,
) -> tuple[CurrentRoleProjection, ...]:
    values: list[CurrentRoleProjection] = []
    for tenure in sorted(tenures, key=lambda item: item.record_id):
        active = bool(
            tenure.valid_from is not None
            and tenure.valid_from <= state_at
            and (tenure.valid_to is None or tenure.valid_to > state_at)
        )
        values.append(
            CurrentRoleProjection(
                tenure=tenure,
                active_at_state=active,
                confirmed_current=active and coverage_complete,
            )
        )
    return tuple(values)


class NumericDeltaKind(str, Enum):
    INCREASE = "increase"
    DECREASE = "decrease"
    PLEDGE = "pledge"
    RELEASE = "release"


@dataclass(frozen=True)
class NumericBalanceDelta:
    delta_id: str
    kind: NumericDeltaKind
    effective_at: datetime
    announced_at: datetime
    available_at: datetime
    quantity: Decimal | None
    unit: str | None
    calculation_basis: str | None
    event_id: str | None = None
    supersedes_delta_id: str | None = None


@dataclass(frozen=True)
class NumericBalanceResult:
    balance: Decimal | None
    applied_delta_ids: tuple[str, ...]
    retained_delta_ids: tuple[str, ...]
    retained_event_ids: tuple[str, ...]
    excluded_delta_ids: tuple[str, ...]
    excluded_reasons: tuple[DeltaExclusion, ...]
    issues: tuple[ReductionIssue, ...]
    completeness_status: CompletenessStatus


@dataclass(frozen=True)
class OwnershipPositionDelta:
    delta_id: str
    holder_entity_id: str
    share_class: str
    kind: NumericDeltaKind
    effective_at: datetime
    announced_at: datetime
    available_at: datetime
    quantity: Decimal | None
    unit: str | None
    capital_basis: str | None
    event_id: str
    supersedes_delta_id: str | None = None


@dataclass(frozen=True)
class PledgePositionDelta:
    delta_id: str
    pledgor_entity_id: str
    pledgee_entity_id: str | None
    kind: NumericDeltaKind
    effective_at: datetime
    announced_at: datetime
    available_at: datetime
    quantity: Decimal | None
    unit: str | None
    capital_basis: str | None
    event_id: str
    supersedes_delta_id: str | None = None


@dataclass(frozen=True)
class PositionBalanceState:
    subject_key: tuple[str, ...]
    balance: Decimal | None
    unit: str | None
    calculation_basis: str | None
    completeness_status: CompletenessStatus


@dataclass(frozen=True)
class TypedBalanceReductionResult:
    positions: tuple[PositionBalanceState, ...]
    applied_delta_ids: tuple[str, ...]
    retained_delta_ids: tuple[str, ...]
    retained_event_ids: tuple[str, ...]
    excluded_delta_ids: tuple[str, ...]
    issues: tuple[ReductionIssue, ...]
    completeness_status: CompletenessStatus


def reduce_numeric_balance(
    anchor_balance: Decimal | None,
    deltas: Iterable[NumericBalanceDelta],
    *,
    state_at: datetime,
    known_at: datetime,
    anchor_unit: str | None = None,
    anchor_calculation_basis: str | None = None,
) -> NumericBalanceResult:
    state_at = _aware_utc(state_at, field_name="state_at")
    known_at = _aware_utc(known_at, field_name="known_at")
    if anchor_balance is not None:
        if not isinstance(anchor_balance, Decimal):
            raise ReducerIntegrityError("numeric anchor must use Decimal")
        if anchor_balance < 0:
            raise ReducerIntegrityError("numeric anchor balance cannot be negative")
    balance = anchor_balance
    exact_known = anchor_balance is not None
    applied: list[str] = []
    retained: list[str] = []
    retained_delta_ids: list[str] = []
    exclusions: list[DeltaExclusion] = []
    issues: list[ReductionIssue] = []
    values = tuple(deltas)
    by_id: dict[str, NumericBalanceDelta] = {}
    applicable: list[NumericBalanceDelta] = []
    for delta in values:
        available_at = _aware_utc(
            delta.available_at, field_name="delta.available_at"
        )
        if available_at > known_at:
            exclusions.append(DeltaExclusion(delta.delta_id, "available_after_known_at"))
            continue
        _aware_utc(delta.effective_at, field_name="delta.effective_at")
        _aware_utc(delta.announced_at, field_name="delta.announced_at")
        if delta.available_at < delta.announced_at:
            raise ReducerIntegrityError("numeric delta is available before announcement")
        existing = by_id.get(delta.delta_id)
        if existing is not None and existing != delta:
            raise ReducerIntegrityError("one numeric delta ID has multiple payloads")
        by_id[delta.delta_id] = delta
        if delta.effective_at > state_at:
            exclusions.append(DeltaExclusion(delta.delta_id, "effective_after_state_at"))
        else:
            applicable.append(delta)

    applicable_by_id = {item.delta_id: item for item in applicable}
    superseded: set[str] = set()
    superseders: dict[str, str] = {}
    for delta in applicable:
        if delta.supersedes_delta_id is None:
            continue
        if delta.supersedes_delta_id not in applicable_by_id:
            raise ReducerIntegrityError("numeric delta has a dangling supersedes reference")
        if delta.supersedes_delta_id == delta.delta_id:
            raise ReducerIntegrityError("numeric delta cannot supersede itself")
        target = applicable_by_id[delta.supersedes_delta_id]
        if delta.available_at < target.available_at:
            raise ReducerIntegrityError(
                "numeric delta supersedes chain is not append-only"
            )
        if delta.supersedes_delta_id in superseders:
            raise ReducerIntegrityError("numeric delta supersedes chain has parallel heads")
        superseders[delta.supersedes_delta_id] = delta.delta_id
        superseded.add(delta.supersedes_delta_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_numeric(delta_id: str) -> None:
        if delta_id in visiting:
            raise ReducerIntegrityError("numeric delta supersedes chain contains a cycle")
        if delta_id in visited:
            return
        visiting.add(delta_id)
        parent_id = applicable_by_id[delta_id].supersedes_delta_id
        if parent_id is not None:
            visit_numeric(parent_id)
        visiting.remove(delta_id)
        visited.add(delta_id)

    for delta_id in sorted(applicable_by_id):
        visit_numeric(delta_id)
    for item_id in sorted(superseded):
        exclusions.append(DeltaExclusion(item_id, "superseded_by_visible_delta"))

    for delta in sorted(
        (item for item in applicable if item.delta_id not in superseded),
        key=lambda item: (item.effective_at, item.announced_at, item.delta_id),
    ):
        retained.append(delta.event_id or delta.delta_id)
        retained_delta_ids.append(delta.delta_id)
        if delta.quantity is not None and not isinstance(delta.quantity, Decimal):
            raise ReducerIntegrityError("numeric delta quantity must use Decimal")
        if delta.quantity is not None and delta.quantity < 0:
            raise ReducerIntegrityError("numeric delta quantity cannot be negative")
        complete_delta = (
            delta.quantity is not None
            and delta.unit is not None
            and delta.calculation_basis is not None
        )
        compatible = (
            (anchor_unit is None or delta.unit == anchor_unit)
            and (
                anchor_calculation_basis is None
                or delta.calculation_basis == anchor_calculation_basis
            )
        )
        if not exact_known:
            issues.append(
                ReductionIssue(
                    "numeric_state_already_unknown",
                    (delta.delta_id,),
                    "先前缺口已使精确余额未知；保留后续事件但不标记为精确应用",
                )
            )
            continue
        if not complete_delta or not compatible:
            issue_code = (
                "numeric_basis_mismatch" if complete_delta and not compatible else "numeric_delta_incomplete"
            )
            issues.append(
                ReductionIssue(
                    issue_code,
                    (delta.delta_id,),
                    "数量、单位、基数或锚点不足/不一致；保留事件但不推算精确余额",
                )
            )
            exact_known = False
            balance = None
            continue
        if balance is None:
            raise ReducerIntegrityError("exact numeric state lost its balance")
        sign = Decimal(1) if delta.kind in {
            NumericDeltaKind.INCREASE,
            NumericDeltaKind.PLEDGE,
        } else Decimal(-1)
        candidate = balance + sign * delta.quantity
        if candidate < 0:
            raise ReducerIntegrityError("numeric conservation would produce a negative balance")
        balance = candidate
        applied.append(delta.delta_id)
    complete = exact_known and not issues
    exclusions = sorted(
        {item.delta_id: item for item in exclusions}.values(),
        key=lambda item: item.delta_id,
    )
    return NumericBalanceResult(
        balance=balance if complete else None,
        applied_delta_ids=tuple(applied),
        retained_delta_ids=tuple(retained_delta_ids),
        retained_event_ids=tuple(retained),
        excluded_delta_ids=tuple(item.delta_id for item in exclusions),
        excluded_reasons=tuple(exclusions),
        issues=tuple(issues),
        completeness_status=(
            CompletenessStatus.COMPLETE
            if complete
            else CompletenessStatus.INCOMPLETE
        ),
    )


def reduce_ownership_state(
    anchor: OwnershipSnapshot | None,
    deltas: Iterable[OwnershipPositionDelta],
    *,
    state_at: datetime,
    known_at: datetime,
) -> TypedBalanceReductionResult:
    """Reduce share-count changes independently by holder/share-class/basis."""

    grouped_deltas: dict[tuple[str, str, str | None], list[OwnershipPositionDelta]] = {}
    for delta in deltas:
        grouped_deltas.setdefault(
            (delta.holder_entity_id, delta.share_class, delta.capital_basis), []
        ).append(delta)
    anchors: dict[tuple[str, str, str | None], Decimal | None] = {}
    if anchor is not None and anchor.available_at <= known_at and (
        anchor.reference_at is None or anchor.reference_at <= state_at
    ):
        for position in anchor.positions:
            key = (
                position.holder_entity_id,
                position.share_class,
                position.capital_basis or anchor.capital_basis,
            )
            if key in anchors:
                raise ReducerIntegrityError("ownership anchor contains a duplicate bucket")
            anchors[key] = position.share_count
    keys = sorted(
        {*anchors, *grouped_deltas},
        key=lambda value: tuple(item or "" for item in value),
    )
    states: list[PositionBalanceState] = []
    applied: list[str] = []
    retained: list[str] = []
    retained_delta_ids: list[str] = []
    excluded: list[str] = []
    issues: list[ReductionIssue] = []
    for key in keys:
        converted = tuple(
            NumericBalanceDelta(
                delta_id=item.delta_id,
                kind=item.kind,
                effective_at=item.effective_at,
                announced_at=item.announced_at,
                available_at=item.available_at,
                quantity=item.quantity,
                unit=item.unit,
                calculation_basis=item.capital_basis,
                event_id=item.event_id,
                supersedes_delta_id=item.supersedes_delta_id,
            )
            for item in grouped_deltas.get(key, ())
        )
        result = reduce_numeric_balance(
            anchors.get(key),
            converted,
            state_at=state_at,
            known_at=known_at,
            anchor_unit="shares",
            anchor_calculation_basis=key[2],
        )
        states.append(
            PositionBalanceState(
                subject_key=tuple(item for item in key if item is not None),
                balance=result.balance,
                unit="shares" if result.balance is not None else None,
                calculation_basis=key[2],
                completeness_status=result.completeness_status,
            )
        )
        applied.extend(result.applied_delta_ids)
        retained_delta_ids.extend(result.retained_delta_ids)
        retained.extend(result.retained_event_ids)
        excluded.extend(result.excluded_delta_ids)
        issues.extend(result.issues)
    overall_complete = (
        anchor is not None
        and anchor.is_complete
        and bool(states)
        and all(
            item.completeness_status == CompletenessStatus.COMPLETE
            for item in states
        )
    )
    return TypedBalanceReductionResult(
        positions=tuple(states),
        applied_delta_ids=tuple(applied),
        retained_delta_ids=tuple(retained_delta_ids),
        retained_event_ids=tuple(retained),
        excluded_delta_ids=tuple(sorted(set(excluded))),
        issues=tuple(issues),
        completeness_status=(
            CompletenessStatus.COMPLETE
            if overall_complete
            else CompletenessStatus.INCOMPLETE
        ),
    )


def reduce_pledge_state(
    anchor_positions: Iterable[PledgePositionSnapshot],
    deltas: Iterable[PledgePositionDelta],
    *,
    state_at: datetime,
    known_at: datetime,
    anchor_complete: bool = True,
) -> TypedBalanceReductionResult:
    """Reduce pledged-share balances by pledgor/pledgee/capital basis."""

    anchors: dict[tuple[str, str | None, str | None], Decimal | None] = {}
    for position in anchor_positions:
        if position.available_at > known_at or (
            position.reference_at is not None and position.reference_at > state_at
        ):
            continue
        key = (
            position.pledgor_entity_id,
            position.pledgee_entity_id,
            position.capital_basis,
        )
        if key in anchors:
            raise ReducerIntegrityError("pledge anchor contains a duplicate bucket")
        anchors[key] = position.pledged_shares
    grouped: dict[tuple[str, str | None, str | None], list[PledgePositionDelta]] = {}
    for delta in deltas:
        grouped.setdefault(
            (delta.pledgor_entity_id, delta.pledgee_entity_id, delta.capital_basis),
            [],
        ).append(delta)
    keys = sorted({*anchors, *grouped}, key=lambda value: tuple(item or "" for item in value))
    states: list[PositionBalanceState] = []
    applied: list[str] = []
    retained: list[str] = []
    retained_delta_ids: list[str] = []
    excluded: list[str] = []
    issues: list[ReductionIssue] = []
    for key in keys:
        converted = tuple(
            NumericBalanceDelta(
                delta_id=item.delta_id,
                kind=item.kind,
                effective_at=item.effective_at,
                announced_at=item.announced_at,
                available_at=item.available_at,
                quantity=item.quantity,
                unit=item.unit,
                calculation_basis=item.capital_basis,
                event_id=item.event_id,
                supersedes_delta_id=item.supersedes_delta_id,
            )
            for item in grouped.get(key, ())
        )
        result = reduce_numeric_balance(
            anchors.get(key),
            converted,
            state_at=state_at,
            known_at=known_at,
            anchor_unit="shares",
            anchor_calculation_basis=key[2],
        )
        states.append(
            PositionBalanceState(
                subject_key=tuple(item for item in key if item is not None),
                balance=result.balance,
                unit="shares" if result.balance is not None else None,
                calculation_basis=key[2],
                completeness_status=result.completeness_status,
            )
        )
        applied.extend(result.applied_delta_ids)
        retained_delta_ids.extend(result.retained_delta_ids)
        retained.extend(result.retained_event_ids)
        excluded.extend(result.excluded_delta_ids)
        issues.extend(result.issues)
    complete = (
        anchor_complete
        and bool(anchors)
        and all(
            item.completeness_status == CompletenessStatus.COMPLETE
            for item in states
        )
    )
    return TypedBalanceReductionResult(
        positions=tuple(states),
        applied_delta_ids=tuple(applied),
        retained_delta_ids=tuple(retained_delta_ids),
        retained_event_ids=tuple(retained),
        excluded_delta_ids=tuple(sorted(set(excluded))),
        issues=tuple(issues),
        completeness_status=(
            CompletenessStatus.COMPLETE if complete else CompletenessStatus.INCOMPLETE
        ),
    )


def validate_control_graph(relations: Iterable[ControlRelation]) -> tuple[ControlRelation, ...]:
    ordered = tuple(sorted(relations, key=lambda item: item.record_id))
    if len({item.company_id for item in ordered}) > 1:
        raise ReducerIntegrityError("control graph crosses company namespace")
    identities = {
        (
            item.controller_entity_id,
            item.controlled_entity_id,
            item.relation_type,
            item.direction,
            item.chain_path,
        )
        for item in ordered
    }
    if len(identities) != len(ordered):
        raise ReducerIntegrityError("duplicate control relation")
    edges = {(item.controller_entity_id, item.controlled_entity_id) for item in ordered}
    direct_edges = {
        (item.controller_entity_id, item.controlled_entity_id)
        for item in ordered
        if item.direction == DirectionKind.DIRECT
    }
    for item in ordered:
        if item.direction != DirectionKind.INDIRECT:
            continue
        missing = [
            pair
            for pair in zip(item.chain_path, item.chain_path[1:], strict=False)
            if pair not in direct_edges
        ]
        if missing:
            raise ReducerIntegrityError(
                "indirect control chain contains an unproven segment"
            )
    adjacency: dict[str, set[str]] = {}
    for left, right in edges:
        adjacency.setdefault(left, set()).add(right)

    def walk(node: str, path: frozenset[str]) -> None:
        if node in path:
            raise ReducerIntegrityError("control graph contains a cycle")
        for child in adjacency.get(node, set()):
            walk(child, path | {node})

    for node in sorted(adjacency):
        walk(node, frozenset())
    return ordered


@dataclass(frozen=True)
class ProjectionConflict:
    natural_key: tuple[str, ...]
    record_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class TypedProjectionResult:
    records: tuple[GovernanceRecord, ...]
    excluded_record_ids: tuple[str, ...]
    conflicts: tuple[ProjectionConflict, ...]
    completeness_status: CompletenessStatus


def _record_is_state_eligible(record: GovernanceRecord, state_at: datetime) -> bool:
    if isinstance(record, OwnershipPosition):
        raise ReducerIntegrityError(
            "OwnershipPosition is an embedded value; project its OwnershipSnapshot"
        )
    if (record.valid_from is not None and record.valid_from > state_at) or (
        record.valid_to is not None and record.valid_to <= state_at
    ):
        return False
    if record.effective_at is not None and record.effective_at > state_at:
        return False
    if record.reference_at is not None and record.reference_at > state_at:
        return False
    local_date = state_at.astimezone(SHANGHAI_TIMEZONE).date()
    fiscal_year = getattr(record, "fiscal_year", None)
    if fiscal_year is not None and fiscal_year > local_date.year:
        return False
    for field_name in ("fiscal_period_end", "report_period_end", "decision_date", "effective_date"):
        value = getattr(record, field_name, None)
        if isinstance(value, date) and value > local_date:
            return False
    for field_name in ("grant_at", "issued_at"):
        value = getattr(record, field_name, None)
        if isinstance(value, datetime) and value > state_at:
            return False
    return True


def _default_natural_key(record: GovernanceRecord) -> tuple[str, ...]:
    parts = [record.kind, record.company_id]
    for name in (
        "person_id",
        "role_code",
        "role_scope",
        "fiscal_year",
        "scope",
        "related_entity_id",
        "counterparty_entity_id",
        "transaction_type",
        "plan_id",
        "period_label",
        "audit_firm_entity_id",
        "authority",
        "policy_id",
    ):
        value = getattr(record, name, None)
        if value is not None:
            parts.append(f"{name}={value}")
    if len(parts) == 2:
        parts.append(f"record={record.record_id}")
    return tuple(parts)


def project_versioned_records(
    records: Iterable[GovernanceRecord],
    *,
    state_at: datetime,
    known_at: datetime,
    known_at_precision: TimePrecision = TimePrecision.DATETIME,
    natural_key: Any | None = None,
) -> TypedProjectionResult:
    state_at = _aware_utc(state_at, field_name="state_at")
    known_at = _aware_utc(known_at, field_name="known_at")
    values = tuple(records)
    visible: list[GovernanceRecord] = []
    excluded: set[str] = set()
    by_id: dict[str, GovernanceRecord] = {}
    for record in values:
        if isinstance(record, OwnershipPosition):
            raise ReducerIntegrityError(
                "OwnershipPosition is an embedded value; project its OwnershipSnapshot"
            )
        if not _available_at_or_before(
            record.available_at,
            record.available_time_precision,
            known_at,
            known_at_precision,
        ):
            excluded.add(record.record_id)
            continue
        existing = by_id.get(record.record_id)
        if existing is not None and existing != record:
            raise ReducerIntegrityError("one record ID has multiple payloads")
        by_id[record.record_id] = record
        visible.append(record)

    visible_by_id = {item.record_id: item for item in visible}
    corrections = [item for item in visible if isinstance(item, CorrectionRecord)]
    for correction in corrections:
        target_id = correction.corrected_record_id
        if target_id is None:
            continue
        if target_id not in visible_by_id:
            raise ReducerIntegrityError("correction references an unknown record")
        replacements = [
            item
            for item in visible
            if not isinstance(item, CorrectionRecord)
            and item.supersedes_record_id == target_id
        ]
        if len(replacements) != 1:
            raise ReducerIntegrityError(
                "record correction requires exactly one explicit replacement version"
            )
    successor_by_target: dict[str, str] = {}
    for record in visible:
        target_id = record.supersedes_record_id
        if target_id is None:
            continue
        target = visible_by_id.get(target_id)
        if target is None:
            raise ReducerIntegrityError("visible record has a dangling supersedes reference")
        if target.company_id != record.company_id or target.kind != record.kind:
            raise ReducerIntegrityError("record supersedes relation crosses company or kind")
        if record.available_at < target.available_at:
            raise ReducerIntegrityError(
                "record supersedes chain is not append-only"
            )
        previous = successor_by_target.get(target_id)
        if previous is not None:
            raise ReducerIntegrityError(
                f"record supersedes chain has parallel heads at {target_id}: "
                f"{previous}, {record.record_id}"
            )
        successor_by_target[target_id] = record.record_id

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(record_id: str) -> None:
        if record_id in visiting:
            raise ReducerIntegrityError("record supersedes chain contains a cycle")
        if record_id in visited:
            return
        visiting.add(record_id)
        parent_id = visible_by_id[record_id].supersedes_record_id
        if parent_id is not None:
            visit(parent_id)
        visiting.remove(record_id)
        visited.add(record_id)

    for record_id in sorted(visible_by_id):
        visit(record_id)

    applicable = [
        item
        for item in visible
        if not isinstance(item, CorrectionRecord)
        and _record_is_state_eligible(item, state_at)
    ]
    applicable_ids = {item.record_id for item in applicable}
    superseded_at_state = {
        item.supersedes_record_id
        for item in applicable
        if item.supersedes_record_id in applicable_ids
    }
    heads = [item for item in applicable if item.record_id not in superseded_at_state]
    excluded.update(item.record_id for item in visible if item not in heads)

    key_fn = natural_key or _default_natural_key
    grouped: dict[tuple[str, ...], list[GovernanceRecord]] = {}
    for record in heads:
        key = tuple(str(part) for part in key_fn(record))
        grouped.setdefault(key, []).append(record)
    conflicts: list[ProjectionConflict] = []
    canonical: list[GovernanceRecord] = []
    for key, group in sorted(grouped.items()):
        if len(group) == 1:
            canonical.append(group[0])
            continue
        record_ids = tuple(sorted(item.record_id for item in group))
        conflicts.append(
            ProjectionConflict(
                natural_key=key,
                record_ids=record_ids,
                reason="multiple visible authoritative record heads share one natural key",
            )
        )
        excluded.update(record_ids)
    return TypedProjectionResult(
        records=tuple(sorted(canonical, key=lambda item: (item.kind, item.record_id))),
        excluded_record_ids=tuple(sorted(excluded)),
        conflicts=tuple(conflicts),
        completeness_status=(
            CompletenessStatus.CONFLICTED
            if conflicts
            else CompletenessStatus.COMPLETE
        ),
    )


def project_interval_records(
    records: Iterable[GovernanceRecord],
    *,
    state_at: datetime,
    known_at: datetime,
    known_at_precision: TimePrecision = TimePrecision.DATETIME,
) -> tuple[GovernanceRecord, ...]:
    return project_versioned_records(
        records,
        state_at=state_at,
        known_at=known_at,
        known_at_precision=known_at_precision,
    ).records


def project_annual_records(
    records: Iterable[GovernanceRecord],
    *,
    fiscal_year: int,
    state_at: datetime,
    known_at: datetime,
    known_at_precision: TimePrecision = TimePrecision.DATETIME,
) -> TypedProjectionResult:
    if fiscal_year < 1900 or fiscal_year > 9999:
        raise ValueError("fiscal_year is outside the supported range")
    selected: list[GovernanceRecord] = []
    for record in records:
        explicit_year = getattr(record, "fiscal_year", None)
        if explicit_year is not None:
            if explicit_year == fiscal_year:
                selected.append(record)
            continue
        period_end = getattr(record, "fiscal_period_end", None) or getattr(
            record, "report_period_end", None
        )
        if isinstance(period_end, date) and period_end.year == fiscal_year:
            selected.append(record)
    return project_versioned_records(
        selected,
        state_at=state_at,
        known_at=known_at,
        known_at_precision=known_at_precision,
    )


def select_active_conflicts(
    conflicts: Iterable[ConflictRecord],
    *,
    known_at: datetime,
) -> tuple[ConflictRecord, ...]:
    known_at = _aware_utc(known_at, field_name="known_at")
    visible = [item for item in conflicts if item.available_at <= known_at]
    by_id = {item.conflict_id: item for item in visible}
    if len(by_id) != len(visible):
        raise ReducerIntegrityError("duplicate conflict identity")
    superseded: set[str] = set()
    for item in visible:
        if item.supersedes_conflict_id is not None:
            target = by_id.get(item.supersedes_conflict_id)
            if target is None:
                raise ReducerIntegrityError("conflict has a dangling supersedes reference")
            if (
                target.company_id != item.company_id
                or target.question_id != item.question_id
                or target.subject_id != item.subject_id
                or target.predicate != item.predicate
            ):
                raise ReducerIntegrityError(
                    "conflict supersedes relation crosses its natural identity"
                )
            superseded.add(item.supersedes_conflict_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(conflict_id: str) -> None:
        if conflict_id in visiting:
            raise ReducerIntegrityError("conflict supersedes chain contains a cycle")
        if conflict_id in visited:
            return
        visiting.add(conflict_id)
        parent_id = by_id[conflict_id].supersedes_conflict_id
        if parent_id is not None:
            visit(parent_id)
        visiting.remove(conflict_id)
        visited.add(conflict_id)

    for conflict_id in sorted(by_id):
        visit(conflict_id)
    return tuple(
        sorted(
            (
                item
                for item in visible
                if item.conflict_id not in superseded
                and item.status == RecordResolutionStatus.ACTIVE
            ),
            key=lambda item: item.conflict_id,
        )
    )


def project_control_state(
    relations: Iterable[ControlRelation],
    *,
    state_at: datetime,
    known_at: datetime,
    known_at_precision: TimePrecision = TimePrecision.DATETIME,
) -> TypedProjectionResult:
    values = tuple(relations)
    if any(not isinstance(item, ControlRelation) for item in values):
        raise ReducerIntegrityError("control projection received a non-control record")
    projected = project_versioned_records(
        values,
        state_at=state_at,
        known_at=known_at,
        known_at_precision=known_at_precision,
        natural_key=lambda item: (
            item.kind,
            item.company_id,
            item.controller_entity_id,
            item.controlled_entity_id,
            item.relation_type,
        ),
    )
    typed = tuple(item for item in projected.records if isinstance(item, ControlRelation))
    validate_control_graph(typed)
    return TypedProjectionResult(
        records=typed,
        excluded_record_ids=projected.excluded_record_ids,
        conflicts=projected.conflicts,
        completeness_status=projected.completeness_status,
    )


__all__ = [
    "CurrentRoleProjection",
    "DEFAULT_ROLE_TAXONOMY",
    "DeltaExclusion",
    "NumericBalanceDelta",
    "NumericBalanceResult",
    "NumericDeltaKind",
    "OwnershipPositionDelta",
    "PledgePositionDelta",
    "PositionBalanceState",
    "ReducerIntegrityError",
    "ReductionIssue",
    "RoleAction",
    "RoleDelta",
    "RoleOccupancy",
    "RoleReductionResult",
    "RoleRule",
    "ProjectionConflict",
    "TypedProjectionResult",
    "TypedBalanceReductionResult",
    "project_annual_records",
    "project_control_state",
    "project_current_roles",
    "project_interval_records",
    "project_versioned_records",
    "reduce_numeric_balance",
    "reduce_ownership_state",
    "reduce_pledge_state",
    "reduce_role_state",
    "reduce_role_tenures",
    "select_active_conflicts",
    "validate_control_graph",
]
