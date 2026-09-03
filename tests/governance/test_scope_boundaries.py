from __future__ import annotations

import inspect
from pathlib import Path

from pydantic import BaseModel

from analysis.governance import models
from analysis.governance.registry import load_question_registry


ROOT = Path(__file__).resolve().parents[2]


def test_governance_fact_schemas_exclude_scores_ratings_integrity_and_ability():
    forbidden = {
        "governance_score",
        "governance_total_score",
        "governance_rating",
        "management_integrity",
        "management_integrity_rating",
        "management_ability",
        "management_ability_rating",
    }
    offenders: dict[str, set[str]] = {}
    for name, candidate in inspect.getmembers(models, inspect.isclass):
        if candidate.__module__ != models.__name__:
            continue
        if not issubclass(candidate, BaseModel):
            continue
        fields = set(candidate.model_fields)
        overlap = fields & forbidden
        if overlap:
            offenders[name] = overlap
    assert offenders == {}


def test_governance_methodology_remains_an_explicit_skeleton():
    methodology = (
        ROOT / "docs" / "methodology" / "steps" / "03_governance.md"
    ).read_text(encoding="utf-8")
    assert "content_status: skeleton" in methodology
    assert "不代表方法已经完成" in methodology
    assert methodology.count("待补充") >= 9


def test_coverage_policy_never_infers_negative_facts():
    registry = load_question_registry(
        ROOT
        / "config"
        / "data_sources"
        / "governance_management_questions.v1.json"
    )
    assert registry.negative_fact_policy == "never_infer_from_coverage"
