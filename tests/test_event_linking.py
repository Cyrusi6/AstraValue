from datetime import datetime, timedelta, timezone

from analysis.event_linking import derive_event_identity, link_event_series
from analysis.models import EventRecord


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(
    event_id: str,
    event_type: str,
    state: str,
    day: int,
    summary: str,
    *,
    event_terms=None,
) -> EventRecord:
    timestamp = BASE_TIME + timedelta(days=day)
    return EventRecord(
        event_id=event_id,
        ticker="600519",
        event_type=event_type,
        announced_at=timestamp,
        available_at=timestamp,
        lifecycle_state=state,
        summary=summary,
        event_terms=event_terms or {},
        status_updated_at=timestamp,
        data_snapshot_id=f"sync-{day}",
    )


def test_dividend_series_links_by_explicit_fiscal_period():
    proposal = _event(
        "dividend-proposal",
        "dividend",
        "proposal",
        0,
        "2025年年度利润分配方案",
    )
    registration = _event(
        "dividend-registration",
        "dividend",
        "registration",
        30,
        "2025年年度权益分派实施公告",
    )
    implemented = _event(
        "dividend-implemented",
        "dividend",
        "implemented",
        40,
        "2025年年度权益分派实施完成公告",
    )
    linked = link_event_series([implemented, proposal, registration])

    assert derive_event_identity(proposal) == "dividend:2025:年度"
    assert [item.event_id for item in linked] == [
        "dividend-proposal",
        "dividend-registration",
        "dividend-implemented",
    ]
    assert linked[1].previous_event_id == linked[0].event_id
    assert linked[2].previous_event_id == linked[1].event_id
    assert {item.root_event_id for item in linked} == {"dividend-proposal"}
    assert linked[2].metadata["root_matching"] == "explicit_identity"
    assert linked[2].metadata["root_matching_confidence"] == 1.0


def test_unique_compatible_open_event_can_link_without_guessing_identity():
    plan = _event(
        "repurchase-plan",
        "repurchase",
        "plan",
        0,
        "关于回购股份的方案",
    )
    progress = _event(
        "repurchase-progress",
        "repurchase",
        "in_progress",
        10,
        "关于首次回购股份的公告",
    )
    linked = link_event_series([progress, plan])

    assert linked[1].root_event_id == plan.event_id
    assert linked[1].previous_event_id == plan.event_id
    assert linked[1].metadata["root_matching"] == "unique_compatible_open_event"
    assert linked[1].metadata["root_matching_confidence"] == 0.7


def test_2024_repurchase_price_cap_update_keeps_identity_for_completion():
    plan = _event(
        "repurchase-2024-plan",
        "repurchase",
        "plan",
        0,
        "关于以集中竞价方式回购公司股份方案的公告",
        event_terms={"plan_id": "2024-09-21"},
    )
    price_cap_update = _event(
        "repurchase-2024-price-cap",
        "repurchase",
        "in_progress",
        30,
        "关于调整回购股份价格上限的公告",
    )
    completion = _event(
        "repurchase-2024-completion",
        "repurchase",
        "completed",
        60,
        "关于股份回购实施结果暨股份变动的公告",
        event_terms={"plan_id": "2024-09-21"},
    )

    linked = link_event_series([completion, price_cap_update, plan])

    assert {item.root_event_id for item in linked} == {plan.event_id}
    assert linked[1].metadata["event_identity"] == "repurchase:plan_id:2024-09-21"
    assert (
        linked[1].metadata["event_identity_origin"]
        == "inherited_unique_open_match"
    )
    assert linked[1].metadata["event_identity_inherited_from"] == plan.event_id
    assert linked[2].previous_event_id == price_cap_update.event_id
    assert linked[2].metadata["root_matching"] == "explicit_identity"


def test_2025_repurchase_plan_report_progress_and_completion_share_one_root():
    identity = {"plan_id": "2025-11-06"}
    plan = _event(
        "repurchase-2025-plan",
        "repurchase",
        "plan",
        0,
        "关于以集中竞价交易方式回购公司股份方案的公告",
        event_terms=identity,
    )
    report = _event(
        "repurchase-2025-report",
        "repurchase",
        "plan",
        5,
        "关于以集中竞价交易方式回购公司股份的回购报告书",
        event_terms=identity,
    )
    monthly_progress = _event(
        "repurchase-2025-monthly",
        "repurchase",
        "in_progress",
        35,
        "关于回购公司股份的进展公告",
        event_terms=identity,
    )
    completion = _event(
        "repurchase-2025-completion",
        "repurchase",
        "completed",
        100,
        "关于股份回购实施结果暨股份变动的公告",
        event_terms=identity,
    )

    linked = link_event_series([completion, report, monthly_progress, plan])

    assert [item.previous_event_id for item in linked] == [
        None,
        plan.event_id,
        report.event_id,
        monthly_progress.event_id,
    ]
    assert {item.root_event_id for item in linked} == {plan.event_id}


def test_holding_change_supporting_loan_and_result_continue_original_plan():
    identity = {"holder_id": "贵州茅台集团"}
    plan = _event(
        "increase-plan",
        "holding_change",
        "plan",
        0,
        "控股股东增持股份计划公告",
        event_terms=identity,
    )
    loan_commitment = _event(
        "increase-loan",
        "holding_change",
        "in_progress",
        20,
        "收到增持股份专项贷款承诺函的公告",
    )
    result = _event(
        "increase-result",
        "holding_change",
        "completed",
        90,
        "控股股东增持股份结果公告",
        event_terms=identity,
    )

    linked = link_event_series([result, loan_commitment, plan])

    assert {item.root_event_id for item in linked} == {plan.event_id}
    assert linked[1].previous_event_id == plan.event_id
    assert linked[1].metadata["event_identity"] == (
        "holding_change:holder_id:贵州茅台集团"
    )
    assert linked[2].previous_event_id == loan_commitment.event_id
    assert linked[2].metadata["root_matching"] == "explicit_identity"


def test_ambiguous_open_events_remain_independent():
    first_plan = _event(
        "repurchase-plan-a",
        "repurchase",
        "plan",
        0,
        "第一期回购股份方案",
    )
    second_plan = _event(
        "repurchase-plan-b",
        "repurchase",
        "plan",
        1,
        "第二期回购股份方案",
    )
    completion = _event(
        "repurchase-completion",
        "repurchase",
        "completed",
        10,
        "回购股份实施结果公告",
    )
    linked = link_event_series([completion, second_plan, first_plan])
    final = linked[-1]

    assert final.root_event_id == final.event_id
    assert final.previous_event_id is None
    assert final.metadata["root_matching"] == "ambiguous"
    assert set(final.metadata["root_matching_candidates"]) == {
        first_plan.event_id,
        second_plan.event_id,
    }


def test_event_terms_select_the_correct_root_when_multiple_are_open():
    first_plan = _event(
        "plan-a",
        "repurchase",
        "plan",
        0,
        "第一期回购计划",
        event_terms={"plan_id": "A-2026"},
    )
    second_plan = _event(
        "plan-b",
        "repurchase",
        "plan",
        1,
        "第二期回购计划",
        event_terms={"plan_id": "B-2026"},
    )
    completion = _event(
        "completion-b",
        "repurchase",
        "completed",
        10,
        "第二期回购完成",
        event_terms={"plan_id": "B-2026"},
    )
    linked = link_event_series([completion, first_plan, second_plan])
    final = linked[-1]

    assert final.root_event_id == second_plan.event_id
    assert final.previous_event_id == second_plan.event_id
    assert final.metadata["root_matching"] == "explicit_identity"


def test_pledge_is_not_linked_without_a_specific_identity():
    pledged = _event(
        "pledge-a",
        "pledge",
        "pledged",
        0,
        "股东部分股份质押",
    )
    released = _event(
        "release-unknown",
        "pledge",
        "released",
        10,
        "股东股份解除质押",
    )
    linked = link_event_series([released, pledged])
    assert linked[-1].root_event_id == linked[-1].event_id
    assert linked[-1].metadata["root_matching"] == "new_root"
