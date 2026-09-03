from __future__ import annotations

from datetime import datetime, timezone

import pytest

from analysis.governance.event_compatibility import (
    legacy_event_terms_are_canonical_facts,
    legacy_source_event_ids,
    reference_legacy_event,
    source_event_ids_from_legacy_events,
)
from analysis.models import EventRecord


NOW = datetime(2024, 1, 2, tzinfo=timezone.utc)


def legacy_event(event_id: str = "legacy:event:1") -> EventRecord:
    return EventRecord(
        event_id=event_id,
        ticker="600519",
        event_type="management_change",
        announced_at=NOW,
        available_at=NOW,
        effective_at=NOW,
        lifecycle_state="effective",
        amount=1.25,
        shares=3.5,
        ratio=0.125,
        summary="正式公告披露管理层变更",
        event_terms={
            "free_form": {"threshold": 0.75, "roles": ["董事", "总经理"]},
            "untyped": True,
        },
        data_snapshot_id="legacy-snapshot:1",
    )


def test_real_event_record_float_payload_maps_stably_without_mutation() -> None:
    event = legacy_event()
    before_json = event.model_dump_json()
    before_payload = event.model_dump(mode="json")
    first = reference_legacy_event(event)
    second = reference_legacy_event(event)
    assert first == second
    assert first.assessment_status == "legacy_unassessed"
    assert first.source_event_ids == (event.event_id,)
    assert event.model_dump_json() == before_json
    assert event.model_dump(mode="json") == before_payload


def test_source_event_ids_are_one_way_stable_and_deduplicated() -> None:
    first = legacy_event("legacy:event:2")
    second = legacy_event("legacy:event:1")
    references = (
        reference_legacy_event(first),
        reference_legacy_event(second),
        reference_legacy_event(first),
    )
    assert legacy_source_event_ids(references) == (
        "legacy:event:1",
        "legacy:event:2",
    )
    assert source_event_ids_from_legacy_events((first, second, first)) == (
        "legacy:event:1",
        "legacy:event:2",
    )


def test_free_event_terms_never_become_canonical_state() -> None:
    event = legacy_event()
    reference_legacy_event(event)
    assert legacy_event_terms_are_canonical_facts(event.event_terms) is False


def test_mapper_rejects_non_event_record_and_blank_identity() -> None:
    with pytest.raises(TypeError, match="EventRecord"):
        reference_legacy_event({"event_id": "legacy:event:1"})  # type: ignore[arg-type]
    event = legacy_event(" ")
    with pytest.raises(ValueError, match="non-blank"):
        reference_legacy_event(event)
