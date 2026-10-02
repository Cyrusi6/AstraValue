"""Regression probes for the core-material/public-candidate integration."""
import json
from datetime import date
from pathlib import Path

import pytest

from analysis.research.jobs import MaterialJobs
from analysis.research.report_materials import refresh_report_materials
from analysis.research.workspace import read_json, sha
from test_research_workspace import workspace, dump


@pytest.mark.parametrize("kind,category,period", [
    ("annual", "D01", "2025-12-31"), ("interim", "D02", "2026-06-30"),
])
def test_default_report_requirement_reaches_report_service(tmp_path, kind, category, period):
    # Exact next-work shape emitted for a missing default full report.
    result = refresh_report_materials(ticker="600519", cutoff=date(2026, 9, 14),
        output_root=tmp_path / "out", db_path=tmp_path / "db.sqlite", data_root=tmp_path / "data",
        requirements=[{"requirement_id": "lite.context.latest_" + kind + "_original",
                       "material_types": ["report_documents"], "acquire_allowed": True, "period": period,
                       "task_hint": {"action": "acquire_and_parse", "document_classes": [category],
                                     "report_periods": [period]}}], allow_network=False)
    assert result["missing_documents"]


def _registered_projection(workspace):
    projection = workspace.root / "projection"
    source = projection / "600519/coverage-facts.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("", encoding="utf-8")
    workspace.config["projection_roots"] = [str(projection)]
    return {"root": str(projection), "files": {source.name: {"path": str(source), "sha256": sha(source)}}}


def _rewrite_fixture_pack(pack, payload, manifest, extras=None):
    dump(pack / "core-pack.json", payload)
    for name, content in (extras or {}).items():
        path = pack / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    names = set(manifest["output_hashes"]) | set(extras or {})
    manifest["output_hashes"] = {name: sha(pack / name) for name in names}
    dump(pack / "manifest.json", manifest)


def test_core_candidate_preserves_already_adopted_processing_materials(workspace, monkeypatch):
    parent = workspace.root / "packs/600519/2025-01-01/lite-pack-test"
    payload, manifest = read_json(parent / "core-pack.json"), read_json(parent / "manifest.json")
    original = workspace.root / "processed-original.txt"
    original.write_text("已核验的原文与报表整理结果", encoding="utf-8")
    evidence = {"evidence_id": "adopted-evidence", "original_path": str(original),
                "original_sha256": sha(original), "content": original.read_text(encoding="utf-8"), "locator": "pages:1"}
    preserved = {"supplemental_evidence": [evidence],
                 "processing_attachments": [{"path": str(original), "sha256": sha(original)}],
                 "statements": [{"evidence_id": evidence["evidence_id"], "period": "2023-12-31"}],
                 "financial_cell_resolutions": [{"metric_id": "contract_assets", "state": "disclosed_blank",
                    "period": "2023-12-31", "period_type": "instant", "evidence_id": evidence["evidence_id"],
                    "page": 1, "source_cells": ["合同资产", "", ""], "reason": "Original current cell is blank"}]}
    payload.update(preserved)
    manifest["source_inputs"] = [_registered_projection(workspace)]
    _rewrite_fixture_pack(parent, payload, manifest, {"audit/adopted-proof.bin": b"\xff frozen proof"})
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    job = jobs.request_materials(rid, "补行情", "更新行情不应丢失已整理原文", material_types=["market_quote"], execute=False)
    monkeypatch.setattr("analysis.research.core_materials._collect_structured", lambda *args: ([], [], []))
    jobs.execute_round(job["task_id"])
    result = jobs.get(job["task_id"])
    assert result["state"] == "completed", result
    candidate = Path(result["result"]["candidate_pack_path"])
    rebuilt = read_json(candidate / "core-pack.json")
    missing = [name for name, value in preserved.items() if rebuilt.get(name) != value]
    if not (candidate / "audit/adopted-proof.bin").is_file():
        missing.append("audit/adopted-proof.bin")
    assert not missing, "Already adopted material lost: " + ", ".join(missing)


def test_core_candidate_cannot_attach_to_a_snapshot_adopted_during_collection(workspace, monkeypatch):
    _registered_projection(workspace)
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    old_state, old_path, old_payload = workspace.pack(rid)
    jobs = MaterialJobs(workspace)
    job = jobs.request_materials(rid, "补行情", "检验并发采用隔离", material_types=["market_quote"], execute=False)
    adopted = workspace.root / "already-adopted-candidate"
    manifest = read_json(old_path / "manifest.json")
    for name in manifest["output_hashes"]:
        target = adopted / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((old_path / name).read_bytes())
    manifest["pack_id"] = "newer-adopted-snapshot"
    _rewrite_fixture_pack(adopted, dict(old_payload, pack_id=manifest["pack_id"]), manifest)
    ready = workspace.artifact(rid, "snapshot_candidate", {
        "candidate_pack_path": str(adopted), "candidate_snapshot_id": manifest["pack_id"],
        "reason": "Another candidate", "build_status": "ready"})
    monkeypatch.setattr("analysis.research.core_materials._collect_structured", lambda *args: ([], [], []))
    def change_snapshot_during_build(**kwargs):
        jobs.adopt_snapshot(rid, ready["artifact_id"])
        return {"pack_dir": str(old_path), "pack_id": old_state["snapshot_id"], "status": "ready"}
    monkeypatch.setattr("analysis.structured.research_lite.build_lite_pack", change_snapshot_during_build)
    jobs.execute_round(job["task_id"])
    assert workspace.task(rid)[0]["snapshot_id"] == "newer-adopted-snapshot"
    assert workspace.artifacts(rid, "snapshot_candidate") == [], "Old-parent candidate was attached to the newly active snapshot"
