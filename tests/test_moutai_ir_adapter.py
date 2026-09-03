from __future__ import annotations

from types import SimpleNamespace

import pytest

from analysis.acquisition.adapters import AcquisitionAdapterFactory
from analysis.acquisition.adapters.factory import SourcePolicyDisabledError


class CountingTransport:
    def __init__(self):
        self.calls = 0

    def request(self, work):
        self.calls += 1
        raise AssertionError("pending IR definition must never perform I/O")


def test_disabled_zero_io() -> None:
    transport = CountingTransport()
    with pytest.raises(SourcePolicyDisabledError):
        AcquisitionAdapterFactory().create(
            SimpleNamespace(
                adapter_key="moutai_ir",
                enabled=False,
                policy_status="pending_policy",
            ),
            transport=transport,
            snapshot_reader=lambda _: b"",
        )
    assert transport.calls == 0
