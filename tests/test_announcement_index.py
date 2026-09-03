from datetime import datetime, timedelta, timezone

import fitz

from analysis.adapters.official_adapter import (
    OfficialDisclosureAdapter,
    _CninfoClient,
    _SseClient,
)
from analysis.announcement_index import (
    AnnouncementCandidate,
    attach_document_and_event,
    build_announcement_record,
    canonical_announcement_key,
    merge_announcement_candidates,
    normalize_announcement_title,
)
from analysis.documents import ingest_downloaded_document
from analysis.models import SyncRequest, VerificationStatus


AS_OF = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _candidate(
    provider: str,
    *,
    title: str = "2025年年度权益分派实施公告",
    announcement_id: str | None = None,
    published_at: datetime = AS_OF - timedelta(days=1),
) -> AnnouncementCandidate:
    return AnnouncementCandidate(
        ticker="600519",
        company_name="贵州茅台",
        title=title,
        published_at=published_at,
        url=f"https://example.test/{provider}.pdf",
        provider=provider,
        announcement_id=announcement_id or f"{provider}-001",
        category="权益分派",
        metadata={"fixture": True},
    )


def _pdf_bytes(text: str = "official announcement") -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


def test_canonical_key_merges_official_mirrors_but_keeps_provenance():
    sse = _candidate("sse")
    cninfo = _candidate(
        "cninfo",
        title="<em>2025年年度权益分派实施公告</em>",
        published_at=sse.published_at + timedelta(hours=1),
    )
    assert canonical_announcement_key(sse) == canonical_announcement_key(cninfo)
    merged = merge_announcement_candidates([cninfo, sse], max_records=10)

    assert len(merged) == 1
    assert merged[0].primary.provider == "cninfo"
    assert [item.provider for item in merged[0].mirrors] == ["cninfo", "sse"]
    assert merged[0].classification.event_type == "dividend"
    record = build_announcement_record(merged[0], data_snapshot_id="sync-001")
    record_v2 = build_announcement_record(merged[0], data_snapshot_id="sync-002")
    assert record.classified_event_type == "dividend"
    assert record_v2.announcement_record_id != record.announcement_record_id
    assert record_v2.canonical_key == record.canonical_key
    assert len(record.metadata["mirrors"]) == 2
    assert normalize_announcement_title(cninfo.title) == "2025年年度权益分派实施公告"


def test_attached_event_uses_archived_document_as_authoritative_evidence(tmp_path):
    merged = merge_announcement_candidates([_candidate("cninfo")], max_records=10)[0]
    announcement = build_announcement_record(
        merged,
        data_snapshot_id="sync-001",
    )
    document = ingest_downloaded_document(
        ticker="600519",
        content=_pdf_bytes(),
        title=announcement.title,
        source_name="巨潮资讯正式披露",
        source_url=announcement.url,
        published_at=announcement.announced_at,
        provider="cninfo",
        announcement_id=announcement.announcement_id,
        raw_root=tmp_path / "raw",
    )
    attached, event = attach_document_and_event(announcement, document)

    assert event is not None
    assert event.verification_status == VerificationStatus.AUTHORITATIVE_SINGLE
    assert event.source_ids == [document.source.source_id]
    assert event.document_ids == [document.document_id]
    assert event.data_snapshot_id == "sync-001"
    assert attached.document_id == document.document_id
    assert attached.event_ids == [event.event_id]


def test_downloaded_document_identity_versions_context_but_keeps_upstream(tmp_path):
    kwargs = {
        "ticker": "600519",
        "content": _pdf_bytes(),
        "title": "测试公告",
        "source_name": "巨潮资讯正式披露",
        "source_url": "https://example.test/notice.pdf",
        "published_at": AS_OF,
        "provider": "cninfo",
        "announcement_id": "notice-001",
        "raw_root": tmp_path / "raw",
        "metadata": {"fixture": True},
    }
    first = ingest_downloaded_document(**kwargs)
    repeated = ingest_downloaded_document(**kwargs)
    another_publication = ingest_downloaded_document(
        **{**kwargs, "announcement_id": "notice-002"}
    )

    assert first.model_dump_json() == repeated.model_dump_json()
    assert first.source.model_dump_json() == repeated.source.model_dump_json()
    assert first.document_id.startswith("doc-600519-v2-cninfo-")
    assert first.source.source_id.startswith("src-official-v2-cninfo-")
    assert another_publication.document_id != first.document_id
    assert another_publication.source.source_id != first.source.source_id
    assert (
        another_publication.source.upstream_source_id
        == first.source.upstream_source_id
    )
    assert another_publication.archived_path == first.archived_path


def test_official_adapter_can_index_metadata_without_downloading(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        _CninfoClient,
        "list_announcements",
        lambda self, ticker, start, end, as_of, maximum: [_candidate("cninfo")],
    )
    monkeypatch.setattr(
        _SseClient,
        "list_announcements",
        lambda self, ticker, start, end, as_of, maximum: [_candidate("sse")],
    )
    result = OfficialDisclosureAdapter(tmp_path / "raw").sync(
        "600519",
        SyncRequest(
            providers=["official"],
            scopes=["announcements"],
            as_of=AS_OF,
            announcement_years=2,
            download_official_documents=False,
        ),
    )

    assert len(result.announcements) == 1
    assert result.announcements[0].classified_event_type == "dividend"
    assert result.events == []
    assert result.documents == []
    assert "公告索引1条" in result.provider_results["official_announcements"]


def test_official_adapter_downloads_only_classified_event_documents(
    monkeypatch,
    tmp_path,
):
    candidates = [
        _candidate("cninfo"),
        _candidate("cninfo", title="关于召开年度股东大会的通知", announcement_id="other"),
    ]
    monkeypatch.setattr(
        _CninfoClient,
        "list_announcements",
        lambda self, ticker, start, end, as_of, maximum: candidates,
    )
    monkeypatch.setattr(
        _SseClient,
        "list_announcements",
        lambda self, ticker, start, end, as_of, maximum: [],
    )
    monkeypatch.setattr(
        "analysis.adapters.official_adapter._download_pdf",
        lambda client, url, referer=None: _pdf_bytes(),
    )
    result = OfficialDisclosureAdapter(tmp_path / "raw").sync(
        "600519",
        SyncRequest(
            providers=["official"],
            scopes=["announcements"],
            as_of=AS_OF,
            announcement_years=2,
            max_event_documents=10,
            download_official_documents=True,
        ),
    )

    assert len(result.announcements) == 2
    assert len(result.documents) == 1
    assert len(result.events) == 1
    event_announcement = next(
        item for item in result.announcements if item.classified_event_type == "dividend"
    )
    other_announcement = next(
        item for item in result.announcements if item.classified_event_type == "other"
    )
    assert event_announcement.document_id is not None
    assert event_announcement.event_ids == [result.events[0].event_id]
    assert other_announcement.document_id is None
    assert "classified_event_type" not in result.documents[0].metadata
    assert "classified_event_subtype" not in result.documents[0].metadata
    assert "classified_lifecycle_state" not in result.documents[0].metadata
    assert "结构化事件1条" in result.provider_results["official_announcements"]
