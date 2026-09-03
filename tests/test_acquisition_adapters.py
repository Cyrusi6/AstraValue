from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis.acquisition.adapters import (
    AcquisitionAdapterFactory,
    BoundedTransportEnvelope,
    QueryWork,
)
from analysis.acquisition.adapters.factory import (
    SourcePolicyDisabledError,
    UnknownAdapterError,
)


class FakeTransport:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body
        self.calls = []

    def request(self, work: QueryWork) -> BoundedTransportEnvelope:
        self.calls.append(work)
        now = datetime.now(timezone.utc)
        return BoundedTransportEnvelope(
            request_url=work.url,
            final_url=work.url,
            status_code=200,
            headers={"content-type": "application/json"},
            body=self.body,
            observed_at=now,
            retrieved_at=now,
            body_limit=work.max_response_bytes,
        )


def _definition(key="cninfo", *, enabled=True, status="enabled"):
    return SimpleNamespace(adapter_key=key, enabled=enabled, policy_status=status)


def _work(**changes) -> QueryWork:
    payload = {
        "source_definition_id": "cninfo.disclosures",
        "source_definition_version": "1",
        "query_id": "periodic",
        "query_family": "periodic_report",
        "execution_key": "periodic",
        "method": "POST",
        "url": "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        "max_response_bytes": 10_000,
        "context": {"page_size": 30, "fetch_policy": "required_attachment"},
    }
    payload.update(changes)
    return QueryWork(**payload)


def test_protocol_has_no_raw_root_or_data_root() -> None:
    factory = AcquisitionAdapterFactory()
    adapter = factory.create(
        _definition(), transport=FakeTransport(), snapshot_reader=lambda _: b"{}"
    )
    for name in (
        "bootstrap_company",
        "execute_query",
        "parse_retained_discovery",
        "validate_and_normalize_without_retention",
        "fetch_resource",
    ):
        parameters = inspect.signature(getattr(adapter, name)).parameters
        assert "data_root" not in parameters
        assert "raw_root" not in parameters


def test_factory_is_capability_only_and_fails_closed() -> None:
    factory = AcquisitionAdapterFactory()
    with pytest.raises(UnknownAdapterError):
        factory.create(
            _definition("arbitrary_web"),
            transport=FakeTransport(),
            snapshot_reader=lambda _: b"",
        )
    with pytest.raises(SourcePolicyDisabledError):
        factory.create(
            _definition("moutai_ir", enabled=False, status="pending_policy"),
            transport=FakeTransport(),
            snapshot_reader=lambda _: b"",
        )


def test_retained_parser_reads_only_by_snapshot_id() -> None:
    body = json.dumps(
        {"announcements": [], "totalRecordNum": 0}, separators=(",", ":")
    ).encode()
    seen = []
    adapter = AcquisitionAdapterFactory().create(
        _definition(),
        transport=FakeTransport(body),
        snapshot_reader=lambda snapshot_id: seen.append(snapshot_id) or body,
    )
    result = adapter.parse_retained_discovery("snapshot-1", _work())
    assert seen == ["snapshot-1"]
    assert result.replayable is True
    assert result.terminal is True


def test_non_retained_parser_consumes_bounded_envelope_once() -> None:
    body = json.dumps(
        {"announcements": [], "totalRecordNum": 0}, separators=(",", ":")
    ).encode()
    transport = FakeTransport(body)
    adapter = AcquisitionAdapterFactory().create(
        _definition(), transport=transport, snapshot_reader=lambda _: b""
    )
    envelope = transport.request(_work())
    result = adapter.validate_and_normalize_without_retention(envelope, _work())
    assert result.replayable is False
    assert result.response_sha256 == envelope.sha256
    assert result.normalized_total == 0
