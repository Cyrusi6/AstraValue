from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from analysis.structured.models import RequirementPath, SupportStatus
from analysis.structured.registry import (
    DEFAULT_CONFIG_DIR,
    EXPECTED_BM_IDS,
    StructuredRegistryError,
    StructuredRegistryLoader,
    canonical_registry_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
LEGACY_QUESTIONS = ROOT / "config" / "data_sources" / "business_model_questions.v1.json"


def _copy_registry(tmp_path: Path) -> Path:
    target = tmp_path / "structured_data"
    shutil.copytree(DEFAULT_CONFIG_DIR, target)
    return target


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_rehashed(path: Path, payload: dict) -> None:
    payload["content_sha256"] = canonical_registry_sha256(payload)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_default_registry_loads_atomically_with_frozen_counts_and_hashes():
    bundle = StructuredRegistryLoader().load()

    assert len(bundle.datasets.datasets) == 55
    assert len(bundle.fields.fields) == bundle.fields.declared_field_positions == 2504
    assert len(bundle.peer_sets.peer_sets[0].companies) == 7
    assert len(bundle.schedules.dataset_schedules) == 55
    assert len(bundle.reading_rules.rules) == 12
    assert len(bundle.research_requirements.questions) == 54
    assert len(bundle.industry_profiles.profiles) == 11
    assert set(bundle.content_hashes) == {
        "datasets",
        "fields",
        "peer_sets",
        "schedules",
        "reading_rules",
        "research_requirements",
        "industry_profiles",
    }
    assert all(len(value) == 64 for value in bundle.content_hashes.values())


def test_duplicate_dataset_id_is_rejected(tmp_path: Path):
    config_dir = _copy_registry(tmp_path)
    path = config_dir / "datasets.v1.json"
    payload = _read(path)
    payload["datasets"].append(copy.deepcopy(payload["datasets"][0]))
    _write_rehashed(path, payload)

    with pytest.raises(StructuredRegistryError, match="dataset_id不得重复"):
        StructuredRegistryLoader(config_dir).load()


def test_wrong_declared_hash_is_rejected_before_cross_registry_validation(tmp_path: Path):
    config_dir = _copy_registry(tmp_path)
    path = config_dir / "peer_sets.v1.json"
    payload = _read(path)
    payload["peer_sets"][0]["selected_at"] = "2026-09-09"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(StructuredRegistryError, match="content_sha256不匹配"):
        StructuredRegistryLoader(config_dir).load()


def test_unknown_raw_field_reference_is_rejected(tmp_path: Path):
    config_dir = _copy_registry(tmp_path)
    path = config_dir / "research_requirements.v1.json"
    payload = _read(path)
    raw_path = next(
        path
        for requirement in payload["requirements"]
        for path in requirement["paths"]
        if path["kind"] == "raw_field"
    )
    raw_path["raw_name"] = "NEW_UNKNOWN_FIELD"
    _write_rehashed(path, payload)

    with pytest.raises(StructuredRegistryError, match="引用未知原字段"):
        StructuredRegistryLoader(config_dir).load()


def test_two_primary_routes_for_same_semantics_are_rejected(tmp_path: Path):
    config_dir = _copy_registry(tmp_path)
    path = config_dir / "fields.v1.json"
    payload = _read(path)
    duplicate = copy.deepcopy(payload["routes"][0])
    duplicate["route_id"] += ".duplicate"
    payload["routes"].append(duplicate)
    _write_rehashed(path, payload)

    with pytest.raises(StructuredRegistryError, match="必须且只能有一个主源"):
        StructuredRegistryLoader(config_dir).load()


def test_dynamic_unknown_field_is_preserved_but_never_formula_eligible():
    bundle = StructuredRegistryLoader().load()
    dynamic = bundle.dynamic_field(
        "balance_fields",
        "NEW_UPSTREAM_COLUMN",
        ("number",),
        nonempty=True,
    )

    assert dynamic.field_id == "balance_fields.NEW_UPSTREAM_COLUMN"
    assert dynamic.nature.value == "unclassified"
    assert dynamic.definition_status.value == "unknown"
    assert dynamic.unit_status.value == "unknown"
    assert dynamic.formula_eligible is False


def test_field_unclassified_count_is_exact_and_unknown_definitions_do_not_enter_formulas():
    bundle = StructuredRegistryLoader().load()
    unclassified = [item for item in bundle.fields.fields if item.nature.value == "unclassified"]
    assert len(unclassified) > 0
    assert not any(item.formula_eligible for item in unclassified)
    assert bundle.fields.classification_summary == {
        "total": 2504,
        "unclassified_nature": len(unclassified),
        "definition_unknown": sum(item.definition_status.value == "unknown" for item in bundle.fields.fields),
        "definition_candidate": sum(item.definition_status.value == "candidate" for item in bundle.fields.fields),
        "formula_eligible": sum(item.formula_eligible for item in bundle.fields.fields),
    }


def test_dataset_requests_use_production_placeholders_not_probe_values():
    bundle = StructuredRegistryLoader().load()
    probe_values = {"600519", "600519.SH", "SH600519", "sh.600519", "1.600519", "2026-06-30", "2025-12-31"}
    for dataset in bundle.datasets.datasets:
        executable = set(dataset.request.fixed_parameters.values()) | set(
            dataset.request.parameter_template.values()
        )
        assert not executable & probe_values
        if dataset.request.protocol in {"em_s", "em_w", "em_m"} and dataset.history_enumeration in {"complete_pagination", "on_demand_complete_pagination"}:
            assert dataset.pagination == "page_number"
    assert bundle.dataset("balance_fields").request.parameter_template["companyType"] == "{resolved_company_type}"
    assert bundle.dataset("segments").request.parameter_template["pageSize"] == "{bounded_page_size}"
    assert all(item.request.protocol != "em_q" for item in bundle.datasets.datasets)


def test_peer_set_has_exact_seven_codes_roles_and_unbounded_history_scope():
    peer_set = StructuredRegistryLoader().load().peer_sets.peer_sets[0]
    by_ticker = {item.canonical_ticker: item.role for item in peer_set.companies}
    assert by_ticker == {
        "600519.SH": "target",
        "000858.SZ": "core_peer",
        "000568.SZ": "core_peer",
        "600809.SH": "operating_peer",
        "002304.SZ": "operating_peer",
        "000596.SZ": "operating_peer",
        "603369.SH": "operating_peer",
    }
    assert peer_set.display_windows_do_not_limit_acquisition is True
    assert all(item.inclusion_basis for item in peer_set.companies)


def test_all_datasets_have_history_schedule_and_display_window_never_shortens_it():
    bundle = StructuredRegistryLoader().load()
    history = {item.dataset_id: item.history_mode for item in bundle.datasets.datasets}
    schedules = {item.dataset_id: item for item in bundle.schedules.dataset_schedules}

    assert schedules.keys() == history.keys()
    assert all(schedules[dataset_id].baseline_scope == mode for dataset_id, mode in history.items())
    assert bundle.schedules.analysis_default_complete_years == 5
    assert bundle.schedules.analysis_default_published_quarters == 12
    assert bundle.schedules.analysis_window_limits_acquisition is False
    assert bundle.dataset("market_cap").history_mode == "all_available_history"
    assert bundle.dataset("macro_cpi").history_mode == "on_demand_all_available_history"
    assert bundle.dataset("income_fields").history_mode == "all_available_history"


def test_research_registry_has_54_stable_questions_all_eight_steps_and_exact_bm_crosswalk():
    registry = StructuredRegistryLoader().load().research_requirements
    step_counts: dict[str, int] = {}
    for question in registry.questions:
        step_counts[question.step_id] = step_counts.get(question.step_id, 0) + 1
        assert question.required_requirement_ids
        assert question.output_slots
    assert step_counts == {"ES01": 10, "ES02": 10, "ES03": 6, "ES04": 6, "ES05": 6, "ES06": 5, "ES07": 6, "ES08": 5}
    assert tuple(registry.legacy_crosswalk.mappings[f"ES01.Q{i:02d}"] for i in range(1, 11)) == EXPECTED_BM_IDS
    assert registry.legacy_crosswalk.legacy_file_sha256 == hashlib.sha256(LEGACY_QUESTIONS.read_bytes()).hexdigest()


def test_requirement_cycle_is_rejected(tmp_path: Path):
    config_dir = _copy_registry(tmp_path)
    path = config_dir / "research_requirements.v1.json"
    payload = _read(path)
    first, second = payload["requirements"][:2]
    first["depends_on_requirement_ids"] = [second["requirement_id"]]
    second["depends_on_requirement_ids"] = [first["requirement_id"]]
    _write_rehashed(path, payload)

    with pytest.raises(StructuredRegistryError, match="循环就绪依赖"):
        StructuredRegistryLoader(config_dir).load()


def test_ambiguous_any_of_and_title_only_paths_are_rejected(tmp_path: Path):
    config_dir = _copy_registry(tmp_path)
    path = config_dir / "research_requirements.v1.json"
    payload = _read(path)
    requirement = payload["requirements"][0]
    other = copy.deepcopy(requirement["paths"][0])
    other["semantic_key"] = "different-economic-meaning"
    requirement["combination"] = "any_of"
    requirement["paths"].append(other)
    _write_rehashed(path, payload)

    with pytest.raises(StructuredRegistryError, match="any_of只允许"):
        StructuredRegistryLoader(config_dir).load()

    with pytest.raises(ValidationError, match="不得用标题代替"):
        RequirementPath(
            kind="title_only",
            support_status=SupportStatus.UNSUPPORTED_FREE_SOURCE,
            semantic_key="a title",
            period_semantics="NOW",
            scope_semantics="company",
            unit_semantics="n/a",
        )


def test_all_gaps_have_affected_inputs_and_next_paths():
    gaps = StructuredRegistryLoader().load().research_requirements.gaps
    assert {item.gap_id for item in gaps} == {f"GAP{i:02d}" for i in range(1, 13)}
    assert all(item.affected_inputs and item.next_paths and item.completion_evidence for item in gaps)


def test_calculation_routes_have_exact_field_or_explicit_nonfield_dependencies():
    routes = StructuredRegistryLoader().load().research_requirements.calculation_routes
    assert {item.route_id for item in routes} == {f"K{i:02d}" for i in range(1, 12)}
    assert all(item.input_refs for item in routes)
    assert all(
        ref.startswith(("f:", "rd:", "k:", "research:"))
        for route in routes
        for ref in route.input_refs
    )
    assert "f:balance_fields.SHORT_LOAN" in next(
        item for item in routes if item.route_id == "K05"
    ).input_refs


def test_industry_profiles_keep_conditions_separate_and_never_default_unknown_to_general():
    registry = StructuredRegistryLoader().load().industry_profiles
    profiles = {item.profile_id: item for item in registry.profiles}

    assert registry.unknown_industry_default_profile is None
    assert registry.classification_sources_are_not_research_profiles is True
    assert "50%" in registry.mixed_business_rule
    assert profiles["preprofit"].mode == "overlay"
    assert all(profile.preserve_unreplaced_steps for profile in profiles.values())

    technology_conditions = {item.condition_id for item in profiles["technology"].conditional_groups}
    insurance_conditions = {item.condition_id for item in profiles["insurance"].conditional_groups}
    utility_conditions = {item.condition_id for item in profiles["utility"].conditional_groups}
    assert technology_conditions == {"hardware", "software_subscription"}
    assert insurance_conditions == {"life", "property_casualty"}
    assert utility_conditions == {"electricity", "water", "gas"}

    bank_paths = {item.path.raw_name: item.path.support_status.value for item in profiles["bank"].inputs if item.path.kind == "raw_field"}
    assert bank_paths["NET_INTEREST_MARGIN"] == "candidate_mapping"
    assert bank_paths["NON_PERFORMING_LOAN"] == "candidate_mapping"
    nim_field = StructuredRegistryLoader().load().field("em_metrics", "NET_INTEREST_MARGIN")
    assert nim_field.nonempty_in_sample is False
    assert nim_field.definition_status.value == "candidate"
    assert nim_field.formula_eligible is False


def test_registry_models_are_frozen():
    bundle = StructuredRegistryLoader().load()
    with pytest.raises(ValidationError):
        bundle.datasets.datasets[0].history_mode = "snapshot_from_first_retrieval"
