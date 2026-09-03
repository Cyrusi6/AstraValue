from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frontend" / "src" / "App.jsx"


def test_business_model_sources_are_loaded_from_api_not_hardcoded() -> None:
    source = APP.read_text(encoding="utf-8")
    assert '/source-definitions?scope=business_model' in source
    assert "sourceDefinitions.map" in source
    for source_definition_id in (
        "cninfo.disclosures",
        "sse.disclosures",
        "szse.disclosures",
        "moutai.ir",
    ):
        assert source_definition_id not in source


def test_business_model_plan_cannot_be_reduced_with_source_checkboxes() -> None:
    source = APP.read_text(encoding="utf-8")
    acquisition_section = source.split(
        'title="公司业务与商业模式资料采集"', 1
    )[1].split('title="Legacy 财务同步"', 1)[0]
    assert "来源与问题不可省略" in acquisition_section
    assert "sourceDefinitions" in acquisition_section
    assert 'type="checkbox"' not in acquisition_section
    assert "coverage_accounted" in acquisition_section
    assert "material_gap_count" in acquisition_section
    assert "/acquisition-runs" in source
    assert "/execute" in source


def test_legacy_financial_scope_is_visibly_separate() -> None:
    source = APP.read_text(encoding="utf-8")
    assert 'title="Legacy 财务同步"' in source
    assert "非 business_model v1" in source
