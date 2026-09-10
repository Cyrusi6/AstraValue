from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .storage import stable_structured_id


class Applicability(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class InputReadiness(str, Enum):
    READY = "ready"
    SOURCE_EMPTY = "source_empty"
    SOURCE_FAILED = "source_failed"
    NOT_DISCLOSED = "not_disclosed"
    UNMAPPED = "unmapped"
    DEFINITION_UNKNOWN = "definition_unknown"
    READING_PENDING = "reading_pending"
    STALE = "stale"
    FORMULA_INPUT_MISSING = "formula_input_missing"
    UNSUPPORTED = "unsupported"
    APPLICABILITY_UNKNOWN = "applicability_unknown"


class CoverageState(str, Enum):
    READY = "ready"
    PENDING = "pending"
    NOT_APPLICABLE = "not_applicable"


class GroupMode(str, Enum):
    ALL_OF = "all_of"
    ANY_OF = "any_of"


@dataclass(frozen=True)
class RequirementInput:
    requirement_id: str
    period_key: str
    readiness: InputReadiness
    applicability: Applicability = Applicability.TRUE
    required_group_id: str = "default"
    group_mode: GroupMode = GroupMode.ALL_OF
    optional: bool = False
    value_present: bool = False
    value: Any = None
    reason_code: str | None = None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.requirement_id or not self.period_key:
            raise ValueError("requirement_id and period_key are required")
        if self.readiness == InputReadiness.READY:
            # Zero, False, and the empty string may be legitimate disclosed
            # values; readiness therefore relies on an explicit presence bit.
            if not self.value_present and not self.evidence_ids:
                raise ValueError("ready requirement must have a value or evidence")
        if self.readiness != InputReadiness.READY and self.value_present:
            raise ValueError("non-ready requirement cannot claim a usable value")
        if self.applicability == Applicability.FALSE and not self.evidence_ids:
            raise ValueError("not-applicable requirement needs rule/material evidence")

    def evaluation_payload(
        self,
        *,
        coverage_snapshot_id: str,
        company_id: str,
        step_id: str,
        question_id: str,
        evaluated_at: datetime,
    ) -> dict[str, Any]:
        readiness = (
            InputReadiness.APPLICABILITY_UNKNOWN.value
            if self.applicability == Applicability.UNKNOWN
            else self.readiness.value
        )
        identity = {
            "coverage_snapshot_id": coverage_snapshot_id,
            "requirement_id": self.requirement_id,
            "period_key": self.period_key,
        }
        return {
            "evaluation_id": stable_structured_id("reqeval", identity),
            "coverage_snapshot_id": coverage_snapshot_id,
            "company_id": company_id,
            "step_id": step_id,
            "question_id": question_id,
            "requirement_id": self.requirement_id,
            "period_key": self.period_key,
            "applicability": self.applicability.value,
            "readiness": readiness,
            "required_group_id": self.required_group_id,
            "group_mode": self.group_mode.value,
            "optional": self.optional,
            "reason_code": self.reason_code,
            "evidence_ids": list(self.evidence_ids),
            "value_present": self.value_present,
            "value": self.value if self.value_present else None,
            "evaluated_at": _aware_utc(evaluated_at).isoformat(),
        }


@dataclass(frozen=True)
class QuestionCoverage:
    step_id: str
    question_id: str
    state: CoverageState
    applicability: Applicability
    required_requirement_ids: tuple[str, ...]
    ready_requirement_ids: tuple[str, ...]
    missing_requirement_ids: tuple[str, ...]
    not_applicable_requirement_ids: tuple[str, ...]
    optional_ready_ids: tuple[str, ...]
    optional_missing_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    method_status: str = "unknown"
    assumption_status: str = "unknown"
    analysis_status: str = "not_started"


@dataclass(frozen=True)
class StepCoverage:
    step_id: str
    state: CoverageState
    ready_question_ids: tuple[str, ...]
    pending_question_ids: tuple[str, ...]
    not_applicable_question_ids: tuple[str, ...]


@dataclass(frozen=True)
class CoverageSummary:
    questions: tuple[QuestionCoverage, ...]
    steps: tuple[StepCoverage, ...]
    ready_question_count: int
    applicable_or_unknown_question_count: int
    not_applicable_question_count: int
    readiness_ratio: float | None


def evaluate_question(
    *,
    step_id: str,
    question_id: str,
    applicability: Applicability,
    inputs: Iterable[RequirementInput],
    applicability_evidence_ids: Sequence[str] = (),
    method_status: str = "unknown",
    assumption_status: str = "unknown",
    analysis_status: str = "not_started",
) -> QuestionCoverage:
    values = tuple(inputs)
    if not values:
        raise ValueError("question must keep a non-empty requirement set")
    if applicability == Applicability.FALSE:
        if not applicability_evidence_ids:
            raise ValueError("question not_applicable needs rule/material evidence")
        return QuestionCoverage(
            step_id=step_id,
            question_id=question_id,
            state=CoverageState.NOT_APPLICABLE,
            applicability=applicability,
            required_requirement_ids=tuple(
                sorted({item.requirement_id for item in values if not item.optional})
            ),
            ready_requirement_ids=(),
            missing_requirement_ids=(),
            not_applicable_requirement_ids=tuple(
                sorted({item.requirement_id for item in values})
            ),
            optional_ready_ids=(),
            optional_missing_ids=(),
            reason_codes=(),
            method_status=method_status,
            assumption_status=assumption_status,
            analysis_status=analysis_status,
        )
    if applicability == Applicability.UNKNOWN:
        return QuestionCoverage(
            step_id=step_id,
            question_id=question_id,
            state=CoverageState.PENDING,
            applicability=applicability,
            required_requirement_ids=tuple(
                sorted({item.requirement_id for item in values if not item.optional})
            ),
            ready_requirement_ids=(),
            missing_requirement_ids=tuple(
                sorted({item.requirement_id for item in values if not item.optional})
            ),
            not_applicable_requirement_ids=(),
            optional_ready_ids=(),
            optional_missing_ids=tuple(
                sorted({item.requirement_id for item in values if item.optional})
            ),
            reason_codes=(InputReadiness.APPLICABILITY_UNKNOWN.value,),
            method_status=method_status,
            assumption_status=assumption_status,
            analysis_status=analysis_status,
        )

    optional_missing = {
        item.requirement_id
        for item in values
        if item.optional
        and (
            item.applicability != Applicability.FALSE
            and item.readiness != InputReadiness.READY
        )
    }
    optional_ready = {
        item.requirement_id
        for item in values
        if item.optional
        and item.applicability == Applicability.TRUE
        and item.readiness == InputReadiness.READY
    }
    required = tuple(item for item in values if not item.optional)
    if not required:
        raise ValueError("question must keep a non-empty required set")

    groups: dict[str, list[RequirementInput]] = {}
    for item in required:
        groups.setdefault(item.required_group_id, []).append(item)
    group_results: list[bool] = []
    ready_ids: set[str] = set()
    missing_ids: set[str] = set()
    not_applicable_ids: set[str] = set()
    reasons: set[str] = set()
    for group_id, members in groups.items():
        modes = {item.group_mode for item in members}
        if len(modes) != 1:
            raise ValueError(f"required group mixes modes: {group_id}")
        mode = next(iter(modes))
        member_ready: list[bool] = []
        for item in members:
            ready = (
                item.applicability == Applicability.FALSE
                or (
                    item.applicability == Applicability.TRUE
                    and item.readiness == InputReadiness.READY
                )
            )
            member_ready.append(ready)
            if item.applicability == Applicability.FALSE:
                not_applicable_ids.add(item.requirement_id)
                continue
            if ready:
                ready_ids.add(item.requirement_id)
            else:
                missing_ids.add(item.requirement_id)
                reasons.add(
                    InputReadiness.APPLICABILITY_UNKNOWN.value
                    if item.applicability == Applicability.UNKNOWN
                    else (item.reason_code or item.readiness.value)
                )
        group_ready = all(member_ready) if mode == GroupMode.ALL_OF else any(member_ready)
        group_results.append(group_ready)
        if group_ready and mode == GroupMode.ANY_OF:
            # A missing alternative in a semantically equivalent any_of group
            # is not a question-level gap once one route is ready.
            missing_ids.difference_update(item.requirement_id for item in members)
    state = CoverageState.READY if all(group_results) else CoverageState.PENDING
    return QuestionCoverage(
        step_id=step_id,
        question_id=question_id,
        state=state,
        applicability=applicability,
        required_requirement_ids=tuple(
            sorted({item.requirement_id for item in required})
        ),
        ready_requirement_ids=tuple(sorted(ready_ids)),
        missing_requirement_ids=tuple(sorted(missing_ids)),
        not_applicable_requirement_ids=tuple(sorted(not_applicable_ids)),
        optional_ready_ids=tuple(sorted(optional_ready)),
        optional_missing_ids=tuple(sorted(optional_missing)),
        reason_codes=tuple(sorted(reasons)),
        method_status=method_status,
        assumption_status=assumption_status,
        analysis_status=analysis_status,
    )


def aggregate_coverage(
    questions: Iterable[QuestionCoverage],
    *,
    all_step_ids: Sequence[str],
) -> CoverageSummary:
    values = tuple(questions)
    by_step: dict[str, list[QuestionCoverage]] = {step: [] for step in all_step_ids}
    for item in values:
        if item.step_id not in by_step:
            raise ValueError(f"question references unknown step: {item.step_id}")
        by_step[item.step_id].append(item)
    steps: list[StepCoverage] = []
    for step_id in all_step_ids:
        items = by_step[step_id]
        ready = tuple(item.question_id for item in items if item.state == CoverageState.READY)
        pending = tuple(
            item.question_id for item in items if item.state == CoverageState.PENDING
        )
        not_applicable = tuple(
            item.question_id
            for item in items
            if item.state == CoverageState.NOT_APPLICABLE
        )
        if not items:
            state = CoverageState.PENDING
        elif len(not_applicable) == len(items):
            state = CoverageState.NOT_APPLICABLE
        elif pending:
            state = CoverageState.PENDING
        else:
            state = CoverageState.READY
        steps.append(StepCoverage(step_id, state, ready, pending, not_applicable))
    ready_count = sum(item.state == CoverageState.READY for item in values)
    applicable_count = sum(
        item.state != CoverageState.NOT_APPLICABLE for item in values
    )
    na_count = len(values) - applicable_count
    return CoverageSummary(
        values,
        tuple(steps),
        ready_count,
        applicable_count,
        na_count,
        None if applicable_count == 0 else ready_count / applicable_count,
    )


@dataclass(frozen=True)
class AnalysisScope:
    as_of: datetime
    acquisition_history_mode: str
    now: tuple[str, ...]
    financial_years: tuple[str, ...]
    financial_quarters: tuple[str, ...]
    business_periods: tuple[str, ...]
    event_start: date
    incomplete_event_ids: tuple[str, ...]
    industry_years: tuple[str, ...]
    industry_months: tuple[str, ...]
    scenario_periods: tuple[str, ...]
    dependency_periods: tuple[str, ...]
    missing_dependency_periods: tuple[str, ...]
    excluded_pre_listing_periods: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "acquisition_history_mode": self.acquisition_history_mode,
            "NOW": list(self.now),
            "FIN": {
                "years": list(self.financial_years),
                "quarters": list(self.financial_quarters),
            },
            "BIZ": list(self.business_periods),
            "EVT": {
                "start": self.event_start.isoformat(),
                "incomplete_event_ids": list(self.incomplete_event_ids),
            },
            "IND": {
                "years": list(self.industry_years),
                "months": list(self.industry_months),
            },
            "SCN": list(self.scenario_periods),
            "formula_dependencies": list(self.dependency_periods),
            "missing_formula_dependencies": list(self.missing_dependency_periods),
            "excluded_pre_listing_periods": list(self.excluded_pre_listing_periods),
        }


def expand_analysis_scope(
    *,
    as_of: datetime,
    published_years: Iterable[str],
    published_quarters: Iterable[str],
    published_months: Iterable[str] = (),
    listing_date: date | None = None,
    user_start: date | None = None,
    user_end: date | None = None,
    incomplete_event_ids: Iterable[str] = (),
    scenario_periods: Iterable[str] = (),
    include_yoy_dependencies: bool = True,
    include_ttm_dependencies: bool = True,
) -> AnalysisScope:
    """Expand NOW/FIN/BIZ/EVT/IND/SCN without narrowing acquisition history."""

    cutoff = _aware_utc(as_of)
    end_date = min(user_end, cutoff.date()) if user_end else cutoff.date()
    years = sorted({_normalize_year(item) for item in published_years})
    quarters = sorted({_normalize_quarter(item) for item in published_quarters}, key=_quarter_index)
    months = sorted({_normalize_month(item) for item in published_months})

    excluded: set[str] = set()

    def eligible(period: str) -> bool:
        period_end = _period_end(period)
        if period_end > end_date:
            return False
        if listing_date is not None and period_end < listing_date:
            excluded.add(period)
            return False
        if user_start is not None and period_end < user_start:
            return False
        return True

    selected_years = tuple(item for item in years if eligible(item))[-5:]
    selected_quarters = tuple(item for item in quarters if eligible(item))[-12:]

    business: set[str] = set(selected_years)
    semiannual = [item for item in quarters if item.endswith("Q2") and eligible(item)]
    if semiannual:
        latest_semi = semiannual[-1]
        business.add(latest_semi)
        prior = _shift_quarter(latest_semi, -4)
        if prior in quarters and eligible(prior):
            business.add(prior)

    dependency_candidates: set[str] = set()
    if include_yoy_dependencies:
        dependency_candidates.update(
            f"Y{int(item[1:]) - 1}" for item in selected_years
        )
        dependency_candidates.update(
            _shift_quarter(item, -4) for item in selected_quarters
        )
    if include_ttm_dependencies:
        for item in selected_quarters:
            dependency_candidates.update(_shift_quarter(item, -offset) for offset in (1, 2, 3))
    available_periods = set(years) | set(quarters)
    dependencies = tuple(sorted(dependency_candidates, key=_mixed_period_index))
    missing_dependencies = tuple(
        item for item in dependencies if item not in available_periods
    )

    event_floor = _subtract_years(end_date, 5)
    if listing_date is not None and listing_date > event_floor:
        event_floor = listing_date
    if user_start is not None and user_start > event_floor:
        event_floor = user_start

    selected_months = tuple(item for item in months if eligible(item))[-60:]
    return AnalysisScope(
        cutoff,
        "all_available",
        (cutoff.isoformat(),),
        selected_years,
        selected_quarters,
        tuple(sorted(business, key=_mixed_period_index)),
        event_floor,
        tuple(dict.fromkeys(str(item) for item in incomplete_event_ids)),
        selected_years,
        selected_months,
        tuple(dict.fromkeys(str(item) for item in scenario_periods)),
        dependencies,
        missing_dependencies,
        tuple(sorted(excluded, key=_mixed_period_index)),
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normalize_year(value: str) -> str:
    raw = str(value).upper().removeprefix("Y")
    year = int(raw)
    if year < 1900 or year > 9999:
        raise ValueError(f"invalid year period: {value}")
    return f"Y{year:04d}"


def _normalize_quarter(value: str) -> str:
    raw = str(value).upper().replace("-", "")
    if "Q" not in raw:
        raise ValueError(f"invalid quarter period: {value}")
    year, quarter = raw.split("Q", 1)
    if int(quarter) not in {1, 2, 3, 4}:
        raise ValueError(f"invalid quarter period: {value}")
    return f"{int(year):04d}Q{int(quarter)}"


def _normalize_month(value: str) -> str:
    parsed = datetime.strptime(str(value), "%Y-%m")
    return parsed.strftime("%Y-%m")


def _quarter_index(value: str) -> int:
    return int(value[:4]) * 4 + int(value[-1]) - 1


def _shift_quarter(value: str, offset: int) -> str:
    index = _quarter_index(value) + offset
    return f"{index // 4:04d}Q{index % 4 + 1}"


def _mixed_period_index(value: str) -> tuple[int, int, str]:
    if value.startswith("Y"):
        return int(value[1:]), 4, value
    if "Q" in value:
        return int(value[:4]), int(value[-1]), value
    return 0, 0, value


def _period_end(value: str) -> date:
    if value.startswith("Y"):
        return date(int(value[1:]), 12, 31)
    if "Q" in value:
        year = int(value[:4])
        quarter = int(value[-1])
        return (
            date(year, 3, 31),
            date(year, 6, 30),
            date(year, 9, 30),
            date(year, 12, 31),
        )[quarter - 1]
    parsed = datetime.strptime(value, "%Y-%m")
    if parsed.month == 12:
        return date(parsed.year, 12, 31)
    return date(parsed.year, parsed.month + 1, 1) - timedelta(days=1)


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)
