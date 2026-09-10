from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .registry import DEFAULT_CONFIG_DIR


@dataclass(frozen=True)
class ReportPolicy:
    positive_upside: float
    cautious_downside: float
    minimum_confidence: float
    method_spread_block_ratio: float
    rating_requires_user_confirmation: bool
    scenario_probability_automatic: bool
    report_section_count: int


@dataclass(frozen=True)
class SourcePolicy:
    schema_version: str
    default_strategy: str
    legacy_strategy: str
    default_relative_tolerance: float
    point_in_time_required: bool
    llm_may_fill_missing_values: bool
    priority: tuple[str, ...]
    critical_metrics: tuple[str, ...]
    official_source_types: tuple[str, ...]
    independent_source_types: tuple[str, ...]
    statuses: tuple[str, ...]
    supplier_direct_source_types: tuple[str, ...]


def _load(name: str, config_dir: Path | str = DEFAULT_CONFIG_DIR) -> dict[str, Any]:
    path = Path(config_dir) / name
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def load_report_policy(config_dir: str | None = None) -> ReportPolicy:
    data = _load("report_policies.json", config_dir or DEFAULT_CONFIG_DIR)
    thresholds = data["suggestion_thresholds"]
    return ReportPolicy(
        positive_upside=float(thresholds["positive_upside"]),
        cautious_downside=float(thresholds["cautious_downside"]),
        minimum_confidence=float(thresholds["minimum_confidence"]),
        method_spread_block_ratio=float(data["method_spread_block_ratio"]),
        rating_requires_user_confirmation=bool(data["rating_requires_user_confirmation"]),
        scenario_probability_automatic=bool(data["scenario_probability_automatic"]),
        report_section_count=int(data["report_section_count"]),
    )


@lru_cache(maxsize=4)
def load_source_policy(config_dir: str | None = None) -> SourcePolicy:
    data = _load("source_policy.json", config_dir or DEFAULT_CONFIG_DIR)
    return SourcePolicy(
        schema_version=str(data.get("schema_version", "1.0.0")),
        default_strategy=str(data.get("default_strategy", "legacy-v1")),
        legacy_strategy=str(data.get("legacy_strategy", "legacy-v1")),
        default_relative_tolerance=float(data["default_relative_tolerance"]),
        point_in_time_required=bool(data["point_in_time_required"]),
        llm_may_fill_missing_values=bool(data["llm_may_fill_missing_values"]),
        priority=tuple(data["priority"]),
        critical_metrics=tuple(data.get("critical_metrics", ())),
        official_source_types=tuple(data.get("official_source_types", ("official-document",))),
        independent_source_types=tuple(
            data.get(
                "independent_source_types",
                ("public", "public-adapter", "government", "industry-association"),
            )
        ),
        statuses=tuple(data.get("statuses", ())),
        supplier_direct_source_types=tuple(
            data.get("supplier_direct_source_types", ("supplier-structured",))
        ),
    )
