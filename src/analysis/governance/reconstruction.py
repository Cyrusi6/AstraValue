from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any, Callable, Generic, Iterable, TypeAlias, TypeVar

from .canonical import ensure_aware_utc, shanghai_date_exclusive_upper_bound
from .models import (
    CompletenessStatus,
    ConflictRecord,
    DeltaDisposition,
    GapRecord,
    GovernanceRecordBase,
    GovernancePerspective,
    OwnershipSnapshot,
    PledgePositionSnapshot,
    RecordResolutionStatus,
    RoleTenure,
    RosterSnapshot,
    TimePrecision,
)
from .reducers import (
    NumericBalanceDelta,
    OwnershipPositionDelta,
    PledgePositionDelta,
    RoleDelta,
    project_annual_records,
    project_control_state,
    project_versioned_records,
    reduce_numeric_balance,
    reduce_ownership_state,
    reduce_pledge_state,
    reduce_role_state,
)


TimeInput: TypeAlias = datetime | date | str
T = TypeVar("T")
TAnchor = TypeVar("TAnchor")
TDelta = TypeVar("TDelta")


class ReconstructionValidationError(ValueError):
    """A stable, API-mappable validation error for bitemporal queries."""


class VersionGraphError(ValueError):
    """Raised when a visible version graph is not safe to resolve."""


def _timezone_label(value: datetime) -> str:
    key = getattr(value.tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    label = value.tzname()
    return label or str(value.tzinfo)


@dataclass(frozen=True, slots=True)
class QueryInstant:
    """A query/evidence cutoff with its original precision and provenance."""

    cutoff: datetime
    original_input: str
    original_timezone: str
    precision: TimePrecision

    def __post_init__(self) -> None:
        try:
            normalized = ensure_aware_utc(self.cutoff)
        except ValueError as exc:
            raise ReconstructionValidationError(str(exc)) from exc
        object.__setattr__(self, "cutoff", normalized)
        if not self.original_input:
            raise ReconstructionValidationError("original_input must not be empty")
        if not self.original_timezone:
            raise ReconstructionValidationError("original_timezone must not be empty")
        if not isinstance(self.precision, TimePrecision):
            try:
                object.__setattr__(self, "precision", TimePrecision(self.precision))
            except (TypeError, ValueError) as exc:
                raise ReconstructionValidationError("unsupported time precision") from exc

    @classmethod
    def parse(cls, value: TimeInput | QueryInstant) -> QueryInstant:
        if isinstance(value, QueryInstant):
            return value
        if isinstance(value, datetime):
            try:
                cutoff = ensure_aware_utc(value)
            except ValueError as exc:
                raise ReconstructionValidationError("datetime must be timezone-aware") from exc
            return cls(
                cutoff=cutoff,
                original_input=value.isoformat(),
                original_timezone=_timezone_label(value),
                precision=TimePrecision.DATETIME,
            )
        if isinstance(value, date):
            return cls(
                cutoff=shanghai_date_exclusive_upper_bound(value),
                original_input=value.isoformat(),
                original_timezone="Asia/Shanghai",
                precision=TimePrecision.DATE,
            )
        if not isinstance(value, str):
            raise ReconstructionValidationError(
                "time input must be an aware datetime, RFC3339 string, ISO date, or date"
            )
        raw = value
        if not raw or raw != raw.strip():
            raise ReconstructionValidationError("time strings must not be empty or padded")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                parsed_date = date.fromisoformat(raw)
            except ValueError as exc:
                raise ReconstructionValidationError("invalid ISO date") from exc
            return cls(
                cutoff=shanghai_date_exclusive_upper_bound(parsed_date),
                original_input=raw,
                original_timezone="Asia/Shanghai",
                precision=TimePrecision.DATE,
            )
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
            raw,
        ) is None:
            raise ReconstructionValidationError("invalid RFC3339 datetime")
        normalized_raw = raw[:-1] + "+00:00" if raw[-1] in {"Z", "z"} else raw
        try:
            parsed = datetime.fromisoformat(normalized_raw)
        except ValueError as exc:
            raise ReconstructionValidationError("invalid RFC3339 datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReconstructionValidationError("RFC3339 datetime must include a timezone")
        return cls(
            cutoff=ensure_aware_utc(parsed),
            original_input=raw,
            original_timezone=_timezone_label(parsed),
            precision=TimePrecision.DATETIME,
        )

    @property
    def utc(self) -> datetime:
        return self.cutoff

    @property
    def available_at_upper_bound(self) -> datetime:
        return self.cutoff


def _instant_order_key(value: QueryInstant) -> tuple[datetime, int]:
    # A date D is represented by the exclusive D+1 00:00 Asia/Shanghai
    # boundary.  At the same UTC cutoff it is therefore earlier than an exact
    # datetime located at D+1 00:00, which itself must not leak into an
    # "as of D" query.
    precision_rank = 0 if value.precision == TimePrecision.DATE else 1
    return value.cutoff, precision_rank


def _instant_at_or_before(value: QueryInstant, cutoff: QueryInstant) -> bool:
    return _instant_order_key(value) <= _instant_order_key(cutoff)


def _instant_after(value: QueryInstant, boundary: QueryInstant) -> bool:
    return _instant_order_key(value) > _instant_order_key(boundary)


def _payload_instant(
    value: object,
    *,
    field_name: str,
    precision: TimePrecision = TimePrecision.DATETIME,
) -> QueryInstant:
    if not isinstance(value, datetime):
        raise VersionGraphError(
            f"typed payload {field_name} must be a timezone-aware datetime"
        )
    try:
        cutoff = ensure_aware_utc(value)
    except ValueError as exc:
        raise VersionGraphError(
            f"typed payload {field_name} must be a timezone-aware datetime"
        ) from exc
    return QueryInstant(
        cutoff=cutoff,
        original_input=value.isoformat(),
        original_timezone=_timezone_label(value),
        precision=precision,
    )


def _require_wrapper_payload_instant(
    wrapper: DeltaCandidate[Any],
    *,
    field_name: str,
    payload_precision: TimePrecision = TimePrecision.DATETIME,
) -> None:
    wrapper_value = getattr(wrapper, field_name)
    payload_value = getattr(wrapper.payload, field_name, None)
    if not isinstance(wrapper_value, QueryInstant):
        raise VersionGraphError(f"delta wrapper {field_name} is not normalized")
    payload_instant = _payload_instant(
        payload_value,
        field_name=field_name,
        precision=payload_precision,
    )
    if _instant_order_key(wrapper_value) != _instant_order_key(payload_instant):
        raise VersionGraphError(
            f"{wrapper.stable_id} wrapper {field_name} disagrees with payload "
            f"{field_name}"
        )


def normalize_query_instant(value: TimeInput | QueryInstant) -> QueryInstant:
    return QueryInstant.parse(value)


@dataclass(frozen=True, slots=True)
class GovernanceQuery:
    state_at: QueryInstant
    known_at: QueryInstant
    perspective: GovernancePerspective
    future_knowledge_used: bool

    def __post_init__(self) -> None:
        if not isinstance(self.state_at, QueryInstant) or not isinstance(
            self.known_at, QueryInstant
        ):
            raise ReconstructionValidationError(
                "GovernanceQuery requires normalized QueryInstant values"
            )
        try:
            normalized_perspective = GovernancePerspective(self.perspective)
        except (TypeError, ValueError) as exc:
            raise ReconstructionValidationError("unknown governance perspective") from exc
        object.__setattr__(self, "perspective", normalized_perspective)
        if not isinstance(self.future_knowledge_used, bool):
            raise ReconstructionValidationError("future_knowledge_used must be boolean")
        if _instant_order_key(self.known_at) < _instant_order_key(self.state_at):
            raise ReconstructionValidationError("known_at must not precede state_at")
        if (
            self.perspective == GovernancePerspective.STRICT
            and _instant_order_key(self.known_at) != _instant_order_key(self.state_at)
        ):
            raise ReconstructionValidationError("strict perspective requires known_at == state_at")
        expected = (
            self.perspective == GovernancePerspective.RECONSTRUCTED
            and _instant_order_key(self.known_at) > _instant_order_key(self.state_at)
        )
        if self.future_knowledge_used != expected:
            raise ReconstructionValidationError(
                "future_knowledge_used does not match the bitemporal perspective"
            )

    @property
    def state_cutoff(self) -> datetime:
        return self.state_at.cutoff

    @property
    def known_cutoff(self) -> datetime:
        return self.known_at.cutoff

    @classmethod
    def normalize(
        cls,
        state_at: TimeInput | QueryInstant,
        known_at: TimeInput | QueryInstant | None = None,
        perspective: GovernancePerspective | str | None = None,
    ) -> GovernanceQuery:
        return normalize_governance_query(state_at, known_at, perspective)


def normalize_governance_query(
    state_at: TimeInput | QueryInstant,
    known_at: TimeInput | QueryInstant | None = None,
    perspective: GovernancePerspective | str | None = None,
) -> GovernanceQuery:
    state = QueryInstant.parse(state_at)
    known = state if known_at is None else QueryInstant.parse(known_at)
    if _instant_order_key(known) < _instant_order_key(state):
        raise ReconstructionValidationError("known_at must not precede state_at")
    if perspective is None:
        if _instant_order_key(known) > _instant_order_key(state):
            raise ReconstructionValidationError(
                "known_at after state_at requires explicit reconstructed perspective"
            )
        normalized_perspective = GovernancePerspective.STRICT
    else:
        try:
            normalized_perspective = GovernancePerspective(perspective)
        except (TypeError, ValueError) as exc:
            raise ReconstructionValidationError("unknown governance perspective") from exc
    if (
        normalized_perspective == GovernancePerspective.STRICT
        and _instant_order_key(known) != _instant_order_key(state)
    ):
        raise ReconstructionValidationError("strict perspective requires known_at == state_at")
    return GovernanceQuery(
        state_at=state,
        known_at=known,
        perspective=normalized_perspective,
        future_knowledge_used=(
            normalized_perspective == GovernancePerspective.RECONSTRUCTED
            and _instant_order_key(known) > _instant_order_key(state)
        ),
    )


@dataclass(frozen=True, slots=True)
class VersionedItem(Generic[T]):
    stable_id: str
    payload: T
    available_at: QueryInstant | TimeInput
    supersedes_id: str | None = None
    withdrawn: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ReconstructionValidationError("stable_id must be a non-empty string")
        object.__setattr__(self, "available_at", QueryInstant.parse(self.available_at))
        if self.supersedes_id == self.stable_id:
            raise VersionGraphError("a version cannot supersede itself")

    @property
    def available_at_upper_bound(self) -> datetime:
        assert isinstance(self.available_at, QueryInstant)
        return self.available_at.cutoff


@dataclass(frozen=True, slots=True)
class VersionDecision:
    stable_id: str
    selected: bool
    reason: str


@dataclass(frozen=True, slots=True)
class VersionSelection(Generic[T]):
    selected: tuple[VersionedItem[T], ...]
    decisions: tuple[VersionDecision, ...]

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(item.stable_id for item in self.selected)


def _known_instant(
    value: GovernanceQuery | QueryInstant | TimeInput,
) -> QueryInstant:
    if isinstance(value, GovernanceQuery):
        return value.known_at
    return QueryInstant.parse(value)


def resolve_visible_versions(
    items: Iterable[VersionedItem[T]],
    known_at: GovernanceQuery | QueryInstant | TimeInput,
) -> VersionSelection[T]:
    """Filter by availability first, then fail closed on the visible graph."""

    candidates = tuple(items)
    all_ids: set[str] = set()
    for item in candidates:
        if item.stable_id in all_ids:
            raise VersionGraphError(
                f"duplicate version id before availability filtering: {item.stable_id}"
            )
        all_ids.add(item.stable_id)
    known = _known_instant(known_at)
    visible = tuple(
        item
        for item in candidates
        if _instant_at_or_before(item.available_at, known)
    )

    by_id: dict[str, VersionedItem[T]] = {}
    for item in visible:
        if item.stable_id in by_id:
            raise VersionGraphError(f"duplicate visible version id: {item.stable_id}")
        by_id[item.stable_id] = item

    children: dict[str, str] = {}
    for item in visible:
        target = item.supersedes_id
        if target is None:
            continue
        if target not in by_id:
            raise VersionGraphError(
                f"visible version {item.stable_id} has dangling supersedes target {target}"
            )
        if _instant_order_key(item.available_at) < _instant_order_key(
            by_id[target].available_at
        ):
            raise VersionGraphError(
                f"visible version {item.stable_id} predates superseded target {target}"
            )
        previous = children.get(target)
        if previous is not None:
            raise VersionGraphError(
                f"visible supersedes graph branches at {target}: {previous}, {item.stable_id}"
            )
        children[target] = item.stable_id

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            raise VersionGraphError("visible supersedes graph contains a cycle")
        visiting.add(item_id)
        target = by_id[item_id].supersedes_id
        if target is not None:
            visit(target)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in sorted(by_id):
        visit(item_id)

    selected: list[VersionedItem[T]] = []
    decisions: list[VersionDecision] = []
    visible_ids = set(by_id)
    for item in candidates:
        if item.stable_id not in visible_ids or not _instant_at_or_before(
            item.available_at, known
        ):
            decisions.append(
                VersionDecision(item.stable_id, False, "available_after_known_at")
            )
            continue
        replacement = children.get(item.stable_id)
        if replacement is not None:
            decisions.append(
                VersionDecision(
                    item.stable_id,
                    False,
                    f"superseded_by_visible_version:{replacement}",
                )
            )
            continue
        if item.withdrawn:
            decisions.append(VersionDecision(item.stable_id, False, "visible_withdrawal"))
            continue
        selected.append(item)
        decisions.append(VersionDecision(item.stable_id, True, "visible_head"))

    selected.sort(key=lambda item: item.stable_id)
    decisions.sort(key=lambda item: item.stable_id)
    return VersionSelection(tuple(selected), tuple(decisions))


@dataclass(frozen=True, slots=True)
class AnchorCandidate(Generic[TAnchor]):
    stable_id: str
    payload: TAnchor
    reference_at: QueryInstant | TimeInput
    available_at: QueryInstant | TimeInput
    complete: bool
    supersedes_id: str | None = None
    withdrawn: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ReconstructionValidationError("anchor stable_id must be non-empty")
        object.__setattr__(self, "reference_at", QueryInstant.parse(self.reference_at))
        object.__setattr__(self, "available_at", QueryInstant.parse(self.available_at))
        if self.supersedes_id == self.stable_id:
            raise VersionGraphError("an anchor cannot supersede itself")

    @property
    def reference_cutoff(self) -> datetime:
        assert isinstance(self.reference_at, QueryInstant)
        return self.reference_at.cutoff

    @property
    def available_at_upper_bound(self) -> datetime:
        assert isinstance(self.available_at, QueryInstant)
        return self.available_at.cutoff


@dataclass(frozen=True, slots=True)
class AnchorOutcome:
    anchor_id: str
    selected: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AnchorSelection(Generic[TAnchor]):
    selected: AnchorCandidate[TAnchor] | None
    outcomes: tuple[AnchorOutcome, ...]


def select_anchor(
    anchors: Iterable[AnchorCandidate[TAnchor]],
    query: GovernanceQuery,
) -> AnchorSelection[TAnchor]:
    candidates = tuple(anchors)
    wrapped = tuple(
        VersionedItem(
            stable_id=item.stable_id,
            payload=item,
            available_at=item.available_at,
            supersedes_id=item.supersedes_id,
            withdrawn=item.withdrawn,
        )
        for item in candidates
    )
    versions = resolve_visible_versions(wrapped, query)
    decision_by_id = {item.stable_id: item for item in versions.decisions}
    eligible = [
        item.payload
        for item in versions.selected
        if item.payload.complete
        and _instant_at_or_before(item.payload.reference_at, query.state_at)
    ]
    selected = max(
        eligible,
        key=lambda item: (
            _instant_order_key(item.reference_at),
            _instant_order_key(item.available_at),
            item.stable_id,
        ),
        default=None,
    )

    outcomes: list[AnchorOutcome] = []
    for item in candidates:
        decision = decision_by_id[item.stable_id]
        if not decision.selected:
            reason = decision.reason
        elif not item.complete:
            reason = "anchor_not_complete"
        elif not _instant_at_or_before(item.reference_at, query.state_at):
            reason = "anchor_reference_after_state_at"
        elif selected is not None and item.stable_id == selected.stable_id:
            reason = "selected_latest_complete_anchor"
        else:
            reason = "older_complete_anchor"
        outcomes.append(
            AnchorOutcome(
                anchor_id=item.stable_id,
                selected=selected is not None and item.stable_id == selected.stable_id,
                reason=reason,
            )
        )
    outcomes.sort(key=lambda item: item.anchor_id)
    return AnchorSelection(selected=selected, outcomes=tuple(outcomes))


@dataclass(frozen=True, slots=True)
class DeltaCandidate(Generic[TDelta]):
    stable_id: str
    payload: TDelta
    effective_at: QueryInstant | TimeInput
    announced_at: QueryInstant | TimeInput
    available_at: QueryInstant | TimeInput
    supersedes_id: str | None = None
    withdrawn: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.stable_id, str) or not self.stable_id.strip():
            raise ReconstructionValidationError("delta stable_id must be non-empty")
        object.__setattr__(self, "effective_at", QueryInstant.parse(self.effective_at))
        object.__setattr__(self, "announced_at", QueryInstant.parse(self.announced_at))
        object.__setattr__(self, "available_at", QueryInstant.parse(self.available_at))
        if _instant_order_key(self.announced_at) > _instant_order_key(
            self.available_at
        ):
            raise ReconstructionValidationError(
                "delta announced_at must not follow available_at"
            )
        if self.supersedes_id == self.stable_id:
            raise VersionGraphError("a delta cannot supersede itself")

    @property
    def effective_cutoff(self) -> datetime:
        assert isinstance(self.effective_at, QueryInstant)
        return self.effective_at.cutoff

    @property
    def announced_cutoff(self) -> datetime:
        assert isinstance(self.announced_at, QueryInstant)
        return self.announced_at.cutoff

    @property
    def available_at_upper_bound(self) -> datetime:
        assert isinstance(self.available_at, QueryInstant)
        return self.available_at.cutoff


@dataclass(frozen=True, slots=True)
class DeltaOutcome:
    delta_id: str
    disposition: DeltaDisposition
    reason: str


@dataclass(frozen=True, slots=True)
class DeltaSelection(Generic[TDelta]):
    ordered: tuple[DeltaCandidate[TDelta], ...]
    outcomes: tuple[DeltaOutcome, ...]

    @property
    def ordered_ids(self) -> tuple[str, ...]:
        return tuple(item.stable_id for item in self.ordered)


Eligibility: TypeAlias = Callable[[DeltaCandidate[Any], GovernanceQuery], str | None]


def _coalesce_delta_supersedes(
    deltas: Iterable[DeltaCandidate[TDelta]],
) -> tuple[DeltaCandidate[TDelta], ...]:
    """Promote typed payload version metadata into the outer visibility gate.

    Typed reducers remain independently usable and therefore understand their
    own ``supersedes_delta_id``.  The reconstruction pipeline, however, must
    resolve versions before a payload reaches a reducer.  Coalescing the two
    representations here prevents both a future-information bypass and a
    second, dangling resolution pass inside the reducer.
    """

    values: list[DeltaCandidate[TDelta]] = []
    for item in deltas:
        payload_id = getattr(item.payload, "delta_id", None)
        if payload_id is not None:
            if payload_id != item.stable_id:
                raise VersionGraphError(
                    f"delta wrapper {item.stable_id} disagrees with payload ID {payload_id}"
                )
            for field_name in ("effective_at", "announced_at", "available_at"):
                _require_wrapper_payload_instant(item, field_name=field_name)
        payload_target = getattr(item.payload, "supersedes_delta_id", None)
        if payload_target is None:
            values.append(item)
            continue
        if item.supersedes_id not in {None, payload_target}:
            raise VersionGraphError(
                f"delta {item.stable_id} has inconsistent supersedes metadata"
            )
        values.append(replace(item, supersedes_id=payload_target))
    return tuple(values)


def _clear_resolved_payload_supersedes(value: TDelta) -> TDelta:
    if getattr(value, "supersedes_delta_id", None) is None:
        return value
    try:
        return replace(value, supersedes_delta_id=None)
    except TypeError as exc:
        raise VersionGraphError(
            "typed delta exposes supersedes_delta_id but cannot be normalized"
        ) from exc


def _prepare_record_deltas(
    deltas: Iterable[DeltaCandidate[TDelta]],
) -> tuple[DeltaCandidate[TDelta], ...]:
    """Keep record-version resolution inside the typed projection reducer."""

    values: list[DeltaCandidate[TDelta]] = []
    for item in deltas:
        if not isinstance(item.payload, GovernanceRecordBase):
            raise VersionGraphError(
                "typed record delta payload must be a GovernanceRecord"
            )
        record_id = getattr(item.payload, "record_id", None)
        if record_id is None:
            raise VersionGraphError("typed record delta payload is missing record_id")
        if record_id != item.stable_id:
            raise VersionGraphError(
                f"record wrapper {item.stable_id} disagrees with payload ID {record_id}"
            )
        payload_target = getattr(item.payload, "supersedes_record_id", None)
        if item.supersedes_id not in {None, payload_target}:
            raise VersionGraphError(
                f"record {record_id} has inconsistent supersedes metadata"
            )
        if item.supersedes_id is not None and payload_target is None:
            raise VersionGraphError(
                f"record {record_id} wrapper cannot invent a supersedes relation"
            )
        _require_wrapper_payload_instant(
            item,
            field_name="available_at",
            payload_precision=item.payload.available_time_precision,
        )
        if item.payload.effective_at is not None:
            _require_wrapper_payload_instant(item, field_name="effective_at")
        # Outer reconstruction performs availability/effective filtering only;
        # project_versioned_records sees all visible versions and resolves the
        # authoritative record chain exactly once.
        values.append(replace(item, supersedes_id=None))
    return tuple(values)


def select_deltas(
    deltas: Iterable[DeltaCandidate[TDelta]],
    query: GovernanceQuery,
    *,
    anchor: AnchorCandidate[Any] | None = None,
    eligibility: Eligibility | None = None,
) -> DeltaSelection[TDelta]:
    candidates = tuple(deltas)
    wrapped = tuple(
        VersionedItem(
            stable_id=item.stable_id,
            payload=item,
            available_at=item.available_at,
            supersedes_id=item.supersedes_id,
            withdrawn=item.withdrawn,
        )
        for item in candidates
    )
    versions = resolve_visible_versions(wrapped, query)
    version_decisions = {item.stable_id: item for item in versions.decisions}
    selected_ids = set(versions.selected_ids)
    ordered: list[DeltaCandidate[TDelta]] = []
    outcomes: dict[str, DeltaOutcome] = {}

    for item in candidates:
        decision = version_decisions[item.stable_id]
        if item.stable_id not in selected_ids:
            outcomes[item.stable_id] = DeltaOutcome(
                item.stable_id, DeltaDisposition.EXCLUDED, decision.reason
            )
            continue
        if not _instant_at_or_before(item.effective_at, query.state_at):
            outcomes[item.stable_id] = DeltaOutcome(
                item.stable_id,
                DeltaDisposition.EXCLUDED,
                "effective_after_state_at",
            )
            continue
        if anchor is not None and not _instant_after(
            item.effective_at, anchor.reference_at
        ):
            outcomes[item.stable_id] = DeltaOutcome(
                item.stable_id,
                DeltaDisposition.EXCLUDED,
                "not_after_anchor_boundary",
            )
            continue
        reason = eligibility(item, query) if eligibility is not None else None
        if reason is not None:
            outcomes[item.stable_id] = DeltaOutcome(
                item.stable_id, DeltaDisposition.EXCLUDED, reason
            )
            continue
        ordered.append(item)

    ordered.sort(
        key=lambda item: (
            _instant_order_key(item.effective_at),
            _instant_order_key(item.announced_at),
            item.stable_id,
        )
    )
    for item in ordered:
        outcomes[item.stable_id] = DeltaOutcome(
            item.stable_id, DeltaDisposition.APPLIED, "eligible_for_reducer"
        )
    return DeltaSelection(
        ordered=tuple(ordered),
        outcomes=tuple(outcomes[item.stable_id] for item in sorted(candidates, key=lambda x: x.stable_id)),
    )


@dataclass(frozen=True, slots=True)
class CoverageState:
    question_id: str
    completeness_status: CompletenessStatus = CompletenessStatus.COMPLETE
    coverage_entry_ids: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    no_data: bool = False
    available_at: QueryInstant | TimeInput | None = None

    def __post_init__(self) -> None:
        if not self.question_id:
            raise ReconstructionValidationError("coverage question_id must not be empty")
        try:
            status = CompletenessStatus(self.completeness_status)
        except (TypeError, ValueError) as exc:
            raise ReconstructionValidationError("invalid coverage completeness status") from exc
        object.__setattr__(self, "completeness_status", status)
        object.__setattr__(self, "coverage_entry_ids", tuple(sorted(set(self.coverage_entry_ids))))
        object.__setattr__(self, "gap_ids", tuple(sorted(set(self.gap_ids))))
        object.__setattr__(self, "conflict_ids", tuple(sorted(set(self.conflict_ids))))
        if self.available_at is not None:
            object.__setattr__(
                self, "available_at", QueryInstant.parse(self.available_at)
            )


@dataclass(frozen=True, slots=True)
class ReconstructionResult(Generic[T]):
    query: GovernanceQuery
    state: T
    anchor_id: str | None
    anchor_outcomes: tuple[AnchorOutcome, ...]
    ordered_delta_ids: tuple[str, ...]
    delta_outcomes: tuple[DeltaOutcome, ...]
    coverage: tuple[CoverageState, ...]
    gaps: tuple[Any, ...]
    conflicts: tuple[Any, ...]
    issues: tuple[Any, ...]
    completeness_status: CompletenessStatus
    future_knowledge_used: bool
    negative_fact_inferred: bool = False

    @property
    def applied_delta_ids(self) -> tuple[str, ...]:
        applied = {
            item.delta_id
            for item in self.delta_outcomes
            if item.disposition == DeltaDisposition.APPLIED
        }
        return tuple(item for item in self.ordered_delta_ids if item in applied)

    @property
    def excluded_delta_ids(self) -> tuple[str, ...]:
        return tuple(
            item.delta_id
            for item in self.delta_outcomes
            if item.disposition == DeltaDisposition.EXCLUDED
        )

    @property
    def no_data_questions(self) -> tuple[str, ...]:
        return tuple(item.question_id for item in self.coverage if item.no_data)


Reducer: TypeAlias = Callable[[Any, tuple[Any, ...], GovernanceQuery], Any]


def _stable_object_key(value: Any) -> str:
    for attribute in ("gap_id", "conflict_id", "record_id", "stable_id", "id"):
        item = getattr(value, attribute, None)
        if item is not None:
            return str(item)
    return str(value)


def _active_conflict(value: Any) -> bool:
    status = getattr(value, "status", None)
    if status is None:
        return True
    rendered = getattr(status, "value", status)
    return rendered != "resolved"


def _visible_coverage(
    coverage: Iterable[CoverageState], query: GovernanceQuery
) -> tuple[CoverageState, ...]:
    visible = [
        item
        for item in coverage
        if item.available_at is None
        or _instant_at_or_before(item.available_at, query.known_at)
    ]
    return tuple(sorted(visible, key=lambda item: item.question_id))


def _active_side_records(
    values: Iterable[Any],
    query: GovernanceQuery,
    *,
    expected_type: type[Any],
    id_attribute: str,
    supersedes_attribute: str,
    identity_attributes: tuple[str, ...],
) -> tuple[Any, ...]:
    """Select visible active gap/conflict heads without leaking future records."""

    candidates = tuple(values)
    typed: list[Any] = []
    all_ids: set[str] = set()
    for item in candidates:
        if not isinstance(item, expected_type):
            raise ReconstructionValidationError(
                f"{id_attribute} side records must be typed {expected_type.__name__} objects"
            )
        item_id = getattr(item, id_attribute, None)
        available_at = getattr(item, "available_at", None)
        status = getattr(item, "status", None)
        if item_id is None or available_at is None or status is None:
            raise ReconstructionValidationError(
                f"{expected_type.__name__} is missing identity, availability, or status"
            )
        if item_id in all_ids:
            raise VersionGraphError(f"duplicate {id_attribute}: {item_id}")
        all_ids.add(item_id)
        instant = QueryInstant.parse(available_at)
        if _instant_at_or_before(instant, query.known_at):
            typed.append(item)

    visible_by_id = {getattr(item, id_attribute): item for item in typed}
    wrapped: list[VersionedItem[Any]] = []
    for item in typed:
        item_id = getattr(item, id_attribute)
        target_id = getattr(item, supersedes_attribute, None)
        if target_id is not None:
            target = visible_by_id.get(target_id)
            if target is not None and any(
                getattr(target, attribute, None) != getattr(item, attribute, None)
                for attribute in identity_attributes
            ):
                raise VersionGraphError(
                    f"{id_attribute} supersedes relation crosses its natural identity"
                )
        wrapped.append(
            VersionedItem(
                stable_id=item_id,
                payload=item,
                available_at=getattr(item, "available_at"),
                supersedes_id=target_id,
            )
        )
    selected = resolve_visible_versions(wrapped, query)
    active = [
        item.payload
        for item in selected.selected
        if RecordResolutionStatus(getattr(item.payload, "status"))
        == RecordResolutionStatus.ACTIVE
    ]
    return tuple(sorted(active, key=_stable_object_key))


def _reducer_outcomes(
    selection: DeltaSelection[Any],
    reduction: Any,
) -> tuple[DeltaOutcome, ...]:
    outcomes = {item.delta_id: item for item in selection.outcomes}
    eligible_ids = set(selection.ordered_ids)
    applied_ids = set(getattr(reduction, "applied_delta_ids", ()))
    applied_ids.update(
        str(item.record_id) for item in getattr(reduction, "records", ())
    )
    excluded_ids = set(getattr(reduction, "excluded_delta_ids", ()))
    excluded_ids.update(getattr(reduction, "excluded_record_ids", ()))
    retained_ids = set(
        getattr(
            reduction,
            "retained_delta_ids",
            getattr(reduction, "retained_event_ids", ()),
        )
    )
    issues = tuple(getattr(reduction, "issues", ()))
    issue_by_id: dict[str, str] = {}
    for issue in issues:
        issue_code = str(getattr(issue, "issue_code", "reducer_excluded"))
        for object_id in getattr(issue, "object_ids", ()):
            issue_by_id[str(object_id)] = issue_code

    reports_dispositions = bool(applied_ids or excluded_ids or retained_ids)
    for delta_id in eligible_ids:
        if not reports_dispositions:
            outcomes[delta_id] = DeltaOutcome(
                delta_id, DeltaDisposition.APPLIED, "applied"
            )
        elif delta_id in applied_ids:
            outcomes[delta_id] = DeltaOutcome(
                delta_id, DeltaDisposition.APPLIED, "applied"
            )
        elif delta_id in excluded_ids:
            outcomes[delta_id] = DeltaOutcome(
                delta_id,
                DeltaDisposition.EXCLUDED,
                f"reducer_excluded:{issue_by_id.get(delta_id, 'not_applicable')}",
            )
        elif delta_id in retained_ids:
            outcomes[delta_id] = DeltaOutcome(
                delta_id,
                DeltaDisposition.EXCLUDED,
                f"retained_without_state_update:{issue_by_id.get(delta_id, 'incomplete_numeric_evidence')}",
            )
        else:
            outcomes[delta_id] = DeltaOutcome(
                delta_id, DeltaDisposition.EXCLUDED, "reducer_did_not_apply"
            )
    return tuple(outcomes[item_id] for item_id in sorted(outcomes))


def reconstruct_state(
    query: GovernanceQuery,
    *,
    anchors: Iterable[AnchorCandidate[Any]] = (),
    deltas: Iterable[DeltaCandidate[Any]] = (),
    reducer: Reducer,
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
    anchor_required: bool = True,
    coverage_required: bool = False,
    eligibility: Eligibility | None = None,
) -> ReconstructionResult[Any]:
    """Run the fixed availability -> versions -> anchor -> delta -> reducer pipeline."""

    if not isinstance(query, GovernanceQuery):
        raise ReconstructionValidationError("reconstruct_state requires a normalized GovernanceQuery")
    anchor_selection = select_anchor(anchors, query)
    delta_selection = select_deltas(
        deltas,
        query,
        anchor=anchor_selection.selected,
        eligibility=eligibility,
    )
    anchor_payload = (
        anchor_selection.selected.payload if anchor_selection.selected is not None else None
    )
    reduction = reducer(
        anchor_payload,
        tuple(item.payload for item in delta_selection.ordered),
        query,
    )
    delta_outcomes = _reducer_outcomes(delta_selection, reduction)
    coverage_values = _visible_coverage(coverage, query)
    gap_values = _active_side_records(
        gaps,
        query,
        expected_type=GapRecord,
        id_attribute="gap_id",
        supersedes_attribute="supersedes_gap_id",
        identity_attributes=("company_id", "question_id"),
    )
    conflict_values = _active_side_records(
        conflicts,
        query,
        expected_type=ConflictRecord,
        id_attribute="conflict_id",
        supersedes_attribute="supersedes_conflict_id",
        identity_attributes=("company_id", "question_id", "subject_id", "predicate"),
    )
    reduction_conflicts = tuple(getattr(reduction, "conflicts", ()))
    if reduction_conflicts:
        conflict_values = tuple(
            sorted((*conflict_values, *reduction_conflicts), key=_stable_object_key)
        )
    issues = tuple(getattr(reduction, "issues", ()))
    reducer_status = CompletenessStatus(
        getattr(reduction, "completeness_status", CompletenessStatus.COMPLETE)
    )
    has_conflict = (
        reducer_status == CompletenessStatus.CONFLICTED
        or any(item.completeness_status == CompletenessStatus.CONFLICTED for item in coverage_values)
        or any(item.conflict_ids for item in coverage_values)
        or any(_active_conflict(item) for item in conflict_values)
    )
    has_gap = (
        bool(gap_values)
        or any(item.gap_ids for item in coverage_values)
        or any(item.completeness_status == CompletenessStatus.INCOMPLETE for item in coverage_values)
    )
    if has_conflict:
        completeness = CompletenessStatus.CONFLICTED
    elif (
        reducer_status == CompletenessStatus.INCOMPLETE
        or has_gap
        or (anchor_required and anchor_selection.selected is None)
        or (coverage_required and not coverage_values)
    ):
        completeness = CompletenessStatus.INCOMPLETE
    else:
        completeness = CompletenessStatus.COMPLETE
    return ReconstructionResult(
        query=query,
        state=reduction,
        anchor_id=(
            anchor_selection.selected.stable_id
            if anchor_selection.selected is not None
            else None
        ),
        anchor_outcomes=anchor_selection.outcomes,
        ordered_delta_ids=delta_selection.ordered_ids,
        delta_outcomes=delta_outcomes,
        coverage=coverage_values,
        gaps=gap_values,
        conflicts=conflict_values,
        issues=issues,
        completeness_status=completeness,
        future_knowledge_used=query.future_knowledge_used,
    )


def _role_anchor_parts(
    anchor: Any,
) -> tuple[RosterSnapshot | None, tuple[RoleTenure, ...]]:
    if anchor is None:
        return None, ()
    if isinstance(anchor, RosterSnapshot):
        return anchor, ()
    if isinstance(anchor, RoleTenure):
        return None, (anchor,)
    if isinstance(anchor, (tuple, list)):
        roster_values = tuple(
            item for item in anchor if isinstance(item, RosterSnapshot)
        )
        tenure_values = tuple(item for item in anchor if isinstance(item, RoleTenure))
        if len(roster_values) > 1 or len(roster_values) + len(tenure_values) != len(anchor):
            raise ReconstructionValidationError(
                "role anchor must contain at most one RosterSnapshot and only RoleTenure records"
            )
        return (roster_values[0] if roster_values else None), tenure_values
    roster = getattr(anchor, "roster", None)
    tenures = getattr(anchor, "tenures", None)
    if isinstance(roster, RosterSnapshot) and tenures is not None:
        tenure_values = tuple(tenures)
        if not all(isinstance(item, RoleTenure) for item in tenure_values):
            raise ReconstructionValidationError(
                "role anchor tenures must be RoleTenure records"
            )
        return roster, tenure_values
    raise ReconstructionValidationError(
        "role reconstruction requires a RosterSnapshot and/or RoleTenure anchor payload"
    )


def reconstruct_role_tenures(
    query: GovernanceQuery,
    *,
    anchors: Iterable[AnchorCandidate[Any]] = (),
    deltas: Iterable[DeltaCandidate[RoleDelta]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
) -> ReconstructionResult[Any]:
    normalized_deltas = _coalesce_delta_supersedes(deltas)
    coverage_values = tuple(coverage)
    visible_coverage = _visible_coverage(coverage_values, query)
    coverage_complete = bool(visible_coverage) and all(
        item.completeness_status == CompletenessStatus.COMPLETE
        and not item.gap_ids
        and not item.conflict_ids
        for item in visible_coverage
    )

    def reducer(anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        roster, anchor_tenures = _role_anchor_parts(anchor)
        return reduce_role_state(
            roster,
            anchor_tenures,
            tuple(_clear_resolved_payload_supersedes(value) for value in values),
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
            anchor_complete=anchor is not None,
            coverage_complete=coverage_complete,
        )

    return reconstruct_state(
        query,
        anchors=anchors,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage_values,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=True,
        coverage_required=True,
    )


def _numeric_anchor_value(anchor: Any) -> Decimal | None:
    if anchor is None or isinstance(anchor, Decimal):
        return anchor
    for attribute in ("balance", "share_count", "pledged_shares"):
        value = getattr(anchor, attribute, None)
        if value is not None:
            return value
    return None


def reconstruct_numeric_balance(
    query: GovernanceQuery,
    *,
    anchors: Iterable[AnchorCandidate[Any]] = (),
    deltas: Iterable[DeltaCandidate[NumericBalanceDelta]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
) -> ReconstructionResult[Any]:
    normalized_deltas = _coalesce_delta_supersedes(deltas)

    def reducer(anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        return reduce_numeric_balance(
            _numeric_anchor_value(anchor),
            tuple(_clear_resolved_payload_supersedes(value) for value in values),
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
        )

    return reconstruct_state(
        query,
        anchors=anchors,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=True,
        coverage_required=True,
    )


def _record_values(anchor: Any, deltas: tuple[Any, ...]) -> tuple[Any, ...]:
    if anchor is None:
        return deltas
    if isinstance(anchor, (tuple, list)):
        return (*anchor, *deltas)
    records = getattr(anchor, "records", None)
    if records is not None:
        return (*tuple(records), *deltas)
    return (anchor, *deltas)


def reconstruct_ownership_state(
    query: GovernanceQuery,
    *,
    anchors: Iterable[AnchorCandidate[OwnershipSnapshot]] = (),
    deltas: Iterable[DeltaCandidate[OwnershipPositionDelta]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
) -> ReconstructionResult[Any]:
    normalized_deltas = _coalesce_delta_supersedes(deltas)

    def reducer(anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        if anchor is not None and not isinstance(anchor, OwnershipSnapshot):
            raise ReconstructionValidationError(
                "ownership reconstruction requires an OwnershipSnapshot anchor"
            )
        return reduce_ownership_state(
            anchor,
            tuple(_clear_resolved_payload_supersedes(value) for value in values),
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
        )

    return reconstruct_state(
        query,
        anchors=anchors,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=True,
        coverage_required=True,
    )


def _pledge_anchor_values(anchor: Any) -> tuple[PledgePositionSnapshot, ...]:
    if anchor is None:
        return ()
    if isinstance(anchor, PledgePositionSnapshot):
        return (anchor,)
    if isinstance(anchor, (tuple, list)) and all(
        isinstance(item, PledgePositionSnapshot) for item in anchor
    ):
        return tuple(anchor)
    raise ReconstructionValidationError(
        "pledge reconstruction requires PledgePositionSnapshot anchors"
    )


def reconstruct_pledge_state(
    query: GovernanceQuery,
    *,
    anchors: Iterable[AnchorCandidate[Any]] = (),
    deltas: Iterable[DeltaCandidate[PledgePositionDelta]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
) -> ReconstructionResult[Any]:
    normalized_deltas = _coalesce_delta_supersedes(deltas)

    def reducer(anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        anchor_values = _pledge_anchor_values(anchor)
        anchor_complete = bool(anchor_values) and all(
            item.is_complete
            and item.completeness_status == CompletenessStatus.COMPLETE
            for item in anchor_values
        )
        return reduce_pledge_state(
            anchor_values,
            tuple(_clear_resolved_payload_supersedes(value) for value in values),
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
            anchor_complete=anchor_complete,
        )

    return reconstruct_state(
        query,
        anchors=anchors,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=True,
        coverage_required=True,
    )


def reconstruct_control_relations(
    query: GovernanceQuery,
    *,
    anchors: Iterable[AnchorCandidate[Any]] = (),
    deltas: Iterable[DeltaCandidate[Any]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
) -> ReconstructionResult[Any]:
    normalized_deltas = _prepare_record_deltas(deltas)

    def reducer(anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        records = _record_values(anchor, values)
        return project_control_state(
            records,
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
            known_at_precision=current.known_at.precision,
        )

    return reconstruct_state(
        query,
        anchors=anchors,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=True,
        coverage_required=True,
    )


def reconstruct_interval_records(
    query: GovernanceQuery,
    *,
    deltas: Iterable[DeltaCandidate[Any]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
) -> ReconstructionResult[Any]:
    delta_values = tuple(deltas)
    if any(
        not isinstance(item.payload, GovernanceRecordBase) for item in delta_values
    ):
        raise ReconstructionValidationError(
            "interval reconstruction accepts only typed GovernanceRecord payloads"
        )
    normalized_deltas = _prepare_record_deltas(delta_values)

    def reducer(_anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        return project_versioned_records(
            values,
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
            known_at_precision=current.known_at.precision,
        )

    return reconstruct_state(
        query,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=False,
        coverage_required=True,
    )


def _annual_period_end(value: Any) -> date | None:
    for attribute in ("fiscal_period_end", "report_period_end"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, datetime):
            return candidate.date()
        if isinstance(candidate, date):
            return candidate
    fiscal_year = getattr(value, "fiscal_year", None)
    if isinstance(fiscal_year, int) and not isinstance(fiscal_year, bool):
        return date(fiscal_year, 12, 31)
    return None


def reconstruct_annual_records(
    query: GovernanceQuery,
    *,
    deltas: Iterable[DeltaCandidate[Any]] = (),
    coverage: Iterable[CoverageState] = (),
    gaps: Iterable[Any] = (),
    conflicts: Iterable[Any] = (),
    fiscal_year: int | None = None,
) -> ReconstructionResult[Any]:
    delta_values = tuple(deltas)
    if any(
        not isinstance(item.payload, GovernanceRecordBase) for item in delta_values
    ):
        raise ReconstructionValidationError(
            "annual reconstruction accepts only typed GovernanceRecord payloads"
        )
    normalized_deltas = _prepare_record_deltas(delta_values)

    def eligibility(item: DeltaCandidate[Any], current: GovernanceQuery) -> str | None:
        period_end = _annual_period_end(item.payload)
        if (
            period_end is not None
            and not _instant_at_or_before(
                QueryInstant.parse(period_end), current.state_at
            )
        ):
            return "annual_period_after_state_at"
        return None

    def reducer(_anchor: Any, values: tuple[Any, ...], current: GovernanceQuery) -> Any:
        if fiscal_year is not None:
            return project_annual_records(
                values,
                fiscal_year=fiscal_year,
                state_at=current.state_cutoff,
                known_at=current.known_cutoff,
                known_at_precision=current.known_at.precision,
            )
        return project_versioned_records(
            values,
            state_at=current.state_cutoff,
            known_at=current.known_cutoff,
            known_at_precision=current.known_at.precision,
        )

    return reconstruct_state(
        query,
        deltas=normalized_deltas,
        reducer=reducer,
        coverage=coverage,
        gaps=gaps,
        conflicts=conflicts,
        anchor_required=False,
        coverage_required=True,
        eligibility=eligibility,
    )


def reconstruct_governance_state(
    state_kind: str,
    query: GovernanceQuery,
    **kwargs: Any,
) -> ReconstructionResult[Any]:
    """Small typed dispatcher; acquisition remains outside this module."""

    normalized = state_kind.strip().lower()
    if normalized in {"roster", "role", "role_tenure"}:
        return reconstruct_role_tenures(query, **kwargs)
    if normalized in {"ownership", "ownership_balance"}:
        return reconstruct_ownership_state(query, **kwargs)
    if normalized in {"pledge", "pledge_balance"}:
        return reconstruct_pledge_state(query, **kwargs)
    if normalized in {"control", "control_relation", "control_chain"}:
        return reconstruct_control_relations(query, **kwargs)
    if normalized in {"annual", "annual_record"}:
        return reconstruct_annual_records(query, **kwargs)
    if normalized in {"interval", "typed_record"}:
        return reconstruct_interval_records(query, **kwargs)
    raise ReconstructionValidationError(f"unsupported governance state kind: {state_kind}")


__all__ = [
    "AnchorCandidate",
    "AnchorOutcome",
    "AnchorSelection",
    "CoverageState",
    "DeltaCandidate",
    "DeltaOutcome",
    "DeltaSelection",
    "GovernanceQuery",
    "QueryInstant",
    "ReconstructionResult",
    "ReconstructionValidationError",
    "VersionDecision",
    "VersionGraphError",
    "VersionSelection",
    "VersionedItem",
    "normalize_governance_query",
    "normalize_query_instant",
    "reconstruct_annual_records",
    "reconstruct_control_relations",
    "reconstruct_governance_state",
    "reconstruct_interval_records",
    "reconstruct_numeric_balance",
    "reconstruct_ownership_state",
    "reconstruct_pledge_state",
    "reconstruct_role_tenures",
    "reconstruct_state",
    "resolve_visible_versions",
    "select_anchor",
    "select_deltas",
]
