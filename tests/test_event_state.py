from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from analysis.event_state import (
    EventTaxonomyError,
    EventTransitionError,
    classify_announcement,
    current_event_versions,
    link_event_update,
    load_event_taxonomy,
    validate_transition,
)
from analysis.models import EventRecord


BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("title", "event_type", "subtype", "state"),
    [
        ("2025年年度权益分派实施公告", "dividend", "implementation", "registration"),
        ("差异化权益分派事项的法律意见书", "dividend", "supporting_disclosure", "registration"),
        ("关于以集中竞价方式首次回购股份的公告", "repurchase", "progress", "in_progress"),
        ("关于回购股份实施进展的公告", "repurchase", "progress", "in_progress"),
        ("关于调整回购股份价格上限的公告", "repurchase", "supporting_disclosure", "in_progress"),
        ("回购股份事项前十大股东持股情况公告", "repurchase", "supporting_disclosure", "in_progress"),
        ("关于回购股份事项通知债权人公告", "repurchase", "supporting_disclosure", "in_progress"),
        ("关于以集中竞价方式回购公司股份的回购报告书", "repurchase", "plan_document", "in_progress"),
        ("关于以集中竞价方式回购公司股份方案的公告", "repurchase", "plan", "plan"),
        ("控股股东部分股份补充质押的公告", "pledge", "supplementary", "supplementary_pledge"),
        ("股东减持计划实施完成的公告", "holding_change", "completion", "completed"),
        ("控股股东增持股份结果公告", "holding_change", "completion", "completed"),
        ("控股股东取得增持股份贷款承诺函的公告", "holding_change", "financing_support", "in_progress"),
        ("向特定对象发行股票获得同意注册批复", "financing", "regulatory", "registered"),
        ("重大资产重组标的资产完成交割公告", "merger_acquisition", "closing", "closing"),
        ("关于收到行政处罚决定书的公告", "regulatory_penalty", "decision", "decision"),
        ("关于重大诉讼收到一审判决的公告", "litigation", "judgement", "judgement"),
        ("关于聘任总经理的公告", "management_change", "appointment", "effective"),
        ("关于聘任董事会秘书的公告", "management_change", "appointment", "effective"),
        ("关于职工董事选举结果的公告", "management_change", "appointment", "announced"),
        ("关于预计年度日常关联交易的公告", "related_party_transaction", "proposal", "proposal"),
        ("关于集团财务有限公司的风险评估报告", "related_party_transaction", "proposal", "proposal"),
        ("关于会计政策变更的公告", "accounting_governance", "accounting_policy", "announced"),
        ("第五届董事会第三次会议决议公告", "governance_resolution", "board_resolution", "approved"),
        ("董事、高级管理人员考核和薪酬管理办法", "governance_policy", "remuneration", "effective"),
        ("重大事项公告", "material_announcement", "announcement", "announced"),
        ("生产线项目投产完成公告", "operating_event", "commissioning", "completed"),
    ],
)
def test_classification_and_state_inference(title, event_type, subtype, state):
    result = classify_announcement(title)
    assert result.event_type == event_type
    assert result.subtype == subtype
    assert result.lifecycle_state == state
    assert result.matched_patterns
    assert result.taxonomy_version == "1.2.0"


def test_unknown_announcement_is_not_forced_into_an_event():
    result = classify_announcement("关于召开年度股东大会的通知")
    assert result.event_type == "other"
    assert result.lifecycle_state == "unclassified"
    assert result.score == 0


def test_taxonomy_structure_and_transition_validation():
    taxonomy = load_event_taxonomy()
    assert validate_transition(
        "repurchase",
        "plan",
        "in_progress",
        taxonomy=taxonomy,
    )
    assert not validate_transition(
        "repurchase",
        "completed",
        "in_progress",
        taxonomy=taxonomy,
    )
    assert validate_transition(
        "repurchase",
        "in_progress",
        "in_progress",
        taxonomy=taxonomy,
    )

    invalid = deepcopy(taxonomy)
    invalid["event_types"]["repurchase"]["transitions"]["plan"].append("missing")
    from analysis.event_state import _validate_taxonomy

    with pytest.raises(EventTaxonomyError, match="未知目标状态"):
        _validate_taxonomy(invalid)


def _event(
    event_id: str,
    state: str,
    offset_days: int,
    *,
    event_type: str = "repurchase",
) -> EventRecord:
    available_at = BASE_TIME + timedelta(days=offset_days)
    return EventRecord(
        event_id=event_id,
        ticker="600519",
        event_type=event_type,
        announced_at=available_at,
        available_at=available_at,
        lifecycle_state=state,
        summary=f"{event_type}:{state}",
        data_snapshot_id=f"sync-{offset_days}",
    )


def test_link_event_update_is_immutable_and_keeps_root_chain():
    plan = _event("event-plan", "plan", 0)
    progress = link_event_update(plan, _event("event-progress", "in_progress", 1))
    completed = link_event_update(
        progress,
        _event("event-completed", "completed", 2),
    )

    assert plan.previous_event_id is None
    assert plan.root_event_id == plan.event_id
    assert progress.previous_event_id == plan.event_id
    assert progress.root_event_id == plan.event_id
    assert completed.previous_event_id == progress.event_id
    assert completed.root_event_id == plan.event_id
    assert current_event_versions([completed, plan, progress]) == [completed]


def test_link_event_update_rejects_illegal_or_out_of_order_changes():
    completed = _event("event-completed", "completed", 2)
    with pytest.raises(EventTransitionError, match="不允许"):
        link_event_update(completed, _event("event-regression", "in_progress", 3))

    plan = _event("event-plan", "plan", 2)
    with pytest.raises(EventTransitionError, match="早于前序"):
        link_event_update(plan, _event("event-old", "in_progress", 1))

    with pytest.raises(EventTransitionError, match="事件类型不一致"):
        link_event_update(plan, _event("event-dividend", "proposal", 3, event_type="dividend"))


def test_current_event_versions_keeps_independent_roots():
    first = _event("repurchase-a", "plan", 0)
    first_update = link_event_update(first, _event("repurchase-a-progress", "in_progress", 1))
    second = _event("repurchase-b", "plan", 2)
    current = current_event_versions([first, second, first_update])
    assert [item.event_id for item in current] == [
        "repurchase-b",
        "repurchase-a-progress",
    ]
