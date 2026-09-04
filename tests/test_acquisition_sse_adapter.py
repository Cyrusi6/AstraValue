from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from analysis.acquisition.adapters import AcquisitionAdapterFactory, QueryWork
from analysis.acquisition.status_classifier import classify_response


def _work(*, schema_version: str = "1") -> QueryWork:
    return QueryWork(
        source_definition_id="sse.disclosures",
        source_definition_version=schema_version,
        query_id="periodic",
        query_family="periodic_report",
        execution_key="periodic",
        method="GET",
        url="https://query.sse.com.cn/security/stock/queryCompanyStatementNew.do",
        max_response_bytes=50_000,
        parser_schema_version=schema_version,
    )


def _adapter(body: bytes):
    return AcquisitionAdapterFactory().create(
        SimpleNamespace(adapter_key="sse", enabled=True, policy_status="enabled"),
        transport=SimpleNamespace(request=lambda work: None),
        snapshot_reader=lambda _: body,
    )


def test_sse_date_precision_uses_next_local_day_boundary() -> None:
    body = json.dumps(
        {
            "pageHelp": {
                "data": [
                    {
                        "TITLE": "年度报告",
                        "SSEDATE": "2026-09-03",
                        "URL": "/disclosure/listedinfo/announcement/c/new.pdf",
                    }
                ],
                "pageCount": 1,
                "total": 1,
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")
    work = _work()
    adapter = _adapter(body)
    resource = adapter.parse_retained_discovery("snapshot", work).resources[0]
    assert resource.published_at_precision == "date"
    assert resource.published_at == datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc)
    assert resource.metadata == {"expected_mime_types": ("application/pdf",)}


def test_sse_v1_3_lowercase_title_shape_is_normalized() -> None:
    body = json.dumps(
        {
            "pageHelp": {
                "data": [
                    {
                        "title": "脱敏定期报告",
                        "SSEDATE": "2026-09-03",
                        "URL": "/disclosure/listedinfo/announcement/c/fixture.pdf",
                    }
                ],
                "pageCount": 1,
                "total": 1,
            },
            "result": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    result = _adapter(body).parse_retained_discovery("snapshot", _work(schema_version="2"))

    assert result.schema_valid is True
    assert result.resources[0].title == "脱敏定期报告"
    assert result.resources[0].canonical_resource_id == "sse:fixture"


def test_sse_zero_records_and_zero_page_count_is_one_terminal_response() -> None:
    body = json.dumps(
        {
            "pageHelp": {"data": [], "pageCount": 0, "total": 0},
            "result": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    result = _adapter(body).parse_retained_discovery("snapshot", _work(schema_version="2"))

    assert result.resources == ()
    assert result.declared_total == 0
    assert result.page_count == 0
    assert result.terminal is True


def test_sse_challenge_is_restricted_not_empty_data() -> None:
    outcome = classify_response(
        status_code=200,
        headers={"content-type": "text/html"},
        body_prefix=b"CAPTCHA verify you are human",
    )
    assert outcome.outcome == "restricted"
