import hashlib
import json
from pathlib import Path

import pytest

from analysis.research.workspace import ResearchError, ResearchWorkspace


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    stocks = [{"code": code, "zwjc": name, "orgId": org, "category": "A股"}
              for code, name, org in [("600519", "贵州茅台", "gssh0600519"), ("000858", "五粮液", "gssz0000858")]]
    dump(tmp_path / "identity.json", {"stockList": stocks})
    for stock in stocks:
        pack = tmp_path / "packs" / stock["code"] / "2025-01-01" / "lite-pack-test"
        entries = [{"metric_id": "operating_income", "label": "营收", "group": "B", "state": "ready", "required": True,
                    "period_type": "cumulative", "period": "2024-12-31", "fact_ref": "F1",
                    "fact": {"fact_id": stock["code"] + "-revenue", "value": "100", "unit": "CNY"}}]
        body = {"metrics": entries, "coverage_requirements": entries, "peers": [], "evidence": [], "token_count": {"count": 10}}
        dump(pack / "core-pack.json", body)
        dump(pack / "core-coverage.json", {"requirements": entries})
        dump(pack / "next-work.json", {"items": [
            {"stage": "acquisition", "dataset_id": "income", "raw_name": "revenue", "reason": "missing",
             "question_id": q, "period": "2024-12-31"} for q in ["Q1", "Q2"]]})
        (pack / "core-pack.md").write_text(stock["zwjc"] + "；不自动评级", encoding="utf-8")
        (pack / "evidence-index.jsonl").write_text("", encoding="utf-8")
        dump(pack / "manifest.json", {"pack_id": "pack-" + stock["code"], "pack_version": "eight-step-lite-pack-v1.0.4",
            "profile_id": "eight-step-lite-v1.0.0", "ticker": stock["code"], "as_of": "2025-01-01", "status": "ready",
            "output_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in pack.iterdir()}})
    return ResearchWorkspace(tmp_path, {"state_root": "state", "identity_file": "identity.json",
         "pack_roots": ["packs"], "projection_roots": [], "evidence_roots": [], "profile_id": "eight-step-lite-v1.0.0"})


def test_prepare_company_independent_idempotent_and_read_only(workspace):
    first = workspace.prepare_research("贵州茅台", "2025-01-01")
    second = workspace.prepare_research("000858.SZ", "2025-01-01")
    assert first["status"] == second["status"] == "analysis_pending"
    assert first["research_id"] != second["research_id"]
    assert workspace.prepare_research("600519.SH", "2025-01-01")["research_id"] == first["research_id"]
    assert "不自动评级" not in first["content"]
    assert "不自动评级" in next((workspace.root / "packs/600519").rglob("core-pack.md")).read_text(encoding="utf-8")
    restarted = ResearchWorkspace(workspace.root, workspace.config)
    assert restarted.get_task(first["research_id"])["snapshot_id"] == first["snapshot_id"]


def test_identity_ambiguity_precedes_io(workspace):
    path = workspace.root / "identity.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["stockList"][1]["zwjc"] = "贵州茅台"
    dump(path, data)
    assert workspace.prepare_research("贵州茅台", "2025-01-01")["status"] == "ambiguous"


def test_snapshot_integrity_and_query_isolation(workspace):
    result = workspace.prepare_research("贵州茅台", "2025-01-01")
    rid = result["research_id"]
    rows = workspace.query_research(rid)["rows"]
    assert rows[0]["fact"]["fact_id"].startswith("600519")
    pack = next((workspace.root / "packs/600519").rglob("core-pack.md"))
    pack.write_text("tampered", encoding="utf-8")
    with pytest.raises(ResearchError, match="integrity"):
        workspace.get_task(rid)


def test_gap_summary_counts_dependencies_not_files(workspace):
    rid = workspace.prepare_research("贵州茅台", "2025-01-01")["research_id"]
    result = workspace.query_research(rid, topic="gaps")
    assert result["total"] == 1
    assert result["rows"][0]["dependency_count"] == 2
    assert result["rows"][0]["questions"] == ["Q1", "Q2"]
    assert workspace.query_research(rid, topic="invented")["status"] == "capability_gap"
    with pytest.raises(ResearchError):
        workspace.query_research(rid, page_size=100000)


def test_draft_reverting_text_is_a_new_revision_and_survives_restart(workspace):
    from analysis.research.drafts import Drafts
    state = workspace.prepare_research("600519", "2025-01-01")
    drafts = Drafts(workspace)
    args = dict(research_id=state["research_id"], snapshot_id=state["snapshot_id"], number=1,
                judgment="判断", evidence_refs=["F1"], invalidation=["收入持续下降"])
    for body in ["初稿", "修订稿", "初稿"]:
        drafts.save_section(**args, markdown=body)
    restored = Drafts(ResearchWorkspace(workspace.root, workspace.config)).get_draft(state["research_id"])
    assert restored["sections"][0]["markdown"] == "初稿"
    assert restored["sections"][0]["revision"] == 3
    with pytest.raises(ResearchError, match="stale_draft"):
        drafts.save_section(**{**args, "snapshot_id": "other"}, markdown="text")
    with pytest.raises(ResearchError, match="unknown_section_evidence"):
        drafts.save_section(**{**args, "evidence_refs": ["F999"]}, markdown="text")


def test_material_request_wording_cannot_reset_budget(workspace):
    from analysis.research.jobs import MaterialJobs
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    a = jobs.request_materials(rid, "核对公告", "可能改变评级", topic="catalog")
    b = jobs.request_materials(rid, "换个说法核对公告", "可能改变估值", topic="catalog")
    assert a["task_id"] == b["task_id"]


def test_successful_rounds_do_not_exhaust_and_expired_worker_recovers(workspace, monkeypatch):
    from analysis.research.jobs import MaterialJobs
    workspace.config.update(max_attempts=3, max_tool_seconds=120)
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    job = jobs.request_materials(rid, "核对公告", "影响评级", topic="catalog")
    job.update(state="checkpointed", attempts=10, failures=0)
    jobs.save(job)
    class Process:
        pid = 1
    monkeypatch.setattr("analysis.research.jobs.subprocess.Popen", lambda *a, **kw: Process())
    assert jobs.resume_task(job["task_id"])["state"] == "running"
    running = jobs.get(job["task_id"])
    running["lease_until"] = "2000-01-01T00:00:00+00:00"
    jobs.save(running)
    assert jobs.resume_task(job["task_id"])["state"] == "running"
    assert jobs.get(job["task_id"])["attempts"] == 12
    with pytest.raises(ResearchError, match="stale_material_worker"):
        jobs.save(running)


def test_ratio_is_reproducible_and_rejects_unknown_facts(workspace):
    from analysis.research.calculations import Calculations
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    calc = Calculations(workspace)
    a = calc.calculate(rid, "ratio", {"numerator": "F1", "denominator": "F1"})
    assert a["result"]["value"] == "1"
    assert calc.calculate(rid, "ratio", {"numerator": "F1", "denominator": "F1"})["artifact_id"] == a["artifact_id"]
    with pytest.raises(ResearchError, match="unknown_fact"):
        calc.calculate(rid, "ratio", {"numerator": "other", "denominator": "F1"})
