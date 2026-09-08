from dataclasses import asdict
from datetime import date, datetime, timezone

import pytest

from analysis.structured.coverage import (
    Applicability,
    CoverageState,
    GroupMode,
    InputReadiness,
    RequirementInput,
    aggregate_coverage,
    evaluate_question,
    expand_analysis_scope,
)


def requirement(
    requirement_id,
    readiness,
    *,
    group="default",
    mode=GroupMode.ALL_OF,
    optional=False,
    applicability=Applicability.TRUE,
    value_present=False,
    value=None,
    evidence=(),
):
    return RequirementInput(
        requirement_id,
        "Y2025",
        readiness,
        applicability,
        group,
        mode,
        optional,
        value_present,
        value,
        None,
        tuple(evidence),
    )


def test_required_groups_optional_inputs_and_explicit_zero_are_distinct():
    result = evaluate_question(
        step_id="ES02",
        question_id="ES02.Q02",
        applicability=Applicability.TRUE,
        inputs=(
            requirement(
                "revenue",
                InputReadiness.READY,
                value_present=True,
                value=0,
                evidence=("fact:revenue",),
            ),
            requirement(
                "profit-primary",
                InputReadiness.SOURCE_FAILED,
                group="profit",
                mode=GroupMode.ANY_OF,
            ),
            requirement(
                "profit-equivalent",
                InputReadiness.READY,
                group="profit",
                mode=GroupMode.ANY_OF,
                evidence=("fact:profit",),
            ),
            requirement(
                "optional-forecast",
                InputReadiness.SOURCE_EMPTY,
                optional=True,
            ),
        ),
        method_status="skeleton",
    )
    assert result.state == CoverageState.READY
    assert result.missing_requirement_ids == ()
    assert result.optional_missing_ids == ("optional-forecast",)
    assert result.method_status == "skeleton"


def test_empty_unknown_and_not_applicable_need_independent_evidence():
    pending = evaluate_question(
        step_id="ES03",
        question_id="ES03.Q02",
        applicability=Applicability.TRUE,
        inputs=(requirement("pledge", InputReadiness.SOURCE_EMPTY),),
    )
    assert pending.state == CoverageState.PENDING
    assert pending.reason_codes == (InputReadiness.SOURCE_EMPTY.value,)

    with pytest.raises(ValueError, match="needs rule/material evidence"):
        requirement(
            "pledge",
            InputReadiness.SOURCE_EMPTY,
            applicability=Applicability.FALSE,
        )
    not_applicable = evaluate_question(
        step_id="ES03",
        question_id="ES03.Q02",
        applicability=Applicability.FALSE,
        applicability_evidence_ids=("annual-report:no-pledge",),
        inputs=(
            requirement(
                "pledge",
                InputReadiness.SOURCE_EMPTY,
                applicability=Applicability.FALSE,
                evidence=("annual-report:no-pledge",),
            ),
        ),
    )
    assert not_applicable.state == CoverageState.NOT_APPLICABLE
    assert not_applicable.not_applicable_requirement_ids == ("pledge",)


def test_unknown_applicability_is_pending_and_optional_cannot_raise_ratio():
    unknown = evaluate_question(
        step_id="ES05",
        question_id="ES05.Q04",
        applicability=Applicability.UNKNOWN,
        inputs=(requirement("industry-input", InputReadiness.READY, evidence=("fact:1",)),),
    )
    ready = evaluate_question(
        step_id="ES01",
        question_id="ES01.Q01",
        applicability=Applicability.TRUE,
        inputs=(requirement("identity", InputReadiness.READY, evidence=("B08:1",)),),
    )
    summary = aggregate_coverage((unknown, ready), all_step_ids=tuple(f"ES0{i}" for i in range(1, 9)))
    assert unknown.state == CoverageState.PENDING
    assert summary.ready_question_count == 1
    assert summary.applicable_or_unknown_question_count == 2
    assert summary.readiness_ratio == 0.5
    assert len(summary.steps) == 8


def test_analysis_window_expands_yoy_and_ttm_without_shortening_acquisition():
    scope = expand_analysis_scope(
        as_of=datetime(2026, 9, 8, tzinfo=timezone.utc),
        published_years=("2020", "2021", "2022", "2023", "2024", "2025"),
        published_quarters=tuple(f"{year}Q{quarter}" for year in range(2022, 2027) for quarter in range(1, 5)),
        published_months=("2026-07", "2026-08", "2026-09"),
        listing_date=date(2023, 1, 1),
        incomplete_event_ids=("event:open",),
    )
    assert scope.acquisition_history_mode == "all_available"
    assert len(scope.financial_quarters) == 12
    assert "2023Q1" in scope.dependency_periods
    assert "event:open" in scope.incomplete_event_ids
    assert all(item not in scope.financial_quarters for item in ("2022Q1", "2022Q4"))
    assert scope.excluded_pre_listing_periods


def test_unpublished_and_missing_formula_periods_stay_explicit():
    scope = expand_analysis_scope(
        as_of=datetime(2026, 5, 1, tzinfo=timezone.utc),
        published_years=("2024", "2025"),
        published_quarters=("2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q4"),
    )
    assert "2026Q4" not in scope.financial_quarters
    assert "2024Q1" in scope.missing_dependency_periods


def test_more_than_500_evaluations_keep_last_missing_item():
    rows = [
        requirement(
            f"requirement-{index:03d}",
            InputReadiness.READY if index < 500 else InputReadiness.READING_PENDING,
            evidence=(f"fact:{index}",) if index < 500 else (),
        )
        for index in range(501)
    ]
    result = evaluate_question(
        step_id="ES08",
        question_id="ES08.Q01",
        applicability=Applicability.TRUE,
        inputs=rows,
    )
    assert result.state == CoverageState.PENDING
    assert len(result.required_requirement_ids) == 501
    assert result.missing_requirement_ids == ("requirement-500",)
    assert asdict(result)["missing_requirement_ids"][-1] == "requirement-500"
