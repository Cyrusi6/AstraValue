from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenStructuredModel(BaseModel):
    """Strictly shaped, immutable values used by the structured-data registries."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RegistryKind(str, Enum):
    DATASETS = "datasets"
    FIELDS = "fields"
    PEER_SETS = "peer_sets"
    SCHEDULES = "schedules"
    READING_RULES = "reading_rules"
    RESEARCH_REQUIREMENTS = "research_requirements"
    INDUSTRY_PROFILES = "industry_profiles"


class RegistryEnvelope(FrozenStructuredModel):
    schema_version: Literal["structured-registry.v1"]
    registry_kind: RegistryKind
    registry_id: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_from: tuple[str, ...] = Field(min_length=1)


class RequestContract(FrozenStructuredModel):
    protocol: Literal["em_s", "em_w", "em_m", "em_f", "em_q", "baostock_sdk"]
    method: Literal["GET", "SDK"]
    endpoint: str = Field(min_length=1)
    report_name: str | None = None
    allowed_parameters: tuple[str, ...] = Field(min_length=1)
    fixed_parameters: dict[str, str] = Field(default_factory=dict)
    parameter_template: dict[str, str] = Field(default_factory=dict)
    company_filter_field: str | None = None
    provider_code_format: str | None = None

    @model_validator(mode="after")
    def validate_parameters(self) -> "RequestContract":
        allowed = set(self.allowed_parameters)
        if len(allowed) != len(self.allowed_parameters):
            raise ValueError("allowed_parameters不得重复")
        unknown = (set(self.fixed_parameters) | set(self.parameter_template)) - allowed
        if unknown:
            raise ValueError(f"请求模板含未允许参数: {sorted(unknown)}")
        if set(self.fixed_parameters) & set(self.parameter_template):
            raise ValueError("同一参数不能同时固定和模板化")
        if self.method == "SDK" and self.protocol != "baostock_sdk":
            raise ValueError("SDK方法只能用于baostock_sdk")
        if self.method == "GET" and not self.endpoint.startswith("https://"):
            raise ValueError("HTTP数据集必须使用HTTPS endpoint")
        return self


class EmptyResultContract(FrozenStructuredModel):
    success_evidence: tuple[str, ...] = Field(min_length=1)
    empty_evidence: tuple[str, ...] = Field(min_length=1)
    does_not_prove: tuple[str, ...] = Field(min_length=1)


class DatasetDefinition(FrozenStructuredModel):
    dataset_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    plan_group_id: str = Field(pattern=r"^[A-Z]\d{2}$")
    provider: Literal["eastmoney", "baostock"]
    upstream_identity: Literal["eastmoney", "baostock"]
    request: RequestContract
    history_mode: Literal[
        "all_available_history",
        "on_demand_all_available_history",
        "snapshot_from_first_retrieval",
    ]
    history_enumeration: Literal[
        "financial_date_catalog",
        "complete_pagination",
        "year_quarter_batches",
        "date_range_batches",
        "current_snapshot",
        "on_demand_complete_pagination",
    ]
    primary_key_fields: tuple[str, ...] = Field(min_length=1)
    date_fields: tuple[str, ...] = Field(min_length=1)
    pagination: Literal["page_number", "financial_date_catalog", "sdk_exhaustion", "none"]
    update_category: Literal["financial", "market", "event", "snapshot", "on_demand"]
    empty_result: EmptyResultContract
    expected_field_count: int = Field(ge=1)
    field_coverage_status: Literal["response_fields_observed"]
    applicability: str = Field(min_length=1)
    sample_evidence_hashes: tuple[str, ...] = Field(min_length=1)


class DatasetRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.DATASETS]
    datasets: tuple[DatasetDefinition, ...] = Field(min_length=1)


class FieldNature(str, Enum):
    OBSERVED = "observed"
    PROVIDER_DEFINED_INDICATOR = "provider_defined_indicator"
    PROVIDER_ESTIMATE = "provider_estimate"
    FORECAST = "forecast"
    PLATFORM_LABEL = "platform_label"
    SOURCE_TEXT = "source_text"
    ANNOUNCED_PLAN_AMOUNT = "announced_plan_amount"
    RECORDED_ACTUAL_AMOUNT = "recorded_actual_amount"
    UNCLASSIFIED = "unclassified"


class DefinitionStatus(str, Enum):
    CONFIRMED = "confirmed"
    CANDIDATE = "candidate"
    UNKNOWN = "unknown"


class UnitStatus(str, Enum):
    CONFIRMED = "confirmed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class RawFieldDefinition(FrozenStructuredModel):
    field_id: str = Field(min_length=3)
    dataset_id: str
    plan_group_id: str
    raw_name: str = Field(min_length=1)
    observed_json_types: tuple[str, ...] = Field(min_length=1)
    nonempty_in_sample: bool
    nature: FieldNature
    definition_status: DefinitionStatus
    unit_status: UnitStatus
    period_semantics: str
    scope_semantics: str
    formula_eligible: bool
    classification_basis: str

    @model_validator(mode="after")
    def reject_unknown_formula_inputs(self) -> "RawFieldDefinition":
        if self.formula_eligible and (
            self.nature
            not in {FieldNature.OBSERVED, FieldNature.PROVIDER_DEFINED_INDICATOR}
            or self.definition_status != DefinitionStatus.CONFIRMED
            or self.unit_status == UnitStatus.UNKNOWN
        ):
            raise ValueError(f"{self.field_id}: 未确认字段不得进入确定性公式")
        return self


class StandardFieldRoute(FrozenStructuredModel):
    route_id: str
    standard_field_id: str
    period_semantics: str
    consolidation_scope: str
    business_dimension: str
    definition_version: str
    dataset_id: str
    raw_name: str
    role: Literal["primary", "fallback"]
    validation_status: Literal["validated", "unvalidated"]

    @property
    def route_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.standard_field_id,
            self.period_semantics,
            self.consolidation_scope,
            self.business_dimension,
            self.definition_version,
        )


class DynamicFieldPolicy(FrozenStructuredModel):
    preserve_raw_response: Literal[True]
    register_unknown_fields: Literal[True]
    default_nature: Literal[FieldNature.UNCLASSIFIED]
    default_definition_status: Literal[DefinitionStatus.UNKNOWN]
    formula_eligible: Literal[False]


class FieldRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.FIELDS]
    declared_field_positions: int = Field(ge=1)
    classification_summary: dict[str, int]
    fields: tuple[RawFieldDefinition, ...] = Field(min_length=1)
    routes: tuple[StandardFieldRoute, ...] = ()
    dynamic_field_policy: DynamicFieldPolicy


class PeerCompany(FrozenStructuredModel):
    company_name: str
    canonical_ticker: str = Field(pattern=r"^\d{6}\.(SH|SZ)$")
    supplier_security_code: str = Field(pattern=r"^\d{6}$")
    baostock_code: str = Field(pattern=r"^(sh|sz)\.\d{6}$")
    role: Literal["target", "core_peer", "operating_peer"]
    inclusion_basis: str = Field(min_length=1)


class PeerSetDefinition(FrozenStructuredModel):
    peer_set_id: str
    selected_at: str
    industry_taxonomy_version: str
    decision_kind: Literal["rule_selected"]
    user_confirmed: Literal[False]
    companies: tuple[PeerCompany, ...] = Field(min_length=1)
    acquisition_scope: Literal["all_applicable_datasets_all_available_history"]
    display_windows_do_not_limit_acquisition: Literal[True]


class PeerSetRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.PEER_SETS]
    peer_sets: tuple[PeerSetDefinition, ...] = Field(min_length=1)


class DatasetSchedule(FrozenStructuredModel):
    dataset_id: str
    baseline_scope: Literal[
        "all_available_history",
        "on_demand_all_available_history",
        "snapshot_from_first_retrieval",
    ]
    incremental_rule: str
    cron_local: str | None = None
    overlap_days: int | None = Field(default=None, ge=1)
    refresh_open_lifecycle: bool = False


class ScheduleRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.SCHEDULES]
    timezone: Literal["Asia/Shanghai"]
    source_min_interval_seconds: dict[str, int]
    max_attempts_per_execution: int = Field(ge=1, le=2)
    analysis_default_complete_years: Literal[5]
    analysis_default_published_quarters: Literal[12]
    analysis_window_limits_acquisition: Literal[False]
    dataset_schedules: tuple[DatasetSchedule, ...] = Field(min_length=1)


class ReadingRule(FrozenStructuredModel):
    rule_id: str = Field(pattern=r"^R(0[1-9]|1[0-2])$")
    trigger_kind: Literal["quantitative", "event", "correction", "research_question"]
    expression: str = Field(min_length=1)
    input_refs: tuple[str, ...] = Field(min_length=1)
    target_routes: tuple[str, ...] = Field(min_length=1)
    creates_risk_rating: Literal[False]


class ReadingRuleRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.READING_RULES]
    fixed_report_policy: dict[str, str]
    rules: tuple[ReadingRule, ...] = Field(min_length=1)


class SupportStatus(str, Enum):
    SUPPORTED = "supported"
    CANDIDATE_MAPPING = "candidate_mapping"
    READING_REQUIRED = "reading_required"
    UNSUPPORTED_FREE_SOURCE = "unsupported_free_source"
    RESEARCH_CONTEXT_REQUIRED = "research_context_required"


class RequirementPath(FrozenStructuredModel):
    kind: Literal[
        "raw_field",
        "record_set",
        "reading_section",
        "calculation",
        "gap",
        "research_context",
        "coverage_snapshot",
        "title_only",
    ]
    dataset_id: str | None = None
    raw_name: str | None = None
    route_id: str | None = None
    gap_id: str | None = None
    research_object_kind: str | None = None
    support_status: SupportStatus
    semantic_key: str
    period_semantics: str
    scope_semantics: str
    unit_semantics: str

    @model_validator(mode="after")
    def validate_exact_path(self) -> "RequirementPath":
        if self.kind == "title_only":
            raise ValueError("需求不得用标题代替取得路径")
        if self.kind == "raw_field" and not (self.dataset_id and self.raw_name):
            raise ValueError("raw_field路径必须精确到dataset_id/raw_name")
        if self.kind == "record_set" and not self.dataset_id:
            raise ValueError("record_set路径必须指定dataset_id")
        if self.kind in {"reading_section", "calculation"} and not self.route_id:
            raise ValueError(f"{self.kind}路径必须指定route_id")
        if self.kind == "gap" and not self.gap_id:
            raise ValueError("gap路径必须指定gap_id")
        if self.kind == "research_context" and not self.research_object_kind:
            raise ValueError("research_context必须指定已有研究对象类型")
        return self


class ResearchRequirement(FrozenStructuredModel):
    requirement_id: str = Field(pattern=r"^REQ\.ES\d{2}\.Q\d{2}\.\d{3}$")
    question_id: str = Field(pattern=r"^ES\d{2}\.Q\d{2}$")
    requiredness: Literal["required", "optional"]
    combination: Literal["all_of", "any_of"] = "all_of"
    paths: tuple[RequirementPath, ...] = Field(min_length=1)
    applicability_condition: str
    output_slots: tuple[str, ...] = Field(min_length=1)
    depends_on_requirement_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_any_of(self) -> "ResearchRequirement":
        if self.combination == "any_of":
            if len(self.paths) < 2:
                raise ValueError("any_of必须至少包含两个取得路径")
            semantics = {
                (
                    path.semantic_key,
                    path.period_semantics,
                    path.scope_semantics,
                    path.unit_semantics,
                )
                for path in self.paths
            }
            if len(semantics) != 1:
                raise ValueError("any_of只允许经济含义、期间、范围和单位相同的路径")
        return self


class ResearchQuestion(FrozenStructuredModel):
    question_id: str = Field(pattern=r"^ES\d{2}\.Q\d{2}$")
    step_id: str = Field(pattern=r"^ES\d{2}$")
    ordinal: int = Field(ge=1)
    title: str = Field(min_length=1)
    method_ref: str = Field(min_length=1)
    period_modes: tuple[Literal["NOW", "FIN", "BIZ", "EVT", "IND", "SCN"], ...]
    applicability: str = Field(min_length=1)
    required_requirement_ids: tuple[str, ...] = Field(min_length=1)
    optional_requirement_ids: tuple[str, ...] = ()
    output_slots: tuple[str, ...] = Field(min_length=1)


class CalculationRoute(FrozenStructuredModel):
    route_id: str = Field(pattern=r"^K(0[1-9]|1[01])$")
    input_semantics: tuple[str, ...] = Field(min_length=1)
    input_refs: tuple[str, ...] = Field(min_length=1)
    output_semantics: tuple[str, ...] = Field(min_length=1)
    depends_on_routes: tuple[str, ...] = ()
    formula_boundary: str = Field(min_length=1)


class ReadingRoute(FrozenStructuredModel):
    route_id: str = Field(pattern=r"^RD(0[1-9]|1[0-2])$")
    materials_and_sections: str = Field(min_length=1)
    extraction_targets: tuple[str, ...] = Field(min_length=1)


class GapDefinition(FrozenStructuredModel):
    gap_id: str = Field(pattern=r"^GAP(0[1-9]|1[0-2])$")
    affected_inputs: tuple[str, ...] = Field(min_length=1)
    next_paths: tuple[str, ...] = Field(min_length=1)
    completion_evidence: str = Field(min_length=1)


class LegacyCrosswalk(FrozenStructuredModel):
    legacy_question_set_id: Literal["business_model_questions"]
    legacy_question_set_version: Literal["1.0.0"]
    legacy_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mappings: dict[str, str]


class ResearchRequirementRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.RESEARCH_REQUIREMENTS]
    question_set_id: Literal["eight_step_research_requirements"]
    question_set_version: Literal["1.0.0"]
    default_analysis_window: dict[str, Any]
    questions: tuple[ResearchQuestion, ...] = Field(min_length=1)
    requirements: tuple[ResearchRequirement, ...] = Field(min_length=1)
    calculation_routes: tuple[CalculationRoute, ...] = Field(min_length=1)
    reading_routes: tuple[ReadingRoute, ...] = Field(min_length=1)
    gaps: tuple[GapDefinition, ...] = Field(min_length=1)
    legacy_crosswalk: LegacyCrosswalk


class IndustryInput(FrozenStructuredModel):
    input_id: str
    path: RequirementPath
    activation_condition: str
    affected_question_ids: tuple[str, ...] = Field(min_length=1)
    replaces_semantic_keys: tuple[str, ...] = ()


class ConditionalRequirementGroup(FrozenStructuredModel):
    condition_id: str
    evidence_required: tuple[str, ...] = Field(min_length=1)
    unknown_behavior: Literal["pending"]
    input_ids: tuple[str, ...] = Field(min_length=1)


class IndustryProfile(FrozenStructuredModel):
    profile_id: Literal[
        "general",
        "manufacturing",
        "consumer",
        "technology",
        "bank",
        "insurance",
        "securities",
        "real_estate",
        "resources",
        "utility",
        "preprofit",
    ]
    label: str
    mode: Literal["base", "industry", "overlay"]
    inherits_common_requirements: Literal[True]
    activation_evidence: tuple[str, ...] = Field(min_length=1)
    unknown_behavior: Literal["pending_not_general_fallback"]
    inputs: tuple[IndustryInput, ...] = Field(min_length=1)
    conditional_groups: tuple[ConditionalRequirementGroup, ...] = ()
    preserve_unreplaced_steps: Literal[True]


class IndustryProfileRegistry(RegistryEnvelope):
    registry_kind: Literal[RegistryKind.INDUSTRY_PROFILES]
    profile_selection_version: Literal["1.0.0"]
    mixed_business_rule: str
    preprofit_is_overlay: Literal[True]
    classification_sources_are_not_research_profiles: Literal[True]
    unknown_industry_default_profile: None
    profiles: tuple[IndustryProfile, ...] = Field(min_length=1)
