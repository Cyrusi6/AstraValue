from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis.acquisition.adapters import AcquisitionAdapterFactory, QueryWork


def _work() -> QueryWork:
    return QueryWork(
        source_definition_id="szse.disclosures",
        source_definition_version="1",
        query_id="announcements",
        query_family="business_announcement",
        execution_key="announcements",
        method="POST",
        url="https://www.szse.cn/api/disc/announcement/annList",
        max_response_bytes=50_000,
        context={"page_size": 50, "fetch_policy": "required_attachment"},
    )


def _adapter(body: bytes):
    return AcquisitionAdapterFactory().create(
        SimpleNamespace(adapter_key="szse", enabled=True, policy_status="enabled"),
        transport=SimpleNamespace(request=lambda work: None),
        snapshot_reader=lambda _: body,
    )


def test_szse_schema_pagination_canonical_and_required_fetch() -> None:
    body = json.dumps(
        {
            "data": [
                {
                    "annId": "sz-1",
                    "title": "年度报告",
                    "publishTime": "2026-09-03 15:30:45",
                    "attachPath": "/disc/disk03/finalpage.pdf",
                }
            ],
            "announceCount": "1",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    result = _adapter(body).parse_retained_discovery("snapshot", _work())
    assert result.terminal is True
    assert result.resources[0].canonical_resource_id == "szse:sz-1"
    assert result.resources[0].required_fetch is True
    assert result.resources[0].published_at_precision == "instant"
    assert result.resources[0].published_at == datetime(
        2026, 9, 3, 7, 30, 45, tzinfo=timezone.utc
    )


def test_szse_rejects_unknown_response_shape() -> None:
    with pytest.raises(ValueError, match="schema"):
        _adapter(b"{}").parse_retained_discovery("snapshot", _work())
