from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis.acquisition.adapters import AcquisitionAdapterFactory, BoundedTransportEnvelope, QueryWork


class NoIoTransport:
    def request(self, work):
        raise AssertionError("parser must not perform network I/O")


def _nullable_work(**changes):
    values = dict(
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.3.0",
        query_id="cninfo.periodic_report",
        query_family="periodic_report",
        execution_key="periodic.v1.3",
        method="POST",
        url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        parser_schema_version="2",
        context={"schema_id": "cninfo.announcements", "total_path": "totalAnnouncement"},
    )
    values.update(changes)
    return QueryWork(**values)


def _parse_payload(payload, work=None):
    body = json.dumps(payload).encode("utf-8")
    adapter = AcquisitionAdapterFactory().create(
        SimpleNamespace(adapter_key="cninfo", enabled=True, policy_status="enabled"),
        transport=NoIoTransport(),
        snapshot_reader=lambda _snapshot_id: body,
    )
    return adapter.parse_retained_discovery("synthetic", work or _nullable_work())


@pytest.mark.parametrize("auxiliary", [False, True])
def test_cninfo_nullable_empty_v2_has_unique_terminal_proof(auxiliary):
    payload = {"announcements": None, "totalAnnouncement": 0, "hasMore": False}
    if auxiliary:
        payload.update(totalRecordNum=0, totalSecurities=0, totalpages=0,
                       classifiedAnnouncements=None, categoryList=None)
    result = _parse_payload(payload)
    assert result.resources == ()
    assert result.declared_total == result.normalized_total == 0
    assert result.page == result.page_count == 1
    assert result.terminal and result.next_cursor is None and result.replayable
    assert result.parser_schema_version == "2"


@pytest.mark.parametrize("field,value", [
    ("totalAnnouncement", 1), ("totalAnnouncement", -1),
    ("totalAnnouncement", False), ("totalAnnouncement", "0"),
    ("totalAnnouncement", 0.0), ("totalAnnouncement", None),
    ("hasMore", True), ("hasMore", 0), ("hasMore", "false"), ("hasMore", None),
    ("totalRecordNum", 1), ("totalRecordNum", False), ("totalRecordNum", "0"),
    ("totalSecurities", 1), ("totalSecurities", None),
    ("totalpages", 1), ("totalpages", 0.0),
    ("classifiedAnnouncements", [{}]), ("categoryList", []),
    ("error", "denied"), ("success", False), ("unknown", 0),
])
def test_cninfo_nullable_empty_rejects_ambiguous_or_error_payload(field, value):
    payload = {"announcements": None, "totalAnnouncement": 0, "hasMore": False}
    payload[field] = value
    with pytest.raises(ValueError):
        _parse_payload(payload)


@pytest.mark.parametrize("field", ["announcements", "totalAnnouncement", "hasMore"])
def test_cninfo_nullable_empty_requires_explicit_fields(field):
    payload = {"announcements": None, "totalAnnouncement": 0, "hasMore": False}
    del payload[field]
    with pytest.raises(ValueError):
        _parse_payload(payload)


@pytest.mark.parametrize("changes", [
    {"page": 2}, {"cursor": "next"},
    {"parser_schema_version": "1", "source_definition_version": "1.2.0"},
    {"parser_schema_version": "99"},
    {"context": {"schema_id": "other", "total_path": "totalAnnouncement"}},
    {"context": {"schema_id": "cninfo.announcements", "total_path": "totalRecordNum"}},
])
def test_cninfo_nullable_empty_requires_frozen_schema_and_first_page(changes):
    payload = {"announcements": None, "totalAnnouncement": 0, "hasMore": False}
    with pytest.raises(ValueError):
        _parse_payload(payload, _nullable_work(**changes))


def test_cninfo_v2_preserves_array_results_and_pagination():
    row = {"announcementId": "synthetic-1", "announcementTitle": "Synthetic report",
           "announcementTime": 1750000000000, "adjunctUrl": "finalpage/test.pdf"}
    work = _nullable_work(context={**_nullable_work().context, "page_size": 1})
    first = _parse_payload({"announcements": [row], "totalAnnouncement": 2}, work)
    assert not first.terminal and first.next_cursor == "2"
    assert first.declared_total == 2 and first.normalized_total == 1
    last = _parse_payload({"announcements": [row], "totalAnnouncement": 2}, replace(work, page=2))
    assert last.terminal and last.next_cursor is None


def test_cninfo_discovery_proof_canonical_and_required_fetch() -> None:
    body = json.dumps(
        {
            "announcements": [
                {
                    "announcementId": "12345",
                    "announcementTitle": "2025年年度报告",
                    "announcementTime": 1750000000000,
                    "adjunctUrl": "/finalpage/2025/report.pdf",
                }
            ],
            "totalRecordNum": 1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    work = QueryWork(
        source_definition_id="cninfo.disclosures",
        source_definition_version="1",
        query_id="periodic",
        query_family="periodic_report",
        execution_key="periodic",
        method="POST",
        url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        max_response_bytes=50_000,
        context={"page_size": 30, "fetch_policy": "required_attachment"},
    )
    adapter = AcquisitionAdapterFactory().create(
        SimpleNamespace(adapter_key="cninfo", enabled=True, policy_status="enabled"),
        transport=NoIoTransport(),
        snapshot_reader=lambda snapshot_id: body,
    )
    result = adapter.parse_retained_discovery("snapshot", work)
    resource = result.resources[0]
    assert result.declared_total == result.normalized_total == 1
    assert result.terminal is True
    assert resource.canonical_resource_id == "cninfo:12345"
    assert resource.required_fetch is True
    assert resource.row_locator == "page:1/announcements:0"
    assert len(resource.row_hash) == 64
    assert resource.published_at.tzinfo == timezone.utc


def test_cninfo_v1_2_uses_total_announcement_from_query_context() -> None:
    body = json.dumps(
        {
            "announcements": [],
            "totalAnnouncement": 0,
        }
    ).encode("utf-8")
    work = QueryWork(
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.2.0",
        query_id="cninfo.periodic_report",
        query_family="periodic_report",
        execution_key="periodic",
        method="POST",
        url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
        max_response_bytes=50_000,
        context={"page_size": 30, "total_path": "totalAnnouncement"},
    )
    adapter = AcquisitionAdapterFactory().create(
        SimpleNamespace(adapter_key="cninfo", enabled=True, policy_status="enabled"),
        transport=NoIoTransport(),
        snapshot_reader=lambda snapshot_id: body,
    )

    result = adapter.parse_retained_discovery("snapshot", work)

    assert result.declared_total == 0
    assert result.normalized_total == 0
    assert result.terminal is True


def test_cninfo_bootstrap_filters_ticker_and_exposes_persistable_org_binding() -> None:
    body = json.dumps(
        {
            "stockList": [
                {"code": "000001", "orgId": "gssz0000001", "zwjc": "平安银行"},
                {"code": "600519", "orgId": "gssh0600519", "zwjc": "贵州茅台"},
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    work = QueryWork(
        source_definition_id="cninfo.disclosures",
        source_definition_version="1.2.0",
        query_id="cninfo.company_bootstrap",
        query_family="company_bootstrap",
        execution_key="bootstrap",
        method="GET",
        url="https://www.cninfo.com.cn/new/data/szse_stock.json",
        max_response_bytes=50_000,
        context={"ticker": "600519", "fetch_policy": "metadata_only"},
    )
    adapter = AcquisitionAdapterFactory().create(
        SimpleNamespace(adapter_key="cninfo", enabled=True, policy_status="enabled"),
        transport=NoIoTransport(),
        snapshot_reader=lambda snapshot_id: body,
    )

    result = adapter.parse_retained_discovery("snapshot", work)

    assert result.declared_total == result.normalized_total == 1
    assert result.resources[0].required_fetch is False
    assert result.resources[0].metadata == {
        "ticker": "600519",
        "org_id": "gssh0600519",
        "wire_stock": "600519,gssh0600519",
        "company_name": "贵州茅台",
    }
