from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .event_state import EventClassification, classify_announcement
from .models import (
    AnnouncementRecord,
    DocumentRecord,
    EventRecord,
    EvidenceSpan,
    VerificationStatus,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")
# 巨潮静态附件在当前网络环境下比交易所附件稳定；两者仍保留为
# 同一正式披露的镜像，不会被误算成两个独立来源。
PROVIDER_PRIORITY = {"cninfo": 3, "sse": 2, "szse": 2, "manual": 1}


@dataclass(frozen=True)
class AnnouncementCandidate:
    ticker: str
    company_name: str
    title: str
    published_at: datetime
    url: str
    provider: str
    announcement_id: str
    category: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MergedAnnouncement:
    canonical_key: str
    primary: AnnouncementCandidate
    mirrors: tuple[AnnouncementCandidate, ...]
    classification: EventClassification


def canonical_announcement_key(candidate: AnnouncementCandidate) -> str:
    normalized_title = normalize_announcement_title(candidate.title)
    china_date = candidate.published_at.astimezone(CHINA_TZ).date().isoformat()
    payload = f"{candidate.ticker}|{china_date}|{normalized_title}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:20]
    return f"announcement:{candidate.ticker}:{china_date}:{digest}"


def merge_announcement_candidates(
    candidates: list[AnnouncementCandidate],
    *,
    max_records: int,
) -> list[MergedAnnouncement]:
    grouped: dict[str, list[AnnouncementCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(canonical_announcement_key(candidate), []).append(candidate)
    merged = []
    for canonical_key, group in grouped.items():
        ordered = sorted(
            group,
            key=lambda item: (
                PROVIDER_PRIORITY.get(item.provider, 0),
                item.published_at,
                item.announcement_id,
            ),
            reverse=True,
        )
        primary = ordered[0]
        merged.append(
            MergedAnnouncement(
                canonical_key=canonical_key,
                primary=primary,
                mirrors=tuple(ordered),
                classification=classify_announcement(primary.title),
            )
        )
    merged.sort(
        key=lambda item: (
            item.primary.published_at,
            item.canonical_key,
        ),
        reverse=True,
    )
    return merged[:max_records]


def build_announcement_record(
    merged: MergedAnnouncement,
    *,
    data_snapshot_id: str,
) -> AnnouncementRecord:
    candidate = merged.primary
    classification = merged.classification
    record_id = "ann-" + hashlib.sha256(
        f"{merged.canonical_key}|{data_snapshot_id}".encode("utf-8")
    ).hexdigest()[:24]
    return AnnouncementRecord(
        announcement_record_id=record_id,
        ticker=candidate.ticker,
        company_name=candidate.company_name,
        title=normalize_announcement_title(candidate.title),
        announced_at=candidate.published_at,
        available_at=candidate.published_at,
        provider=candidate.provider,
        announcement_id=candidate.announcement_id,
        url=candidate.url,
        category=candidate.category,
        canonical_key=merged.canonical_key,
        classification_version=classification.taxonomy_version,
        classified_event_type=classification.event_type,
        classified_event_subtype=classification.subtype,
        classified_lifecycle_state=classification.lifecycle_state,
        classification_score=classification.score,
        data_snapshot_id=data_snapshot_id,
        metadata={
            **candidate.metadata,
            "report_steps": list(classification.report_steps),
            "matched_patterns": list(classification.matched_patterns),
            "mirrors": [
                {
                    "provider": item.provider,
                    "announcement_id": item.announcement_id,
                    "url": item.url,
                    "published_at": item.published_at.isoformat(),
                    "category": item.category,
                }
                for item in merged.mirrors
            ],
        },
    )


def attach_document_and_event(
    announcement: AnnouncementRecord,
    document: DocumentRecord,
) -> tuple[AnnouncementRecord, EventRecord | None]:
    if announcement.classified_event_type in {None, "other"}:
        return (
            AnnouncementRecord.model_validate(
                {
                    **announcement.model_dump(mode="python"),
                    "source_ids": [document.source.source_id],
                    "document_id": document.document_id,
                }
            ),
            None,
        )
    event_id = "evt-" + hashlib.sha256(
        (
            f"{announcement.canonical_key}|{document.sha256}|"
            f"{announcement.classified_event_type}|"
            f"{announcement.classified_event_subtype or ''}|"
            f"{announcement.data_snapshot_id}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    event = EventRecord(
        event_id=event_id,
        ticker=announcement.ticker,
        event_type=announcement.classified_event_type,
        event_subtype=announcement.classified_event_subtype,
        announced_at=announcement.announced_at,
        available_at=announcement.available_at,
        lifecycle_state=announcement.classified_lifecycle_state or "unclassified",
        summary=announcement.title,
        source_ids=[document.source.source_id],
        document_ids=[document.document_id],
        evidence_spans=[
            EvidenceSpan(
                document_id=document.document_id,
                text=announcement.title,
            )
        ],
        verification_status=VerificationStatus.AUTHORITATIVE_SINGLE,
        materiality="unassessed",
        status_updated_at=announcement.available_at,
        data_snapshot_id=announcement.data_snapshot_id,
        metadata={
            "announcement_record_id": announcement.announcement_record_id,
            "canonical_announcement_key": announcement.canonical_key,
            "classification_version": announcement.classification_version,
            "classification_score": announcement.classification_score,
            "matched_patterns": announcement.metadata.get("matched_patterns", []),
            "root_matching": "unresolved",
        },
    )
    attached = AnnouncementRecord.model_validate(
        {
            **announcement.model_dump(mode="python"),
            "source_ids": [document.source.source_id],
            "document_id": document.document_id,
            "event_ids": [event.event_id],
        }
    )
    return attached, event


def candidate_download_order(merged: MergedAnnouncement) -> list[AnnouncementCandidate]:
    return list(merged.mirrors)


def normalize_announcement_title(title: str) -> str:
    value = html.unescape(title or "")
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()
