from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from pydantic import ValidationError

from .models import (
    BusinessQuestionSet,
    LiveAccessReviewStatus,
    PolicyDecision,
    SourceCandidate,
    SourceCandidateStatus,
    SourceDefinition,
    SourcePolicyStatus,
    SourceRegistry,
    canonical_json_bytes,
    canonical_json_sha256,
    stable_acquisition_id,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_CONFIG_DIR = PROJECT_ROOT / "config" / "data_sources"
INITIAL_REGISTRY_PATH = DEFAULT_SOURCE_CONFIG_DIR / "business_model_sources.v1.json"
REVIEWED_REGISTRY_PATH = DEFAULT_SOURCE_CONFIG_DIR / "business_model_sources.v1.1.json"
POLICY_APPROVED_REGISTRY_PATH = (
    DEFAULT_SOURCE_CONFIG_DIR / "business_model_sources.v1.2.json"
)
DEFAULT_REGISTRY_PATH = DEFAULT_SOURCE_CONFIG_DIR / "business_model_sources.v1.3.json"
DEFAULT_QUESTIONS_PATH = DEFAULT_SOURCE_CONFIG_DIR / "business_model_questions.v1.json"

SUPPORTED_REGISTRY_SCHEMA_VERSION = "1.0.0"
SUPPORTED_QUESTION_SCHEMA_VERSION = "1.0.0"
BUSINESS_MODEL_V1_SOURCE_IDS = frozenset(
    {
        "cninfo.disclosures",
        "sse.disclosures",
        "szse.disclosures",
        "moutai.ir",
    }
)
BUSINESS_MODEL_V1_ADAPTER_KEYS = {
    "cninfo.disclosures": "cninfo",
    "sse.disclosures": "sse",
    "szse.disclosures": "szse",
    "moutai.ir": "moutai_ir",
}
BUSINESS_MODEL_V1_INITIAL_REGISTRY_VERSION = "1.0.0"
MOUTAI_IR_INITIAL_DEFINITION_VERSION = "1.0.0"
KNOWN_ADAPTER_KEYS = frozenset(
    {
        "cninfo",
        "sse",
        "szse",
        "moutai_ir",
        "official",
        "akshare",
        "sina",
        "baostock",
        "tushare",
    }
)


class SourceRegistryError(RuntimeError):
    """Raised before network I/O when a source registry is not safe to use."""


@dataclass(frozen=True, slots=True)
class LoadedSourceRegistry:
    registry: SourceRegistry
    canonical_json: bytes
    content_hash: str
    source_definition_hashes: dict[tuple[str, str], str]

    def definition(self, source_definition_id: str) -> SourceDefinition:
        return self.registry.definition(source_definition_id)


@dataclass(frozen=True, slots=True)
class LoadedQuestionSet:
    question_set: BusinessQuestionSet
    canonical_json: bytes
    content_hash: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceRegistryError(f"JSON包含重复键: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SourceRegistryError(f"缺少来源配置: {path}") from exc
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except SourceRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceRegistryError(f"来源配置不是合法UTF-8 JSON: {path}: {exc}") from exc


class SourceRegistryLoader:
    """Load, normalize and freeze registry inputs before any adapter is resolved."""

    def __init__(
        self,
        *,
        known_adapter_keys: Iterable[str] = KNOWN_ADAPTER_KEYS,
    ) -> None:
        self.known_adapter_keys = frozenset(known_adapter_keys)

    def load_registry(
        self,
        path: Path | str = DEFAULT_REGISTRY_PATH,
        *,
        question_set: BusinessQuestionSet | LoadedQuestionSet | None = None,
        expect_business_model_v1: int | None = None,
    ) -> LoadedSourceRegistry:
        resolved = Path(path)
        payload = _read_json(resolved)
        try:
            registry = SourceRegistry.model_validate(payload)
        except ValidationError as exc:
            raise SourceRegistryError(f"来源注册表schema校验失败: {exc}") from exc
        if registry.schema_version != SUPPORTED_REGISTRY_SCHEMA_VERSION:
            raise SourceRegistryError(
                f"不支持的来源注册表schema版本: {registry.schema_version}"
            )
        if not registry.definitions:
            raise SourceRegistryError("来源注册表definitions不能为空")
        for definition in (*registry.definitions, *registry.legacy_definitions):
            if definition.adapter_key not in self.known_adapter_keys:
                raise SourceRegistryError(
                    f"未知adapter_key，已在联网前拒绝: {definition.adapter_key}"
                )
            self._validate_definition(definition)
        self._validate_aliases(registry)
        loaded_questions = (
            question_set.question_set
            if isinstance(question_set, LoadedQuestionSet)
            else question_set
        )
        if loaded_questions is not None:
            self.validate_traceability(registry, loaded_questions)
        if expect_business_model_v1 is not None:
            self.validate_business_model_v1(
                registry,
                expected_count=expect_business_model_v1,
            )
        canonical = canonical_json_bytes(registry)
        hashes = {
            (definition.source_definition_id, definition.version): canonical_json_sha256(
                definition
            )
            for definition in (*registry.definitions, *registry.legacy_definitions)
        }
        return LoadedSourceRegistry(
            registry=registry,
            canonical_json=canonical,
            content_hash=canonical_json_sha256(registry),
            source_definition_hashes=hashes,
        )

    def load_questions(
        self,
        path: Path | str = DEFAULT_QUESTIONS_PATH,
    ) -> LoadedQuestionSet:
        resolved = Path(path)
        payload = _read_json(resolved)
        try:
            question_set = BusinessQuestionSet.model_validate(payload)
        except ValidationError as exc:
            raise SourceRegistryError(f"业务问题清单schema校验失败: {exc}") from exc
        if question_set.schema_version != SUPPORTED_QUESTION_SCHEMA_VERSION:
            raise SourceRegistryError(
                f"不支持的问题清单schema版本: {question_set.schema_version}"
            )
        expected_ids = [f"BM.Q{ordinal:02d}." for ordinal in range(1, 11)]
        actual_ids = [item.question_id for item in question_set.topics]
        for prefix, actual in zip(expected_ids, actual_ids, strict=True):
            if not actual.startswith(prefix):
                raise SourceRegistryError(
                    "business_model问题必须按BM.Q01至BM.Q10稳定排序并命名"
                )
        canonical = canonical_json_bytes(question_set)
        return LoadedQuestionSet(
            question_set=question_set,
            canonical_json=canonical,
            content_hash=canonical_json_sha256(question_set),
        )

    @staticmethod
    def _validate_definition(definition: SourceDefinition) -> None:
        if definition.live_access_review.status == LiveAccessReviewStatus.REJECTED:
            if definition.policy_status not in {
                SourcePolicyStatus.PENDING_POLICY,
                SourcePolicyStatus.DISABLED,
            }:
                raise SourceRegistryError(
                    "live access拒绝来源必须保持pending_policy或disabled"
                )
            if definition.enabled or definition.access_method != "disabled":
                raise SourceRegistryError("live access拒绝来源必须disabled且零I/O")
            if definition.initial_request_allowlist or definition.redirect_allowlist:
                raise SourceRegistryError(
                    "live access拒绝来源不得保留可联网allowlist"
                )
            if any(query.endpoint is not None for query in definition.queries):
                raise SourceRegistryError(
                    "live access拒绝来源query不得具有可请求endpoint"
                )
        if definition.policy_status == SourcePolicyStatus.ENABLED:
            for query in definition.queries:
                if query.endpoint is None:
                    raise SourceRegistryError(
                        f"启用来源query缺少endpoint: {definition.source_definition_id}/{query.query_id}"
                    )
                if not definition.allows_url(query.endpoint):
                    raise SourceRegistryError(
                        f"query endpoint越过初始allowlist: {definition.source_definition_id}/{query.query_id}"
                    )
        if definition.policy_status == SourcePolicyStatus.PENDING_POLICY:
            if definition.enabled or definition.access_method != "disabled":
                raise SourceRegistryError("pending_policy来源必须disabled且零I/O")
            if definition.initial_request_allowlist or definition.redirect_allowlist:
                raise SourceRegistryError("pending_policy来源不得预置可联网allowlist")
            if any(query.endpoint is not None for query in definition.queries):
                raise SourceRegistryError("pending_policy来源query不得具有可请求endpoint")
        if definition.expires_at is not None and definition.expires_at <= definition.effective_at:
            raise SourceRegistryError("来源定义有效期非法")

    @staticmethod
    def _validate_aliases(registry: SourceRegistry) -> None:
        business_ids = {
            item.source_definition_id
            for item in registry.definitions
            if "business_model" in item.scopes
        }
        for alias in registry.aliases:
            if "business_model" in alias.scopes and not set(
                alias.target_source_definition_ids
            ) <= business_ids:
                raise SourceRegistryError("business_model alias不得绕过v1来源边界")

    @staticmethod
    def validate_traceability(
        registry: SourceRegistry,
        question_set: BusinessQuestionSet,
    ) -> None:
        if registry.question_set_version != question_set.version:
            raise SourceRegistryError("registry与question set版本不一致")
        question_ids = {item.question_id for item in question_set.topics}
        query_question_ids = {
            question_id
            for definition in registry.definitions
            if "business_model" in definition.scopes
            for query in definition.queries
            for question_id in query.question_ids
        }
        unknown = query_question_ids - question_ids
        if unknown:
            raise SourceRegistryError(f"query映射未知question ID: {sorted(unknown)}")
        missing = question_ids - query_question_ids
        if missing:
            raise SourceRegistryError(f"question缺少source query追踪: {sorted(missing)}")
        available_families = {
            query.query_family
            for definition in registry.definitions
            if "business_model" in definition.scopes
            for query in definition.queries
        }
        for question in question_set.topics:
            missing_families = set(question.query_families) - available_families
            if missing_families:
                raise SourceRegistryError(
                    f"{question.question_id}缺少query family定义: {sorted(missing_families)}"
                )

    @staticmethod
    def validate_business_model_v1(
        registry: SourceRegistry,
        *,
        expected_count: int = 4,
    ) -> None:
        definitions = [
            item
            for item in registry.definitions
            if "business_model" in item.scopes and item.scope_version == "v1"
        ]
        ids = {item.source_definition_id for item in definitions}
        if len(definitions) != expected_count:
            raise SourceRegistryError(
                f"business_model v1来源数量应为{expected_count}，实际为{len(definitions)}"
            )
        if expected_count == 4 and ids != BUSINESS_MODEL_V1_SOURCE_IDS:
            raise SourceRegistryError(
                f"business_model v1来源边界不符: {sorted(ids)}"
            )
        for definition in definitions:
            expected_adapter = BUSINESS_MODEL_V1_ADAPTER_KEYS.get(
                definition.source_definition_id
            )
            if (
                expected_adapter is not None
                and definition.adapter_key != expected_adapter
            ):
                raise SourceRegistryError(
                    "business_model v1来源adapter_key不符: "
                    f"{definition.source_definition_id}必须使用{expected_adapter}"
                )
            if definition.license_policy.access_cost.value == "paid":
                raise SourceRegistryError("business_model v1不得包含付费源")
            if definition.rate_limit.max_concurrency != 1:
                raise SourceRegistryError("business_model v1同源最大并发必须为1")
        ir = next(
            item for item in definitions if item.source_definition_id == "moutai.ir"
        )
        initial_ir_contract = (
            registry.registry_version == BUSINESS_MODEL_V1_INITIAL_REGISTRY_VERSION
            or ir.version == MOUTAI_IR_INITIAL_DEFINITION_VERSION
        )
        if initial_ir_contract:
            SourceRegistryLoader._validate_pending_moutai_ir(
                ir,
                reason="business_model v1初始registry和IR定义",
            )
            return
        if ir.enabled:
            SourceRegistryLoader._validate_enabled_moutai_ir(ir)
        elif ir.policy_status == SourcePolicyStatus.PENDING_POLICY:
            SourceRegistryLoader._validate_pending_moutai_ir(
                ir,
                reason="尚未批准的贵州茅台IR定义",
            )

    @staticmethod
    def _validate_pending_moutai_ir(
        definition: SourceDefinition,
        *,
        reason: str,
    ) -> None:
        if (
            definition.policy_status != SourcePolicyStatus.PENDING_POLICY
            or definition.enabled
            or definition.access_method != "disabled"
        ):
            raise SourceRegistryError(f"{reason}必须保持pending_policy/disabled")
        if definition.initial_request_allowlist or definition.redirect_allowlist:
            raise SourceRegistryError("未批准IR不得预置可联网allowlist")
        if any(query.endpoint is not None for query in definition.queries):
            raise SourceRegistryError("未批准IR query不得具有可请求endpoint")

    @staticmethod
    def _validate_enabled_moutai_ir(definition: SourceDefinition) -> None:
        if definition.policy_status != SourcePolicyStatus.ENABLED:
            raise SourceRegistryError("启用贵州茅台IR必须使用enabled策略状态")
        if definition.adapter_key != BUSINESS_MODEL_V1_ADAPTER_KEYS["moutai.ir"]:
            raise SourceRegistryError("启用贵州茅台IR必须使用moutai_ir专用adapter")
        if definition.access_method not in {"https_api", "https_document"}:
            raise SourceRegistryError("启用贵州茅台IR必须使用已审核的HTTPS访问方式")
        if definition.live_access_review.status != LiveAccessReviewStatus.APPROVED:
            raise SourceRegistryError("启用贵州茅台IR必须完成live access人工审核")
        license_decisions = {
            "automated_access": definition.license_policy.automated_access,
            "archive_original": definition.license_policy.archive_original,
            "save_derived_text": definition.license_policy.save_derived_text,
            "llm_processing": definition.license_policy.llm_processing,
        }
        pending_decisions = sorted(
            name
            for name, decision in license_decisions.items()
            if decision == PolicyDecision.PENDING
        )
        if pending_decisions:
            raise SourceRegistryError(
                "启用贵州茅台IR的许可与LLM决策不得pending: "
                f"{pending_decisions}"
            )
        if not (
            definition.initial_request_allowlist
            and definition.redirect_allowlist
        ):
            raise SourceRegistryError("启用贵州茅台IR必须声明初始请求和逐跳重定向allowlist")
        if not definition.queries or any(
            query.endpoint is None for query in definition.queries
        ):
            raise SourceRegistryError("启用贵州茅台IR的每个query必须具有固定endpoint")

    @staticmethod
    def assert_effective(
        definition: SourceDefinition,
        as_of: datetime,
    ) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise SourceRegistryError("有效期检查时间必须包含时区")
        checked = as_of.astimezone(definition.effective_at.tzinfo)
        if checked < definition.effective_at:
            raise SourceRegistryError("来源定义在run as_of尚未生效")
        if definition.expires_at is not None and checked >= definition.expires_at:
            raise SourceRegistryError("来源定义在run as_of已经失效")

    @staticmethod
    def validate_request_url(
        definition: SourceDefinition,
        url: str,
        *,
        redirect: bool = False,
    ) -> None:
        if not definition.enabled:
            raise SourceRegistryError("来源未启用，禁止联网")
        if (
            definition.access_method in {"https_api", "https_document"}
            and definition.live_access_review.status
            != LiveAccessReviewStatus.APPROVED
        ):
            raise SourceRegistryError("来源未获live access人工批准，禁止联网")
        if not definition.allows_url(url, redirect=redirect):
            stage = "redirect" if redirect else "initial request"
            raise SourceRegistryError(f"{stage} URL不在固定allowlist")

    @staticmethod
    def resolve_alias(
        registry: SourceRegistry,
        alias: str,
        *,
        scope: str,
    ) -> tuple[SourceDefinition, ...]:
        for entry in registry.aliases:
            if entry.alias == alias and scope in entry.scopes:
                return tuple(registry.definition(item) for item in entry.target_source_definition_ids)
        raise SourceRegistryError(f"未知来源alias: {alias}")

    @staticmethod
    def find_registered_definition_for_url(
        registry: SourceRegistry,
        url: str,
    ) -> SourceDefinition | None:
        for definition in registry.definitions:
            if SourceRegistryLoader.is_formal_evidence_definition_for_url(
                definition,
                url,
            ):
                return definition
        return None

    @staticmethod
    def is_approved_for_formal_evidence(definition: SourceDefinition) -> bool:
        """Keep historical definitions readable without granting evidence authority."""

        return (
            definition.enabled
            and definition.policy_status == SourcePolicyStatus.ENABLED
            and definition.access_method in {"https_api", "https_document"}
            and definition.live_access_review.status
            == LiveAccessReviewStatus.APPROVED
        )

    @staticmethod
    def is_formal_evidence_definition_for_url(
        definition: SourceDefinition,
        url: str,
    ) -> bool:
        return SourceRegistryLoader.is_approved_for_formal_evidence(
            definition
        ) and (
            definition.allows_url(url)
            or definition.allows_url(url, redirect=True)
        )


def create_source_candidate(
    registry: SourceRegistry | LoadedSourceRegistry,
    *,
    url: str,
    discovery_context: dict[str, Any],
    suggested_upstream_identity: str | None = None,
    discovered_at: datetime | None = None,
) -> SourceCandidate:
    source_registry = registry.registry if isinstance(registry, LoadedSourceRegistry) else registry
    if SourceRegistryLoader.find_registered_definition_for_url(source_registry, url):
        raise SourceRegistryError("URL已属于已批准来源，不应创建candidate")
    parsed = urlsplit(url)
    domain = (parsed.hostname or "").lower()
    candidate_id = stable_acquisition_id(
        "source-candidate",
        {
            "url": url,
            "context": discovery_context,
            "suggested_upstream_identity": suggested_upstream_identity,
        },
    )
    return SourceCandidate(
        candidate_id=candidate_id,
        candidate_url=url,
        candidate_domain=domain,
        discovered_at=discovered_at or datetime.now().astimezone(),
        discovery_context=discovery_context,
        suggested_upstream_identity=suggested_upstream_identity,
        status=SourceCandidateStatus.PENDING_REVIEW,
    )


def review_source_candidate(
    candidate: SourceCandidate,
    *,
    status: SourceCandidateStatus,
    reviewed_by: str,
    reviewed_at: datetime,
    decision_reason: str,
    approved_registry_version: str | None = None,
) -> SourceCandidate:
    if candidate.status != SourceCandidateStatus.PENDING_REVIEW:
        raise SourceRegistryError("candidate已经审核，不得原地再次裁决")
    if status == SourceCandidateStatus.PENDING_REVIEW:
        raise SourceRegistryError("审核结果必须是approved或rejected")
    return SourceCandidate(
        **candidate.model_dump(
            exclude={
                "status",
                "reviewed_at",
                "reviewed_by",
                "decision_reason",
                "approved_registry_version",
            }
        ),
        status=status,
        reviewed_at=reviewed_at,
        reviewed_by=reviewed_by,
        decision_reason=decision_reason,
        approved_registry_version=approved_registry_version,
    )


__all__ = [
    "BUSINESS_MODEL_V1_ADAPTER_KEYS",
    "BUSINESS_MODEL_V1_INITIAL_REGISTRY_VERSION",
    "BUSINESS_MODEL_V1_SOURCE_IDS",
    "DEFAULT_QUESTIONS_PATH",
    "DEFAULT_REGISTRY_PATH",
    "POLICY_APPROVED_REGISTRY_PATH",
    "REVIEWED_REGISTRY_PATH",
    "INITIAL_REGISTRY_PATH",
    "KNOWN_ADAPTER_KEYS",
    "LoadedQuestionSet",
    "LoadedSourceRegistry",
    "MOUTAI_IR_INITIAL_DEFINITION_VERSION",
    "SourceRegistryError",
    "SourceRegistryLoader",
    "create_source_candidate",
    "review_source_candidate",
]
