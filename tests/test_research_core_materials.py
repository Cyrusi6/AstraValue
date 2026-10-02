from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from test_research_workspace import workspace, dump
from analysis.research.jobs import MaterialJobs
from analysis.research.core_materials import _targets, bound_roots
from analysis.research.workspace import ResearchError


def test_evidence_roots_reuse_earlier_indexes_and_exclude_future_or_archive(workspace):
    for relative in ("evidence/2024-09-01", "evidence/2025-02-01", "evidence/archive/2023-01-01", "direct"):
        dump(workspace.root / relative / "live-documents.json", {"documents": []})
    workspace.config["evidence_roots"] = ["evidence", "direct", "evidence/2024-09-01"]
    _, evidence = workspace.material_input_roots(date(2025, 1, 1))
    assert evidence == [workspace.root / "evidence/2024-09-01", workspace.root / "direct"]


def test_core_requests_are_idempotent_and_offline_does_not_spawn(workspace, monkeypatch):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    first = jobs.request_materials(rid, "补资料", "影响估值", material_types=["market_quote", "report_documents"], execute=False)
    second = jobs.request_materials(rid, "换种说法", "影响结论", material_types=["report_documents", "market_quote"], execute=False)
    assert first["task_id"] == second["task_id"]
    assert first["topic"] == "core_materials"
    workspace.config["offline"] = True
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("offline network worker"))
    assert jobs.resume_task(first["task_id"])["status"] == "offline"
    assert jobs.request_materials(rid, "同行", "比较", material_types=["peer_facts"])["status"] == "offline"


def test_document_and_peer_requirements_are_routable_without_dataset_id(workspace, monkeypatch):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    rows = [{"requirement_id": kind, "acquire_allowed": True, "material_types": [kind]}
            for kind in ("report_documents", "peer_facts")]
    monkeypatch.setattr("analysis.research.jobs.read_json", lambda path: {"items": rows})
    result = MaterialJobs(workspace).request_materials(rid, "缺少原文和同行", "影响比较和证据",
        requirement_ids=["report_documents", "peer_facts"])
    assert result["topic"] == "core_materials"
    assert result["material_types"] == ["peer_facts", "report_documents"]
    rows[0]["acquire_allowed"] = False
    result = MaterialJobs(workspace).request_materials(rid, "已有原文", "需要阅读", requirement_ids=["report_documents"])
    assert result["status"] == "processing_or_research_gap"


def test_peer_targets_only_fetch_missing_registered_peer_inputs(workspace):
    state = {"ticker": "600519"}
    payload = {"periods": {"annual": ["2023-12-31", "2024-12-31"]}, "peers": [
        {"ticker": "000858", "metrics": [{"metric_id": m} for m in
            ("operating_income", "gross_margin", "eastmoney_pe_ttm", "eastmoney_pb_mrq")]},
        {"ticker": "000568", "metrics": [{"metric_id": "operating_income"}]}]}
    targets = _targets(workspace, state, payload, {"material_types": ["peer_facts"]})
    assert {x["ticker"] for x in targets} == {"000568", "600809"}
    assert {x["dataset_id"] for x in targets} == {"income_fields", "market_cap"}
    assert all(x["periods"] == ["2024-12-31"] for x in targets if x["dataset_id"] == "income_fields")


def test_adopted_inputs_are_preserved_and_verified(workspace, tmp_path):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    state, _, _ = workspace.pack(rid)
    from analysis.research.workspace import sha
    source = tmp_path / "adopted-projection/600519/facts.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("", encoding="utf8")
    pack = tmp_path / "bound-pack"
    dump(pack / "manifest.json", {"source_inputs": [{"root": str(source.parent.parent),
        "files": {"facts.jsonl": {"path": str(source), "sha256": sha(source)}}}]})
    roots, _, _ = bound_roots(workspace, state, pack)
    assert source.parent.parent in roots
    source.write_text("changed", encoding="utf8")
    with pytest.raises(ResearchError, match="material_parent_input_changed"):
        bound_roots(workspace, state, pack)


def test_market_failure_does_not_skip_peer_and_empty_query_is_terminal(workspace, monkeypatch):
    from analysis.research.core_materials import _collect_structured
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    state, _, _ = workspace.pack(rid)
    jobs = MaterialJobs(workspace)
    job = jobs.request_materials(rid, "补取", "估值", material_types=["market_quote"], execute=False)
    targets = [{"ticker": "600519", "dataset_id": d, "material_type": "market_quote", "periods": []}
               for d in ("market_cap", "income_fields")]
    monkeypatch.setattr("analysis.research.core_materials._targets", lambda *a: targets)
    calls = []
    class Runtime:
        def __init__(self, _):
            pass
        def plan(self, identities, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(run_ids=kwargs["dataset_ids"])
        def execute(self, run_id, **kwargs):
            if run_id == "market_cap":
                raise OSError("source timeout")
            return {"status": {"failed": 0, "pending": 0, "partial": 0, "retryable": 0,
                "no_data": 1, "summary": {"empty": [{"query_complete": True}]}}}
    monkeypatch.setattr("analysis.acquisition.runtime.AcquisitionRuntime.create", lambda *a, **kw: nullcontext(object()))
    monkeypatch.setattr("analysis.structured.runtime.StructuredDataRuntime", Runtime)
    _, pending, failures = _collect_structured(jobs, job, state, {}, workspace.state, date(2025, 1, 1))
    assert not pending and len(failures) == 1
    assert "source timeout" in failures[0]["reason"]
    assert len(calls) == 2
    assert calls[0]["valuation_start"] == date(2024, 12, 18)
    assert calls[0]["dataset_ids"] == ["market_cap"]
    assert job["dataset_status"]["600519:income_fields"]["no_data"] == 1


def mixed_job(workspace, monkeypatch, supplements=("audit_opinion",)):
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    state, pack, _ = workspace.pack(rid)
    root = workspace.root / "projection"
    (root / "600519").mkdir(parents=True)
    workspace.config["projection_roots"] = [str(root)]
    monkeypatch.setattr("analysis.research.core_materials._collect_structured", lambda *a: ([], [], []))
    monkeypatch.setattr("analysis.structured.research_lite.build_lite_pack", lambda **kw: {
        "pack_dir": str(pack), "pack_id": state["snapshot_id"], "status": "ready"})
    jobs = MaterialJobs(workspace)
    parent = jobs.request_materials(rid, "补行情及资料", "保留独立任务进度",
        material_types=["market_quote", *supplements], execute=False)
    children = {kind: jobs.request_materials(rid, "资料", "补充证据", material_types=[kind], execute=False)
                for kind in supplements}
    return jobs, parent, children


@pytest.mark.parametrize("child_state,failures,expected_child,expected_parent", [
    ("running", 0, "running", "checkpointed"),
    ("exhausted", 3, "exhausted", "partial"),
    ("failed", 3, "exhausted", "partial"),
])
def test_mixed_request_respects_child_lease_and_retry_budget(
        workspace, monkeypatch, child_state, failures, expected_child, expected_parent):
    jobs, parent, children = mixed_job(workspace, monkeypatch)
    child = children["audit_opinion"]
    child.update(state=child_state, failures=failures,
                 lease_until=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat())
    jobs.save(child)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("protected child started again"))
    monkeypatch.setattr("analysis.research.supplements.execute", lambda *a: pytest.fail("child lease bypassed"))
    jobs.execute_round(parent["task_id"])
    assert jobs.get(child["task_id"])["state"] == expected_child
    result = jobs.get(parent["task_id"])
    assert result["state"] == expected_parent
    assert result["result"]["candidate_pack_path"]
    assert result["material_results"]["audit_opinion"]["state"] == expected_child


def test_mixed_request_waits_for_child_completion_and_reuses_finished_work(workspace, monkeypatch):
    jobs, parent, children = mixed_job(workspace, monkeypatch)
    spawned = []
    def spawn(*args, **kwargs):
        spawned.append(args)
        return SimpleNamespace(pid=1)
    monkeypatch.setattr("subprocess.Popen", spawn)
    jobs.execute_round(parent["task_id"])
    assert jobs.get(parent["task_id"])["state"] == "checkpointed"
    child = jobs.get(children["audit_opinion"]["task_id"])
    assert child["state"] == "running" and len(spawned) == 1
    child.update(state="completed", result={"materials": {"audit_opinion": "readable"}})
    jobs.save(child)
    jobs.execute_round(parent["task_id"])
    assert jobs.get(parent["task_id"])["state"] == "completed"
    assert len(spawned) == 1


def test_mixed_child_failure_keeps_other_materials_and_core_candidate(workspace, monkeypatch):
    jobs, parent, children = mixed_job(workspace, monkeypatch, ("audit_opinion", "guarantee"))
    children["guarantee"].update(state="completed", result={"materials": {"guarantee": "readable"}})
    jobs.save(children["guarantee"])
    resume = jobs.resume_task
    def fail_one(task_id):
        if task_id == children["audit_opinion"]["task_id"]:
            raise OSError("worker unavailable")
        return resume(task_id)
    monkeypatch.setattr(jobs, "resume_task", fail_one)
    jobs.execute_round(parent["task_id"])
    result = jobs.get(parent["task_id"])
    assert result["state"] == "partial"
    assert result["result"]["candidate_pack_path"]
    assert result["material_results"]["guarantee"]["state"] == "completed"
    assert result["unfinished_datasets"][0]["reason"] == "worker unavailable"
