from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .canonical import canonical_json_bytes, canonical_sha256
from .models import SourceRole


GOVERNANCE_ACQUISITION_SCOPE = "governance_management"
GOVERNANCE_QUESTION_SET_ID = "governance_management_questions"
GOVERNANCE_QUESTION_SET_VERSION = "1.0.0"
NEGATIVE_FACT_POLICY = "never_infer_from_coverage"

EXACT_QUESTION_IDS: tuple[str, ...] = (
    "GOV.Q01.OWNERSHIP_CONTROL",
    "GOV.Q02.PLEDGE_FREEZE",
    "GOV.Q03.BOARD_COMMITTEES",
    "GOV.Q04.EXECUTIVE_ROSTER_BACKGROUND",
    "GOV.Q05.REMUNERATION_INCENTIVES",
    "GOV.Q06.RELATED_PARTIES",
    "GOV.Q07.EXTERNAL_AUDIT",
    "GOV.Q08.INTERNAL_CONTROL_CORRECTIONS",
    "GOV.Q09.REGULATORY_DISCLOSURE",
    "GOV.Q10.LITIGATION_COMMITMENTS",
    "GOV.Q11.GOVERNANCE_RULES",
)

DEFERRED_SOURCE_IDS: frozenset[str] = frozenset(
    {
        "court.records",
        "samr.corporate_registry",
        "chinaclear.registrations",
    }
)


class GovernanceRegistryError(ValueError):
    """Raised when a governance question or query registry fails closed."""


class FrozenRegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HistoryPolicy(str, Enum):
    SINCE_LISTING = "since_listing"
    LAST_FIVE_COMPLETE_FISCAL_YEARS = "last_five_complete_fiscal_years"
    OVERLAPPING_LIFECYCLE = "overlapping_lifecycle"
    FIRST_PROVEN_ARCHIVE = "first_proven_archive"


class QueryTimeSemantics(str, Enum):
    HISTORICAL_EVENT_STREAM = "historical_event_stream"
    CURRENT_OBSERVATION_ONLY = "current_observation_only"


class QueryStage(str, Enum):
    FETCH = "fetch"
    DISCOVERY = "discovery"
    DEFERRED = "deferred"


class HistoryPolicySpec(FrozenRegistryModel):
    default: HistoryPolicy
    record_overrides: Mapping[str, HistoryPolicy] = Field(default_factory=dict)

    @field_validator("record_overrides", mode="after")
    @classmethod
    def freeze_record_overrides(
        cls, value: Mapping[str, HistoryPolicy]
    ) -> Mapping[str, HistoryPolicy]:
        return MappingProxyType(dict(value))

    @field_serializer("record_overrides")
    def serialize_record_overrides(
        self, value: Mapping[str, HistoryPolicy]
    ) -> dict[str, HistoryPolicy]:
        return dict(value)


class GovernanceQuestion(FrozenRegistryModel):
    question_id: str
    label_zh: str
    target_record_kinds: tuple[str, ...]
    history_policy: HistoryPolicySpec
    anchor_kinds: tuple[str, ...]
    allowed_delta_kinds: tuple[str, ...]

    @model_validator(mode="after")
    def validate_question(self) -> "GovernanceQuestion":
        for field_name in (
            "target_record_kinds",
            "anchor_kinds",
            "allowed_delta_kinds",
        ):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"{self.question_id}: {field_name}不能为空")
            if len(set(values)) != len(values):
                raise ValueError(f"{self.question_id}: {field_name}不得重复")
            if any(not item.strip() for item in values):
                raise ValueError(f"{self.question_id}: {field_name}不得包含空值")
        unknown_override = set(self.history_policy.record_overrides) - set(
            self.target_record_kinds
        )
        if unknown_override:
            raise ValueError(
                f"{self.question_id}: history policy覆盖未知记录 "
                f"{sorted(unknown_override)}"
            )
        return self


class GovernanceQuestionRegistry(FrozenRegistryModel):
    schema_version: str
    acquisition_scope: str
    question_set_id: str
    question_set_version: str
    negative_fact_policy: str
    questions: tuple[GovernanceQuestion, ...]

    @model_validator(mode="after")
    def validate_registry(self) -> "GovernanceQuestionRegistry":
        if self.schema_version != "governance-questions.v1":
            raise ValueError("不支持的问题集schema_version")
        if self.acquisition_scope != GOVERNANCE_ACQUISITION_SCOPE:
            raise ValueError("治理问题集acquisition_scope必须为governance_management")
        if self.question_set_id != GOVERNANCE_QUESTION_SET_ID:
            raise ValueError("治理问题集question_set_id不匹配")
        if self.question_set_version != GOVERNANCE_QUESTION_SET_VERSION:
            raise ValueError("治理问题集question_set_version不匹配")
        if self.negative_fact_policy != NEGATIVE_FACT_POLICY:
            raise ValueError("coverage不得用于推导负面事实")
        ids = tuple(item.question_id for item in self.questions)
        if ids != EXACT_QUESTION_IDS:
            missing = sorted(set(EXACT_QUESTION_IDS) - set(ids))
            unknown = sorted(set(ids) - set(EXACT_QUESTION_IDS))
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            raise ValueError(
                "治理问题必须精确且按GOV.Q01至GOV.Q11排序: "
                f"missing={missing}, unknown={unknown}, duplicates={duplicates}"
            )
        return self

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            self,
            schema_name="governance-question-registry",
            schema_version=self.schema_version,
        )

    def question(self, question_id: str) -> GovernanceQuestion:
        for question in self.questions:
            if question.question_id == question_id:
                return question
        raise KeyError(question_id)


class SourceReference(FrozenRegistryModel):
    source_definition_id: str
    source_definition_version: str
    source_role: SourceRole
    enabled: bool
    stage: QueryStage

    @model_validator(mode="after")
    def validate_role_boundary(self) -> "SourceReference":
        normalized = self.source_definition_id.lower()
        if "akshare" in normalized:
            if self.source_role != SourceRole.DISCOVERY_ONLY:
                raise ValueError("AKShare必须保持discovery_only")
            if self.stage != QueryStage.DISCOVERY:
                raise ValueError("AKShare只能用于discovery stage")
        if self.source_definition_id in DEFERRED_SOURCE_IDS:
            if self.source_role != SourceRole.DEFERRED:
                raise ValueError("法院、工商和中登来源在v1必须为deferred")
            if self.enabled or self.stage != QueryStage.DEFERRED:
                raise ValueError("deferred来源必须禁用且不得进入采集阶段")
        if self.source_role == SourceRole.DEFERRED:
            if self.enabled or self.stage != QueryStage.DEFERRED:
                raise ValueError("deferred来源必须禁用")
        elif self.source_role == SourceRole.DISCOVERY_ONLY:
            if self.stage != QueryStage.DISCOVERY:
                raise ValueError("discovery_only来源必须使用discovery stage")
        elif self.stage != QueryStage.FETCH:
            raise ValueError("正式、监管或contextual来源只能使用fetch stage")
        return self

    @property
    def identity(self) -> tuple[str, str]:
        return self.source_definition_id, self.source_definition_version


class PhysicalQuerySemantics(FrozenRegistryModel):
    date_semantics: str
    page_semantics: str
    canonical_semantics: str
    dedup_fields: tuple[str, ...]

    @model_validator(mode="after")
    def validate_dedup(self) -> "PhysicalQuerySemantics":
        if not self.dedup_fields or len(set(self.dedup_fields)) != len(
            self.dedup_fields
        ):
            raise ValueError("physical query去重字段必须非空且唯一")
        if "source_definition_id" not in self.dedup_fields:
            raise ValueError("physical query去重必须包含source_definition_id")
        return self


class GovernanceQuery(FrozenRegistryModel):
    query_id: str
    source_definition_id: str
    source_definition_version: str
    question_ids: tuple[str, ...]
    execution_key: str
    query_family: str
    market_applicability: tuple[str, ...]
    time_semantics: QueryTimeSemantics
    stage: QueryStage
    required_attachment: bool
    physical_query: PhysicalQuerySemantics

    @model_validator(mode="after")
    def validate_query(self) -> "GovernanceQuery":
        if not self.query_id.startswith("GOV.QRY."):
            raise ValueError("治理query_id必须使用GOV.QRY命名空间")
        if not self.question_ids or len(set(self.question_ids)) != len(
            self.question_ids
        ):
            raise ValueError(f"{self.query_id}: question_ids必须非空且唯一")
        if not self.market_applicability:
            raise ValueError(f"{self.query_id}: market_applicability不能为空")
        if not self.execution_key.strip() or not self.query_family.strip():
            raise ValueError(f"{self.query_id}: execution_key/query_family不能为空")
        if self.time_semantics == QueryTimeSemantics.CURRENT_OBSERVATION_ONLY:
            if self.physical_query.date_semantics != "retrieved_at_only":
                raise ValueError("current_observation_only只能从retrieved_at开始证明")
        return self

    @property
    def source_identity(self) -> tuple[str, str]:
        return self.source_definition_id, self.source_definition_version

    @property
    def physical_signature(self) -> bytes:
        return canonical_json_bytes(
            {
                "source_definition_id": self.source_definition_id,
                "source_definition_version": self.source_definition_version,
                "query_family": self.query_family,
                "market_applicability": self.market_applicability,
                "time_semantics": self.time_semantics,
                "stage": self.stage,
                "required_attachment": self.required_attachment,
                "physical_query": self.physical_query,
            }
        )


class GovernanceQueryPack(FrozenRegistryModel):
    schema_version: str
    query_pack_id: str
    query_pack_version: str
    acquisition_scope: str
    question_set_id: str
    question_set_version: str
    source_registry_version: str
    source_references: tuple[SourceReference, ...]
    queries: tuple[GovernanceQuery, ...]

    @model_validator(mode="after")
    def validate_pack(self) -> "GovernanceQueryPack":
        if self.schema_version != "governance-query-pack.v1":
            raise ValueError("不支持的query pack schema_version")
        if self.acquisition_scope != GOVERNANCE_ACQUISITION_SCOPE:
            raise ValueError("治理query pack的scope必须为governance_management")
        if self.question_set_id != GOVERNANCE_QUESTION_SET_ID:
            raise ValueError("治理query pack的question_set_id不匹配")
        if self.question_set_version != GOVERNANCE_QUESTION_SET_VERSION:
            raise ValueError("治理query pack的question_set_version不匹配")
        if not self.source_registry_version.strip():
            raise ValueError("query pack必须冻结source_registry_version")
        source_ids = [item.identity for item in self.source_references]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source reference身份不得重复")
        query_ids = [item.query_id for item in self.queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("query_id不得重复")
        source_map = {item.identity: item for item in self.source_references}
        execution_to_signature: dict[str, bytes] = {}
        signature_to_execution: dict[bytes, str] = {}
        for query in self.queries:
            source = source_map.get(query.source_identity)
            if source is None:
                raise ValueError(f"{query.query_id}: 引用未知SourceDefinition")
            if not source.enabled:
                raise ValueError(f"{query.query_id}: 不得引用disabled/deferred来源")
            if query.stage != source.stage:
                raise ValueError(f"{query.query_id}: stage与来源角色不一致")
            if source.source_role == SourceRole.DEFERRED:
                raise ValueError(f"{query.query_id}: v1不得查询deferred来源")
            if source.source_role == SourceRole.DISCOVERY_ONLY and query.required_attachment:
                raise ValueError(f"{query.query_id}: discovery不得冒充必需正式附件")
            existing_signature = execution_to_signature.setdefault(
                query.execution_key, query.physical_signature
            )
            if existing_signature != query.physical_signature:
                raise ValueError(
                    f"{query.query_id}: 相同execution_key对应不同物理查询"
                )
            existing_execution = signature_to_execution.setdefault(
                query.physical_signature, query.execution_key
            )
            if existing_execution != query.execution_key:
                raise ValueError(
                    f"{query.query_id}: 相同物理查询使用不同execution_key，去重错误"
                )
        return self

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            self,
            schema_name="governance-query-pack",
            schema_version=self.schema_version,
        )

    @property
    def physical_query_count(self) -> int:
        return len({query.execution_key for query in self.queries})

    def queries_for_question(self, question_id: str) -> tuple[GovernanceQuery, ...]:
        return tuple(
            query for query in self.queries if question_id in query.question_ids
        )


class AcquisitionScopeIdentity(FrozenRegistryModel):
    acquisition_scope: str
    question_set_id: str
    question_set_version: str
    query_pack_version: str
    source_registry_version: str

    def checkpoint_key(
        self,
        *,
        ticker: str,
        source_definition_id: str,
        source_definition_version: str,
        partition_key: str,
        checkpoint_version: int,
    ) -> tuple[str | int, ...]:
        return (
            ticker,
            self.acquisition_scope,
            self.question_set_id,
            self.question_set_version,
            self.query_pack_version,
            self.source_registry_version,
            source_definition_id,
            source_definition_version,
            partition_key,
            checkpoint_version,
        )

    def manifest_selector_key(self, *, ticker: str) -> tuple[str, ...]:
        return (
            ticker,
            self.acquisition_scope,
            self.question_set_id,
            self.question_set_version,
            self.query_pack_version,
            self.source_registry_version,
        )

    def latest_selector_key(self, *, ticker: str) -> tuple[str, ...]:
        return self.manifest_selector_key(ticker=ticker)


class GovernanceRegistryBundle(FrozenRegistryModel):
    questions: GovernanceQuestionRegistry
    query_pack: GovernanceQueryPack

    @model_validator(mode="after")
    def validate_traceability(self) -> "GovernanceRegistryBundle":
        pairs = (
            ("acquisition_scope", self.questions.acquisition_scope, self.query_pack.acquisition_scope),
            ("question_set_id", self.questions.question_set_id, self.query_pack.question_set_id),
            (
                "question_set_version",
                self.questions.question_set_version,
                self.query_pack.question_set_version,
            ),
        )
        for name, left, right in pairs:
            if left != right:
                raise ValueError(f"问题集与query pack的{name}不一致")
        known = set(EXACT_QUESTION_IDS)
        referenced: set[str] = set()
        authoritative: set[str] = set()
        source_map = {
            item.identity: item for item in self.query_pack.source_references
        }
        for query in self.query_pack.queries:
            unknown = set(query.question_ids) - known
            if unknown:
                raise ValueError(f"{query.query_id}: 包含未知问题 {sorted(unknown)}")
            referenced.update(query.question_ids)
            role = source_map[query.source_identity].source_role
            if role in {SourceRole.OFFICIAL_DISCLOSURE, SourceRole.REGULATOR_EXCHANGE}:
                authoritative.update(query.question_ids)
        missing = sorted(known - referenced)
        missing_authoritative = sorted(known - authoritative)
        if missing:
            raise ValueError(f"问题未映射任何查询: {missing}")
        if missing_authoritative:
            raise ValueError(f"问题未映射正式或监管查询: {missing_authoritative}")
        return self

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(
            {
                "questions_hash": self.questions.canonical_hash,
                "query_pack_hash": self.query_pack.canonical_hash,
            },
            schema_name="governance-registry-bundle",
            schema_version="1",
        )

    @property
    def scope_identity(self) -> AcquisitionScopeIdentity:
        return AcquisitionScopeIdentity(
            acquisition_scope=self.query_pack.acquisition_scope,
            question_set_id=self.query_pack.question_set_id,
            question_set_version=self.query_pack.question_set_version,
            query_pack_version=self.query_pack.query_pack_version,
            source_registry_version=self.query_pack.source_registry_version,
        )


def _load_json_bytes(path: Path | str) -> bytes:
    resolved = Path(path)
    try:
        raw = resolved.read_bytes()
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GovernanceRegistryError(f"无法读取治理配置 {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise GovernanceRegistryError(f"治理配置顶层必须是object: {resolved}")
    return raw


def load_question_registry(path: Path | str) -> GovernanceQuestionRegistry:
    try:
        return GovernanceQuestionRegistry.model_validate_json(
            _load_json_bytes(path), strict=True
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, GovernanceRegistryError):
            raise
        raise GovernanceRegistryError(f"问题集校验失败: {exc}") from exc


def load_query_pack(path: Path | str) -> GovernanceQueryPack:
    try:
        return GovernanceQueryPack.model_validate_json(
            _load_json_bytes(path), strict=True
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, GovernanceRegistryError):
            raise
        raise GovernanceRegistryError(f"query pack校验失败: {exc}") from exc


def load_registry_bundle(
    questions_path: Path | str,
    query_pack_path: Path | str,
) -> GovernanceRegistryBundle:
    try:
        return GovernanceRegistryBundle(
            questions=load_question_registry(questions_path),
            query_pack=load_query_pack(query_pack_path),
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, GovernanceRegistryError):
            raise
        raise GovernanceRegistryError(f"治理注册表追踪校验失败: {exc}") from exc


def ensure_question_traceability(
    questions: GovernanceQuestionRegistry,
    queries: Iterable[GovernanceQuery],
) -> None:
    """Compatibility helper for callers that validate a materialized plan."""
    known = {item.question_id for item in questions.questions}
    mapped = {question_id for query in queries for question_id in query.question_ids}
    unknown = sorted(mapped - known)
    missing = sorted(known - mapped)
    if unknown or missing:
        raise GovernanceRegistryError(
            f"问题-query追踪不完整: missing={missing}, unknown={unknown}"
        )


__all__ = [
    "AcquisitionScopeIdentity",
    "DEFERRED_SOURCE_IDS",
    "EXACT_QUESTION_IDS",
    "GOVERNANCE_ACQUISITION_SCOPE",
    "GOVERNANCE_QUESTION_SET_ID",
    "GOVERNANCE_QUESTION_SET_VERSION",
    "GovernanceQuery",
    "GovernanceQueryPack",
    "GovernanceQuestion",
    "GovernanceQuestionRegistry",
    "GovernanceRegistryBundle",
    "GovernanceRegistryError",
    "HistoryPolicy",
    "NEGATIVE_FACT_POLICY",
    "QueryStage",
    "QueryTimeSemantics",
    "SourceReference",
    "ensure_question_traceability",
    "load_query_pack",
    "load_question_registry",
    "load_registry_bundle",
]
