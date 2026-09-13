"""八步请求与消费范围。旧注册是接口能力证据，不是新任务默认范围。"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from .storage import canonical_sha256

SCOPE_ID = "eight-step-scope-v1.0.0"
ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def load_scope():
    value = json.loads((ROOT / "config/structured_data/research_scope.v1.json").read_text(encoding="utf-8"))
    if value["scope_id"] != SCOPE_ID or canonical_sha256({k: v for k, v in value.items() if k != "content_sha256"}) != value["content_sha256"]:
        raise ValueError("research scope hash mismatch")
    return value


def selected_datasets(requested=None):
    rules = load_scope()["datasets"]
    chosen = list(requested) if requested else [k for k, v in rules.items() if v["selection"] == "required"]
    for name in chosen:
        if name not in rules or rules[name]["selection"] == "excluded":
            raise ValueError(f"research_scope_excluded:{name}")
    return tuple(chosen)


def scope_start(as_of: date) -> date:
    # Five complete years plus a predecessor year for average balances/TTM.
    return date(as_of.year - 6, 1, 1)


def profile_fields(profile_id, dataset_id):
    registry=json.loads((ROOT/'config/structured_data/industry_profiles.v1.json').read_text(encoding='utf8'))
    profile=next((p for p in registry['profiles'] if p['profile_id']==profile_id),None)
    if profile is None: raise ValueError('unknown_industry_profile')
    return sorted({i['path']['raw_name'] for i in profile['inputs'] if i['path'].get('dataset_id')==dataset_id and i['path'].get('raw_name')})


def request_fields(dataset_id, params, profile_id=None):
    rule = load_scope()["datasets"][dataset_id]
    if rule["selection"] == "excluded":
        raise ValueError(f"research_scope_excluded:{dataset_id}")
    result = dict(params)
    fields = sorted(set(rule["request_fields"]) | set(profile_fields(profile_id,dataset_id) if profile_id else ()))
    if profile_id and dataset_id == 'em_metrics':
        fields = sorted((set(rule['request_fields'])-set(rule['consume_fields'])) | set(profile_fields(profile_id,dataset_id)))
    for key in ("columns", "fields", "sty"):
        if key in result:
            if not fields:
                raise ValueError(f"research_scope_fields_missing:{dataset_id}")
            result[key] = ",".join(fields)
    return result


def field_selected(dataset_id, raw_name):
    rule = load_scope()["datasets"].get(dataset_id)
    return bool(rule and rule["selection"] != "excluded" and raw_name in rule["consume_fields"])


def assert_network_scope(context, dataset_id):
    frozen = context.frozen_config.get("research_scope")
    if not frozen or frozen.get("content_sha256") != load_scope()["content_sha256"]:
        raise ValueError("legacy_scope_network_blocked:create_scoped_plan_from_gaps")
    selected_datasets([dataset_id])
