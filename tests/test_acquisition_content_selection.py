from dataclasses import replace
import json

import pytest

from analysis.acquisition.content_selection import (
    AUDIT_EXCLUSION_REASON, NO_STANDALONE_AUDIT_PDF_V1, apply_content_selection,
    is_standalone_audit_pdf,
    NO_AUDIT_ENGLISH_ANNUAL_V1, ENGLISH_ANNUAL_EXCLUSION_REASON, is_english_annual_report,
)
from analysis.acquisition.discovery import NormalizedResource
from analysis.acquisition.manifests import EvidenceGateError
from analysis.acquisition.models import EvidenceManifestExclusion
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH, INITIAL_REGISTRY_PATH
from orchestrator_support import ScenarioAdapter, discovery_result, envelope, make_runtime, resource, targeted_plan


@pytest.mark.parametrize("title", [
    "贵州茅台审计报告", "贵州茅台2024年度审计报告及财务报表",
    "2024年度内部控制审计报告", "贵州茅台：审计报告（含财务报表及附注）",
    "Independent Auditor's Report and Financial Statements",
])
def test_standalone_audit_pdf_is_selected_including_financial_statements(title):
    assert is_standalone_audit_pdf(title, "https://example.test/a.PDF", ("application/pdf",))


@pytest.mark.parametrize("title", [
    "贵州茅台2024年年度报告", "2024年年度报告（含审计报告）", "招股说明书附录（审计报告）",
    "首次公开发行招股说明书", "上市公告书", "关于审计报告的公告",
    "董事会决议暨股东大会通知", "2024年度审计委员会履职报告", "审计报告专项说明",
])
def test_full_reports_ipo_and_audit_references_are_retained(title):
    assert not is_standalone_audit_pdf(title, "https://example.test/a.pdf", ("application/pdf",))


@pytest.mark.parametrize("url,mimes", [
    ("a.pdf", ()), ("a.pdf", ("text/html",)),
    ("a.html", ("text/html",)), ("a.html", ("application/pdf", "text/html")),
])
def test_pdf_url_does_not_override_a_non_pdf_or_ambiguous_contract(url, mimes):
    assert not is_standalone_audit_pdf("审计报告", "https://example.test/" + url, mimes)


def test_selection_only_downgrades_new_required_fetch_and_is_idempotent():
    row = NormalizedResource("a", "https://example.test/a.pdf", "审计报告", "Asia/Shanghai", True,
        expected_mime_types=("application/pdf",), metadata={"upstream": "unchanged"})
    assert apply_content_selection(row, None) is row
    selected = apply_content_selection(row, NO_STANDALONE_AUDIT_PDF_V1)
    assert not selected.required_fetch and row.required_fetch
    assert selected.metadata["upstream"] == "unchanged"
    assert apply_content_selection(selected, NO_STANDALONE_AUDIT_PDF_V1) is selected
    metadata_only = replace(row, required_fetch=False)
    assert apply_content_selection(metadata_only, NO_STANDALONE_AUDIT_PDF_V1) is metadata_only
    with pytest.raises(ValueError, match="unknown_content_selection_policy"):
        apply_content_selection(row, "unknown")


def _fixture_registry(tmp_path, retained, policy=NO_STANDALONE_AUDIT_PDF_V1):
    payload = json.loads(INITIAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    definition = next(d for d in payload["definitions"] if d["source_definition_id"] == "cninfo.disclosures")
    definition["content_selection_policy"] = policy
    if retained:
        definition["retention_policy"]["discovery_body"] = "retain"
        for query in definition["queries"]:
            query["discovery_body_policy"] = "retain"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _adapter(*, include_report=False, excluded_title="2024年度审计报告及财务报表"):
    def parsed(_, work):
        item = replace(resource(f"page-{work.page}"), title=(
            "2024年年度报告" if include_report and work.page == 2 else excluded_title))
        return discovery_result(work, resources=(item,), declared_total=2,
            page_count=2, terminal=work.page == 2)
    class Adapter(ScenarioAdapter):
        def parse_retained_discovery(self, snapshot_id, work):
            return parsed(None, work)
    return Adapter(lambda w: envelope(url=w.url), parsed,
        lambda w: envelope(url=w.resource.url, body=b"%PDF-1.4\nfixture", content_type="application/pdf"))


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("include_report", [False, True])
def test_complete_discovery_pages_preserve_rows_and_skip_only_audit_fetch(tmp_path, retained, include_report, monkeypatch):
    adapter = _adapter(include_report=include_report)
    runtime = make_runtime(tmp_path / "runtime", adapter, registry_path=_fixture_registry(tmp_path, retained))
    plan = targeted_plan(runtime)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.material_gap_count == 0 and result.outcome_counts.get("no_data", 0) == 0
    assert [w.page for w in adapter.query_calls] == [1, 2]
    assert len(adapter.fetch_calls) == int(include_report)
    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id, limit=None)
    proofs = [p for a in attempts for p in runtime.repository.list_discovery_proofs(a.attempt_id)]
    assert len(proofs) == 2 and all(p.normalized_row_count == 1 and not p.proves_no_data for p in proofs)
    rows = [r for p in proofs for r in runtime.repository.list_discovered_resources(p.observation_id)]
    assert len(rows) == 2 and sum(r.required_fetch for r in rows) == int(include_report)
    assert sum(a.attempt_kind.value == "fetch" for a in attempts) == int(include_report)
    manifest = runtime.repository.get_evidence_manifest(result.manifest_id)
    selected = [e for e in manifest.exclusions if e.object_type == "resource"]
    assert len(selected) == 2 - int(include_report)
    assert manifest.coverage_summary["content_selection_exclusions"] == {AUDIT_EXCLUSION_REASON: len(selected)}
    service = runtime.manifest_service
    assert service._validate_resource_exclusions(selected * 2, plan.run.run_id) == tuple(sorted(selected, key=lambda e: e.object_id))
    with pytest.raises(EvidenceGateError, match="正文排除"):
        service._validate_resource_exclusions(selected, "another-run")
    with pytest.raises(EvidenceGateError, match="正文排除"):
        service._validate_resource_exclusions([EvidenceManifestExclusion(
            object_type="resource", object_id="missing-resource", reason_code=AUDIT_EXCLUSION_REASON)], plan.run.run_id)
    # Re-consumption validates exclusions even when this fixture's content gate is closed.
    with pytest.raises(EvidenceGateError, match="仅供部分审计"):
        service.validate_evidence_manifest(manifest)
    original_get = runtime.repository.get_source_definition_version
    with monkeypatch.context() as patch:
        patch.setattr(runtime.repository, "get_source_definition_version", lambda *args:
            original_get(*args).model_copy(update={"content_selection_policy": None}))
        with pytest.raises(EvidenceGateError, match="正文排除"):
            service.validate_evidence_manifest(manifest)
    with pytest.raises(EvidenceGateError, match="正文排除"):
        service._validate_resource_exclusions([EvidenceManifestExclusion(
            object_type="resource", object_id=selected[0].object_id, reason_code="policy_skipped")], plan.run.run_id)
    assert runtime.orchestrator.execute_run(plan.run.run_id) == result
    assert len(adapter.query_calls) == 2
    runtime.close()


def test_old_run_resumes_with_its_frozen_policy_after_default_registry_upgrade(tmp_path):
    adapter = _adapter()
    runtime = make_runtime(tmp_path / "runtime", adapter)
    plan = targeted_plan(runtime)
    frozen = plan.run.model_dump_json()
    runtime.close()
    reopened = make_runtime(tmp_path / "runtime", adapter, registry_path=DEFAULT_REGISTRY_PATH)
    result = reopened.orchestrator.execute_run(plan.run.run_id)
    assert result.material_gap_count == 0 and len(adapter.fetch_calls) == 2
    assert reopened.repository.get_run(plan.run.run_id).model_dump_json() == frozen
    manifest = reopened.repository.get_evidence_manifest(result.manifest_id)
    assert not any(e.object_type == "resource" for e in manifest.exclusions)
    reopened.close()


def test_selection_upgrade_does_not_inherit_prior_version_checkpoints():
    from analysis.acquisition.registry import SourceRegistryLoader
    loaded = SourceRegistryLoader().load_registry()
    for source_id in ("cninfo.disclosures", "sse.disclosures", "szse.disclosures", "moutai.ir"):
        definition = loaded.definition(source_id)
        assert definition.incremental_policy.checkpoint_compatible_from_versions == ()


@pytest.mark.parametrize("title", ["贵州茅台2024年年度报告（英文版）", "2024 Annual Report",
    "ANNUAL REPORT 2024", "英文版2024年度报告", "2024年年度报告（英文）（修订版）"])
def test_english_annual_report_exclusion_requires_new_frozen_policy(title):
    row = NormalizedResource("a", "https://example.test/a.pdf", title, "Asia/Shanghai", True,
                             expected_mime_types=("application/pdf",))
    assert apply_content_selection(row, NO_STANDALONE_AUDIT_PDF_V1) is row
    chosen = apply_content_selection(row, NO_AUDIT_ENGLISH_ANNUAL_V1)
    assert not chosen.required_fetch
    assert chosen.metadata["content_selection"]["reason_code"] == ENGLISH_ANNUAL_EXCLUSION_REASON


@pytest.mark.parametrize("title", ["2024年年度报告", "2024年半年度报告（英文版）", "2024年ESG报告（英文版）",
    "2024 ESG Annual Report", "2024 Annual Report (Chinese)", "关于英文年度报告更正的公告",
    "招股说明书附录 Annual Report", "Annual Report presentation notice", "2024 Annual Report（中英双语）"])
def test_non_english_annual_materials_are_kept(title):
    assert not is_english_annual_report(title, "https://example.test/a.pdf", ("application/pdf",))


@pytest.mark.parametrize("url,mimes", [("a.pdf", ()), ("a.pdf", ("text/html",)),
    ("a.html", ("text/html", "application/pdf"))])
def test_english_titles_do_not_override_mime_contract(url, mimes):
    assert not is_english_annual_report("2024年年度报告（英文版）", "https://example.test/" + url, mimes)


@pytest.mark.parametrize("retained", [False, True])
def test_english_annual_exclusion_is_persisted_and_revalidated(tmp_path, retained):
    adapter = _adapter(include_report=True, excluded_title="2024年年度报告（英文版）")
    runtime = make_runtime(tmp_path / "runtime", adapter,
        registry_path=_fixture_registry(tmp_path, retained, NO_AUDIT_ENGLISH_ANNUAL_V1))
    plan = targeted_plan(runtime)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert len(adapter.fetch_calls) == 1 and result.material_gap_count == 0
    manifest = runtime.repository.get_evidence_manifest(result.manifest_id)
    excluded = [e for e in manifest.exclusions if e.object_type == "resource"]
    assert len(excluded) == 1 and excluded[0].reason_code == ENGLISH_ANNUAL_EXCLUSION_REASON
    runtime.manifest_service._validate_resource_exclusions(excluded, plan.run.run_id)
    with pytest.raises(EvidenceGateError):
        runtime.manifest_service._validate_resource_exclusions([
            excluded[0].model_copy(update={"reason_code": AUDIT_EXCLUSION_REASON})], plan.run.run_id)
    runtime.close()
