from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .models import (
    DatasetRegistry,
    DefinitionStatus,
    FieldNature,
    FieldRegistry,
    IndustryProfileRegistry,
    PeerSetRegistry,
    RawFieldDefinition,
    ReadingRuleRegistry,
    RegistryKind,
    ResearchRequirementRegistry,
    ScheduleRegistry,
    UnitStatus,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config" / "structured_data"
LEGACY_BUSINESS_QUESTIONS = (
    PROJECT_ROOT / "config" / "data_sources" / "business_model_questions.v1.json"
)

EXPECTED_DATASET_IDS = frozenset(
    {
        "balance_fields",
        "income_fields",
        "cashflow_fields",
        "income_quarter",
        "cashflow_quarter",
        "em_metrics",
        "company_basic",
        "segments",
        "customers_peer",
        "rd",
        "staff_pay",
        "staff_structure",
        "subsidiaries",
        "controller",
        "equity",
        "holders_history",
        "float_holders_history",
        "holder_count",
        "dividend",
        "repurchase",
        "guarantee",
        "litigation",
        "violation",
        "seo",
        "allotment",
        "bond_issuance",
        "goodwill",
        "pledge",
        "unlock_peer",
        "management_trades",
        "management_roster",
        "management_salary",
        "margin",
        "block_trade",
        "billboard",
        "institution_holds",
        "fund_holds",
        "surveys",
        "tags",
        "forecasts",
        "market_cap",
        "macro_cpi",
        "macro_retail",
        "capital_projects",
        "capital_raise",
        "baostock_adjust",
        "baostock_balance",
        "baostock_basic",
        "baostock_calendar",
        "baostock_cash_flow",
        "baostock_daily",
        "baostock_dupont",
        "baostock_growth",
        "baostock_operation",
        "baostock_profit",
    }
)
EXPECTED_STEP_COUNTS = {
    "ES01": 10,
    "ES02": 10,
    "ES03": 6,
    "ES04": 6,
    "ES05": 6,
    "ES06": 5,
    "ES07": 6,
    "ES08": 5,
}
EXPECTED_PEERS = {
    "600519.SH": "target",
    "000858.SZ": "core_peer",
    "000568.SZ": "core_peer",
    "600809.SH": "operating_peer",
    "002304.SZ": "operating_peer",
    "000596.SZ": "operating_peer",
    "603369.SH": "operating_peer",
}
EXPECTED_INDUSTRY_PROFILES = frozenset(
    {
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
    }
)
EXPECTED_BM_IDS = tuple(
    [
        "BM.Q01.IDENTITY_BUSINESS_MODEL",
        "BM.Q02.PRODUCT_REGION_ECONOMICS",
        "BM.Q03.COUNTERPARTY_CHANNEL",
        "BM.Q04.UNIT_ECONOMICS_PRICING",
        "BM.Q05.CAPACITY_OPERATIONS",
        "BM.Q06.CAPEX_CYCLE",
        "BM.Q07.RD_INPUT_EFFICIENCY",
        "BM.Q08.VALUE_CHAIN",
        "BM.Q09.STRATEGY_CHANGES",
        "BM.Q10.COMPETITIVE_CLAIMS_EVIDENCE",
    ]
)


class StructuredRegistryError(ValueError):
    """Raised before I/O when a structured-data registry is invalid."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredRegistryError(f"JSON包含重复键: {key}")
        result[key] = value
    return result


def canonical_registry_bytes(payload: dict[str, Any]) -> bytes:
    """Hash a registry without its declared digest to avoid self-reference."""

    value = dict(payload)
    value.pop("content_sha256", None)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StructuredRegistryError(f"注册表不能规范化为canonical JSON: {exc}") from exc


def canonical_registry_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_registry_bytes(payload)).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StructuredRegistryError(f"缺少结构化注册配置: {path}") from exc
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except StructuredRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredRegistryError(f"配置不是合法UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StructuredRegistryError(f"配置顶层必须是对象: {path}")
    return payload


T = TypeVar("T", bound=BaseModel)


def _load_model(
    path: Path,
    model_type: type[T],
    *,
    expected_kind: RegistryKind,
    expected_id: str,
) -> tuple[T, str]:
    payload = _read_json(path)
    declared = payload.get("content_sha256")
    actual = canonical_registry_sha256(payload)
    if declared != actual:
        raise StructuredRegistryError(
            f"{path.name}: content_sha256不匹配: declared={declared!r}, actual={actual}"
        )
    try:
        model = model_type.model_validate(payload)
    except ValidationError as exc:
        raise StructuredRegistryError(f"{path.name}: schema校验失败: {exc}") from exc
    if model.registry_kind != expected_kind or model.registry_id != expected_id:
        raise StructuredRegistryError(
            f"{path.name}: registry_kind/registry_id与文件合同不匹配"
        )
    return model, actual


@dataclass(frozen=True, slots=True)
class StructuredRegistryBundle:
    datasets: DatasetRegistry
    fields: FieldRegistry
    peer_sets: PeerSetRegistry
    schedules: ScheduleRegistry
    reading_rules: ReadingRuleRegistry
    research_requirements: ResearchRequirementRegistry
    industry_profiles: IndustryProfileRegistry
    content_hashes: dict[str, str]

    def dataset(self, dataset_id: str):
        for item in self.datasets.datasets:
            if item.dataset_id == dataset_id:
                return item
        raise KeyError(dataset_id)

    def field(self, dataset_id: str, raw_name: str):
        for item in self.fields.fields:
            if item.dataset_id == dataset_id and item.raw_name == raw_name:
                return item
        raise KeyError((dataset_id, raw_name))

    def requirements_for_question(self, question_id: str):
        return tuple(
            item
            for item in self.research_requirements.requirements
            if item.question_id == question_id
        )

    def industry_inputs(self, profile_ids: tuple[str, ...]) -> tuple[Any, ...]:
        requested = set(profile_ids)
        unknown = requested - {item.profile_id for item in self.industry_profiles.profiles}
        if unknown:
            raise KeyError(sorted(unknown))
        return tuple(
            item
            for profile in self.industry_profiles.profiles
            if profile.profile_id in requested
            for item in profile.inputs
        )

    def dynamic_field(
        self,
        dataset_id: str,
        raw_name: str,
        observed_json_types: tuple[str, ...],
        *,
        nonempty: bool,
    ) -> RawFieldDefinition:
        dataset = self.dataset(dataset_id)
        try:
            return self.field(dataset_id, raw_name)
        except KeyError:
            policy = self.fields.dynamic_field_policy
            return RawFieldDefinition(
                field_id=f"{dataset_id}.{raw_name}",
                dataset_id=dataset_id,
                plan_group_id=dataset.plan_group_id,
                raw_name=raw_name,
                observed_json_types=observed_json_types,
                nonempty_in_sample=nonempty,
                nature=FieldNature(policy.default_nature),
                definition_status=DefinitionStatus(policy.default_definition_status),
                unit_status=UnitStatus.UNKNOWN,
                period_semantics="unknown",
                scope_semantics="preserved from a new upstream response pending classification",
                formula_eligible=policy.formula_eligible,
                classification_basis="dynamic field policy",
            )


class StructuredRegistryLoader:
    """Atomically load and cross-check all seven structured registry objects."""

    def __init__(self, config_dir: Path | str = DEFAULT_CONFIG_DIR) -> None:
        self.config_dir = Path(config_dir)

    def load(self) -> StructuredRegistryBundle:
        specs: tuple[tuple[str, type[BaseModel], RegistryKind, str], ...] = (
            ("datasets.v1.json", DatasetRegistry, RegistryKind.DATASETS, "structured_data_datasets"),
            ("fields.v1.json", FieldRegistry, RegistryKind.FIELDS, "structured_data_fields"),
            ("peer_sets.v1.json", PeerSetRegistry, RegistryKind.PEER_SETS, "structured_data_peer_sets"),
            ("schedules.v1.json", ScheduleRegistry, RegistryKind.SCHEDULES, "structured_data_schedules"),
            ("reading_rules.v1.json", ReadingRuleRegistry, RegistryKind.READING_RULES, "structured_data_reading_rules"),
            (
                "research_requirements.v1.json",
                ResearchRequirementRegistry,
                RegistryKind.RESEARCH_REQUIREMENTS,
                "eight_step_research_requirements",
            ),
            (
                "industry_profiles.v1.json",
                IndustryProfileRegistry,
                RegistryKind.INDUSTRY_PROFILES,
                "structured_data_industry_profiles",
            ),
        )
        loaded: dict[str, BaseModel] = {}
        hashes: dict[str, str] = {}
        for filename, model_type, kind, registry_id in specs:
            model, digest = _load_model(
                self.config_dir / filename,
                model_type,
                expected_kind=kind,
                expected_id=registry_id,
            )
            loaded[kind.value] = model
            hashes[kind.value] = digest

        bundle = StructuredRegistryBundle(
            datasets=loaded[RegistryKind.DATASETS.value],  # type: ignore[arg-type]
            fields=loaded[RegistryKind.FIELDS.value],  # type: ignore[arg-type]
            peer_sets=loaded[RegistryKind.PEER_SETS.value],  # type: ignore[arg-type]
            schedules=loaded[RegistryKind.SCHEDULES.value],  # type: ignore[arg-type]
            reading_rules=loaded[RegistryKind.READING_RULES.value],  # type: ignore[arg-type]
            research_requirements=loaded[RegistryKind.RESEARCH_REQUIREMENTS.value],  # type: ignore[arg-type]
            industry_profiles=loaded[RegistryKind.INDUSTRY_PROFILES.value],  # type: ignore[arg-type]
            content_hashes=hashes,
        )
        self._validate_bundle(bundle)
        return bundle

    @staticmethod
    def _validate_bundle(bundle: StructuredRegistryBundle) -> None:
        StructuredRegistryLoader._validate_datasets_and_fields(bundle)
        StructuredRegistryLoader._validate_peer_scope(bundle)
        StructuredRegistryLoader._validate_schedules(bundle)
        StructuredRegistryLoader._validate_reading_rules(bundle)
        StructuredRegistryLoader._validate_research_requirements(bundle)
        StructuredRegistryLoader._validate_industry_profiles(bundle)

    @staticmethod
    def _validate_datasets_and_fields(bundle: StructuredRegistryBundle) -> None:
        datasets = bundle.datasets.datasets
        dataset_ids = [item.dataset_id for item in datasets]
        _require_unique(dataset_ids, "dataset_id")
        if set(dataset_ids) != EXPECTED_DATASET_IDS:
            raise StructuredRegistryError(
                "55个数据集ID与实测附录不一致: "
                f"missing={sorted(EXPECTED_DATASET_IDS - set(dataset_ids))}, "
                f"unknown={sorted(set(dataset_ids) - EXPECTED_DATASET_IDS)}"
            )
        if len(dataset_ids) != 55:
            raise StructuredRegistryError(f"数据集数量必须为55，当前为{len(dataset_ids)}")
        for dataset in datasets:
            if dataset.provider != dataset.upstream_identity:
                raise StructuredRegistryError(f"{dataset.dataset_id}: 真实上游身份不匹配")
            synthetic_keys = [
                name for name in dataset.primary_key_fields if name.startswith("__")
            ]
            if synthetic_keys:
                raise StructuredRegistryError(
                    f"{dataset.dataset_id}: primary_key_fields不得包含合成字段: {synthetic_keys}"
                )
            if any(len(value) != 64 for value in dataset.sample_evidence_hashes):
                raise StructuredRegistryError(f"{dataset.dataset_id}: 样本证据hash非法")
            probe_literals = {
                "600519",
                "600519.SH",
                "SH600519",
                "sh.600519",
                "1.600519",
                "2026-06-30",
                "2025-12-31",
            }
            executable_values = {
                str(value)
                for value in (
                    *dataset.request.fixed_parameters.values(),
                    *dataset.request.parameter_template.values(),
                )
            }
            if executable_values & probe_literals:
                raise StructuredRegistryError(
                    f"{dataset.dataset_id}: 生产请求不得固化两行/单公司探针参数"
                )
            if dataset.pagination == "page_number":
                required_page_parameters = (
                    {"p", "ps"}
                    if dataset.request.protocol == "em_m"
                    else {"pageNumber", "pageSize"}
                )
                if not required_page_parameters <= set(dataset.request.allowed_parameters):
                    raise StructuredRegistryError(f"{dataset.dataset_id}: 分页参数合同不完整")

        fields = bundle.fields.fields
        if bundle.fields.declared_field_positions != len(fields) or len(fields) != 2504:
            raise StructuredRegistryError(
                "字段位置必须完整登记2504项: "
                f"declared={bundle.fields.declared_field_positions}, actual={len(fields)}"
            )
        expected_summary = {
            "total": len(fields),
            "unclassified_nature": sum(
                item.nature == FieldNature.UNCLASSIFIED for item in fields
            ),
            "definition_unknown": sum(
                item.definition_status == DefinitionStatus.UNKNOWN for item in fields
            ),
            "definition_candidate": sum(
                item.definition_status == DefinitionStatus.CANDIDATE for item in fields
            ),
            "formula_eligible": sum(item.formula_eligible for item in fields),
        }
        if bundle.fields.classification_summary != expected_summary:
            raise StructuredRegistryError(
                "fields classification_summary与实际字段分类统计不一致"
            )
        _require_unique([item.field_id for item in fields], "field_id")
        field_keys = [(item.dataset_id, item.raw_name) for item in fields]
        _require_unique(field_keys, "dataset/raw field")
        field_key_set = set(field_keys)
        by_dataset: dict[str, int] = {}
        for field in fields:
            if field.dataset_id not in EXPECTED_DATASET_IDS:
                raise StructuredRegistryError(f"字段引用未知数据集: {field.field_id}")
            by_dataset[field.dataset_id] = by_dataset.get(field.dataset_id, 0) + 1
        for dataset in datasets:
            if by_dataset.get(dataset.dataset_id, 0) != dataset.expected_field_count:
                raise StructuredRegistryError(
                    f"{dataset.dataset_id}: 字段计数与数据集合同不一致"
                )

        route_ids = [item.route_id for item in bundle.fields.routes]
        _require_unique(route_ids, "route_id")
        routes_by_key: dict[tuple[str, str, str, str, str], list[Any]] = {}
        for route in bundle.fields.routes:
            if (route.dataset_id, route.raw_name) not in field_key_set:
                raise StructuredRegistryError(f"{route.route_id}: 路由引用未知原字段")
            routes_by_key.setdefault(route.route_key, []).append(route)
        for key, routes in routes_by_key.items():
            primary_count = sum(route.role == "primary" for route in routes)
            if primary_count != 1:
                raise StructuredRegistryError(f"字段路由{key}必须且只能有一个主源")

    @staticmethod
    def _validate_peer_scope(bundle: StructuredRegistryBundle) -> None:
        peer_sets = bundle.peer_sets.peer_sets
        if len(peer_sets) != 1:
            raise StructuredRegistryError("v1必须且只能有一个首批同行集合")
        companies = peer_sets[0].companies
        tickers = [item.canonical_ticker for item in companies]
        _require_unique(tickers, "同行证券代码")
        actual = {item.canonical_ticker: item.role for item in companies}
        if actual != EXPECTED_PEERS:
            raise StructuredRegistryError("首批七家公司代码或角色与冻结附录不一致")

    @staticmethod
    def _validate_schedules(bundle: StructuredRegistryBundle) -> None:
        schedules = bundle.schedules
        if schedules.source_min_interval_seconds.get("eastmoney", 0) < 3:
            raise StructuredRegistryError("东方财富跨数据集限速不得小于3秒")
        if schedules.source_min_interval_seconds.get("baostock", 0) < 3:
            raise StructuredRegistryError("BaoStock跨进程限速不得小于3秒")
        if schedules.source_min_interval_seconds.get("cninfo", 0) < 5:
            raise StructuredRegistryError("巨潮现有直连限速不得小于5秒")
        schedule_ids = [item.dataset_id for item in schedules.dataset_schedules]
        _require_unique(schedule_ids, "dataset schedule")
        if set(schedule_ids) != EXPECTED_DATASET_IDS:
            raise StructuredRegistryError("每个数据集必须且只能有一条baseline/增量调度")
        history_by_dataset = {
            item.dataset_id: item.history_mode for item in bundle.datasets.datasets
        }
        for item in schedules.dataset_schedules:
            if item.baseline_scope != history_by_dataset[item.dataset_id]:
                raise StructuredRegistryError(f"{item.dataset_id}: 调度缩短了数据集历史范围")

    @staticmethod
    def _validate_reading_rules(bundle: StructuredRegistryBundle) -> None:
        ids = [item.rule_id for item in bundle.reading_rules.rules]
        _require_unique(ids, "reading rule")
        expected = {f"R{ordinal:02d}" for ordinal in range(1, 13)}
        if set(ids) != expected:
            raise StructuredRegistryError("reading_rules必须完整包含R01至R12")

    @staticmethod
    def _validate_research_requirements(bundle: StructuredRegistryBundle) -> None:
        registry = bundle.research_requirements
        questions = registry.questions
        question_ids = [item.question_id for item in questions]
        _require_unique(question_ids, "question_id")
        if len(question_ids) != 54:
            raise StructuredRegistryError(f"八步问题数必须为54，当前为{len(question_ids)}")
        actual_counts: dict[str, int] = {}
        for question in questions:
            actual_counts[question.step_id] = actual_counts.get(question.step_id, 0) + 1
            if not question.question_id.startswith(f"{question.step_id}."):
                raise StructuredRegistryError(f"{question.question_id}: step_id不匹配")
        if actual_counts != EXPECTED_STEP_COUNTS:
            raise StructuredRegistryError(f"八步问题分布不完整: {actual_counts}")

        requirements = registry.requirements
        requirement_ids = [item.requirement_id for item in requirements]
        _require_unique(requirement_ids, "requirement_id")
        requirement_by_id = {item.requirement_id: item for item in requirements}
        question_id_set = set(question_ids)
        for requirement in requirements:
            if requirement.question_id not in question_id_set:
                raise StructuredRegistryError(
                    f"{requirement.requirement_id}: 引用未知question_id"
                )
        for question in questions:
            for requirement_id in question.required_requirement_ids:
                requirement = requirement_by_id.get(requirement_id)
                if requirement is None or requirement.question_id != question.question_id:
                    raise StructuredRegistryError(
                        f"{question.question_id}: 引用未知或跨题必需需求{requirement_id}"
                    )
                if requirement.requiredness != "required":
                    raise StructuredRegistryError(f"{requirement_id}: 必需列表引用可选项")
            for requirement_id in question.optional_requirement_ids:
                requirement = requirement_by_id.get(requirement_id)
                if requirement is None or requirement.question_id != question.question_id:
                    raise StructuredRegistryError(
                        f"{question.question_id}: 引用未知或跨题可选需求{requirement_id}"
                    )
                if requirement.requiredness != "optional":
                    raise StructuredRegistryError(f"{requirement_id}: 可选列表引用必需项")

        field_by_key = {
            (item.dataset_id, item.raw_name): item for item in bundle.fields.fields
        }
        dataset_fields = set(field_by_key)
        dataset_ids = {item.dataset_id for item in bundle.datasets.datasets}
        reading_ids = {item.route_id for item in registry.reading_routes}
        calculation_ids = {item.route_id for item in registry.calculation_routes}
        gap_ids = {item.gap_id for item in registry.gaps}
        if reading_ids != {f"RD{ordinal:02d}" for ordinal in range(1, 13)}:
            raise StructuredRegistryError("research requirements必须完整登记RD01至RD12")
        if calculation_ids != {f"K{ordinal:02d}" for ordinal in range(1, 12)}:
            raise StructuredRegistryError("research requirements必须完整登记K01至K11")
        if gap_ids != {f"GAP{ordinal:02d}" for ordinal in range(1, 13)}:
            raise StructuredRegistryError("research requirements必须完整登记GAP01至GAP12")
        for route in registry.calculation_routes:
            for input_ref in route.input_refs:
                prefix, separator, value = input_ref.partition(":")
                if not separator:
                    raise StructuredRegistryError(
                        f"{route.route_id}: 计算输入必须是精确类型引用"
                    )
                if prefix == "f":
                    dataset_id, dot, raw_name = value.partition(".")
                    if not dot or (dataset_id, raw_name) not in dataset_fields:
                        raise StructuredRegistryError(
                            f"{route.route_id}: 计算引用未知原字段{value}"
                        )
                elif prefix == "rd" and value not in reading_ids:
                    raise StructuredRegistryError(
                        f"{route.route_id}: 计算引用未知RD路由{value}"
                    )
                elif prefix == "k" and value not in calculation_ids:
                    raise StructuredRegistryError(
                        f"{route.route_id}: 计算引用未知K路由{value}"
                    )
                elif prefix not in {"f", "rd", "k", "research"}:
                    raise StructuredRegistryError(
                        f"{route.route_id}: 计算输入引用类型未知{prefix}"
                    )
        for requirement in requirements:
            for path in requirement.paths:
                if path.kind == "raw_field" and (path.dataset_id, path.raw_name) not in dataset_fields:
                    raise StructuredRegistryError(
                        f"{requirement.requirement_id}: 引用未知原字段 "
                        f"{path.dataset_id}.{path.raw_name}"
                    )
                if path.kind == "raw_field":
                    field = field_by_key[(path.dataset_id, path.raw_name)]
                    if (
                        path.support_status.value == "supported"
                        and field.definition_status != DefinitionStatus.CONFIRMED
                    ):
                        raise StructuredRegistryError(
                            f"{requirement.requirement_id}: 未确认原字段不得标为supported"
                        )
                if path.kind == "record_set" and path.dataset_id not in dataset_ids:
                    raise StructuredRegistryError(
                        f"{requirement.requirement_id}: 引用未知记录数据集{path.dataset_id}"
                    )
                if path.kind == "reading_section" and path.route_id not in reading_ids:
                    raise StructuredRegistryError(
                        f"{requirement.requirement_id}: 引用未知RD路由{path.route_id}"
                    )
                if path.kind == "calculation" and path.route_id not in calculation_ids:
                    raise StructuredRegistryError(
                        f"{requirement.requirement_id}: 引用未知K路由{path.route_id}"
                    )
                if path.kind == "gap" and path.gap_id not in gap_ids:
                    raise StructuredRegistryError(
                        f"{requirement.requirement_id}: 引用未知缺口{path.gap_id}"
                    )
            for dependency in requirement.depends_on_requirement_ids:
                if dependency not in requirement_by_id:
                    raise StructuredRegistryError(
                        f"{requirement.requirement_id}: 引用未知需求依赖{dependency}"
                    )
        _reject_cycles(
            {
                item.requirement_id: item.depends_on_requirement_ids
                for item in requirements
            },
            "requirement",
        )
        _reject_cycles(
            {item.route_id: item.depends_on_routes for item in registry.calculation_routes},
            "calculation route",
        )

        crosswalk = registry.legacy_crosswalk
        expected_es = {f"ES01.Q{ordinal:02d}" for ordinal in range(1, 11)}
        if set(crosswalk.mappings) != expected_es or tuple(
            crosswalk.mappings[f"ES01.Q{ordinal:02d}"] for ordinal in range(1, 11)
        ) != EXPECTED_BM_IDS:
            raise StructuredRegistryError("ES01与旧BM十主题crosswalk不完整或不稳定")
        if LEGACY_BUSINESS_QUESTIONS.exists():
            actual_legacy_hash = hashlib.sha256(LEGACY_BUSINESS_QUESTIONS.read_bytes()).hexdigest()
            if actual_legacy_hash != crosswalk.legacy_file_sha256:
                raise StructuredRegistryError("旧BM问题文件已变化，禁止静默改写crosswalk基线")

    @staticmethod
    def _validate_industry_profiles(bundle: StructuredRegistryBundle) -> None:
        profiles = bundle.industry_profiles.profiles
        ids = [item.profile_id for item in profiles]
        _require_unique(ids, "industry profile")
        if set(ids) != EXPECTED_INDUSTRY_PROFILES:
            raise StructuredRegistryError("行业画像必须精确包含既有11类key")
        question_ids = {
            item.question_id for item in bundle.research_requirements.questions
        }
        dataset_fields = {
            (item.dataset_id, item.raw_name) for item in bundle.fields.fields
        }
        dataset_ids = {item.dataset_id for item in bundle.datasets.datasets}
        reading_ids = {
            item.route_id for item in bundle.research_requirements.reading_routes
        }
        gap_ids = {item.gap_id for item in bundle.research_requirements.gaps}
        for profile in profiles:
            input_ids = [item.input_id for item in profile.inputs]
            _require_unique(input_ids, f"{profile.profile_id} input_id")
            input_id_set = set(input_ids)
            for item in profile.inputs:
                if not set(item.affected_question_ids) <= question_ids:
                    raise StructuredRegistryError(
                        f"{item.input_id}: 引用未知八步问题"
                    )
                path = item.path
                if path.kind == "raw_field" and (path.dataset_id, path.raw_name) not in dataset_fields:
                    raise StructuredRegistryError(f"{item.input_id}: 引用未知行业原字段")
                if path.kind == "record_set" and path.dataset_id not in dataset_ids:
                    raise StructuredRegistryError(f"{item.input_id}: 引用未知行业记录集")
                if path.kind == "reading_section" and path.route_id not in reading_ids:
                    raise StructuredRegistryError(f"{item.input_id}: 引用未知行业RD路由")
                if path.kind == "gap" and path.gap_id not in gap_ids:
                    raise StructuredRegistryError(f"{item.input_id}: 引用未知行业缺口")
            for group in profile.conditional_groups:
                if not set(group.input_ids) <= input_id_set:
                    raise StructuredRegistryError(
                        f"{profile.profile_id}/{group.condition_id}: 条件组引用未知input_id"
                    )
        preprofit = next(item for item in profiles if item.profile_id == "preprofit")
        if preprofit.mode != "overlay":
            raise StructuredRegistryError("尚未盈利必须是叠加画像，不得删除原行业需求")


def _require_unique(values: list[Any], label: str) -> None:
    seen: set[Any] = set()
    duplicate: set[Any] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    if duplicate:
        raise StructuredRegistryError(f"{label}不得重复: {sorted(duplicate)}")


def _reject_cycles(graph: dict[str, tuple[str, ...]], label: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise StructuredRegistryError(f"{label}存在循环就绪依赖: {node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, ()):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
