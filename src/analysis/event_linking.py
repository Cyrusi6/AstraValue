from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from .event_state import (
    EventTransitionError,
    link_event_update,
    load_event_taxonomy,
    validate_transition,
)
from .models import EventRecord, aware_utc


CONSERVATIVE_UNIQUE_OPEN_MATCH_TYPES = {
    "dividend",
    "repurchase",
    "holding_change",
    "financing",
    "merger_acquisition",
    "regulatory_penalty",
    "litigation",
    "related_party_transaction",
}
MAX_OPEN_EVENT_GAP = timedelta(days=800)


def link_event_series(
    events: list[EventRecord],
    *,
    taxonomy: dict[str, Any] | None = None,
) -> list[EventRecord]:
    taxonomy = taxonomy or load_event_taxonomy()
    ordered = sorted(events, key=lambda item: _series_sort_key(item, taxonomy))
    current_by_root: dict[str, EventRecord] = {}
    linked_events: list[EventRecord] = []

    for raw_event in ordered:
        event = _annotate_identity(raw_event)
        identity = event.metadata.get("event_identity")
        compatible = [
            previous
            for previous in current_by_root.values()
            if _can_follow(previous, event, taxonomy)
            and _within_gap(previous, event)
        ]
        matching_method = None
        confidence = None
        candidates: list[EventRecord] = []
        if identity:
            candidates = [
                previous
                for previous in compatible
                if previous.metadata.get("event_identity") == identity
            ]
            matching_method = "explicit_identity"
            confidence = 1.0
        elif _may_use_unique_open_match(event, taxonomy):
            candidates = compatible
            matching_method = "unique_compatible_open_event"
            confidence = 0.7

        if len(candidates) == 1:
            previous = candidates[0]
            if not identity and matching_method == "unique_compatible_open_event":
                event = _inherit_event_identity(event, previous)
            event = _with_matching_metadata(
                event,
                method=matching_method or "unknown",
                confidence=confidence or 0.0,
                candidate_root_ids=[previous.root_event_id or previous.event_id],
            )
            event = link_event_update(previous, event, taxonomy=taxonomy)
            root_id = event.root_event_id or event.event_id
            current_by_root[root_id] = event
        else:
            method = "ambiguous" if len(candidates) > 1 else "new_root"
            event = _with_matching_metadata(
                event,
                method=method,
                confidence=0.0 if method == "ambiguous" else 1.0,
                candidate_root_ids=[
                    item.root_event_id or item.event_id for item in candidates
                ],
            )
            current_by_root[event.event_id] = event
        linked_events.append(event)
    return linked_events


def derive_event_identity(event: EventRecord) -> str | None:
    for key in (
        "case_number",
        "plan_id",
        "transaction_id",
        "pledge_id",
        "holder_id",
        "target_asset",
        "bond_code",
    ):
        value = event.event_terms.get(key)
        if value not in (None, ""):
            return f"{event.event_type}:{key}:{_normalize_identity(value)}"

    if event.event_type == "dividend":
        match = re.search(
            r"(?P<year>20\d{2})年(?P<period>年度|半年度|中期|三季度|第一季度)?",
            event.summary,
        )
        if match:
            period = match.group("period") or "年度"
            period = {"半年度": "中期", "中期": "中期"}.get(period, period)
            return f"dividend:{match.group('year')}:{period}"
    return None


def _annotate_identity(event: EventRecord) -> EventRecord:
    identity = derive_event_identity(event)
    if not identity:
        return event
    metadata = dict(event.metadata)
    metadata["event_identity"] = identity
    return EventRecord.model_validate(
        {**event.model_dump(mode="python"), "metadata": metadata}
    )


def _with_matching_metadata(
    event: EventRecord,
    *,
    method: str,
    confidence: float,
    candidate_root_ids: list[str],
) -> EventRecord:
    metadata = dict(event.metadata)
    metadata.update(
        {
            "root_matching": method,
            "root_matching_confidence": confidence,
            "root_matching_candidates": sorted(set(candidate_root_ids)),
        }
    )
    return EventRecord.model_validate(
        {**event.model_dump(mode="python"), "metadata": metadata}
    )


def _inherit_event_identity(
    event: EventRecord,
    previous: EventRecord,
) -> EventRecord:
    inherited_identity = previous.metadata.get("event_identity")
    if not inherited_identity:
        return event
    metadata = dict(event.metadata)
    metadata.update(
        {
            "event_identity": inherited_identity,
            "event_identity_origin": "inherited_unique_open_match",
            "event_identity_inherited_from": previous.event_id,
        }
    )
    return EventRecord.model_validate(
        {**event.model_dump(mode="python"), "metadata": metadata}
    )


def _can_follow(
    previous: EventRecord,
    update: EventRecord,
    taxonomy: dict[str, Any],
) -> bool:
    if previous.ticker != update.ticker or previous.event_type != update.event_type:
        return False
    try:
        return validate_transition(
            previous.event_type,
            previous.lifecycle_state,
            update.lifecycle_state,
            taxonomy=taxonomy,
        )
    except EventTransitionError:
        return False


def _within_gap(previous: EventRecord, update: EventRecord) -> bool:
    gap = aware_utc(update.available_at) - aware_utc(previous.available_at)
    return timedelta(0) <= gap <= MAX_OPEN_EVENT_GAP


def _may_use_unique_open_match(
    event: EventRecord,
    taxonomy: dict[str, Any],
) -> bool:
    if event.event_type not in CONSERVATIVE_UNIQUE_OPEN_MATCH_TYPES:
        return False
    spec = taxonomy["event_types"].get(event.event_type, {})
    return event.lifecycle_state != spec.get("default_state")


def _series_sort_key(event: EventRecord, taxonomy: dict[str, Any]) -> tuple:
    states = taxonomy["event_types"].get(event.event_type, {}).get("states", [])
    try:
        state_order = states.index(event.lifecycle_state)
    except ValueError:
        state_order = len(states)
    return (
        aware_utc(event.available_at),
        event.event_type,
        state_order,
        event.event_id,
    )


def _normalize_identity(value: Any) -> str:
    return re.sub(r"\s+", "", str(value)).lower()
