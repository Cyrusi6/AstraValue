from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import PurePath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(r"^\d{6}$")
STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def acquisition_new_id() -> str:
    return str(uuid4())


def acquisition_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("采集时间必须包含时区")
    return value.astimezone(timezone.utc)


AwareDateTime = Annotated[datetime, AfterValidator(_aware_utc)]


def _require_sha256(value: str, *, field_name: str = "sha256") -> str:
    normalized = value.lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field_name}必须是完整的64位SHA-256")
    return normalized


def _require_stable_id(value: str) -> str:
    if not STABLE_ID_RE.fullmatch(value):
        raise ValueError("稳定ID只能包含字母、数字、点、下划线、冒号和连字符")
    return value


def _require_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePath(normalized)
    if not normalized or path.is_absolute() or normalized.startswith(("/", "\\")):
        raise ValueError("归档标识必须是data_root内的相对路径")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("归档标识不得包含空段、当前目录或父目录穿越")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("归档标识不得包含Windows绝对路径")
    return normalized


def _require_https_url(value: str, *, field_name: str = "url") -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"{field_name}必须使用HTTPS")
    if not parsed.hostname:
        raise ValueError(f"{field_name}缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name}不得包含凭据")
    if parsed.fragment:
        raise ValueError(f"{field_name}不得包含fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name}端口无效") from exc
    if port not in (None, 443):
        raise ValueError(f"{field_name}只能使用默认HTTPS端口或显式443")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single canonical UTF-8 representation used by registry/run hashes."""

    if isinstance(value, BaseModel):
        model_name = type(value).__name__
        value = value.model_dump(mode="json", exclude_none=False)
        # Registry/definition payloads are immutable audit inputs. Fields
        # introduced by the v1.2 wire contract must not retroactively change
        # the canonical hashes of already published v1.0/v1.1 definitions.
        if model_name == "SourceDefinition":
            _strip_pre_v1_2_query_contract(value)
        elif model_name == "SourceRegistry":
            for definition in (
                *value.get("definitions", ()),
                *value.get("legacy_definitions", ()),
            ):
                _strip_pre_v1_2_query_contract(definition)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strip_pre_v1_2_query_contract(definition: dict[str, Any]) -> None:
    """Apply the canonical serialization used before request contract v1.2."""

    # Optional collection roles did not exist in historical immutable payloads.
    for key in ("collection_role", "supplements_source_id"):
        if definition.get(key) is None:
            definition.pop(key, None)
    incremental = definition.get("incremental_policy", {})
    if incremental.get("content_validator_compatible_from_versions") is None:
        incremental.pop("content_validator_compatible_from_versions", None)

    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", str(definition.get("version", "")))
    if match is None or (int(match.group(1)), int(match.group(2))) >= (1, 2):
        return
    for query in definition.get("queries", ()):
        query.pop("request_encoding", None)
        query.pop("fixed_headers", None)
        query.pop("parameter_bindings", None)


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def stable_acquisition_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{canonical_json_sha256(value)[:24]}"


class FrozenAcquisitionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class AcquisitionMode(str, Enum):
    BASELINE = "baseline"
    INCREMENTAL = "incremental"
    RECONCILE = "reconcile"


class AcquisitionRunKind(str, Enum):
    PRODUCTION = "production"
    SMOKE = "smoke"
    AD_HOC = "ad_hoc"


class AcquisitionRunEventType(str, Enum):
    PLANNED = "planned"
    LEASE_CLAIMED = "lease_claimed"
    LEASE_RENEWED = "lease_renewed"
    LEASE_RELEASED = "lease_released"
    LEASE_RECLAIMED = "lease_reclaimed"
    RUNNING = "running"
    FINALIZED = "finalized"


class AcquisitionRunResult(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class AttemptKind(str, Enum):
    DISCOVERY = "discovery"
    FETCH = "fetch"


class AcquisitionOutcome(str, Enum):
    """The complete and closed v1 set of protocol/transport outcomes."""

    SUCCESS = "success"
    UNCHANGED = "unchanged"
    NO_DATA = "no_data"
    RESTRICTED = "restricted"
    PAYWALLED = "paywalled"
    LOGIN_REQUIRED = "login_required"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_FAILED = "network_failed"
    PARSE_FAILED = "parse_failed"
    POLICY_SKIPPED = "policy_skipped"
    PARTIAL_SUCCESS = "partial_success"


class AcquisitionAttemptEventType(str, Enum):
    STARTED = "started"
    SEGMENT_COMMITTED = "segment_committed"
    OUTCOME_TERMINAL = "outcome_terminal"
    ABANDONED = "abandoned"


class SourcePolicyStatus(str, Enum):
    ENABLED = "enabled"
    PENDING_POLICY = "pending_policy"
    DISABLED = "disabled"
    LEGACY = "legacy"


class PolicyDecision(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"


class LiveAccessReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"


class LiveAccessReviewCheck(str, Enum):
    EXACT_REQUEST_ENDPOINTS = "exact_request_endpoints"
    EXACT_REDIRECT_BOUNDARIES = "exact_redirect_boundaries"
    ACCESS_METHOD = "access_method"
    RESPONSE_SCHEMA = "response_schema"
    TIME_SEMANTICS = "time_semantics"
    RETENTION = "discovery_and_content_retention"
    RESPONSE_LIMITS = "response_limits"
    RATE_AND_RETRY = "rate_and_retry"
    LICENSE_TERMS = "license_terms"
    LLM_TERMS = "llm_terms"


class AccessCost(str, Enum):
    FREE = "free"
    USER_CONFIGURED = "user_configured"
    PAID = "paid"


class DiscoveryBodyPolicy(str, Enum):
    RETAIN = "retain"
    MINIMAL_PROOF = "minimal_proof"
    FORBIDDEN = "forbidden"


class QueryStage(str, Enum):
    DISCOVERY = "discovery"


class FetchPolicy(str, Enum):
    METADATA_ONLY = "metadata_only"
    REQUIRED_ATTACHMENT = "required_attachment"


class PublishedAtPrecision(str, Enum):
    INSTANT = "instant"
    DATE = "date"
    UNKNOWN = "unknown"


class ResourceRole(str, Enum):
    CONTENT = "content"
    DISCOVERY_RESPONSE = "discovery_response"


class AvailableAtBasis(str, Enum):
    VERIFIED_PUBLISHED_INSTANT = "verified_published_instant"
    SOURCE_DATE_NEXT_BOUNDARY = "source_date_next_boundary"
    RETRIEVED_AT = "retrieved_at"
    LEGACY_VERIFIED = "legacy_verified"


class ResourceDisposition(str, Enum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class CoveragePlanDisposition(str, Enum):
    REQUIRED = "required"
    STATIC_POLICY_SKIPPED = "static_policy_skipped"


class CoverageResolutionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    STATIC_POLICY_SKIPPED = "static_policy_skipped"


class SourceCandidateStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class SnapshotIntegrityStatus(str, Enum):
    VERIFIED = "verified"
    QUARANTINED = "quarantined"


class BootstrapStage(str, Enum):
    PREFLIGHT = "preflight"
    BACKUP_V4_VERIFIED = "backup_v4_verified"
    MIGRATED_V5 = "migrated_v5"
    BACKUP_V5_VERIFIED = "backup_v5_verified"
    COMMITTED_V6 = "committed_v6"
    MARKER_BOUND = "marker_bound"


class SourceEndpointRule(FrozenAcquisitionModel):
    scheme: Literal["https"] = "https"
    host: str
    port: int = Field(default=443, ge=1, le=65535)
    path_prefix: str = "/"

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        normalized = value.rstrip(".").lower()
        if not normalized or "*" in normalized or "/" in normalized or "://" in normalized:
            raise ValueError("allowlist host必须是精确主机名，不允许通配符")
        return normalized

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: str) -> str:
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError("allowlist path_prefix必须是无穿越的绝对URL路径前缀")
        return value

    @model_validator(mode="after")
    def validate_https_port(self) -> "SourceEndpointRule":
        if self.port != 443:
            raise ValueError("v1来源仅允许HTTPS 443端口")
        return self

    def matches(self, url: str) -> bool:
        try:
            checked = _require_https_url(url)
            parsed = urlsplit(checked)
        except ValueError:
            return False
        return (
            parsed.scheme.lower() == self.scheme
            and (parsed.hostname or "").lower() == self.host
            and (parsed.port or 443) == self.port
            and (parsed.path or "/").startswith(self.path_prefix)
        )


class SourceLicensePolicy(FrozenAcquisitionModel):
    automated_access: PolicyDecision
    archive_original: PolicyDecision
    save_derived_text: PolicyDecision
    llm_processing: PolicyDecision
    access_cost: AccessCost = AccessCost.FREE
    evidence_basis: str = Field(min_length=1)
    checked_at: AwareDateTime | None = None
    restrictions: tuple[str, ...] = ()


class SourceLiveAccessReview(FrozenAcquisitionModel):
    """Versioned review decision; human sign-off is required before approval."""

    status: LiveAccessReviewStatus
    checklist_version: str = Field(min_length=1)
    completed_checks: tuple[LiveAccessReviewCheck, ...] = ()
    reviewed_at: AwareDateTime | None = None
    reviewed_by: str | None = Field(default=None, min_length=1)
    evidence_reference: str | None = Field(default=None, min_length=1)

    @field_validator("completed_checks")
    @classmethod
    def validate_unique_checks(
        cls, values: tuple[LiveAccessReviewCheck, ...]
    ) -> tuple[LiveAccessReviewCheck, ...]:
        if len(set(values)) != len(values):
            raise ValueError("live access人工审核检查项不得重复")
        return values

    @model_validator(mode="after")
    def validate_review_decision(self) -> "SourceLiveAccessReview":
        if self.status in {
            LiveAccessReviewStatus.APPROVED,
            LiveAccessReviewStatus.REJECTED,
        }:
            decision_label = (
                "批准"
                if self.status == LiveAccessReviewStatus.APPROVED
                else "拒绝"
            )
            missing = set(LiveAccessReviewCheck) - set(self.completed_checks)
            if missing:
                raise ValueError(
                    f"live access{decision_label}缺少人工审核检查项: "
                    + ", ".join(sorted(item.value for item in missing))
                )
            if not (self.reviewed_at and self.reviewed_by and self.evidence_reference):
                raise ValueError(
                    f"live access{decision_label}必须记录复核人、时间和证据引用"
                )
        return self


class SourceRetentionPolicy(FrozenAcquisitionModel):
    discovery_body: DiscoveryBodyPolicy
    content_body: PolicyDecision
    independent_replay_required: bool = False

    @model_validator(mode="after")
    def validate_replay_contract(self) -> "SourceRetentionPolicy":
        if self.independent_replay_required and self.discovery_body != DiscoveryBodyPolicy.RETAIN:
            raise ValueError("要求独立重放时必须允许保留discovery body")
        return self


class SourceResponseLimits(FrozenAcquisitionModel):
    max_response_bytes: int = Field(gt=0)
    max_compressed_bytes: int = Field(gt=0)
    max_decompressed_bytes: int = Field(gt=0)
    max_redirects: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def validate_sizes(self) -> "SourceResponseLimits":
        if self.max_compressed_bytes > self.max_decompressed_bytes:
            raise ValueError("压缩响应上限不得大于解压后上限")
        if self.max_response_bytes > self.max_decompressed_bytes:
            raise ValueError("响应上限不得大于解压后上限")
        return self


class SourceRateLimitPolicy(FrozenAcquisitionModel):
    max_concurrency: int = Field(default=1, ge=1)
    min_interval_seconds: float = Field(gt=0)
    workspace_shared: bool = True


class SourceRetryPolicy(FrozenAcquisitionModel):
    max_attempts: int = Field(default=1, ge=1, le=10)
    retryable_methods: tuple[Literal["GET", "HEAD", "POST"], ...] = ("GET", "HEAD")
    initial_backoff_seconds: float = Field(default=0.5, ge=0)
    max_backoff_seconds: float = Field(default=5.0, ge=0)
    retry_after_cap_seconds: float = Field(default=30.0, gt=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    attempt_deadline_seconds: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def validate_deadline(self) -> "SourceRetryPolicy":
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("初始退避不得大于最大退避")
        if self.request_timeout_seconds > self.attempt_deadline_seconds:
            raise ValueError("单请求超时不得晚于attempt deadline")
        if self.retry_after_cap_seconds > self.attempt_deadline_seconds:
            raise ValueError("Retry-After上限不得超过attempt deadline")
        if len(set(self.retryable_methods)) != len(self.retryable_methods):
            raise ValueError("retryable_methods不得重复")
        return self


class SourceIncrementalPolicy(FrozenAcquisitionModel):
    overlap_days: int = Field(ge=0)
    canonical_id_required: bool = True
    use_etag: bool = True
    use_last_modified: bool = True
    hash_is_final: bool = True
    checkpoint_compatible_from_versions: tuple[str, ...] = ()
    content_validator_compatible_from_versions: tuple[str, ...] | None = None
    historical_reconcile_days: int | None = Field(default=None, gt=0)
    silent_replacement_limit: str | None = None

    @field_validator("content_validator_compatible_from_versions")
    @classmethod
    def validate_content_versions(cls, values: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if values is not None and (len(values) != len(set(values)) or any(
            not re.fullmatch(r"\d+\.\d+\.\d+", value) for value in values
        )):
            raise ValueError("正文validator兼容版本必须为不重复的语义版本")
        return values


class SourceApplicability(FrozenAcquisitionModel):
    markets: tuple[Literal["ALL", "SSE", "SZSE"], ...] = ("ALL",)
    tickers: tuple[str, ...] = ()
    excluded_tickers: tuple[str, ...] = ()

    @field_validator("tickers", "excluded_tickers")
    @classmethod
    def validate_tickers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if not TICKER_RE.fullmatch(value):
                raise ValueError("适用性ticker必须是6位数字")
        if len(set(values)) != len(values):
            raise ValueError("适用性ticker不得重复")
        return values

    @model_validator(mode="after")
    def validate_sets(self) -> "SourceApplicability":
        if set(self.tickers) & set(self.excluded_tickers):
            raise ValueError("ticker不能同时位于纳入与排除清单")
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("markets不得重复")
        if "ALL" in self.markets and len(self.markets) != 1:
            raise ValueError("ALL不能与具体市场同时声明")
        return self

    def applies_to(self, ticker: str, market: str) -> bool:
        if ticker in self.excluded_tickers:
            return False
        if self.tickers and ticker not in self.tickers:
            return False
        return "ALL" in self.markets or market in self.markets


class PaginationPolicy(FrozenAcquisitionModel):
    strategy: Literal["none", "page", "cursor"]
    page_parameter: str | None = None
    page_size_parameter: str | None = None
    page_size: int | None = Field(default=None, gt=0)
    cursor_parameter: str | None = None
    termination: Literal[
        "single_response",
        "declared_total",
        "declared_page_count",
        "empty_page_with_total",
        "next_cursor_absent",
    ]
    total_path: str | None = None
    items_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_strategy(self) -> "PaginationPolicy":
        if self.strategy == "page" and not (self.page_parameter and self.page_size):
            raise ValueError("page分页必须声明page_parameter与page_size")
        if self.strategy == "cursor" and not self.cursor_parameter:
            raise ValueError("cursor分页必须声明cursor_parameter")
        if self.strategy == "none" and any(
            value is not None
            for value in (self.page_parameter, self.page_size_parameter, self.page_size, self.cursor_parameter)
        ):
            raise ValueError("非分页查询不得声明分页参数")
        if self.termination in {"declared_total", "empty_page_with_total"} and not self.total_path:
            raise ValueError("该分页终止语义必须声明total_path")
        return self


class DiscoverySchemaPolicy(FrozenAcquisitionModel):
    schema_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    response_mime_types: tuple[str, ...]
    root_type: Literal["object", "array"]
    required_paths: tuple[str, ...]
    resource_fields: dict[str, str]

    @model_validator(mode="after")
    def validate_schema(self) -> "DiscoverySchemaPolicy":
        if not self.response_mime_types:
            raise ValueError("response_mime_types不能为空")
        if not self.required_paths:
            raise ValueError("required_paths不能为空")
        required_resource_fields = {"canonical_id", "url", "title"}
        if not required_resource_fields <= set(self.resource_fields):
            raise ValueError("resource_fields必须声明canonical_id、url和title")
        return self


class SourceParameterBinding(FrozenAcquisitionModel):
    """Resolve one wire parameter from an earlier persisted discovery row."""

    source_query_id: str
    match_metadata_key: str
    match_value_template: str = Field(min_length=1)
    value_metadata_key: str
    value_template: str = Field(default="{value}", min_length=1)

    @field_validator(
        "source_query_id",
        "match_metadata_key",
        "value_metadata_key",
    )
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _require_stable_id(value)

    @model_validator(mode="after")
    def validate_templates(self) -> "SourceParameterBinding":
        if "{value}" not in self.value_template:
            raise ValueError("parameter binding value_template必须包含{value}")
        return self


class SourceQueryDefinition(FrozenAcquisitionModel):
    query_id: str
    query_family: str
    query_stage: QueryStage = QueryStage.DISCOVERY
    execution_key: str
    adapter_operation: str
    question_ids: tuple[str, ...]
    request_method: Literal["GET", "POST", "SDK"]
    request_encoding: Literal["query", "form", "json", "sdk"] = "query"
    fixed_headers: dict[str, str] = Field(default_factory=dict)
    endpoint: str | None = None
    parameter_template: dict[str, Any] = Field(default_factory=dict)
    parameter_bindings: dict[str, SourceParameterBinding] = Field(default_factory=dict)
    allowed_parameter_names: tuple[str, ...] = ()
    partition_key: str
    earliest_available_at: AwareDateTime | None = None
    max_window_days: int | None = Field(default=None, gt=0)
    pagination: PaginationPolicy
    discovery_schema: DiscoverySchemaPolicy
    discovery_body_policy: DiscoveryBodyPolicy
    fetch_policy: FetchPolicy
    canonical_id_rule: str = Field(min_length=1)
    smoke_enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_request_encoding(cls, value: Any) -> Any:
        if isinstance(value, dict) and "request_encoding" not in value:
            method = str(value.get("request_method", "")).upper()
            if method in {"GET", "POST", "SDK"}:
                return {
                    **value,
                    "request_encoding": (
                        "sdk" if method == "SDK" else "query" if method == "GET" else "form"
                    ),
                }
        return value

    @field_validator("query_id", "query_family", "execution_key", "adapter_operation", "partition_key")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _require_stable_id(value)

    @field_validator("question_ids")
    @classmethod
    def validate_question_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("业务采集query必须映射至少一个question ID")
        if len(set(values)) != len(values):
            raise ValueError("question_ids不得重复")
        return values

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        return None if value is None else _require_https_url(value, field_name="query endpoint")

    @model_validator(mode="after")
    def validate_parameters(self) -> "SourceQueryDefinition":
        undeclared = set(self.parameter_template) - set(self.allowed_parameter_names)
        if undeclared:
            raise ValueError(f"parameter_template含未允许参数: {sorted(undeclared)}")
        undeclared_bindings = set(self.parameter_bindings) - set(
            self.allowed_parameter_names
        )
        if undeclared_bindings:
            raise ValueError(
                f"parameter_bindings含未允许参数: {sorted(undeclared_bindings)}"
            )
        duplicated = set(self.parameter_template) & set(self.parameter_bindings)
        if duplicated:
            raise ValueError(
                f"参数不得同时由template和discovery binding提供: {sorted(duplicated)}"
            )
        if self.request_method == "GET" and self.request_encoding != "query":
            raise ValueError("GET query只能使用query参数编码")
        seen_headers: set[str] = set()
        forbidden_headers = {
            "authorization",
            "cookie",
            "proxy-authorization",
            "x-api-key",
            "x-auth-token",
            "host",
            "content-length",
        }
        for raw_name, raw_value in self.fixed_headers.items():
            name = str(raw_name).strip()
            normalized = name.lower()
            if (
                not name
                or normalized in seen_headers
                or normalized in forbidden_headers
                or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
            ):
                raise ValueError(f"fixed_headers包含禁止或重复的header: {raw_name}")
            if not isinstance(raw_value, str) or not raw_value or "\r" in raw_value or "\n" in raw_value:
                raise ValueError(f"fixed_headers值非法: {raw_name}")
            seen_headers.add(normalized)
        return self


class SourceDefinition(FrozenAcquisitionModel):
    source_definition_id: str
    version: str
    upstream_identity: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    authority_level: int = Field(ge=1, le=5)
    adapter_key: str
    policy_status: SourcePolicyStatus
    enabled: bool
    access_method: Literal["https_api", "https_document", "disabled", "legacy_adapter"]
    scopes: tuple[str, ...]
    scope_version: str | None = None
    topics: tuple[str, ...] = ()
    applicability: SourceApplicability
    refresh_frequency: str = Field(min_length=1)
    initial_request_allowlist: tuple[SourceEndpointRule, ...] = ()
    redirect_allowlist: tuple[SourceEndpointRule, ...] = ()
    source_timezone: str
    published_at_precision: PublishedAtPrecision
    incremental_policy: SourceIncrementalPolicy
    retention_policy: SourceRetentionPolicy
    response_limits: SourceResponseLimits
    rate_limit: SourceRateLimitPolicy
    retry_policy: SourceRetryPolicy
    license_policy: SourceLicensePolicy
    live_access_review: SourceLiveAccessReview
    effective_at: AwareDateTime
    expires_at: AwareDateTime | None = None
    queries: tuple[SourceQueryDefinition, ...] = ()
    aliases: tuple[str, ...] = ()
    legacy: bool = False
    collection_role: Literal["primary", "on_demand"] | None = None
    supplements_source_id: str | None = None
    content_selection_policy: Literal["business_model_no_standalone_audit_pdf_v1", "business_model_no_audit_english_annual_v1"] | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_selection_payload(self, handler):
        payload = handler(self)
        if self.content_selection_policy is None:
            payload.pop("content_selection_policy", None)
        return payload

    @field_validator("source_definition_id", "adapter_key")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _require_stable_id(value)

    @field_validator("source_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知来源时区: {value}") from exc
        return value

    @model_validator(mode="after")
    def validate_definition(self) -> "SourceDefinition":
        if self.collection_role == "on_demand":
            if not self.supplements_source_id or self.supplements_source_id == self.source_definition_id:
                raise ValueError("按需来源必须引用另一个主采来源")
        elif self.supplements_source_id is not None:
            raise ValueError("只有按需来源可以声明supplements_source_id")
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("来源定义expires_at必须晚于effective_at")
        if self.live_access_review.status == LiveAccessReviewStatus.REJECTED:
            if self.policy_status not in {
                SourcePolicyStatus.PENDING_POLICY,
                SourcePolicyStatus.DISABLED,
            }:
                raise ValueError("live access拒绝来源必须保持pending_policy或disabled")
            if self.enabled or self.access_method != "disabled":
                raise ValueError("live access拒绝来源必须disabled且零I/O")
            if self.initial_request_allowlist or self.redirect_allowlist:
                raise ValueError("live access拒绝来源不得保留可联网allowlist")
            if any(query.endpoint is not None for query in self.queries):
                raise ValueError("live access拒绝来源query不得具有可请求endpoint")
        if self.enabled != (self.policy_status in {SourcePolicyStatus.ENABLED, SourcePolicyStatus.LEGACY}):
            raise ValueError("enabled必须与policy_status一致")
        if self.policy_status == SourcePolicyStatus.PENDING_POLICY and self.enabled:
            raise ValueError("pending_policy来源不得启用")
        if self.enabled:
            if self.license_policy.automated_access != PolicyDecision.ALLOWED:
                raise ValueError("启用来源必须明确允许自动访问")
            if self.license_policy.checked_at is None:
                raise ValueError("启用来源必须记录许可检查时间")
        if self.access_method in {"https_api", "https_document"} and self.enabled:
            if not self.initial_request_allowlist or not self.redirect_allowlist:
                raise ValueError("启用HTTPS来源必须同时声明初始请求和逐跳重定向allowlist")
        if (
            self.access_method in {"https_api", "https_document"}
            and self.live_access_review.status == LiveAccessReviewStatus.NOT_REQUIRED
        ):
            raise ValueError("HTTPS来源不得跳过live access人工审核")
        if self.access_method == "disabled" and self.enabled:
            raise ValueError("disabled访问方式不得启用")
        if (
            self.access_method == "legacy_adapter"
            and self.live_access_review.status != LiveAccessReviewStatus.NOT_REQUIRED
        ):
            raise ValueError("legacy adapter不得伪装成已完成live access人工审核")
        if self.legacy != (self.policy_status == SourcePolicyStatus.LEGACY):
            raise ValueError("legacy字段必须与legacy policy_status一致")
        if len({item.query_id for item in self.queries}) != len(self.queries):
            raise ValueError("同一来源版本query_id必须唯一")
        if len({item.execution_key for item in self.queries}) != len(self.queries):
            raise ValueError("同一来源版本execution_key必须唯一")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("来源alias不得重复")
        query_ordinals = {
            query.query_id: ordinal for ordinal, query in enumerate(self.queries)
        }
        for query_ordinal, query in enumerate(self.queries):
            if query.endpoint is None:
                if self.enabled:
                    raise ValueError("启用来源的query必须具有固定endpoint")
                continue
            if not any(rule.matches(query.endpoint) for rule in self.initial_request_allowlist):
                raise ValueError(f"query endpoint不在初始请求allowlist: {query.query_id}")
            if query.discovery_body_policy != self.retention_policy.discovery_body:
                raise ValueError(f"query与来源discovery保留策略冲突: {query.query_id}")
            if (
                query.request_method not in self.retry_policy.retryable_methods
                and self.retry_policy.max_attempts > 1
            ):
                raise ValueError(f"非声明幂等方法不得配置自动重试: {query.query_id}")
            for binding in query.parameter_bindings.values():
                source_ordinal = query_ordinals.get(binding.source_query_id)
                if source_ordinal is None:
                    raise ValueError(
                        f"parameter binding引用未知query: {binding.source_query_id}"
                    )
                if source_ordinal >= query_ordinal:
                    raise ValueError("parameter binding只能引用此前执行的discovery query")
        if "business_model" in self.scopes:
            if self.scope_version != "v1":
                raise ValueError("business_model来源必须声明scope_version=v1")
            if self.rate_limit.max_concurrency != 1:
                raise ValueError("business_model v1来源最大并发固定为1")
            if self.license_policy.access_cost == AccessCost.PAID:
                raise ValueError("business_model v1不得包含付费来源")
        return self

    @property
    def source_definition_version(self) -> str:
        return self.version

    def applies_to(self, ticker: str, market: str) -> bool:
        return self.applicability.applies_to(ticker, market)

    def allows_url(self, url: str, *, redirect: bool = False) -> bool:
        rules = self.redirect_allowlist if redirect else self.initial_request_allowlist
        return any(rule.matches(url) for rule in rules)


class SourceDefinitionRef(FrozenAcquisitionModel):
    source_definition_id: str
    version: str
    content_hash: str

    _hash = field_validator("content_hash")(
        lambda value: _require_sha256(value, field_name="source definition content_hash")
    )


class SourceAlias(FrozenAcquisitionModel):
    alias: str
    target_source_definition_ids: tuple[str, ...]
    scopes: tuple[str, ...]
    deprecated: bool = True

    @model_validator(mode="after")
    def validate_targets(self) -> "SourceAlias":
        if not self.target_source_definition_ids:
            raise ValueError("alias必须至少指向一个来源定义")
        if len(set(self.target_source_definition_ids)) != len(self.target_source_definition_ids):
            raise ValueError("alias目标不得重复")
        return self


class SourceRegistry(FrozenAcquisitionModel):
    schema_version: str
    registry_id: str
    registry_version: str
    effective_at: AwareDateTime
    question_set_version: str
    definitions: tuple[SourceDefinition, ...]
    legacy_definitions: tuple[SourceDefinition, ...] = ()
    aliases: tuple[SourceAlias, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> "SourceRegistry":
        all_definitions = (*self.definitions, *self.legacy_definitions)
        identities = [(item.source_definition_id, item.version) for item in all_definitions]
        if len(set(identities)) != len(identities):
            raise ValueError("注册表内source_definition_id/version必须唯一")
        current_ids = [item.source_definition_id for item in self.definitions]
        if len(set(current_ids)) != len(current_ids):
            raise ValueError("当前注册表同一source_definition_id只能有一个版本")
        aliases = [item.alias for item in self.aliases]
        if len(set(aliases)) != len(aliases):
            raise ValueError("注册表alias必须唯一")
        known_ids = set(current_ids) | {item.source_definition_id for item in self.legacy_definitions}
        by_id = {d.source_definition_id: d for d in self.definitions}
        for definition in self.definitions:
            if definition.collection_role == "on_demand":
                primary = by_id.get(definition.supplements_source_id)
                if primary is None or primary.collection_role == "on_demand" or not primary.enabled:
                    raise ValueError("按需来源必须引用已启用主采定义")
        missing = {
            target
            for alias in self.aliases
            for target in alias.target_source_definition_ids
            if target not in known_ids
        }
        if missing:
            raise ValueError(f"alias指向未知来源定义: {sorted(missing)}")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_json_sha256(self)

    def definition(self, source_definition_id: str) -> SourceDefinition:
        for definition in (*self.definitions, *self.legacy_definitions):
            if definition.source_definition_id == source_definition_id:
                return definition
        raise KeyError(source_definition_id)


class BusinessQuestion(FrozenAcquisitionModel):
    question_id: str
    topic: str = Field(min_length=1)
    description: str = Field(min_length=1)
    query_families: tuple[str, ...]

    @field_validator("question_id")
    @classmethod
    def validate_question_id(cls, value: str) -> str:
        return _require_stable_id(value)

    @model_validator(mode="after")
    def validate_families(self) -> "BusinessQuestion":
        if not self.query_families or len(set(self.query_families)) != len(self.query_families):
            raise ValueError("question必须映射一个或多个不重复query family")
        return self


class BusinessQuestionSet(FrozenAcquisitionModel):
    schema_version: str
    question_set_id: str
    version: str
    scope: Literal["business_model"]
    topics: tuple[BusinessQuestion, ...]

    @model_validator(mode="after")
    def validate_topics(self) -> "BusinessQuestionSet":
        ids = [item.question_id for item in self.topics]
        if len(ids) != 10:
            raise ValueError("business_model v1问题清单必须恰含十项主题")
        if len(set(ids)) != len(ids):
            raise ValueError("question_id必须唯一")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_json_sha256(self)


class AnchorEvidence(FrozenAcquisitionModel):
    value_date: date
    source_definition_id: str
    snapshot_id: str | None = None
    proof_id: str | None = None
    quality: Literal["verified", "source_declared", "fallback"] = "verified"

    @model_validator(mode="after")
    def validate_evidence(self) -> "AnchorEvidence":
        if self.quality == "verified" and not (self.snapshot_id or self.proof_id):
            raise ValueError("verified公司锚点必须引用snapshot或proof")
        return self


class CompanyAcquisitionProfile(FrozenAcquisitionModel):
    ticker: str
    company_name: str
    market: Literal["SSE", "SZSE"]
    listing_date: date | None = None
    listing_evidence: AnchorEvidence | None = None
    prospectus_date: date | None = None
    prospectus_evidence: AnchorEvidence | None = None
    fallback_earliest_date: date | None = None
    fallback_reason: str | None = None

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        if not TICKER_RE.fullmatch(value):
            raise ValueError("A股ticker必须是6位数字")
        return value

    @model_validator(mode="after")
    def validate_anchors(self) -> "CompanyAcquisitionProfile":
        if self.listing_evidence and self.listing_evidence.value_date != self.listing_date:
            raise ValueError("上市日与其证据值不一致")
        if self.prospectus_evidence and self.prospectus_evidence.value_date != self.prospectus_date:
            raise ValueError("招股书日期与其证据值不一致")
        if self.fallback_earliest_date is not None and not self.fallback_reason:
            raise ValueError("fallback起点必须说明依据")
        return self

    def history_start(self) -> tuple[date, str]:
        anchors = [value for value in (self.prospectus_date, self.listing_date) if value is not None]
        if anchors:
            return min(anchors), "prospectus_or_listing"
        if self.fallback_earliest_date is not None:
            return self.fallback_earliest_date, "source_earliest_fallback"
        raise ValueError("缺少可审计的baseline历史起点")


class AcquisitionRun(FrozenAcquisitionModel):
    run_id: str = Field(default_factory=acquisition_new_id)
    ticker: str
    company_name: str
    mode: AcquisitionMode
    run_kind: AcquisitionRunKind = AcquisitionRunKind.PRODUCTION
    as_of: AwareDateTime
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    registry_id: str
    registry_version: str
    registry_content_hash: str
    question_set_id: str
    question_set_version: str
    question_set_content_hash: str
    source_definition_refs: tuple[SourceDefinitionRef, ...]
    request_scope: Literal["complete", "ad_hoc"] = "complete"
    parent_run_id: str | None = None
    reconcile_target: dict[str, Any] | None = None
    company_anchor_date: date | None = None
    company_anchor_quality: str | None = None
    storage_namespace_id: str | None = None

    # Missing in historical payloads: never infer an earlier run's route.
    http_route_policy: Literal["direct-v1"] | None = None

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        if not TICKER_RE.fullmatch(value):
            raise ValueError("A股ticker必须是6位数字")
        return value

    @field_validator("registry_content_hash", "question_set_content_hash")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def validate_run(self) -> "AcquisitionRun":
        if self.run_kind == AcquisitionRunKind.PRODUCTION and self.request_scope != "complete":
            raise ValueError("production run不得用ad_hoc范围声称完整")
        if self.run_kind == AcquisitionRunKind.AD_HOC and self.request_scope != "ad_hoc":
            raise ValueError("ad_hoc run必须显式标记request_scope=ad_hoc")
        if self.mode == AcquisitionMode.RECONCILE and not self.parent_run_id:
            raise ValueError("reconcile run必须关联parent_run_id")
        if self.mode != AcquisitionMode.RECONCILE and self.reconcile_target is not None:
            raise ValueError("只有reconcile run可以声明reconcile_target")
        identities = [(item.source_definition_id, item.version) for item in self.source_definition_refs]
        if len(set(identities)) != len(identities):
            raise ValueError("run固定的来源定义引用不得重复")
        return self


class AcquisitionRunEvent(FrozenAcquisitionModel):
    event_id: str = Field(default_factory=acquisition_new_id)
    run_id: str
    event_type: AcquisitionRunEventType
    occurred_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    lease_epoch: int | None = Field(default=None, ge=1)
    result: AcquisitionRunResult | None = None
    coverage_accounted: bool | None = None
    material_gap_count: int | None = Field(default=None, ge=0)
    default_consume_eligible: bool | None = None
    reason_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> "AcquisitionRunEvent":
        terminal_values = (
            self.result,
            self.coverage_accounted,
            self.material_gap_count,
            self.default_consume_eligible,
        )
        if self.event_type == AcquisitionRunEventType.FINALIZED:
            if any(value is None for value in terminal_values):
                raise ValueError("finalized run event必须包含全部终态字段")
            if self.default_consume_eligible and (
                not self.coverage_accounted or self.material_gap_count != 0 or self.result != AcquisitionRunResult.SUCCEEDED
            ):
                raise ValueError("默认可消费run必须成功、覆盖可解释且没有材料缺口")
        elif any(value is not None for value in terminal_values):
            raise ValueError("非finalized run event不得携带终态字段")
        return self


class TimeSliceMixin(FrozenAcquisitionModel):
    time_start: AwareDateTime
    time_end: AwareDateTime

    @model_validator(mode="after")
    def validate_time_slice(self) -> "TimeSliceMixin":
        if self.time_end <= self.time_start:
            raise ValueError("采集时间片必须是非空半开区间[start,end)")
        return self


class PhysicalQueryPlanItem(TimeSliceMixin):
    plan_item_id: str
    run_id: str
    source_definition_id: str
    source_definition_version: str
    query_id: str
    query_family: str
    execution_key: str
    attempt_kind: AttemptKind = AttemptKind.DISCOVERY
    request_method: Literal["GET", "POST", "SDK"]
    request_encoding: Literal["query", "form", "json", "sdk"] = "query"
    fixed_headers: dict[str, str] = Field(default_factory=dict)
    parameter_binding_names: tuple[str, ...] = ()
    prerequisite_query_ids: tuple[str, ...] = ()
    prerequisite_plan_item_ids: tuple[str, ...] = ()
    endpoint: str
    normalized_parameters: dict[str, Any] = Field(default_factory=dict)
    partition_key: str
    pagination_fingerprint: str
    ordinal: int = Field(ge=0)
    parent_plan_item_id: str | None = None
    discovered_resource_id: str | None = None
    # Local, verified directory input; endpoint remains an origin descriptor only.
    retained_inventory_ref: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_request_encoding(cls, value: Any) -> Any:
        if isinstance(value, dict) and "request_encoding" not in value:
            method = str(value.get("request_method", "")).upper()
            if method in {"GET", "POST", "SDK"}:
                return {
                    **value,
                    "request_encoding": (
                        "sdk" if method == "SDK" else "query" if method == "GET" else "form"
                    ),
                }
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if value.startswith("baostock+sdk://"):
            method = value.removeprefix("baostock+sdk://")
            if not re.fullmatch(r"query_[a-z0-9_]+", method):
                raise ValueError("BaoStock SDK endpoint必须是固定query_*方法")
            return value
        return _require_https_url(value, field_name="physical query endpoint")

    @model_validator(mode="after")
    def validate_kind(self) -> "PhysicalQueryPlanItem":
        is_sdk = self.endpoint.startswith("baostock+sdk://")
        if is_sdk != (self.request_method == "SDK"):
            raise ValueError("BaoStock SDK端点必须使用SDK方法且HTTP端点不得标为SDK")
        if (self.request_encoding == "sdk") != (self.request_method == "SDK"):
            raise ValueError("SDK方法必须使用sdk请求编码")
        if self.retained_inventory_ref is not None:
            if self.attempt_kind != AttemptKind.DISCOVERY:
                raise ValueError("本地目录只能作为discovery输入")
            _require_sha256(self.retained_inventory_ref["sha256"])
            _require_relative_path(self.retained_inventory_ref["archive_relative_path"])
            if self.retained_inventory_ref["byte_length"] <= 0:
                raise ValueError("本地目录必须非空")
        if self.attempt_kind == AttemptKind.FETCH and not (
            self.parent_plan_item_id and self.discovered_resource_id
        ):
            raise ValueError("fetch plan item必须关联父discovery plan与资源")
        if self.attempt_kind == AttemptKind.DISCOVERY and any(
            value is not None for value in (self.parent_plan_item_id, self.discovered_resource_id)
        ):
            raise ValueError("discovery plan item不得伪造父资源关系")
        return self


class PhysicalQueryCoverageLink(FrozenAcquisitionModel):
    plan_item_id: str
    coverage_entry_id: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.plan_item_id, self.coverage_entry_id


class CoverageEntry(TimeSliceMixin):
    coverage_entry_id: str
    run_id: str
    source_definition_id: str
    source_definition_version: str
    question_id: str
    query_id: str
    plan_disposition: CoveragePlanDisposition
    static_reason_code: Literal[
        "source_not_available",
        "market_not_applicable",
        "pending_policy",
        "source_disabled",
        "no_relevant_query",
        "on_demand_supplement",
    ] | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> "CoverageEntry":
        if self.plan_disposition == CoveragePlanDisposition.REQUIRED and self.static_reason_code:
            raise ValueError("required coverage不得携带静态跳过理由")
        if self.plan_disposition == CoveragePlanDisposition.STATIC_POLICY_SKIPPED and not self.static_reason_code:
            raise ValueError("静态跳过coverage必须携带机器可读理由")
        return self


class CoverageResolution(FrozenAcquisitionModel):
    resolution_id: str = Field(default_factory=acquisition_new_id)
    coverage_entry_id: str
    status: CoverageResolutionStatus
    attempt_ids: tuple[str, ...] = ()
    discovery_proof_ids: tuple[str, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    material_gap_count: int = Field(default=0, ge=0)
    reason_codes: tuple[str, ...] = ()
    resolved_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    lease_epoch: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_resolution(self) -> "CoverageResolution":
        if self.status == CoverageResolutionStatus.COMPLETE and self.material_gap_count:
            raise ValueError("complete coverage不得有材料缺口")
        if self.status in {CoverageResolutionStatus.PARTIAL, CoverageResolutionStatus.BLOCKED} and not self.material_gap_count:
            raise ValueError("partial/blocked coverage必须显示材料缺口")
        if self.status == CoverageResolutionStatus.STATIC_POLICY_SKIPPED and self.attempt_ids:
            raise ValueError("静态处置coverage不得引用attempt")
        return self


class AcquisitionPlan(FrozenAcquisitionModel):
    run: AcquisitionRun
    coverage_entries: tuple[CoverageEntry, ...]
    physical_query_plan_items: tuple[PhysicalQueryPlanItem, ...]
    coverage_links: tuple[PhysicalQueryCoverageLink, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> "AcquisitionPlan":
        if any(p.retained_inventory_ref is not None for p in self.physical_query_plan_items):
            if self.run.run_kind != AcquisitionRunKind.AD_HOC or self.run.request_scope != "ad_hoc":
                raise ValueError("本地目录归档只能创建ad_hoc运行")
        coverage_ids = {item.coverage_entry_id for item in self.coverage_entries}
        plan_ids = {item.plan_item_id for item in self.physical_query_plan_items}
        if len(coverage_ids) != len(self.coverage_entries):
            raise ValueError("coverage_entry_id必须唯一")
        if len(plan_ids) != len(self.physical_query_plan_items):
            raise ValueError("plan_item_id必须唯一")
        if len({item.identity for item in self.coverage_links}) != len(self.coverage_links):
            raise ValueError("plan-to-coverage link不得重复")
        for link in self.coverage_links:
            if link.plan_item_id not in plan_ids or link.coverage_entry_id not in coverage_ids:
                raise ValueError("plan-to-coverage link存在悬空引用")
        linked_required = {
            link.coverage_entry_id
            for link in self.coverage_links
        }
        missing = {
            item.coverage_entry_id
            for item in self.coverage_entries
            if item.plan_disposition == CoveragePlanDisposition.REQUIRED
            and item.coverage_entry_id not in linked_required
        }
        if missing:
            raise ValueError(f"required coverage缺少physical plan link: {sorted(missing)}")
        static_linked = {
            item.coverage_entry_id
            for item in self.coverage_entries
            if item.plan_disposition == CoveragePlanDisposition.STATIC_POLICY_SKIPPED
            and item.coverage_entry_id in linked_required
        }
        if static_linked:
            raise ValueError(f"静态处置coverage不得链接物理I/O计划: {sorted(static_linked)}")
        return self


class AcquisitionExecutionLease(FrozenAcquisitionModel):
    run_id: str
    owner_token_hash: str
    lease_epoch: int = Field(ge=1)
    acquired_at: AwareDateTime
    heartbeat_at: AwareDateTime
    expires_at: AwareDateTime
    released_at: AwareDateTime | None = None

    @field_validator("owner_token_hash")
    @classmethod
    def validate_owner_hash(cls, value: str) -> str:
        return _require_sha256(value, field_name="owner_token_hash")

    @model_validator(mode="after")
    def validate_lease(self) -> "AcquisitionExecutionLease":
        if self.heartbeat_at < self.acquired_at:
            raise ValueError("lease heartbeat不能早于取得时间")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("lease必须具有正TTL")
        if self.released_at is not None and self.released_at < self.acquired_at:
            raise ValueError("lease释放时间不能早于取得时间")
        return self

    @property
    def ttl(self) -> timedelta:
        return self.expires_at - self.heartbeat_at

    def is_active_at(self, value: datetime) -> bool:
        checked = _aware_utc(value)
        return self.released_at is None and checked < self.expires_at


class AcquisitionAttempt(FrozenAcquisitionModel):
    attempt_id: str = Field(default_factory=acquisition_new_id)
    run_id: str
    source_definition_id: str
    source_definition_version: str
    physical_query_plan_item_id: str
    execution_key: str
    attempt_kind: AttemptKind
    query_id: str | None = None
    discovered_resource_id: str | None = None
    parent_discovery_attempt_id: str | None = None
    time_start: AwareDateTime | None = None
    time_end: AwareDateTime | None = None
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    work_position: str
    retry_group_id: str
    retry_ordinal: int = Field(default=0, ge=0)
    lease_epoch: int = Field(ge=1)
    request_summary: dict[str, Any] = Field(default_factory=dict)
    started_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    supersedes_attempt_id: str | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> "AcquisitionAttempt":
        if (self.time_start is None) != (self.time_end is None):
            raise ValueError("attempt时间范围必须同时具有起止")
        if self.time_start is not None and self.time_end <= self.time_start:
            raise ValueError("attempt时间范围必须是非空半开区间")
        if self.attempt_kind == AttemptKind.DISCOVERY:
            if self.query_id is None:
                raise ValueError("discovery attempt必须引用query_id")
            if self.discovered_resource_id or self.parent_discovery_attempt_id:
                raise ValueError("discovery attempt不得伪造fetch父关系")
        else:
            if not (self.discovered_resource_id and self.parent_discovery_attempt_id):
                raise ValueError("fetch attempt必须引用discovered resource与父discovery attempt")
        if self.supersedes_attempt_id == self.attempt_id:
            raise ValueError("attempt不得supersede自身")
        return self


class AcquisitionAttemptEvent(FrozenAcquisitionModel):
    event_id: str = Field(default_factory=acquisition_new_id)
    attempt_id: str
    event_type: AcquisitionAttemptEventType
    occurred_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    lease_epoch: int = Field(ge=1)
    outcome: AcquisitionOutcome | None = None
    reason_code: str | None = None
    segment_id: str | None = None
    proof_ids: tuple[str, ...] = ()
    snapshot_ids: tuple[str, ...] = ()
    protocol_summary: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AcquisitionAttemptEvent":
        if self.event_type == AcquisitionAttemptEventType.OUTCOME_TERMINAL:
            if self.outcome is None:
                raise ValueError("outcome_terminal事件必须包含12类结果之一")
            if self.segment_id is not None:
                raise ValueError("outcome_terminal不得伪装成segment事件")
        elif self.outcome is not None:
            raise ValueError("abandoned/started/segment事件不得携带来源outcome")
        if self.event_type == AcquisitionAttemptEventType.SEGMENT_COMMITTED and not self.segment_id:
            raise ValueError("segment_committed事件必须引用segment")
        if self.event_type != AcquisitionAttemptEventType.SEGMENT_COMMITTED and self.segment_id:
            raise ValueError("只有segment_committed事件可以引用segment")
        if self.event_type == AcquisitionAttemptEventType.ABANDONED and not self.reason_code:
            raise ValueError("abandoned closure必须说明原因")
        return self


class AcquisitionAttemptSegment(FrozenAcquisitionModel):
    segment_id: str = Field(default_factory=acquisition_new_id)
    attempt_id: str
    segment_ordinal: int = Field(ge=0)
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    work_position: str
    lease_epoch: int = Field(ge=1)
    committed_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    discovery_observation_id: str | None = None
    discovery_proof_id: str | None = None
    snapshot_ids: tuple[str, ...] = ()
    next_safe_position: str | None = None


class DiscoveryObservation(FrozenAcquisitionModel):
    observation_id: str = Field(default_factory=acquisition_new_id)
    attempt_id: str
    physical_query_plan_item_id: str
    source_definition_id: str
    source_definition_version: str
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    observed_at: AwareDateTime
    retrieved_at: AwareDateTime
    http_status: int | None = Field(default=None, ge=100, le=599)
    mime_type: str | None = None
    response_sha256: str | None = None
    response_byte_length: int | None = Field(default=None, ge=0)
    snapshot_id: str | None = None
    request_summary: dict[str, Any] = Field(default_factory=dict)
    response_summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("response_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value, field_name="response_sha256")

    @model_validator(mode="after")
    def validate_observation(self) -> "DiscoveryObservation":
        if self.retrieved_at < self.observed_at:
            raise ValueError("retrieved_at不能早于observed_at")
        if (self.response_sha256 is None) != (self.response_byte_length is None):
            raise ValueError("discovery响应哈希与长度必须成对出现")
        return self


class DiscoveryProof(FrozenAcquisitionModel):
    proof_id: str = Field(default_factory=acquisition_new_id)
    observation_id: str
    attempt_id: str
    physical_query_plan_item_id: str
    response_sha256: str
    response_byte_length: int = Field(ge=0)
    http_status: int | None = Field(default=None, ge=100, le=599)
    proof_kind: Literal["http_response", "retained_inventory"] = "http_response"
    mime_type: str
    parser_id: str
    parser_version: str
    schema_id: str
    schema_version: str
    schema_valid: bool
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    declared_total: int | None = Field(default=None, ge=0)
    declared_page_count: int | None = Field(default=None, ge=0)
    normalized_row_count: int = Field(ge=0)
    terminal: bool
    body_retained: bool
    replayable: bool
    discovery_snapshot_id: str | None = None
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)

    @field_validator("response_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value, field_name="response_sha256")

    @model_validator(mode="after")
    def validate_proof(self) -> "DiscoveryProof":
        if self.proof_kind == "http_response" and self.http_status is None:
            raise ValueError("HTTP proof必须有实际HTTP状态")
        if self.proof_kind == "retained_inventory" and (
            self.http_status is not None or not self.body_retained or self.normalized_row_count == 0
        ):
            raise ValueError("本地目录proof必须保留非空输入，不得伪造HTTP状态或no_data")
        if self.body_retained:
            if not self.discovery_snapshot_id or not self.replayable:
                raise ValueError("保留正文的discovery proof必须引用可重放response snapshot")
        elif self.discovery_snapshot_id is not None or self.replayable:
            raise ValueError("未保留正文的proof不得伪称有snapshot或可独立重放")
        if self.terminal and self.declared_total is not None:
            if self.normalized_row_count > self.declared_total:
                raise ValueError("规范化行数不得超过上游声明总数")
        return self

    @property
    def proves_no_data(self) -> bool:
        return (
            self.proof_kind == "http_response"
            and self.schema_valid
            and self.terminal
            and self.normalized_row_count == 0
            and self.declared_total == 0
        )


class DiscoveredResource(FrozenAcquisitionModel):
    discovered_resource_id: str = Field(default_factory=acquisition_new_id)
    proof_id: str
    discovery_observation_id: str
    discovery_attempt_id: str
    source_definition_id: str
    source_definition_version: str
    canonical_resource_id: str
    upstream_material_id: str | None = None
    resource_url: str
    title: str
    published_at_raw: str | None = None
    published_at: AwareDateTime | None = None
    published_at_precision: PublishedAtPrecision = PublishedAtPrecision.UNKNOWN
    source_timezone: str
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    row_locator: str | None = None
    row_hash: str
    required_fetch: bool
    expected_mime_types: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("resource_url")
    @classmethod
    def validate_resource_url(cls, value: str) -> str:
        return _require_https_url(value, field_name="resource_url")

    @field_validator("row_hash")
    @classmethod
    def validate_row_hash(cls, value: str) -> str:
        return _require_sha256(value, field_name="row_hash")

    @field_validator("source_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知来源时区: {value}") from exc
        return value

    @model_validator(mode="after")
    def validate_published_precision(self) -> "DiscoveredResource":
        if self.published_at_precision == PublishedAtPrecision.UNKNOWN and self.published_at is not None:
            raise ValueError("unknown发布时间精度不得伪造published_at")
        if self.published_at_precision != PublishedAtPrecision.UNKNOWN and self.published_at is None:
            raise ValueError("instant/date发布时间精度必须具有published_at")
        if not (self.row_locator or self.row_hash):
            raise ValueError("discovered resource必须保留row lineage")
        return self


class ResourceObservation(FrozenAcquisitionModel):
    observation_id: str = Field(default_factory=acquisition_new_id)
    attempt_id: str
    discovered_resource_id: str
    parent_discovery_attempt_id: str
    source_definition_id: str
    source_definition_version: str
    snapshot_id: str | None = None
    disposition: ResourceDisposition | None = None
    attempt_outcome: AcquisitionOutcome | None = None
    original_url: str
    final_url: str | None = None
    redirect_chain: tuple[dict[str, Any], ...] = ()
    request_summary: dict[str, Any] = Field(default_factory=dict)
    response_summary: dict[str, Any] = Field(default_factory=dict)
    http_status: int | None = Field(default=None, ge=100, le=599)
    etag: str | None = None
    last_modified: str | None = None
    validator_source_snapshot_id: str | None = None
    observed_at: AwareDateTime
    retrieved_at: AwareDateTime | None = None
    reason_code: str | None = None

    @field_validator("original_url", "final_url")
    @classmethod
    def validate_urls(cls, value: str | None) -> str | None:
        return None if value is None else _require_https_url(value)

    @model_validator(mode="after")
    def validate_result(self) -> "ResourceObservation":
        if self.retrieved_at is not None and self.retrieved_at < self.observed_at:
            raise ValueError("resource retrieved_at不能早于observed_at")
        if self.disposition is not None:
            if not self.snapshot_id:
                raise ValueError("正式内容disposition必须引用snapshot")
            allowed = {AcquisitionOutcome.SUCCESS, AcquisitionOutcome.UNCHANGED}
            if self.attempt_outcome not in allowed:
                raise ValueError("new/changed/unchanged disposition只允许success或unchanged attempt")
        else:
            if self.snapshot_id is not None:
                raise ValueError("无正式内容disposition时不得引用snapshot")
            if self.attempt_outcome in {AcquisitionOutcome.SUCCESS, AcquisitionOutcome.UNCHANGED}:
                raise ValueError("成功fetch observation必须声明内容disposition")
        if self.attempt_outcome == AcquisitionOutcome.UNCHANGED and self.disposition != ResourceDisposition.UNCHANGED:
            raise ValueError("unchanged attempt必须具有unchanged disposition")
        return self


class ValidatorAnchor(FrozenAcquisitionModel):
    canonical_resource_id: str
    resource_url: str
    snapshot_id: str
    etag: str | None = None
    last_modified: str | None = None
    observed_at: AwareDateTime

    @field_validator("resource_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _require_https_url(value)

    @model_validator(mode="after")
    def validate_validator(self) -> "ValidatorAnchor":
        if not (self.etag or self.last_modified):
            raise ValueError("validator anchor必须具有ETag或Last-Modified")
        return self


class CheckpointPosition(FrozenAcquisitionModel):
    time_upper_bound: AwareDateTime
    canonical_resource_id: str
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None

    @property
    def ordering_key(self) -> tuple[datetime, str]:
        return self.time_upper_bound, self.canonical_resource_id


class CheckpointPartition(FrozenAcquisitionModel):
    partition_key: str
    execution_key: str
    safe_through: CheckpointPosition | None = None
    opaque_cursor: str | None = None
    validator_anchors: tuple[ValidatorAnchor, ...] = ()
    unresolved_barrier_ids: tuple[str, ...] = ()


class SourceCheckpoint(FrozenAcquisitionModel):
    checkpoint_id: str = Field(default_factory=acquisition_new_id)
    ticker: str
    source_definition_id: str
    source_definition_version: str
    question_set_version: str
    checkpoint_version: int = Field(ge=1)
    parent_checkpoint_id: str | None = None
    parent_checkpoint_version: int | None = Field(default=None, ge=1)
    source_safe_through: CheckpointPosition | None = None
    partitions: tuple[CheckpointPartition, ...]
    overlap_days: int = Field(ge=0)
    latest_successful_run_id: str | None = None
    unresolved_barrier_ids: tuple[str, ...] = ()
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    finalized_lease_epoch: int = Field(ge=1)

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, value: str) -> str:
        if not TICKER_RE.fullmatch(value):
            raise ValueError("checkpoint ticker必须是6位数字")
        return value

    @model_validator(mode="after")
    def validate_versions(self) -> "SourceCheckpoint":
        if self.checkpoint_version == 1:
            if self.parent_checkpoint_id is not None or self.parent_checkpoint_version is not None:
                raise ValueError("首个checkpoint不得具有父版本")
        elif not (self.parent_checkpoint_id and self.parent_checkpoint_version == self.checkpoint_version - 1):
            raise ValueError("后继checkpoint必须引用前一版本")
        if len({item.partition_key for item in self.partitions}) != len(self.partitions):
            raise ValueError("checkpoint partition_key不得重复")
        if self.source_safe_through is not None:
            positions = [item.safe_through for item in self.partitions if item.safe_through is not None]
            if positions and self.source_safe_through.ordering_key > min(item.ordering_key for item in positions):
                raise ValueError("来源级水位线不得越过必需分区安全下界")
        return self


class BarrierResolution(FrozenAcquisitionModel):
    barrier_resolution_id: str = Field(default_factory=acquisition_new_id)
    barrier_id: str
    opening_attempt_id: str
    resolving_attempt_id: str
    source_definition_id: str
    source_definition_version: str
    partition_key: str
    work_position: str
    retry_group_id: str
    canonical_resource_id: str | None = None
    discovery_proof_id: str | None = None
    snapshot_id: str | None = None
    resource_observation_id: str | None = None
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    lease_epoch: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_resolution(self) -> "BarrierResolution":
        if self.opening_attempt_id == self.resolving_attempt_id:
            raise ValueError("barrier必须由后继attempt解决")
        if not (self.discovery_proof_id or (self.snapshot_id and self.resource_observation_id)):
            raise ValueError("barrier resolution必须引用完整discovery proof或snapshot observation")
        return self


class StorageNamespace(FrozenAcquisitionModel):
    namespace_id: str
    binding_nonce: str
    layout_version: int = Field(ge=1)
    database_identity_hash: str
    data_root_identity_hash: str
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)

    @field_validator("database_identity_hash", "data_root_identity_hash")
    @classmethod
    def validate_hashes(cls, value: str) -> str:
        return _require_sha256(value, field_name="storage identity hash")


class StorageBindingIntent(FrozenAcquisitionModel):
    namespace_id: str
    binding_nonce: str
    layout_version: int = Field(ge=1)
    database_identity_hash: str
    data_root_identity_hash: str
    source_database_fingerprint: str
    source_user_version: int = Field(ge=0)
    bootstrap_stage: BootstrapStage
    stage_manifest_hashes: dict[str, str] = Field(default_factory=dict)
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    updated_at: AwareDateTime = Field(default_factory=acquisition_utc_now)

    @field_validator(
        "database_identity_hash",
        "data_root_identity_hash",
        "source_database_fingerprint",
    )
    @classmethod
    def validate_identity_hashes(cls, value: str) -> str:
        return _require_sha256(value, field_name="binding identity hash")

    @field_validator("stage_manifest_hashes")
    @classmethod
    def validate_manifest_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            if any(token in key.lower() for token in ("path", "root", "directory")):
                raise ValueError("binding intent manifest键不得承载路径")
            _require_sha256(value, field_name=f"stage manifest {key}")
        return values

    @model_validator(mode="after")
    def validate_timestamps(self) -> "StorageBindingIntent":
        if self.updated_at < self.created_at:
            raise ValueError("binding intent更新时间不能早于创建时间")
        for value in (
            self.namespace_id,
            self.binding_nonce,
            self.database_identity_hash,
            self.data_root_identity_hash,
            self.source_database_fingerprint,
            *self.stage_manifest_hashes.keys(),
            *self.stage_manifest_hashes.values(),
        ):
            if re.search(r"(?:^[A-Za-z]:[\\/]|[\\/]{2}|/[^/])", value):
                raise ValueError("binding intent不得包含绝对路径")
        return self


class SourceCandidate(FrozenAcquisitionModel):
    candidate_id: str = Field(default_factory=acquisition_new_id)
    candidate_url: str
    candidate_domain: str
    discovered_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    discovery_context: dict[str, Any]
    suggested_upstream_identity: str | None = None
    status: SourceCandidateStatus = SourceCandidateStatus.PENDING_REVIEW
    reviewed_at: AwareDateTime | None = None
    reviewed_by: str | None = None
    decision_reason: str | None = None
    approved_registry_version: str | None = None

    @field_validator("candidate_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _require_https_url(value, field_name="candidate_url")

    @field_validator("candidate_domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        normalized = value.rstrip(".").lower()
        if not normalized or any(token in normalized for token in ("/", "://", "*")):
            raise ValueError("candidate_domain必须是精确域名")
        return normalized

    @model_validator(mode="after")
    def validate_review(self) -> "SourceCandidate":
        url_domain = (urlsplit(self.candidate_url).hostname or "").lower()
        if url_domain != self.candidate_domain:
            raise ValueError("candidate URL与domain不一致")
        review_values = (self.reviewed_at, self.reviewed_by, self.decision_reason)
        if self.status == SourceCandidateStatus.PENDING_REVIEW:
            if any(value is not None for value in (*review_values, self.approved_registry_version)):
                raise ValueError("pending candidate不得伪造审核结果")
        else:
            if any(value is None for value in review_values):
                raise ValueError("approved/rejected candidate必须记录审核人、时间和理由")
            if self.status == SourceCandidateStatus.APPROVED and not self.approved_registry_version:
                raise ValueError("candidate批准只可通过新registry version生效")
            if self.status == SourceCandidateStatus.REJECTED and self.approved_registry_version:
                raise ValueError("rejected candidate不得声明批准registry version")
        return self


class ContentBlob(FrozenAcquisitionModel):
    content_blob_id: str
    storage_namespace_id: str
    sha256: str
    byte_length: int = Field(ge=0)
    archive_relative_path: str
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("archive_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _require_relative_path(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "ContentBlob":
        if self.sha256 not in self.archive_relative_path:
            raise ValueError("content blob相对路径必须包含完整SHA-256")
        return self


class RawResourceSnapshot(FrozenAcquisitionModel):
    snapshot_id: str = Field(default_factory=acquisition_new_id)
    resource_role: ResourceRole
    storage_namespace_id: str
    source_definition_id: str
    source_definition_version: str
    creating_observation_id: str
    content_blob_id: str
    mime_type: str
    byte_length: int = Field(ge=0)
    sha256: str
    archive_relative_path: str
    available_at: AwareDateTime
    available_at_basis: AvailableAtBasis
    version: int = Field(default=1, ge=1)
    supersedes_snapshot_id: str | None = None
    policy_decision: str
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    canonical_resource_id: str | None = None
    upstream_material_id: str | None = None
    canonical_url: str | None = None
    published_at_raw: str | None = None
    published_at: AwareDateTime | None = None
    published_at_precision: PublishedAtPrecision | None = None
    source_timezone: str | None = None
    physical_query_plan_item_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    cursor: str | None = None
    query_page_canonical: str | None = None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("archive_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _require_relative_path(value)

    @field_validator("canonical_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return None if value is None else _require_https_url(value, field_name="canonical_url")

    @field_validator("source_timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知来源时区: {value}") from exc
        return value

    @model_validator(mode="after")
    def validate_role(self) -> "RawResourceSnapshot":
        content_fields = (
            self.canonical_resource_id,
            self.upstream_material_id,
            self.canonical_url,
            self.published_at_precision,
            self.source_timezone,
        )
        discovery_fields = (
            self.physical_query_plan_item_id,
            self.query_page_canonical,
        )
        if self.resource_role == ResourceRole.CONTENT:
            if any(value is None for value in content_fields):
                raise ValueError("content snapshot缺少canonical/upstream/URL/precision/timezone字段")
            if any(value is not None for value in (*discovery_fields, self.page_number, self.cursor)):
                raise ValueError("content snapshot不得携带discovery page身份")
            if self.published_at_precision == PublishedAtPrecision.UNKNOWN:
                if self.published_at is not None:
                    raise ValueError("unknown发布时间精度不得伪造published_at")
                if self.available_at_basis not in {AvailableAtBasis.RETRIEVED_AT, AvailableAtBasis.LEGACY_VERIFIED}:
                    raise ValueError("无可验证版本时间的content必须保守使用retrieved_at")
            elif self.published_at is None:
                raise ValueError("已知发布时间精度的content必须记录published_at")
            if (
                self.published_at_precision == PublishedAtPrecision.DATE
                and self.available_at_basis
                not in {
                    AvailableAtBasis.SOURCE_DATE_NEXT_BOUNDARY,
                    AvailableAtBasis.RETRIEVED_AT,
                }
            ):
                raise ValueError(
                    "date-only发布时间只有在版本可证明时使用下一本地日界，否则必须使用retrieved_at"
                )
            if self.published_at_precision == PublishedAtPrecision.INSTANT and self.available_at_basis not in {
                AvailableAtBasis.VERIFIED_PUBLISHED_INSTANT,
                AvailableAtBasis.RETRIEVED_AT,
            }:
                raise ValueError("instant发布时间basis不合法")
        else:
            if any(value is not None for value in (*content_fields, self.published_at_raw, self.published_at)):
                raise ValueError("discovery_response不得伪造公告canonical/upstream/published字段")
            if any(value is None for value in discovery_fields):
                raise ValueError("discovery_response必须固定plan item与query-page canonical")
            if self.page_number is None and self.cursor is None:
                raise ValueError("discovery_response必须固定page或cursor")
            if self.available_at_basis != AvailableAtBasis.RETRIEVED_AT:
                raise ValueError("discovery_response available_at只能以本次retrieved_at为依据")
        if self.version == 1 and self.supersedes_snapshot_id is not None:
            raise ValueError("首个snapshot版本不得supersede旧版本")
        if self.version > 1 and not self.supersedes_snapshot_id:
            raise ValueError("后续snapshot版本必须关联旧版本")
        return self


class SnapshotIntegrityEvent(FrozenAcquisitionModel):
    integrity_event_id: str = Field(default_factory=acquisition_new_id)
    snapshot_id: str
    status: SnapshotIntegrityStatus
    checked_at: AwareDateTime = Field(default_factory=acquisition_utc_now)
    observed_sha256: str | None = None
    observed_byte_length: int | None = Field(default=None, ge=0)
    reason_code: str | None = None

    @field_validator("observed_sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value)

    @model_validator(mode="after")
    def validate_status(self) -> "SnapshotIntegrityEvent":
        if self.status == SnapshotIntegrityStatus.VERIFIED:
            if self.observed_sha256 is None or self.observed_byte_length is None:
                raise ValueError("verified integrity event必须包含实测哈希和长度")
            if self.reason_code is not None:
                raise ValueError("verified integrity event不得携带隔离原因")
        elif not self.reason_code:
            raise ValueError("quarantined integrity event必须说明原因")
        return self


class DerivedArtifact(FrozenAcquisitionModel):
    derived_artifact_id: str = Field(default_factory=acquisition_new_id)
    storage_namespace_id: str
    parent_snapshot_id: str
    artifact_type: Literal["text", "ocr", "table", "page_image", "classification"]
    extractor_id: str
    extractor_version: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    output_sha256: str
    output_byte_length: int = Field(ge=0)
    archive_relative_path: str
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)

    @field_validator("output_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value, field_name="output_sha256")

    @field_validator("archive_relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _require_relative_path(value)


class EvidenceManifestItem(FrozenAcquisitionModel):
    snapshot_id: str
    derived_artifact_ids: tuple[str, ...] = ()
    derived_output_hashes: tuple[str, ...] = ()
    source_definition_id: str
    source_definition_version: str
    snapshot_sha256: str
    available_at: AwareDateTime

    @field_validator("snapshot_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value, field_name="snapshot_sha256")

    @field_validator("derived_output_hashes")
    @classmethod
    def validate_derived_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _require_sha256(value, field_name="derived_output_hash")
        return values

    @model_validator(mode="after")
    def validate_derived_identity(self) -> "EvidenceManifestItem":
        if len(self.derived_artifact_ids) != len(self.derived_output_hashes):
            raise ValueError("manifest派生artifact ID与输出哈希必须一一对应")
        return self


class EvidenceManifestExclusion(FrozenAcquisitionModel):
    object_type: Literal["snapshot", "proof", "resource", "coverage"]
    object_id: str
    reason_code: str


class EvidenceSnapshotManifest(FrozenAcquisitionModel):
    manifest_id: str = Field(default_factory=acquisition_new_id)
    storage_namespace_id: str
    run_id: str
    question_set_version: str
    registry_id: str
    registry_version: str
    as_of: AwareDateTime
    items: tuple[EvidenceManifestItem, ...]
    exclusions: tuple[EvidenceManifestExclusion, ...] = ()
    coverage_summary: dict[str, Any]
    policy_decisions: tuple[str, ...] = ()
    manifest_hash: str
    gate_passed: bool
    created_at: AwareDateTime = Field(default_factory=acquisition_utc_now)

    @field_validator("manifest_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value, field_name="manifest_hash")

    @model_validator(mode="after")
    def validate_manifest(self) -> "EvidenceSnapshotManifest":
        ids = [item.snapshot_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("manifest不得重复纳入snapshot")
        if any(item.available_at > self.as_of for item in self.items):
            raise ValueError("manifest不得包含as_of之后才可得的snapshot")
        included = set(ids)
        excluded_snapshots = {
            item.object_id for item in self.exclusions if item.object_type == "snapshot"
        }
        if included & excluded_snapshots:
            raise ValueError("同一snapshot不能同时纳入与排除")
        if self.gate_passed and not self.items:
            raise ValueError("通过证据门禁的manifest不能为空")
        return self


__all__ = [
    "AcquisitionAttempt",
    "AcquisitionAttemptEvent",
    "AcquisitionAttemptEventType",
    "AcquisitionAttemptSegment",
    "AcquisitionExecutionLease",
    "AcquisitionMode",
    "AcquisitionOutcome",
    "AcquisitionPlan",
    "AcquisitionRun",
    "AcquisitionRunEvent",
    "AcquisitionRunEventType",
    "AcquisitionRunKind",
    "AcquisitionRunResult",
    "AnchorEvidence",
    "AttemptKind",
    "AvailableAtBasis",
    "BarrierResolution",
    "BootstrapStage",
    "BusinessQuestion",
    "BusinessQuestionSet",
    "CheckpointPartition",
    "CheckpointPosition",
    "CompanyAcquisitionProfile",
    "ContentBlob",
    "CoverageEntry",
    "CoveragePlanDisposition",
    "CoverageResolution",
    "CoverageResolutionStatus",
    "DerivedArtifact",
    "DiscoveryBodyPolicy",
    "DiscoveryObservation",
    "DiscoveryProof",
    "DiscoverySchemaPolicy",
    "DiscoveredResource",
    "EvidenceManifestExclusion",
    "EvidenceManifestItem",
    "EvidenceSnapshotManifest",
    "FetchPolicy",
    "FrozenAcquisitionModel",
    "LiveAccessReviewCheck",
    "LiveAccessReviewStatus",
    "PaginationPolicy",
    "PhysicalQueryCoverageLink",
    "PhysicalQueryPlanItem",
    "PolicyDecision",
    "PublishedAtPrecision",
    "QueryStage",
    "RawResourceSnapshot",
    "ResourceDisposition",
    "ResourceObservation",
    "ResourceRole",
    "SnapshotIntegrityEvent",
    "SnapshotIntegrityStatus",
    "SourceAlias",
    "SourceApplicability",
    "SourceCandidate",
    "SourceCandidateStatus",
    "SourceCheckpoint",
    "SourceDefinition",
    "SourceDefinitionRef",
    "SourceEndpointRule",
    "SourceIncrementalPolicy",
    "SourceLicensePolicy",
    "SourceLiveAccessReview",
    "SourceParameterBinding",
    "SourcePolicyStatus",
    "SourceRateLimitPolicy",
    "SourceRegistry",
    "SourceResponseLimits",
    "SourceRetentionPolicy",
    "SourceRetryPolicy",
    "StorageBindingIntent",
    "StorageNamespace",
    "ValidatorAnchor",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "stable_acquisition_id",
]
