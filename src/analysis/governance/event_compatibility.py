from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from analysis.models import EventRecord


@dataclass(frozen=True)
class LegacyEventReference:
    event_id: str
    payload_hash: str
    assessment_status: Literal["legacy_unassessed"] = field(
        default="legacy_unassessed", init=False
    )

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("legacy event reference requires a non-blank event_id")
        if len(self.payload_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.payload_hash
        ):
            raise ValueError("legacy event reference requires a SHA-256 payload_hash")

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        return (self.event_id,)


def _legacy_payload_hash(payload: dict[str, Any]) -> str:
    """Hash a legacy JSON payload without applying new Decimal-only semantics."""

    envelope = {
        "schema_name": "legacy-event-reference",
        "schema_version": "1",
        "payload": payload,
    }
    try:
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"legacy event is not valid JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def reference_legacy_event(event: EventRecord) -> LegacyEventReference:
    """Create a one-way ID reference without interpreting free event_terms."""

    if not isinstance(event, EventRecord):
        raise TypeError("legacy compatibility accepts only analysis.models.EventRecord")
    payload = event.model_dump(mode="json")
    event_id = event.event_id.strip()
    if not event_id:
        raise ValueError("legacy EventRecord requires a non-blank event_id")
    return LegacyEventReference(
        event_id=event_id,
        payload_hash=_legacy_payload_hash(payload),
    )


def legacy_source_event_ids(
    references: Iterable[LegacyEventReference],
) -> tuple[str, ...]:
    """Map validated references one way into typed-record source_event_ids."""

    values = tuple(references)
    if any(not isinstance(item, LegacyEventReference) for item in values):
        raise TypeError("source_event_ids require validated LegacyEventReference values")
    return tuple(sorted({item.event_id for item in values}))


def source_event_ids_from_legacy_events(
    events: Iterable[EventRecord],
) -> tuple[str, ...]:
    return legacy_source_event_ids(reference_legacy_event(item) for item in events)


def legacy_event_terms_are_canonical_facts(_: Any) -> bool:
    """Always false: compatibility never promotes free event terms."""

    return False


__all__ = [
    "LegacyEventReference",
    "legacy_source_event_ids",
    "legacy_event_terms_are_canonical_facts",
    "reference_legacy_event",
    "source_event_ids_from_legacy_events",
]
