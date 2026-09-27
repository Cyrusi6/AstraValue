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
        'title="公告与研究资料"', 1
    )[1].split('title="正式公告归档"', 1)[0]
    assert "按研究问题按需采集" in acquisition_section
    assert "sourceDefinitions" in acquisition_section
    assert 'type="checkbox"' not in acquisition_section
    assert "coverage_accounted" in acquisition_section
    assert "material_gap_count" in acquisition_section
    assert "/acquisition-runs" in source
    assert "/execute" in source


def test_manual_document_form_requires_registered_source_identity() -> None:
    source = APP.read_text(encoding="utf-8")
    document_section = source.split('title="正式公告归档"', 1)[1].split("function PanelTitle", 1)[0]
    assert "source_url" in document_section
    assert "source_definition_id" in document_section
    assert "source_definition_version" in document_section
    assert "不会扫描或下载全量报告" in document_section
    assert "!document.source_url" in document_section


def test_legacy_financial_scope_is_removed_from_frontend() -> None:
    source = APP.read_text(encoding="utf-8")
    assert 'title="Legacy 财务同步"' not in source
    assert "/companies/${ticker}/sync" not in source
    assert "providers" not in source


def test_structured_frontend_uses_plan_and_recovery_routes() -> None:
    source = APP.read_text(encoding="utf-8")
    assert 'api("/structured/plans"' in source
    assert "/structured/runs/${runId}/" in source
    assert "研究工作区" in source


def test_report_coverage_is_separate_from_legacy_evidence_score() -> None:
    source = APP.read_text(encoding="utf-8")
    coverage = source.split("function ResearchCoverage", 1)[1].split(
        "function ExportPanel", 1
    )[0]
    assert "ES01" in coverage and "ES08" in coverage
    assert "required_requirement_ids" in coverage
    assert "missing_requirement_ids" in coverage
    assert "optional_missing_ids" in coverage
    assert "analysis_scope" in coverage
    assert "evidence_completeness" not in coverage
    assert "不会由旧 evidence_scores 推定八步就绪" in coverage
