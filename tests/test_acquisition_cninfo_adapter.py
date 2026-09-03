from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from analysis.acquisition.adapters import AcquisitionAdapterFactory, BoundedTransportEnvelope, QueryWork


class NoIoTransport:
    def request(self, work):
        raise AssertionError("parser must not perform network I/O")


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
