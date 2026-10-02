import json
from pathlib import Path
import sqlite3

import pytest

from analysis.research.api_discovery import register_pages
from analysis.research.api_materials import register as register_legacy
from analysis.research.catalog import Catalog
from analysis.research.jobs import MaterialJobs
from analysis.research.supplements import existing
from analysis.research.sw_industry import register as register_industry
from analysis.research.workspace import ResearchError, read_json, sha
from test_research_api_materials import prepare as prepare_legacy
from test_research_auto_supplements import api_page
from test_research_sw_industry import evidence
from test_research_workspace import dump, workspace


@pytest.fixture
def research(workspace):
    workspace.config["offline"] = True
    return workspace.prepare_research("600519", "2025-01-01")["research_id"]


def _materials(workspace, research_id, root):
    for name in ("auto", "legacy", "industry", "empty"):
        (root / name).mkdir()
    rows = [{"SECURITY_CODE": "600519", "NOTICE_DATE": "2024-12-31", "PUNISH_OBJECT": "A"}] * 2
    register_pages(workspace, research_id, "regulatory_records", [api_page(root / "auto", rows)])
    prepare_legacy(root / "legacy")
    register_legacy(workspace, research_id, root / "legacy")
    result, _ = evidence(root / "industry")
    register_industry(workspace, research_id, result)
    register_pages(workspace, research_id, "guarantee", [api_page(root / "empty", [])])
    return {kind: workspace.artifacts(research_id, kind)
            for kind in ("catalog_api_materials", "sw_industry_classification")}


def _candidate(workspace, research_id, snapshot_id="candidate-1", source_task=None):
    _, parent, payload = workspace.pack(research_id)
    manifest = read_json(parent / "manifest.json")
    target = workspace.root / snapshot_id
    for name in manifest["output_hashes"]:
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((parent / name).read_bytes())
    manifest["pack_id"] = snapshot_id
    dump(target / "core-pack.json", {**payload, "pack_id": snapshot_id})
    manifest["output_hashes"] = {name: sha(target / name) for name in manifest["output_hashes"]}
    dump(target / "manifest.json", manifest)
    return workspace.artifact(research_id, "snapshot_candidate", {
        "candidate_pack_path": str(target), "candidate_snapshot_id": snapshot_id,
        "reason": "更新行情", "build_status": "ready", "source_task": source_task})


def _rows(workspace, research_id, snapshot_id=None):
    with workspace.connect() as con:
        rows = con.execute("SELECT * FROM research_artifacts WHERE research_id=? ORDER BY rowid", (research_id,)).fetchall()
    return [dict(row) for row in rows if snapshot_id is None or row["snapshot_id"] == snapshot_id]


def _assert_binding(current, original, snapshot_id):
    assert current["artifact_id"] != original["artifact_id"]
    assert current["snapshot_id"] == snapshot_id
    assert current["inherited_from_artifact_id"] == original["artifact_id"]
    assert current["inherited_from_snapshot_id"] == original["snapshot_id"]
    binding = {"artifact_id", "snapshot_id", "inherited_from_artifact_id", "inherited_from_snapshot_id"}
    assert {k: v for k, v in current.items() if k not in binding} == {
        k: v for k, v in original.items() if k not in binding}


def test_standalone_market_candidate_keeps_materials_readable_and_old_registrations(workspace, research, tmp_path):
    originals = _materials(workspace, research, tmp_path)
    other = workspace.prepare_research("000858", "2025-01-01")["research_id"]
    jobs = MaterialJobs(workspace)
    workspace.config["offline"] = False
    job = jobs.request_materials(research, "补行情", "更新估值", material_types=["market_quote"], execute=False)
    workspace.config["offline"] = True
    assert job["material_types"] == ["market_quote"]
    candidate = _candidate(workspace, research, source_task=job["task_id"])
    old_state, revision = workspace.task(research)
    old_rows = _rows(workspace, research)
    old_pack = Path(old_state["pack_path"])
    pack_hashes = {p.name: sha(p) for p in old_pack.iterdir() if p.is_file()}
    result = jobs.adopt_snapshot(research, candidate["artifact_id"])
    assert result["snapshot_id"] == "candidate-1" and result["review_required"]
    state, updated_revision = workspace.task(research)
    assert updated_revision == revision + 1
    assert state["status"] == "analysis_pending" and state["stages"]["analysis"] == "needs_review"
    assert state["stages"]["rendering"] == "pending"
    assert _rows(workspace, research, old_state["snapshot_id"]) == old_rows
    assert {p.name: sha(p) for p in old_pack.iterdir() if p.is_file()} == pack_hashes
    for kind, records in originals.items():
        inherited = workspace.artifacts(research, kind)
        assert len(inherited) == len(records)
        for current, original in zip(inherited, records):
            _assert_binding(current, original, "candidate-1")
        assert workspace.artifacts(other, kind) == []
    catalog = Catalog(workspace)
    _, data = catalog._load(research)
    api_item = next(item for item in data["items"] if item["reader"] == "api_record"
                    and item["payload"]["period"] == "2024-12-31")
    content = json.loads(catalog.read_material(research, api_item["material_id"], max_tokens=4000)["content"])
    assert len(content["rows"]) == 2 and content["retrieved_at"] == "2025-01-02T00:00:00+00:00"
    sw_item = next(item for item in data["items"] if item["reader"] == "sw_classification")
    classification = json.loads(catalog.read_material(research, sw_item["material_id"], max_tokens=4000)["content"])
    assert classification["observed_at"] == "2026-09-16T09:00:00+08:00"
    assert classification["after_research_cutoff"] and classification["effective_date"] is None
    assert "no historical membership reconstruction" in classification["temporal_scope"]
    assert existing(workspace, research, "regulatory_records")["status"] == "original_readable"
    assert existing(workspace, research, "sw_industry")["status"] == "alternative"
    empty = existing(workspace, research, "guarantee")
    assert empty["status"] == "unavailable" and empty["source_state"] == "empty" and empty["reused"]


def test_only_reading_materials_are_inherited(workspace, research, tmp_path):
    originals = _materials(workspace, research, tmp_path)
    excluded = ("section", "conclusion", "calculation", "review", "evidence_read", "chart", "business_profile")
    for kind in excluded:
        workspace.artifact(research, kind, {"content": "旧研究成果"})
    candidate = _candidate(workspace, research)
    before = _rows(workspace, research)
    MaterialJobs(workspace).adopt_snapshot(research, candidate["artifact_id"])
    assert all(not workspace.artifacts(research, kind) for kind in (*excluded, "snapshot_candidate"))
    assert len(_rows(workspace, research, "candidate-1")) == sum(map(len, originals.values()))
    assert _rows(workspace, research, "pack-600519") == before


@pytest.mark.parametrize("source_kind", ["api_item", "api_source_only", "sw_result", "sw_source"])
@pytest.mark.parametrize("failure", ["changed", "missing"])
def test_unverifiable_source_rolls_back_adoption(workspace, research, tmp_path, source_kind, failure):
    originals = _materials(workspace, research, tmp_path)
    api = originals["catalog_api_materials"]
    sw = originals["sw_industry_classification"][0]
    paths = {
        "api_item": api[1]["items"][0]["path"],
        "api_source_only": api[2]["sources"][0]["path"],
        "sw_result": sw["result_path"],
        "sw_source": sw["sources"][1]["path"],
    }
    candidate = _candidate(workspace, research)
    before_state = workspace.task(research)
    before_rows = _rows(workspace, research)
    path = Path(paths[source_kind])
    if failure == "changed":
        path.write_text("changed", encoding="utf-8")
    else:
        path.unlink()
    with pytest.raises(ResearchError, match="changed|source_unavailable"):
        MaterialJobs(workspace).adopt_snapshot(research, candidate["artifact_id"])
    assert workspace.task(research) == before_state
    assert _rows(workspace, research) == before_rows


@pytest.mark.parametrize("failure_at", ["second_material", "state_save", "revision_cas"])
def test_material_write_and_state_save_share_one_transaction(workspace, research, tmp_path, monkeypatch, failure_at):
    _materials(workspace, research, tmp_path)
    candidate = _candidate(workspace, research)
    before_state = workspace.task(research)
    before_rows = _rows(workspace, research)
    if failure_at == "revision_cas":
        save = workspace._save
        def stale_save(state, expected_revision, *, connection=None):
            connection.execute("UPDATE research_tasks SET revision=revision+1 WHERE id=?", (research,))
            save(state, expected_revision, connection=connection)
        monkeypatch.setattr(workspace, "_save", stale_save)
        error, pattern = ResearchError, "concurrent_task_update"
    else:
        target = ("BEFORE INSERT ON research_artifacts WHEN NEW.snapshot_id='candidate-1' "
                  "AND NEW.kind='sw_industry_classification'") if failure_at == "second_material" else (
                  "BEFORE UPDATE OF state_json ON research_tasks")
        with workspace.connect() as con:
            con.execute("CREATE TRIGGER fail_adoption " + target +
                        " BEGIN SELECT RAISE(ABORT, 'forced_adoption_failure'); END")
        error, pattern = sqlite3.IntegrityError, "forced_adoption_failure"
    with pytest.raises(error, match=pattern):
        MaterialJobs(workspace).adopt_snapshot(research, candidate["artifact_id"])
    assert workspace.task(research) == before_state
    assert _rows(workspace, research) == before_rows


@pytest.mark.parametrize("change", ["revision", "snapshot"])
def test_change_during_candidate_verification_rejects_stale_adoption(workspace, research, tmp_path, monkeypatch, change):
    _materials(workspace, research, tmp_path)
    candidate = _candidate(workspace, research)
    other = _candidate(workspace, research, "candidate-other")
    jobs = MaterialJobs(workspace)
    verify = workspace._verify_pack
    competing_state = []
    def concurrent_verify(path):
        result = verify(path)
        if str(path) == candidate["candidate_pack_path"] and not competing_state:
            competing_state.append(None)
            if change == "snapshot":
                jobs.adopt_snapshot(research, other["artifact_id"])
            else:
                state, revision = workspace.task(research)
                state["status"] = "concurrent_update"
                workspace._save(state, revision)
            competing_state[0] = workspace.task(research)
        return result
    monkeypatch.setattr(workspace, "_verify_pack", concurrent_verify)
    pattern = "candidate_not_in_active_snapshot" if change == "snapshot" else "concurrent_task_update"
    with pytest.raises(ResearchError, match=pattern):
        jobs.adopt_snapshot(research, candidate["artifact_id"])
    assert workspace.task(research) == competing_state[0]
    assert _rows(workspace, research, "candidate-1") == []
    if change == "snapshot":
        assert len(workspace.artifacts(research, "catalog_api_materials")) == 3


@pytest.mark.parametrize("field,value,pattern", [("ticker", "000858", "candidate_identity_mismatch"),
    ("as_of", "2025-01-02", "candidate_identity_mismatch"),
    ("pack_id", "unregistered-candidate", "candidate_snapshot_mismatch")])
def test_candidate_identity_is_unchanged_before_inheriting(workspace, research, tmp_path, field, value, pattern):
    _materials(workspace, research, tmp_path)
    candidate = _candidate(workspace, research)
    manifest_path = Path(candidate["candidate_pack_path"]) / "manifest.json"
    manifest = read_json(manifest_path)
    manifest[field] = value
    dump(manifest_path, manifest)
    before_state, before_rows = workspace.task(research), _rows(workspace, research)
    with pytest.raises(ResearchError, match=pattern):
        MaterialJobs(workspace).adopt_snapshot(research, candidate["artifact_id"])
    assert workspace.task(research) == before_state
    assert _rows(workspace, research) == before_rows


def test_sequential_adoption_keeps_lineage_and_same_snapshot_adds_no_duplicate_binding(workspace, research, tmp_path):
    originals = _materials(workspace, research, tmp_path)
    jobs = MaterialJobs(workspace)
    first = _candidate(workspace, research)
    jobs.adopt_snapshot(research, first["artifact_id"])
    intermediate = {kind: workspace.artifacts(research, kind) for kind in originals}
    second = _candidate(workspace, research, "candidate-2")
    jobs.adopt_snapshot(research, second["artifact_id"])
    for kind in originals:
        for latest, previous in zip(workspace.artifacts(research, kind), intermediate[kind]):
            _assert_binding(latest, previous, "candidate-2")
    current, _ = workspace.task(research)
    same = workspace.artifact(research, "snapshot_candidate", {
        "candidate_pack_path": current["pack_path"], "candidate_snapshot_id": current["snapshot_id"], "reason": "已是当前输入"})
    before_rows = _rows(workspace, research)
    jobs.adopt_snapshot(research, same["artifact_id"])
    jobs.adopt_snapshot(research, same["artifact_id"])
    assert _rows(workspace, research) == before_rows
    with pytest.raises(ResearchError, match="candidate_not_in_active_snapshot"):
        jobs.adopt_snapshot(research, first["artifact_id"])
    assert _rows(workspace, research) == before_rows
