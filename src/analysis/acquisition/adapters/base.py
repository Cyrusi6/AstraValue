from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("transport timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class BoundedTransportEnvelope:
    request_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    redirect_chain: tuple[str, ...] = ()
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    body_limit: int = 1

    def __post_init__(self) -> None:
        if self.body_limit <= 0 or len(self.body) > self.body_limit:
            raise ValueError("transport envelope body exceeds its frozen bound")
        if not 100 <= self.status_code <= 599:
            raise ValueError("invalid HTTP status")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at))
        object.__setattr__(self, "retrieved_at", _aware_utc(self.retrieved_at))
        object.__setattr__(
            self,
            "headers",
            {str(key).lower(): str(value) for key, value in self.headers.items()},
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def content_type(self) -> str | None:
        value = self.headers.get("content-type")
        return value.split(";", 1)[0].strip().lower() if value else None


class LeaseGuard(Protocol):
    """Ephemeral executor fence carried with work but never persisted."""

    def __call__(self, *, force: bool = False) -> None: ...


@dataclass(frozen=True, slots=True)
class TransportExecutionCapability:
    """Executor-issued authority for one durable attempt to perform I/O.

    The identity fields are deliberately separate from ``QueryWork.context``:
    callers cannot obtain network access by adding free-form context values.
    The guard is bound by the orchestrator to the repository owner token and
    revalidates the durable run/attempt/lease identity at request boundaries.
    """

    run_id: str
    attempt_id: str
    lease_epoch: int
    run_as_of: datetime
    source_definition_id: str
    source_definition_version: str
    lease_guard: LeaseGuard = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        identities = (
            self.run_id,
            self.attempt_id,
            self.source_definition_id,
            str(self.source_definition_version),
        )
        if any(not str(value).strip() for value in identities):
            raise ValueError("transport execution capability identities are required")
        if (
            isinstance(self.lease_epoch, bool)
            or not isinstance(self.lease_epoch, int)
            or self.lease_epoch <= 0
        ):
            raise ValueError("transport execution capability lease_epoch must be positive")
        if not callable(self.lease_guard):
            raise ValueError("transport execution capability lease guard is required")
        object.__setattr__(self, "run_as_of", _aware_utc(self.run_as_of))

    def guard(self, *, force: bool = False) -> None:
        self.lease_guard(force=force)


@dataclass(frozen=True, slots=True)
class QueryWork:
    source_definition_id: str
    source_definition_version: str
    query_id: str
    query_family: str
    execution_key: str
    method: str
    url: str
    page: int = 1
    cursor: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    json_body: Any = None
    form_body: Mapping[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    expected_mime_types: tuple[str, ...] = ("application/json",)
    max_response_bytes: int = 1_048_576
    parser_schema_version: str = "1"
    context: Mapping[str, Any] = field(default_factory=dict)
    execution_capability: TransportExecutionCapability | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in {"GET", "POST", "HEAD"}:
            raise ValueError("unsupported acquisition method")
        if not self.source_definition_id or not str(self.source_definition_version).strip():
            raise ValueError("frozen source definition identity is required")
        if not self.query_id or not self.execution_key or not self.url:
            raise ValueError("query identity and URL are required")
        if self.page < 1 or self.max_response_bytes <= 0:
            raise ValueError("invalid page or response bound")
        object.__setattr__(self, "method", method)


@dataclass(frozen=True, slots=True)
class NormalizedResource:
    canonical_resource_id: str
    upstream_material_id: str
    title: str
    url: str
    published_raw: str | None
    published_at: datetime | None
    published_at_precision: str
    source_timezone: str
    row_locator: str
    row_hash: str
    required_fetch: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.canonical_resource_id or not self.upstream_material_id:
            raise ValueError("canonical and upstream material identities are required")
        if not self.url or not self.row_locator or len(self.row_hash) != 64:
            raise ValueError("resource URL and deterministic row lineage are required")
        if self.published_at_precision not in {"instant", "date", "unknown"}:
            raise ValueError("invalid publication precision")
        if self.published_at is not None:
            object.__setattr__(self, "published_at", _aware_utc(self.published_at))


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    resources: tuple[NormalizedResource, ...]
    schema_valid: bool
    parser_schema_version: str
    declared_total: int | None
    normalized_total: int
    page: int
    page_count: int | None
    next_cursor: str | None
    terminal: bool
    response_sha256: str
    response_length: int
    replayable: bool

    def __post_init__(self) -> None:
        if self.page < 1 or self.normalized_total < 0 or self.response_length < 0:
            raise ValueError("invalid discovery summary")
        if len(self.response_sha256) != 64:
            raise ValueError("full discovery response SHA-256 is required")
        if self.declared_total is not None and self.declared_total < 0:
            raise ValueError("declared_total cannot be negative")


@dataclass(frozen=True, slots=True)
class CompanyBootstrapResult:
    ticker: str
    company_name: str | None
    exchange: str
    listing_date: datetime | None = None
    prospectus_date: datetime | None = None
    anchor_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchWork:
    query: QueryWork
    resource: NormalizedResource
    validators: Mapping[str, str] = field(default_factory=dict)


class TransportClient(Protocol):
    def request(self, work: QueryWork) -> BoundedTransportEnvelope: ...


SnapshotReader = Callable[[str], bytes]


@runtime_checkable
class AcquisitionSourceAdapter(Protocol):
    adapter_key: str

    def bootstrap_company(
        self,
        ticker: str,
        query: QueryWork | None = None,
    ) -> CompanyBootstrapResult: ...

    def execute_query(self, work: QueryWork) -> BoundedTransportEnvelope: ...

    def parse_retained_discovery(
        self,
        snapshot_id: str,
        work: QueryWork,
    ) -> DiscoveryResult: ...

    def validate_and_normalize_without_retention(
        self,
        envelope: BoundedTransportEnvelope,
        work: QueryWork,
    ) -> DiscoveryResult: ...

    def fetch_resource(self, work: FetchWork) -> BoundedTransportEnvelope: ...
