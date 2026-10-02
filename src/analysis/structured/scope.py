"""八步请求与消费范围。旧注册是接口能力证据，不是新任务默认范围。"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from functools import lru_cache
from pathlib import Path

from .storage import canonical_sha256

SCOPE_ID = "eight-step-scope-v1.0.0"
LITE_PROFILE_ID = "eight-step-lite-v1.0.0"
STANDARD_PROFILE_ID = "eight-step-standard-v1.1.0"
ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def load_scope():
    value = json.loads((ROOT / "config/structured_data/research_scope.v1.json").read_text(encoding="utf-8"))
    if value["scope_id"] != SCOPE_ID or canonical_sha256({k: v for k, v in value.items() if k != "content_sha256"}) != value["content_sha256"]:
        raise ValueError("research scope hash mismatch")
    return value


@lru_cache(maxsize=2)
def load_research_profile(profile_id: str):
    if profile_id in {STANDARD_PROFILE_ID, "eight-step-standard-v1.0.0"}:
        value = deepcopy(load_research_profile(LITE_PROFILE_ID))
        value.update(profile_id=profile_id, version=profile_id.rsplit("v",1)[1], authority="计划.md")
        value["windows"].update(complete_annual_years=5, required_quarters=12)
        value["include_latest_cumulative_and_ttm"] = True
        if profile_id == STANDARD_PROFILE_ID:
            value["include_balance_predecessor"] = True
        value["content_sha256"] = canonical_sha256({k: v for k, v in value.items() if k != "content_sha256"})
        return value
    if profile_id != LITE_PROFILE_ID:
        raise ValueError(f"unknown_research_profile:{profile_id}")
    path = ROOT / "config/structured_data/research_lite.v1.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    payload = {key: item for key, item in value.items() if key != "content_sha256"}
    if (
        value.get("profile_id") != profile_id
        or value.get("base_scope_id") != SCOPE_ID
        or canonical_sha256(payload) != value.get("content_sha256")
    ):
        raise ValueError("research profile hash mismatch")
    base = load_scope()["datasets"]
    for dataset_id, rule in value["datasets"].items():
        if dataset_id not in base or base[dataset_id]["selection"] == "excluded":
            raise ValueError(f"research_profile_excluded_dataset:{dataset_id}")
        unknown = set(rule["fields"]) - set(base[dataset_id]["request_fields"])
        if unknown:
            raise ValueError(
                f"research_profile_unknown_fields:{dataset_id}:" + ",".join(sorted(unknown))
            )
    routed = [
        question_id
        for route in ("core", "conditional", "deferred")
        for question_id in value["question_routing"][route]
    ]
    if len(routed) != 54 or len(set(routed)) != 54:
        raise ValueError("research_profile_question_routing_invalid")
    return value


def selected_datasets(requested=None, research_profile_id=None):
    rules = load_scope()["datasets"]
    if research_profile_id:
        profile = load_research_profile(research_profile_id)
        profile_rules = profile["datasets"]
        chosen = list(requested) if requested else [
            name for name, value in profile_rules.items() if value["selection"] == "required"
        ]
    else:
        profile_rules = None
        chosen = list(requested) if requested else [k for k, v in rules.items() if v["selection"] == "required"]
    for name in chosen:
        if name not in rules or rules[name]["selection"] == "excluded":
            raise ValueError(f"research_scope_excluded:{name}")
        if profile_rules is not None and name not in profile_rules:
            raise ValueError(f"research_profile_excluded:{name}")
    return tuple(chosen)


def scope_start(as_of: date, research_profile_id=None) -> date:
    if research_profile_id:
        profile = load_research_profile(research_profile_id)
        if research_profile_id in {STANDARD_PROFILE_ID, "eight-step-standard-v1.0.0"}:
            annual_year = as_of.year - (1 if as_of >= date(as_of.year, 4, 30) else 2)
            return date(annual_year - profile["windows"]["complete_annual_years"], 1, 1)
        # 3 displayed annual years plus one predecessor year for actual formulas.
        return date(as_of.year - 4, 1, 1)
    # Five complete years plus a predecessor year for average balances/TTM.
    return date(as_of.year - 6, 1, 1)


def profile_fields(profile_id, dataset_id):
    registry=json.loads((ROOT/'config/structured_data/industry_profiles.v1.json').read_text(encoding='utf8'))
    profile=next((p for p in registry['profiles'] if p['profile_id']==profile_id),None)
    if profile is None: raise ValueError('unknown_industry_profile')
    return sorted({i['path']['raw_name'] for i in profile['inputs'] if i['path'].get('dataset_id')==dataset_id and i['path'].get('raw_name')})


def request_fields(dataset_id, params, profile_id=None, *, research_profile_id=None):
    rule = load_scope()["datasets"][dataset_id]
    if rule["selection"] == "excluded":
        raise ValueError(f"research_scope_excluded:{dataset_id}")
    result = dict(params)
    fields = set(rule["request_fields"]) | set(profile_fields(profile_id,dataset_id) if profile_id else ())
    if profile_id and dataset_id == 'em_metrics':
        fields = (set(rule['request_fields'])-set(rule['consume_fields'])) | set(profile_fields(profile_id,dataset_id))
    if research_profile_id:
        lite_rule = load_research_profile(research_profile_id)["datasets"].get(dataset_id)
        if lite_rule is None:
            raise ValueError(f"research_profile_excluded:{dataset_id}")
        identity_fields = set(rule["request_fields"]) - set(rule["consume_fields"])
        fields = identity_fields | set(lite_rule["fields"])
        if profile_id:
            fields |= set(profile_fields(profile_id, dataset_id))
        fields &= set(rule["request_fields"])
    # Local provenance is never an upstream column, even if an old profile
    # or an industry overlay still includes it.
    fields.discard("__retrieved_at")
    fields = sorted(fields)
    for key in ("columns", "fields", "sty"):
        if key in result:
            if not fields:
                raise ValueError(f"research_scope_fields_missing:{dataset_id}")
            result[key] = ",".join(fields)
    return result


def field_selected(dataset_id, raw_name, research_profile_id=None):
    rule = load_scope()["datasets"].get(dataset_id)
    if not (rule and rule["selection"] != "excluded" and raw_name in rule["consume_fields"]):
        return False
    if not research_profile_id:
        return True
    lite_rule = load_research_profile(research_profile_id)["datasets"].get(dataset_id)
    return bool(lite_rule and raw_name in lite_rule["fields"])


def assert_network_scope(context, dataset_id):
    frozen = context.frozen_config.get("research_scope")
    if not frozen or frozen.get("content_sha256") != load_scope()["content_sha256"]:
        raise ValueError("legacy_scope_network_blocked:create_scoped_plan_from_gaps")
    profile_id = context.frozen_config.get("research_profile_id")
    if profile_id:
        profile = context.frozen_config.get("research_profile")
        if (
            not profile
            or profile.get("profile_id") != profile_id
            or profile.get("content_sha256")
            != load_research_profile(profile_id)["content_sha256"]
        ):
            raise ValueError("research_profile_network_binding_invalid")
    selected_datasets([dataset_id], profile_id)
