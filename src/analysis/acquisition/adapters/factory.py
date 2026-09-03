from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import AcquisitionSourceAdapter, SnapshotReader, TransportClient
from .official import (
    CninfoAcquisitionAdapter,
    MoutaiIrAcquisitionAdapter,
    SseAcquisitionAdapter,
    SzseAcquisitionAdapter,
)


class UnknownAdapterError(ValueError):
    pass


class SourcePolicyDisabledError(ValueError):
    reason_code = "source_policy_disabled"


Builder = Callable[[TransportClient, SnapshotReader], AcquisitionSourceAdapter]


class AcquisitionAdapterFactory:
    """Resolve installed capabilities without deciding which sources are enabled."""

    def __init__(self, builders: dict[str, Builder] | None = None) -> None:
        self._builders: dict[str, Builder] = {
            "cninfo": CninfoAcquisitionAdapter,
            "sse": SseAcquisitionAdapter,
            "szse": SzseAcquisitionAdapter,
            "moutai_ir": MoutaiIrAcquisitionAdapter,
        }
        if builders:
            for key, builder in builders.items():
                self.register(key, builder)

    @property
    def installed_adapter_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._builders))

    def register(self, adapter_key: str, builder: Builder) -> None:
        key = adapter_key.strip().lower()
        if not key or key in self._builders:
            raise ValueError(f"adapter key already registered or invalid: {adapter_key}")
        self._builders[key] = builder

    def create(
        self,
        source_definition: Any,
        *,
        transport: TransportClient,
        snapshot_reader: SnapshotReader,
    ) -> AcquisitionSourceAdapter:
        status = str(
            getattr(source_definition, "policy_status", None)
            or getattr(source_definition, "status", "")
        ).lower()
        enabled = getattr(source_definition, "enabled", None)
        if enabled is False or "pending" in status or "disabled" in status:
            raise SourcePolicyDisabledError(
                "source definition is not approved for network I/O"
            )
        key = str(getattr(source_definition, "adapter_key", "")).strip().lower()
        builder = self._builders.get(key)
        if builder is None:
            raise UnknownAdapterError(f"adapter capability is not installed: {key}")
        return builder(transport, snapshot_reader)
