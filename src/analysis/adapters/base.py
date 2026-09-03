from __future__ import annotations

from typing import Protocol

from ..models import SyncRequest, SyncResult


class SourceAdapter(Protocol):
    name: str

    def sync(self, ticker: str, options: SyncRequest | None = None) -> SyncResult:
        ...
