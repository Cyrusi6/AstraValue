from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import EventRecord, aware_utc
from .registry import PROJECT_ROOT


DEFAULT_EVENT_TAXONOMY_PATH = PROJECT_ROOT / "config" / "event_taxonomy.json"


class EventTaxonomyError(RuntimeError):
    pass


class EventTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class EventClassification:
    event_type: str
    label: str
    subtype: str | None
    lifecycle_state: str
    score: int
    matched_patterns: tuple[str, ...]
    taxonomy_version: str
    report_steps: tuple[int, ...]


def load_event_taxonomy(path: Path | str = DEFAULT_EVENT_TAXONOMY_PATH) -> dict[str, Any]:
    taxonomy_path = Path(path)
    try:
        payload = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EventTaxonomyError(f"无法读取公告事件分类配置: {taxonomy_path}: {exc}") from exc
    _validate_taxonomy(payload)
    return payload


def classify_announcement(
    title: str,
    text: str = "",
    *,
    taxonomy: dict[str, Any] | None = None,
) -> EventClassification:
    taxonomy = taxonomy or load_event_taxonomy()
    normalized_title = _normalize_text(title)
    normalized_text = _normalize_text(text)
    candidates: list[tuple[int, int, str, dict[str, Any], list[str]]] = []
    for order, (event_type, spec) in enumerate(taxonomy["event_types"].items()):
        title_matches = _matched_patterns(spec.get("title_patterns", []), normalized_title)
        if not title_matches:
            continue
        body_matches = _matched_patterns(spec.get("body_patterns", []), normalized_text)
        excluded = _matched_patterns(
            spec.get("exclude_patterns", []),
            f"{normalized_title} {normalized_text}",
        )
        if excluded:
            continue
        priority = int(spec.get("priority", 0))
        score = len(title_matches) * 1000 + len(body_matches) * 10 + priority
        candidates.append(
            (score, -order, event_type, spec, [*title_matches, *body_matches])
        )
    if not candidates:
        unknown_type = str(taxonomy.get("unknown_event_type") or "other")
        return EventClassification(
            event_type=unknown_type,
            label="未分类公告",
            subtype=None,
            lifecycle_state="unclassified",
            score=0,
            matched_patterns=(),
            taxonomy_version=str(taxonomy["schema_version"]),
            report_steps=(),
        )

    score, _, event_type, spec, matched = max(candidates, key=lambda item: item[:2])
    combined_text = f"{normalized_title} {normalized_text}"
    subtype = _first_rule_value(spec.get("subtype_rules", []), "subtype", combined_text)
    lifecycle_state = _first_rule_value(
        spec.get("state_rules", []),
        "state",
        combined_text,
    ) or str(spec["default_state"])
    return EventClassification(
        event_type=event_type,
        label=str(spec["label"]),
        subtype=subtype,
        lifecycle_state=lifecycle_state,
        score=score,
        matched_patterns=tuple(dict.fromkeys(matched)),
        taxonomy_version=str(taxonomy["schema_version"]),
        report_steps=tuple(int(item) for item in spec.get("report_steps", [])),
    )


def validate_transition(
    event_type: str,
    current_state: str,
    next_state: str,
    *,
    taxonomy: dict[str, Any] | None = None,
    allow_same_state: bool = True,
) -> bool:
    taxonomy = taxonomy or load_event_taxonomy()
    try:
        spec = taxonomy["event_types"][event_type]
    except KeyError as exc:
        raise EventTransitionError(f"未知事件类型: {event_type}") from exc
    states = set(spec["states"])
    if current_state not in states:
        raise EventTransitionError(
            f"{event_type} 当前状态不在分类配置中: {current_state}"
        )
    if next_state not in states:
        raise EventTransitionError(
            f"{event_type} 目标状态不在分类配置中: {next_state}"
        )
    if allow_same_state and current_state == next_state:
        return True
    return next_state in set(spec["transitions"].get(current_state, []))


def link_event_update(
    previous: EventRecord,
    update: EventRecord,
    *,
    taxonomy: dict[str, Any] | None = None,
) -> EventRecord:
    if previous.ticker != update.ticker:
        raise EventTransitionError(
            f"事件股票代码不一致: {previous.ticker} != {update.ticker}"
        )
    if previous.event_type != update.event_type:
        raise EventTransitionError(
            f"事件类型不一致: {previous.event_type} != {update.event_type}"
        )
    if aware_utc(update.available_at) < aware_utc(previous.available_at):
        raise EventTransitionError("事件更新的可得时间早于前序事件")
    if not validate_transition(
        previous.event_type,
        previous.lifecycle_state,
        update.lifecycle_state,
        taxonomy=taxonomy,
    ):
        raise EventTransitionError(
            f"不允许的事件状态迁移: {previous.event_type} "
            f"{previous.lifecycle_state} -> {update.lifecycle_state}"
        )
    root_event_id = previous.root_event_id or previous.event_id
    payload = update.model_dump(mode="python")
    payload.update(
        {
            "previous_event_id": previous.event_id,
            "root_event_id": root_event_id,
            "status_updated_at": max(
                aware_utc(update.status_updated_at),
                aware_utc(update.available_at),
            ),
        }
    )
    return EventRecord.model_validate(payload)


def current_event_versions(events: list[EventRecord]) -> list[EventRecord]:
    latest: dict[str, EventRecord] = {}
    for event in events:
        root_event_id = event.root_event_id or event.event_id
        previous = latest.get(root_event_id)
        if previous is None or _event_rank(event) > _event_rank(previous):
            latest[root_event_id] = event
    return sorted(
        latest.values(),
        key=lambda item: (
            aware_utc(item.available_at),
            item.root_event_id or item.event_id,
        ),
        reverse=True,
    )


def _validate_taxonomy(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise EventTaxonomyError("公告事件分类配置必须是JSON对象")
    if not payload.get("schema_version"):
        raise EventTaxonomyError("公告事件分类配置缺少schema_version")
    event_types = payload.get("event_types")
    if not isinstance(event_types, dict) or not event_types:
        raise EventTaxonomyError("公告事件分类配置缺少event_types")
    for event_type, spec in event_types.items():
        if not isinstance(spec, dict):
            raise EventTaxonomyError(f"{event_type} 配置必须是对象")
        states = spec.get("states")
        transitions = spec.get("transitions")
        if not isinstance(states, list) or not states:
            raise EventTaxonomyError(f"{event_type} 缺少states")
        if len(set(states)) != len(states):
            raise EventTaxonomyError(f"{event_type} states存在重复值")
        if spec.get("default_state") not in states:
            raise EventTaxonomyError(f"{event_type} default_state不在states中")
        initial_states = spec.get("initial_states")
        if not isinstance(initial_states, list) or not set(initial_states) <= set(states):
            raise EventTaxonomyError(f"{event_type} initial_states非法")
        if not isinstance(transitions, dict):
            raise EventTaxonomyError(f"{event_type} 缺少transitions")
        unknown_sources = set(transitions) - set(states)
        if unknown_sources:
            raise EventTaxonomyError(
                f"{event_type} transitions包含未知起始状态: {sorted(unknown_sources)}"
            )
        for state in states:
            targets = transitions.get(state)
            if not isinstance(targets, list):
                raise EventTaxonomyError(f"{event_type}.{state} 缺少迁移列表")
            unknown_targets = set(targets) - set(states)
            if unknown_targets:
                raise EventTaxonomyError(
                    f"{event_type}.{state} 包含未知目标状态: {sorted(unknown_targets)}"
                )
        for pattern_key in ("title_patterns", "body_patterns", "exclude_patterns"):
            _validate_patterns(event_type, pattern_key, spec.get(pattern_key, []))
        for rules_key in ("subtype_rules", "state_rules"):
            rules = spec.get(rules_key, [])
            if not isinstance(rules, list):
                raise EventTaxonomyError(f"{event_type}.{rules_key}必须是列表")
            for rule in rules:
                if not isinstance(rule, dict) or not isinstance(rule.get("patterns"), list):
                    raise EventTaxonomyError(f"{event_type}.{rules_key}规则格式非法")
                _validate_patterns(event_type, rules_key, rule["patterns"])
        configured_states = {
            str(rule.get("state"))
            for rule in spec.get("state_rules", [])
            if rule.get("state")
        }
        if not configured_states <= set(states):
            raise EventTaxonomyError(
                f"{event_type} state_rules包含未知状态: "
                f"{sorted(configured_states - set(states))}"
            )


def _validate_patterns(event_type: str, key: str, patterns: list[str]) -> None:
    if not isinstance(patterns, list):
        raise EventTaxonomyError(f"{event_type}.{key}必须是列表")
    for pattern in patterns:
        try:
            re.compile(str(pattern), flags=re.IGNORECASE)
        except re.error as exc:
            raise EventTaxonomyError(
                f"{event_type}.{key}包含非法正则 {pattern}: {exc}"
            ) from exc


def _normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _matched_patterns(patterns: list[str], value: str) -> list[str]:
    return [
        str(pattern)
        for pattern in patterns
        if re.search(str(pattern), value, flags=re.IGNORECASE)
    ]


def _first_rule_value(
    rules: list[dict[str, Any]],
    value_key: str,
    text: str,
) -> str | None:
    for rule in rules:
        if _matched_patterns(rule.get("patterns", []), text):
            value = rule.get(value_key)
            return str(value) if value is not None else None
    return None


def _event_rank(event: EventRecord) -> tuple:
    return (
        aware_utc(event.available_at),
        aware_utc(event.status_updated_at),
        aware_utc(event.announced_at),
        event.event_id,
    )
