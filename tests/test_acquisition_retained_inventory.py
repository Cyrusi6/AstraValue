import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from analysis.acquisition.adapters.official import (
    CninfoAcquisitionAdapter,
    OfficialAcquisitionAdapter,
)
from analysis.acquisition.models import AcquisitionPlan
from analysis.acquisition.registry import DEFAULT_REGISTRY_PATH
from analysis.acquisition.retained_inventory import export_cninfo_inventory, plan_retained_inventory, validate_inventory
from orchestrator_support import ScenarioAdapter, envelope, make_runtime
from test_acquisition_html_content import _html


def origin_bundle(tmp_path, *, audit=False):
    class Transport:
        def request(self, work):
            if work.query_family == "company_bootstrap":
                data = {"stockList": [{"code": "600519", "orgId": "gssh0600519", "zwjc": "贵州茅台"}]}
            elif work.url.endswith(".html"):
                return envelope(url=work.url, body=_html(), content_type="text/html")
            elif work.url.endswith(".pdf"):
                return envelope(url=work.url, body=b"%PDF-1.4\nfixture audit", content_type="application/pdf")
            else:
                data = {"announcements": [{"announcementId": str(i), "secCode": "600519",
                    "announcementTitle": "2001年年度报告摘要", "announcementTime": 1000000000000,
                    "adjunctUrl": f"finalpage/2001/summary-{i}.html"} for i in (1, 2)],
                    "totalAnnouncement": 2}
                if audit:
                    data["announcements"].append({"announcementId": "3", "secCode": "600519",
                        "announcementTitle": "贵州茅台审计报告及财务报表", "announcementTime": 1000000000000,
                        "adjunctUrl": "finalpage/2001/audit.pdf"})
                    data["totalAnnouncement"] = 3
            return envelope(url=work.url, body=json.dumps(data).encode())

    class LegacyCninfoAdapter(CninfoAcquisitionAdapter):
        def fetch_resource(self, work):
            return OfficialAcquisitionAdapter.fetch_resource(self, work)

    registry = DEFAULT_REGISTRY_PATH.with_name("business_model_sources.v1.9.json") if audit else DEFAULT_REGISTRY_PATH
    runtime = make_runtime(tmp_path/"origin", None, registry_path=registry)
    adapter = LegacyCninfoAdapter(Transport(), runtime.snapshot_bytes)
    runtime.orchestrator._adapter_resolver = lambda *_: adapter
    now = datetime.now(timezone.utc)
    plan = runtime.create_plan(runtime.build_profile("600519"), mode="incremental", run_kind="ad_hoc",
                               as_of=now, start_at=now-timedelta(days=1))
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.material_gap_count == 0
    runtime.close()
    return export_cninfo_inventory(tmp_path/"origin/analysis.db", tmp_path/"origin/data",
                                   [plan.run.run_id], ticker="600519")


def destination(tmp_path, response):
    adapter = ScenarioAdapter(lambda _: pytest.fail("local inventory must perform zero discovery I/O"),
                              lambda *_: pytest.fail("unexpected network parser"), response)
    return make_runtime(tmp_path/"destination", adapter, registry_path=DEFAULT_REGISTRY_PATH), adapter


def test_verified_inventory_new_namespace_html_fetch_and_durable_resume(tmp_path):
    bundle = origin_bundle(tmp_path)
    runtime, adapter = destination(tmp_path, lambda w: envelope(url=w.resource.url,
                                                    body=_html(), content_type="text/html"))
    plan = plan_retained_inventory(runtime, bundle)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.outcome_counts == {"success": 3} and result.material_gap_count == 0
    assert len(adapter.fetch_calls) == 2 and not adapter.query_calls
    assert runtime.orchestrator.execute_run(plan.run.run_id) == result
    assert len(adapter.fetch_calls) == 2
    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id, limit=None)
    local = next(a for a in attempts if a.attempt_kind.value == "discovery")
    assert local.request_summary["io_performed"] is False
    proof = runtime.repository.list_discovery_proofs(local.attempt_id)[0]
    assert proof.proof_kind == "retained_inventory" and proof.http_status is None
    assert proof.body_retained and not proof.proves_no_data
    resources = runtime.repository.list_discovered_resources(proof.observation_id)
    assert all(r.source_definition_version == "1.10.0" and r.expected_mime_types == ("text/html",)
               and r.metadata["origin_namespace_id"] == bundle["origin_namespace_id"] for r in resources)
    assert runtime.namespace_id != bundle["origin_namespace_id"]
    assert runtime.repository.latest_checkpoint("600519", "cninfo.disclosures", "1.10.0", "1.0.0") is None
    with pytest.raises(ValueError, match="ad_hoc"):
        AcquisitionPlan.model_validate({**plan.model_dump(), "run": {
            **plan.run.model_dump(), "run_kind": "production", "request_scope": "complete"}})
    runtime.close()


@pytest.mark.parametrize("mutation", ["title", "body", "namespace", "empty"])
def test_retained_inventory_rejects_tampered_evidence(tmp_path, mutation):
    bundle = copy.deepcopy(origin_bundle(tmp_path))
    if mutation == "title": bundle["entries"][0]["title"] = "伪造公告"
    elif mutation == "body": next(iter(bundle["proofs"].values()))["body_base64"] = "e30="
    elif mutation == "namespace": bundle["origin_namespace_id"] = "wrong-namespace"
    else: bundle["entries"] = []
    with pytest.raises(ValueError, match="retained_inventory_invalid"):
        validate_inventory(bundle, ticker="600519")


def test_old_inventory_selection_preserves_origin_and_records_exclusion(tmp_path):
    from scripts.archive_cninfo_inventory import report
    bundle = origin_bundle(tmp_path, audit=True)
    before = copy.deepcopy(bundle)
    runtime, adapter = destination(tmp_path, lambda w: envelope(
        url=w.resource.url, body=_html(), content_type="text/html"))
    plan = plan_retained_inventory(runtime, bundle)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.material_gap_count == 0
    assert len(adapter.fetch_calls) == 2 and not adapter.query_calls
    assert all(not w.resource.url.endswith("audit.pdf") for w in adapter.fetch_calls)
    manifest = runtime.repository.get_evidence_manifest(result.manifest_id)
    runtime.manifest_service._validate_resource_exclusions(
        (e for e in manifest.exclusions if e.object_type == "resource"), plan.run.run_id)
    exclusions = [e for e in manifest.exclusions if e.object_type == "resource"]
    assert len(exclusions) == 1 and exclusions[0].reason_code == "excluded_standalone_audit_pdf"
    selected = runtime.repository.get_discovered_resource(exclusions[0].object_id)
    original = next(r for r in bundle["entries"] if r["canonical_resource_id"] == "cninfo:3")
    assert original["required_fetch"] and not selected.required_fetch
    assert selected.metadata["origin_row_hash"] == original["row_hash"]
    assert selected.metadata["origin_proof_id"] == original["proof_id"]
    assert selected.metadata["origin_discovered_resource_id"] == original["discovered_resource_id"]
    output = tmp_path / "report"
    output.mkdir()
    summary = report(runtime, bundle, {"batches": [{"run_id": plan.run.run_id}],
        "inventory_sha256": "a" * 64}, output)
    assert summary["fetch_counts"] == {"success": 2, "metadata_only": 1}
    row = next(r for r in summary["rows"] if r["canonical_resource_id"] == "cninfo:3")
    assert row["fetch_reason"] == "excluded_standalone_audit_pdf" and "attempt_id" not in row
    assert runtime.orchestrator.execute_run(plan.run.run_id) == result
    assert len(adapter.fetch_calls) == 2 and bundle == before
    runtime.close()


def test_resume_does_not_auto_retry_terminal_failure_or_duplicate_success(tmp_path, monkeypatch):
    bundle = origin_bundle(tmp_path)
    runtime, adapter = destination(tmp_path, lambda w: envelope(url=w.resource.url,
        status=504 if w.resource.canonical_resource_id == "cninfo:1" else 200,
        body=_html(), content_type="text/html"))
    plan = plan_retained_inventory(runtime, bundle)
    original = runtime.orchestrator._execute_fetch
    count = 0
    def interrupt(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt()
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.orchestrator, "_execute_fetch", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runtime.orchestrator.execute_run(plan.run.run_id)
    monkeypatch.setattr(runtime.orchestrator, "_execute_fetch", original)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.outcome_counts == {"success": 2, "timeout": 1}
    assert len(adapter.fetch_calls) == 2 and result.material_gap_count == 1
    runtime.close()


def test_local_directory_keeps_challenge_halt_and_zero_io_remaining_items(tmp_path):
    bundle = origin_bundle(tmp_path)
    runtime, adapter = destination(tmp_path, lambda w: envelope(url=w.resource.url,
        body=_html(), content_type="text/html", headers={"x-tengine-error": "denied by bot"}))
    plan = plan_retained_inventory(runtime, bundle)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert len(adapter.fetch_calls) == 1
    assert result.outcome_counts == {"success": 1, "restricted": 1, "policy_skipped": 1}
    assert result.material_gap_count == 2
    runtime.close()


def test_archive_report_refreshes_derived_versions_without_network_and_retains_old_projection(tmp_path, monkeypatch):
    from scripts.archive_cninfo_inventory import report
    bundle = origin_bundle(tmp_path)
    runtime, adapter = destination(tmp_path, lambda w: envelope(url=w.resource.url,
                                                    body=_html(), content_type="text/html"))
    plan = plan_retained_inventory(runtime, bundle)
    runtime.orchestrator.execute_run(plan.run.run_id)
    output = tmp_path/"report"
    output.mkdir()
    state = {"batches": [{"run_id": plan.run.run_id}], "inventory_sha256": "a"*64}
    first = report(runtime, bundle, state, output, derive=True)
    assert first["fetch_counts"] == {"success": 2} and first["text_counts"] == {"parsed": 2}
    path = next(output.glob("text-*.json"))
    cached = json.loads(path.read_text(encoding="utf-8"))
    cached["classifier_version"] = cached["material"]["classifier_version"] = "1.2.0"
    path.write_text(json.dumps(cached), encoding="utf-8")
    refreshed = report(runtime, bundle, state, output, derive=True)
    from analysis.acquisition.materials import CLASSIFIER_VERSION
    assert all(r["material"]["classifier_version"] == CLASSIFIER_VERSION for r in refreshed["rows"])
    assert (output/"derivation-history"/f"{path.stem}-1.2.0.json").exists()
    assert len(adapter.fetch_calls) == 2 and not adapter.query_calls
    def broken(_):
        raise ValueError("corrupt run payload")
    monkeypatch.setattr(runtime.repository, "get_run", broken)
    with pytest.raises(ValueError, match="corrupt run"):
        report(runtime, bundle, state, output)
    runtime.close()
