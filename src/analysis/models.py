from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


def aware_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


class VerificationStatus(str, Enum):
    DUAL_SOURCE = "双源一致"
    AUTHORITATIVE_SINGLE = "权威单源"
    SUPPLIER_DIRECT = "供应商直采"
    DERIVED = "程序计算"
    PENDING = "待核验"
    ESTIMATED = "估算"
    NOT_DISCLOSED = "未披露"
    UNAVAILABLE = "暂无该数据"
    NOT_APPLICABLE = "不适用"


class StructuredAcquisitionMethod(str, Enum):
    SUPPLIER_STRUCTURED = "supplier_structured"
    DOCUMENT_EXTRACTION = "document_extraction"
    FORMULA = "formula"
    LEGACY = "legacy"


class StructuredFactNature(str, Enum):
    OBSERVED = "observed"
    DETERMINISTIC = "deterministic"
    PROVIDER_ESTIMATE = "provider_estimate"
    FORECAST = "forecast"
    PLATFORM_LABEL = "platform_label"
    SOURCE_TEXT = "source_text"
    UNCLASSIFIED = "unclassified"


class StructuredQualityStatus(str, Enum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    DEFINITION_UNKNOWN = "definition_unknown"
    NOT_APPLICABLE = "not_applicable"


class StructuredFactAdmission(BaseModel):
    """可复算的供应商字段准入依据；不采信调用者自填布尔值。"""

    acquisition_method: StructuredAcquisitionMethod
    nature: StructuredFactNature
    quality: StructuredQualityStatus
    source_policy_id: str = Field(min_length=1)
    source_policy_version: str = Field(min_length=1)
    field_definition_id: str = Field(min_length=1)
    field_definition_version: str = Field(min_length=1)
    raw_resource_snapshot_id: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_key: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    original_value: Any = None
    original_unit: str | None = None
    retrieved_at: datetime
    available_at: datetime | None = None

    @property
    def programmatic_eligible(self) -> bool:
        return (
            self.acquisition_method == StructuredAcquisitionMethod.SUPPLIER_STRUCTURED
            and self.nature
            in {StructuredFactNature.OBSERVED, StructuredFactNature.DETERMINISTIC}
            and self.quality == StructuredQualityStatus.PASSED
        )


class ClaimKind(str, Enum):
    DISCLOSED_FACT = "披露事实"
    CODE_DERIVED = "代码推导"
    ANALYST_JUDGEMENT = "分析判断"
    SCENARIO_ASSUMPTION = "情景假设"


class ExtractionMethod(str, Enum):
    TABLE_PARSER = "table_parser"
    TEXT_RULE = "text_rule"
    OCR = "ocr"
    MANUAL = "manual"


class DisclosureForm(str, Enum):
    EXACT = "exact"
    RANGE = "range"
    PERCENTAGE_ONLY = "percentage_only"
    ANONYMIZED = "anonymized"


class ForecastStatistic(str, Enum):
    POINT = "point"
    MEAN = "mean"
    MEDIAN = "median"
    HIGH = "high"
    LOW = "low"


class ForecastType(str, Enum):
    PROVIDER_CONSENSUS = "provider_consensus"
    BROKER_REPORT = "broker_report"
    MANAGEMENT_GUIDANCE = "management_guidance"
    USER_ASSUMPTION = "user_assumption"


class ReportStatus(str, Enum):
    DRAFT = "草稿"
    REVIEWED = "已复核"


class ResearchRating(str, Enum):
    POSITIVE = "积极关注"
    NEUTRAL = "中性观察"
    CAUTIOUS = "谨慎"
    UNRATED = "暂不评级"


class ScenarioName(str, Enum):
    BEAR = "悲观"
    BASE = "基准"
    BULL = "乐观"


class ModelRunStatus(str, Enum):
    SUCCESS = "成功"
    FAILED = "失败"
    NOT_APPLICABLE = "不适用"
    BLOCKED = "待核验"


class SourceRecord(BaseModel):
    source_id: str = Field(default_factory=new_id)
    name: str
    source_type: str = "public"
    upstream_source_id: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    document_hash: str | None = None
    authority_level: int = Field(default=3, ge=1, le=5)
    source_definition_id: str | None = None
    source_definition_version: str | None = None
    raw_resource_snapshot_id: str | None = None
    canonical_resource_id: str | None = None
    available_at: datetime | None = None
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FactRecord(BaseModel):
    fact_id: str = Field(default_factory=new_id)
    ticker: str
    metric_id: str
    value: float | None = None
    unit: str = ""
    currency: str = "CNY"
    period_start: date | None = None
    period_end: date | None = None
    period_type: str = "instant"
    disclosed_at: datetime | None = None
    as_of: datetime = Field(default_factory=utc_now)
    scope: str = "consolidated"
    audited: bool | None = None
    original_label: str | None = None
    document_page: int | None = Field(default=None, ge=1)
    source_ids: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING
    method_ref: str | None = None
    derived_from_fact_ids: list[str] = Field(default_factory=list)
    restatement_version: str | None = None
    is_restated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    structured_admission: StructuredFactAdmission | None = None

    @model_validator(mode="after")
    def validate_lineage(self) -> "FactRecord":
        if self.verification_status == VerificationStatus.DERIVED and not (
            self.method_ref and self.derived_from_fact_ids and self.source_ids
        ):
            raise ValueError("程序计算事实必须关联公式版本、输入事实及来源")
        if self.verification_status in {
            VerificationStatus.DUAL_SOURCE,
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
        } and not self.source_ids:
            raise ValueError(f"{self.verification_status.value}事实必须关联来源")
        if self.verification_status == VerificationStatus.SUPPLIER_DIRECT:
            if self.structured_admission is None:
                raise ValueError("供应商直采事实必须关联结构化准入依据")
            if not self.structured_admission.programmatic_eligible:
                raise ValueError("供应商直采事实未通过结构化准入谓词")
        if self.verification_status == VerificationStatus.ESTIMATED and not (
            self.method_ref and (self.source_ids or self.derived_from_fact_ids)
        ):
            raise ValueError("估算事实必须关联方法及来源事实")
        return self


class EvidenceSpan(BaseModel):
    document_id: str
    page: int | None = Field(default=None, ge=1)
    text: str | None = None
    table_title: str | None = None
    row_label: str | None = None
    column_label: str | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_offsets(self) -> "EvidenceSpan":
        if self.start_offset is not None and self.end_offset is not None:
            if self.end_offset < self.start_offset:
                raise ValueError("证据结束位置不能早于开始位置")
        if not any(
            (
                self.page,
                self.text,
                self.table_title,
                self.row_label,
                self.column_label,
                self.start_offset is not None,
            )
        ):
            raise ValueError("证据定位至少需要页码、文本、表格位置或字符位置")
        return self


class DimensionalFactRecord(BaseModel):
    dimensional_fact_id: str = Field(default_factory=new_id)
    ticker: str
    metric_id: str
    dimension_type: str
    dimension_name: str
    dimension_code: str | None = None
    parent_dimension: str | None = None
    value: float | None = None
    unit: str = ""
    currency: str = "CNY"
    period_start: date | None = None
    period_end: date | None = None
    period_type: str = "flow"
    available_at: datetime
    scope: str = "consolidated"
    accounting_basis: str | None = None
    audited: bool | None = None
    source_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    document_page: int | None = Field(default=None, ge=1)
    table_title: str | None = None
    row_label: str | None = None
    column_label: str | None = None
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    extraction_method: ExtractionMethod = ExtractionMethod.TABLE_PARSER
    verification_status: VerificationStatus = VerificationStatus.PENDING
    supporting_fact_ids: list[str] = Field(default_factory=list)
    method_ref: str | None = None
    data_snapshot_id: str = Field(min_length=1)
    disclosed_as: DisclosureForm = DisclosureForm.EXACT
    metadata: dict[str, Any] = Field(default_factory=dict)
    structured_admission: StructuredFactAdmission | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> "DimensionalFactRecord":
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("维度事实期间结束日不能早于开始日")
        unique_sources = set(self.source_ids)
        if self.verification_status == VerificationStatus.DUAL_SOURCE and len(unique_sources) < 2:
            raise ValueError("双源一致的维度事实必须关联至少两个来源")
        if self.verification_status in {
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
        } and not unique_sources:
            raise ValueError("单源准入的维度事实必须关联来源")
        if self.verification_status in {
            VerificationStatus.DUAL_SOURCE,
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
            VerificationStatus.ESTIMATED,
        } and self.value is None:
            raise ValueError("可使用的维度数值事实必须包含数值")
        if self.verification_status == VerificationStatus.ESTIMATED and not (
            self.method_ref and (unique_sources or self.supporting_fact_ids)
        ):
            raise ValueError("估算维度事实必须关联方法及来源事实")
        if self.verification_status == VerificationStatus.SUPPLIER_DIRECT and (
            self.structured_admission is None
            or not self.structured_admission.programmatic_eligible
        ):
            raise ValueError("供应商直采维度事实未通过结构化准入谓词")
        return self


class EventRecord(BaseModel):
    event_id: str = Field(default_factory=new_id)
    ticker: str
    event_type: str
    event_subtype: str | None = None
    announced_at: datetime
    available_at: datetime
    effective_at: datetime | None = None
    period_start: date | None = None
    period_end: date | None = None
    lifecycle_state: str
    previous_event_id: str | None = None
    root_event_id: str | None = None
    parties: list[dict[str, Any]] = Field(default_factory=list)
    amount: float | None = None
    currency: str = "CNY"
    shares: float | None = None
    ratio: float | None = None
    summary: str
    event_terms: dict[str, Any] = Field(default_factory=dict)
    affected_metrics: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    supporting_fact_ids: list[str] = Field(default_factory=list)
    claim_kind: ClaimKind = ClaimKind.DISCLOSED_FACT
    verification_status: VerificationStatus = VerificationStatus.PENDING
    materiality: str = "unknown"
    status_updated_at: datetime = Field(default_factory=utc_now)
    data_snapshot_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    structured_admission: StructuredFactAdmission | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> "EventRecord":
        if aware_utc(self.available_at) < aware_utc(self.announced_at):
            raise ValueError("事件可得时间不能早于公告时间")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("事件期间结束日不能早于开始日")
        if self.previous_event_id == self.event_id:
            raise ValueError("事件不能把自身作为前序事件")
        if self.root_event_id is None:
            self.root_event_id = self.event_id
        unique_sources = set(self.source_ids)
        if self.verification_status == VerificationStatus.DUAL_SOURCE and len(unique_sources) < 2:
            raise ValueError("双源一致事件必须关联至少两个来源")
        if self.verification_status in {
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
        } and not unique_sources:
            raise ValueError("单源准入事件必须关联来源")
        if self.claim_kind == ClaimKind.DISCLOSED_FACT and self.verification_status in {
            VerificationStatus.DUAL_SOURCE,
            VerificationStatus.AUTHORITATIVE_SINGLE,
        } and not (self.document_ids or self.evidence_spans):
            raise ValueError("已核验披露事件必须关联文档或证据片段")
        if self.verification_status == VerificationStatus.SUPPLIER_DIRECT and (
            self.structured_admission is None
            or not self.structured_admission.programmatic_eligible
        ):
            raise ValueError("供应商直采事件未通过结构化准入谓词")
        return self


class IndustryFactRecord(BaseModel):
    industry_fact_id: str = Field(default_factory=new_id)
    industry_code: str
    industry_taxonomy_version: str
    metric_id: str
    product_scope: str | None = None
    region_scope: str | None = None
    value: float | None = None
    unit: str = ""
    currency: str = "CNY"
    period_start: date | None = None
    period_end: date | None = None
    frequency: str
    numerator_definition: str | None = None
    denominator_definition: str | None = None
    coverage_scope: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    dataset_version: str
    release_at: datetime
    available_at: datetime
    is_estimate: bool = False
    calculation_formula: str | None = None
    method_ref: str | None = None
    component_fact_ids: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING
    revision_id: str | None = None
    data_snapshot_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    structured_admission: StructuredFactAdmission | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> "IndustryFactRecord":
        if aware_utc(self.available_at) < aware_utc(self.release_at):
            raise ValueError("行业数据可得时间不能早于发布时间")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("行业事实期间结束日不能早于开始日")
        unique_sources = set(self.source_ids)
        if self.verification_status == VerificationStatus.DUAL_SOURCE and len(unique_sources) < 2:
            raise ValueError("双源一致行业事实必须关联至少两个来源")
        if self.verification_status in {
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
        } and not unique_sources:
            raise ValueError("单源准入行业事实必须关联来源")
        if self.verification_status in {
            VerificationStatus.DUAL_SOURCE,
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
            VerificationStatus.ESTIMATED,
        } and self.value is None:
            raise ValueError("可使用的行业事实必须包含数值")
        if self.is_estimate or self.verification_status == VerificationStatus.ESTIMATED:
            if not self.calculation_formula or not (unique_sources or self.component_fact_ids):
                raise ValueError("估算行业事实必须关联公式及构成来源")
        if self.verification_status == VerificationStatus.SUPPLIER_DIRECT and (
            self.structured_admission is None
            or not self.structured_admission.programmatic_eligible
        ):
            raise ValueError("供应商直采行业事实未通过结构化准入谓词")
        return self


class ForecastSnapshot(BaseModel):
    forecast_snapshot_id: str = Field(default_factory=new_id)
    ticker: str
    provider: str
    as_of: datetime
    available_at: datetime
    target_period: date
    metric_id: str
    value: float | None = None
    unit: str = ""
    statistic: ForecastStatistic = ForecastStatistic.POINT
    analyst_count: int | None = Field(default=None, ge=0)
    forecast_type: ForecastType
    source_ids: list[str] = Field(default_factory=list)
    report_ids: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING
    license_scope: str | None = None
    expires_at: date | None = None
    data_snapshot_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> "ForecastSnapshot":
        if aware_utc(self.available_at) > aware_utc(self.as_of):
            raise ValueError("预测快照不能包含快照时点后才可得的数据")
        if self.expires_at and self.expires_at < self.as_of.date():
            raise ValueError("预测有效期不能早于快照日期")
        unique_sources = set(self.source_ids)
        if self.verification_status == VerificationStatus.DUAL_SOURCE and len(unique_sources) < 2:
            raise ValueError("双源一致预测必须关联至少两个来源")
        if self.verification_status == VerificationStatus.AUTHORITATIVE_SINGLE and not unique_sources:
            raise ValueError("权威单源预测必须关联来源")
        if self.verification_status in {
            VerificationStatus.DUAL_SOURCE,
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.ESTIMATED,
        } and self.value is None:
            raise ValueError("可使用的预测快照必须包含数值")
        return self


class PeerSetVersion(BaseModel):
    peer_set_id: str = Field(default_factory=new_id)
    version: int = Field(default=1, ge=1)
    target_ticker: str
    as_of: datetime
    included_tickers: list[str] = Field(default_factory=list)
    excluded_tickers: list[str] = Field(default_factory=list)
    selection_dimensions: list[str] = Field(default_factory=list)
    inclusion_reason: dict[str, str] = Field(default_factory=dict)
    exclusion_reason: dict[str, str] = Field(default_factory=dict)
    industry_taxonomy_version: str
    user_confirmed: bool = False
    source_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    data_snapshot_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> "PeerSetVersion":
        included = set(self.included_tickers)
        excluded = set(self.excluded_tickers)
        if len(included) != len(self.included_tickers):
            raise ValueError("同行集合不能包含重复的纳入公司")
        if len(excluded) != len(self.excluded_tickers):
            raise ValueError("同行集合不能包含重复的排除公司")
        if included & excluded:
            raise ValueError("同一公司不能同时被纳入和排除")
        missing_inclusion = included - set(self.inclusion_reason)
        missing_exclusion = excluded - set(self.exclusion_reason)
        if missing_inclusion:
            raise ValueError(f"纳入同行缺少理由: {sorted(missing_inclusion)}")
        if missing_exclusion:
            raise ValueError(f"排除同行缺少理由: {sorted(missing_exclusion)}")
        return self


class AnnouncementRecord(BaseModel):
    announcement_record_id: str = Field(default_factory=new_id)
    ticker: str
    company_name: str | None = None
    title: str
    announced_at: datetime
    available_at: datetime
    provider: str
    announcement_id: str
    url: str
    category: str | None = None
    canonical_key: str
    classification_version: str | None = None
    classified_event_type: str | None = None
    classified_event_subtype: str | None = None
    classified_lifecycle_state: str | None = None
    classification_score: int = Field(default=0, ge=0)
    source_ids: list[str] = Field(default_factory=list)
    document_id: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    data_snapshot_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> "AnnouncementRecord":
        if aware_utc(self.available_at) < aware_utc(self.announced_at):
            raise ValueError("公告元数据可得时间不能早于公告时间")
        if self.document_id and not self.source_ids:
            raise ValueError("已下载公告必须关联来源")
        if self.event_ids and not self.document_id:
            raise ValueError("公告生成事件前必须完成正式文档归档")
        return self


class VerificationRecord(BaseModel):
    verification_id: str = Field(default_factory=new_id)
    left_fact_id: str
    right_fact_id: str
    source_ids: list[str]
    status: VerificationStatus
    accepted_fact_id: str | None = None
    accepted_value: float | None = None
    relative_difference: float | None = None
    tolerance: float | None = None
    conflict_dimensions: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


class ClaimRecord(BaseModel):
    claim_id: str = Field(default_factory=new_id)
    ticker: str
    category: str
    text: str
    claim_kind: ClaimKind
    evidence_source_ids: list[str] = Field(default_factory=list)
    evidence_fact_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)
    as_of: datetime = Field(default_factory=utc_now)


class AssumptionRecord(BaseModel):
    assumption_id: str = Field(default_factory=new_id)
    scenario: ScenarioName
    name: str
    value: float
    unit: str = ""
    reason: str
    source_ids: list[str] = Field(default_factory=list)
    valid_until: date | None = None
    confirmed: bool = False
    tracking_metric: str | None = None


class MethodSpec(BaseModel):
    method_id: str
    version: str
    status: str = "active"
    effective_from: date
    applies_to: list[str] = Field(default_factory=lambda: ["all"])
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    formulas: dict[str, str] = Field(default_factory=dict)
    stop_conditions: list[str] = Field(default_factory=list)
    missing_policy: str = "block"
    output_fields: list[str] = Field(default_factory=list)
    doc_path: str
    implementation_ref: str
    test_ref: str
    limitations: list[str] = Field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.method_id}@{self.version}"


class MethodBundle(BaseModel):
    method_bundle_id: str
    created_at: datetime = Field(default_factory=utc_now)
    methods: list[MethodSpec]
    method_hashes: dict[str, str] = Field(default_factory=dict)


class ValuationResult(BaseModel):
    method_ref: str
    status: ModelRunStatus
    fair_value_per_share: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    currency: str = "CNY"
    inputs: dict[str, Any] = Field(default_factory=dict)
    sensitivity: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    failure_reason: str | None = None


class ModelRun(BaseModel):
    run_id: str = Field(default_factory=new_id)
    method_ref: str
    status: ModelRunStatus
    inputs: dict[str, Any] = Field(default_factory=dict)
    input_lineage: dict[str, list[str]] = Field(default_factory=dict)
    input_fact_ids: list[str] = Field(default_factory=list)
    input_assumption_ids: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ReportSection(BaseModel):
    number: int = Field(ge=1, le=8)
    title: str
    summary: str
    facts: list[str] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    method_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ConclusionCard(BaseModel):
    rating: ResearchRating = ResearchRating.UNRATED
    rating_confirmed: bool = False
    current_price: float | None = None
    price_as_of: datetime | None = None
    fair_value_low: float | None = None
    fair_value_base: float | None = None
    fair_value_high: float | None = None
    margin_of_safety: float | None = None
    core_theses: list[str] = Field(default_factory=list)
    major_risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    evidence_completeness: float = Field(default=0, ge=0, le=1)
    evidence_confidence: float = Field(default=0, ge=0, le=1)
    invalidation_conditions: list[str] = Field(default_factory=list)
    next_tracking_items: list[str] = Field(default_factory=list)


class TrackingIndicator(BaseModel):
    tracking_id: str = Field(default_factory=new_id)
    name: str
    current_value: float | None = None
    unit: str = ""
    threshold: str | None = None
    update_frequency: str = "每季"
    trigger_action: str = "重新检查对应论点与估值假设"
    source_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class AuditAppendix(BaseModel):
    sources: list[SourceRecord] = Field(default_factory=list)
    method_bundle: MethodBundle
    assumptions: list[AssumptionRecord] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_items: list[str] = Field(default_factory=list)
    manual_edits: list[str] = Field(default_factory=list)
    version_changes: list[str] = Field(default_factory=list)
    model_runs: list[ModelRun] = Field(default_factory=list)
    verification_records: list[VerificationRecord] = Field(default_factory=list)
    data_quality_checks: list[dict[str, Any]] = Field(default_factory=list)
    unreferenced_numbers: list[str] = Field(default_factory=list)
    dimensional_fact_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    industry_fact_ids: list[str] = Field(default_factory=list)
    forecast_snapshot_ids: list[str] = Field(default_factory=list)
    peer_set_refs: list[str] = Field(default_factory=list)


class ReportVersion(BaseModel):
    schema_version: str = "1.0.0"
    report_id: str = Field(default_factory=new_id)
    parent_report_id: str | None = None
    version: int = 1
    ticker: str
    company_name: str
    industry: str
    created_at: datetime = Field(default_factory=utc_now)
    as_of: datetime
    data_snapshot_id: str
    method_bundle_id: str
    status: ReportStatus = ReportStatus.DRAFT
    conclusion: ConclusionCard
    sections: list[ReportSection]
    audit: AuditAppendix
    facts: list[FactRecord] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    assumptions: list[AssumptionRecord] = Field(default_factory=list)
    tracking_indicators: list[TrackingIndicator] = Field(default_factory=list)
    dimensional_facts: list[DimensionalFactRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    industry_facts: list[IndustryFactRecord] = Field(default_factory=list)
    forecast_snapshots: list[ForecastSnapshot] = Field(default_factory=list)
    peer_sets: list[PeerSetVersion] = Field(default_factory=list)
    model_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    request_metadata: dict[str, Any] = Field(default_factory=dict)
    research_coverage_snapshot_id: str | None = None
    research_coverage: dict[str, Any] = Field(default_factory=dict)
    materialization_selected_fact_ids: list[str] = Field(default_factory=list)

    @field_validator("sections")
    @classmethod
    def validate_eight_sections(cls, sections: list[ReportSection]) -> list[ReportSection]:
        numbers = [section.number for section in sections]
        if numbers != list(range(1, 9)):
            raise ValueError("报告必须严格包含按1至8排序的八个章节")
        return sections

    @model_validator(mode="after")
    def validate_bundle_identity(self) -> "ReportVersion":
        if self.audit.method_bundle.method_bundle_id != self.method_bundle_id:
            raise ValueError("报告method_bundle_id与审计附录不一致")
        return self


class ReportCreateRequest(BaseModel):
    ticker: str
    company_name: str
    industry: str
    as_of: datetime = Field(default_factory=utc_now)
    current_price: float | None = None
    price_as_of: datetime | None = None
    sources: list[SourceRecord] = Field(default_factory=list)
    facts: list[FactRecord] = Field(default_factory=list)
    dimensional_facts: list[DimensionalFactRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    assumptions: list[AssumptionRecord] = Field(default_factory=list)
    verification_records: list[VerificationRecord] = Field(default_factory=list)
    model_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    requested_rating: ResearchRating = ResearchRating.UNRATED
    report_notes: str | None = None
    use_synced_facts: bool = True
    sync_result_id: str | None = None
    dimensional_sync_result_id: str | None = None
    event_sync_result_id: str | None = None
    research_coverage_snapshot_id: str | None = None
    research_coverage: dict[str, Any] = Field(default_factory=dict)
    materialization_selected_fact_ids: list[str] = Field(default_factory=list)
    input_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_price_timestamp(self) -> "ReportCreateRequest":
        selected = self.materialization_selected_fact_ids
        if len(selected) != len(set(selected)):
            raise ValueError("物化选择事实ID不得重复")
        missing_selected = sorted(set(selected) - {item.fact_id for item in self.facts})
        if missing_selected:
            raise ValueError(
                "物化选择事实ID必须存在于报告事实中: " + ", ".join(missing_selected)
            )
        if self.current_price is None:
            if self.price_as_of is not None:
                raise ValueError("没有当前价格时不应单独提供价格时点")
            return self
        if self.current_price <= 0:
            raise ValueError("当前价格必须大于0")
        if self.price_as_of is None:
            raise ValueError("提供当前价格时必须同时提供价格时点")

        def aware(value: datetime) -> datetime:
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

        if aware(self.price_as_of) > aware(self.as_of):
            raise ValueError("价格时点不能晚于报告数据截止时间")
        return self


class AssumptionPatchRequest(BaseModel):
    assumptions: list[AssumptionRecord]
    recalculate: bool = True


class ReviewRequest(BaseModel):
    rating: ResearchRating
    confirm_assumptions: bool = True
    note: str | None = None


class SyncRequest(BaseModel):
    providers: list[str] = Field(
        default_factory=lambda: ["official", "akshare", "sina", "baostock"]
    )
    scopes: list[str] = Field(default_factory=lambda: ["financials", "market"])
    source_strategy: Literal["structured-first-v1", "legacy-v1"] | None = None
    datasets: list[str] = Field(default_factory=list)
    structured_mode: Literal["baseline", "incremental", "due"] = "incremental"
    company_scope: Literal["company-only", "company-with-peers", "peer-set"] = (
        "company-only"
    )
    as_of: datetime = Field(default_factory=utc_now)
    annual_years: int = Field(default=5, ge=1, le=10)
    single_quarters: int = Field(default=12, ge=1, le=40)
    announcement_years: int = Field(default=5, ge=1, le=10)
    max_announcements: int = Field(default=1000, ge=1, le=5000)
    max_event_documents: int = Field(default=200, ge=0, le=1000)
    event_types: list[str] = Field(default_factory=list)
    download_official_documents: bool = True
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    acquisition_mode: Literal["baseline", "incremental", "reconcile"] | None = None
    acquisition_parent_run_id: str | None = None
    acquisition_plan_only: bool = False

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str]) -> list[str]:
        allowed = {
            "financials",
            "announcements",
            "dimensions",
            "governance",
            "capital_actions",
            "risks",
            "industry",
            "market",
            "forecasts",
            "peers",
            "business_model",
        }
        normalized = list(dict.fromkeys(item.strip().lower() for item in scopes if item.strip()))
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            raise ValueError(f"未知同步范围: {unknown}")
        if not normalized:
            raise ValueError("同步范围不能为空")
        return normalized

    @field_validator("event_types")
    @classmethod
    def normalize_event_types(cls, event_types: list[str]) -> list[str]:
        return list(
            dict.fromkeys(item.strip().lower() for item in event_types if item.strip())
        )

    @field_validator("datasets")
    @classmethod
    def normalize_datasets(cls, datasets: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in datasets if item.strip()))
        if any(not item.replace("_", "").replace("-", "").isalnum() for item in normalized):
            raise ValueError("结构化数据集ID只能包含字母、数字、下划线或连字符")
        return normalized

    @property
    def effective_source_strategy(self) -> str:
        if self.source_strategy is not None:
            return self.source_strategy
        # Explicit provider lists are the compatibility signal for old clients.
        # Omitted providers select the new field-routed default even though the
        # model retains legacy defaults for byte-compatible callers/tests.
        if "providers" in self.model_fields_set:
            return "legacy-v1"
        return "structured-first-v1"


class SyncResult(BaseModel):
    sync_result_id: str = Field(default_factory=new_id)
    ticker: str
    provider_results: dict[str, str]
    company_name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    sources: list[SourceRecord] = Field(default_factory=list)
    raw_facts: list[FactRecord] = Field(default_factory=list)
    facts: list[FactRecord] = Field(default_factory=list)
    verification_records: list[VerificationRecord] = Field(default_factory=list)
    documents: list[DocumentRecord] = Field(default_factory=list)
    dimensional_facts: list[DimensionalFactRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    industry_facts: list[IndustryFactRecord] = Field(default_factory=list)
    forecast_snapshots: list[ForecastSnapshot] = Field(default_factory=list)
    peer_sets: list[PeerSetVersion] = Field(default_factory=list)
    announcements: list[AnnouncementRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    acquisition_run_id: str | None = None
    acquisition_status: str = "legacy_unassessed"
    provider_results_authority: Literal[
        "structured_attempts", "legacy_unassessed"
    ] = "legacy_unassessed"
    coverage_accounted: bool | None = None
    material_gap_count: int | None = Field(default=None, ge=0)
    default_consume_eligible: bool = True
    checkpoint_ids: list[str] = Field(default_factory=list)
    raw_resource_snapshot_ids: list[str] = Field(default_factory=list)
    source_strategy: str = "legacy-v1"
    structured_plan_id: str | None = None
    structured_coverage_snapshot_id: str | None = None
    structured_dataset_coverage: dict[str, Any] = Field(default_factory=dict)
    fallback_usage: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_acquisition_compatibility(self) -> "SyncResult":
        if self.acquisition_run_id is None:
            return self
        if self.provider_results_authority != "structured_attempts":
            raise ValueError(
                "关联acquisition run的SyncResult必须以结构化attempts作为provider摘要来源"
            )
        if self.acquisition_status == "legacy_unassessed":
            raise ValueError("关联acquisition run的SyncResult必须声明规范运行状态")
        if self.default_consume_eligible and (
            self.acquisition_status != "succeeded"
            or self.coverage_accounted is not True
            or self.material_gap_count != 0
        ):
            raise ValueError(
                "默认可消费的acquisition SyncResult必须成功、覆盖可解释且无材料缺口"
            )
        return self


class FactVerificationRequest(BaseModel):
    left_fact_id: str
    right_fact_id: str
    relative_tolerance: float | None = Field(default=None, ge=0)


class DocumentIngestRequest(BaseModel):
    ticker: str
    path: str
    title: str
    source_name: str
    source_url: str | None = None
    published_at: datetime | None = None
    source_definition_id: str | None = None
    source_definition_version: str | None = None
    canonical_resource_id: str | None = None
    upstream_material_id: str | None = None


class DocumentRecord(BaseModel):
    document_id: str = Field(default_factory=new_id)
    ticker: str
    title: str
    archived_path: str
    text_path: str
    sha256: str
    source: SourceRecord
    raw_resource_snapshot_id: str | None = None
    derived_artifact_id: str | None = None
    supersedes_document_id: str | None = None
    document_version: int = Field(default=1, ge=1)
    page_count: int = 0
    ocr_used: bool = False
    warnings: list[str] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


# Acquisition v1 lives in a focused module, while these imports preserve the
# original public `analysis.models` import surface for callers migrating in
# stages.  The acquisition module deliberately does not import this module, so
# the compatibility facade cannot create an import cycle.
from .acquisition.models import (  # noqa: E402,F401
    AcquisitionAttempt,
    AcquisitionAttemptEvent,
    AcquisitionAttemptEventType,
    AcquisitionAttemptSegment,
    AcquisitionExecutionLease,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionPlan,
    AcquisitionRun,
    AcquisitionRunEvent,
    AcquisitionRunEventType,
    AcquisitionRunKind,
    AcquisitionRunResult,
    AnchorEvidence,
    AttemptKind,
    AvailableAtBasis,
    BarrierResolution,
    BootstrapStage,
    BusinessQuestion,
    BusinessQuestionSet,
    CheckpointPartition,
    CheckpointPosition,
    CompanyAcquisitionProfile,
    ContentBlob,
    CoverageEntry,
    CoveragePlanDisposition,
    CoverageResolution,
    CoverageResolutionStatus,
    DerivedArtifact,
    DiscoveryBodyPolicy,
    DiscoveryObservation,
    DiscoveryProof,
    DiscoverySchemaPolicy,
    DiscoveredResource,
    EvidenceManifestExclusion,
    EvidenceManifestItem,
    EvidenceSnapshotManifest,
    FetchPolicy,
    LiveAccessReviewCheck,
    LiveAccessReviewStatus,
    PaginationPolicy,
    PhysicalQueryCoverageLink,
    PhysicalQueryPlanItem,
    PolicyDecision,
    PublishedAtPrecision,
    QueryStage,
    RawResourceSnapshot,
    ResourceDisposition,
    ResourceObservation,
    ResourceRole,
    SnapshotIntegrityEvent,
    SnapshotIntegrityStatus,
    SourceAlias,
    SourceApplicability,
    SourceCandidate,
    SourceCandidateStatus,
    SourceCheckpoint,
    SourceDefinition,
    SourceDefinitionRef,
    SourceEndpointRule,
    SourceIncrementalPolicy,
    SourceLicensePolicy,
    SourceLiveAccessReview,
    SourcePolicyStatus,
    SourceRateLimitPolicy,
    SourceRegistry,
    SourceResponseLimits,
    SourceRetentionPolicy,
    SourceRetryPolicy,
    StorageBindingIntent,
    StorageNamespace,
    ValidatorAnchor,
)
