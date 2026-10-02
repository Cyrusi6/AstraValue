"""Offline integration with real repositories, immutable originals and report bridge.

Synthetic document text and research prose are test data, never golden acceptance.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path

import pytest

import conftest
from analysis.acquisition.registry import POLICY_APPROVED_REGISTRY_PATH
from analysis.business_evidence.corpus import FrozenCorpus
from analysis.business_evidence.models import ReviewBatch
from analysis.business_evidence.profile import ProfileInput, build_profile
from analysis.business_evidence.store import FactStore
from analysis.research.business_profile import BusinessProfiles
from analysis.research.drafts import Drafts
from analysis.research.reports import Reports
from analysis.research.tools import operations
from analysis.research.workspace import ResearchError, ResearchWorkspace, sha
from structured.test_reporting_bridge import _pack, _write_json
from test_business_profile import record, spec
from test_raw_resource_storage import _prepare_lineage, _content_request


@pytest.fixture
def context(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 12, 8, tzinfo=timezone.utc)

    monkeypatch.setattr(conftest, "INITIAL_REGISTRY_PATH", POLICY_APPROVED_REGISTRY_PATH)
    monkeypatch.setattr(conftest, "datetime", FixedDatetime)
    monkeypatch.setattr("analysis.acquisition.repository.datetime", FixedDatetime)
    from analysis.acquisition.models import DiscoveredResource
    monkeypatch.setattr("test_raw_resource_storage.DiscoveredResource", lambda **kw:
                        DiscoveredResource(**{**kw, "title": "年度报告（离线合成测试原件）"}))
    acquisition = conftest.acquisition_store.__wrapped__(tmp_path)
    lineage = _prepare_lineage(acquisition)
    frozen = lineage["service"].freeze_content(
        b"offline integration original, not a live annual report",
        _content_request(acquisition, lineage, observation_id="profile-original"),
        owner_token=lineage["token"], lease_epoch=1)
    quote = "本期收入 100 元、200 元和销售量 10 吨，更正披露 0 元。"
    text = lineage["service"].freeze_derived_artifact(
        parent_snapshot_id=frozen.snapshot.snapshot_id, artifact_type="text",
        extractor_id="offline-integration", extractor_version="1.0.0", parameters={},
        output=("--- PDF page 1 ---\n" + quote).encode())
    from analysis.acquisition.manifests import EvidenceManifestService
    gate = EvidenceManifestService(lineage["blob_store"], acquisition.repository,
                                   acquisition.repository.get_source_definition_version)
    manifest = gate.build_evidence_manifest(
        run_id=acquisition.run.run_id, question_set_version=acquisition.run.question_set_version,
        registry_id=acquisition.run.registry_id, registry_version=acquisition.run.registry_version,
        as_of=acquisition.now + timedelta(minutes=1), snapshot_ids=(frozen.snapshot.snapshot_id,),
        derived_artifact_ids=(text.derived_artifact_id,), audit_proof_ids=(), coverage_summary={"complete": 1})
    corpus = FrozenCorpus(acquisition.db_path, acquisition.data_root, manifest.manifest_id)
    facts_path = tmp_path / "business-facts.db"
    store = FactStore(facts_path, manifest.storage_namespace_id, create=True)
    fact = record(citations=[dict(snapshot_id=frozen.snapshot.snapshot_id,
        derived_artifact_id=text.derived_artifact_id, page_number=1, section="收入", quote=quote)])
    store.import_batch(ReviewBatch(schema_version="1.0.0", reviewer="offline test fixture", records=[fact]), corpus)
    request = spec(fact)
    request["as_of"] = "2026-09-13T00:00:00+08:00"
    original = acquisition.data_root / frozen.snapshot.archive_relative_path
    pack = _pack(tmp_path)
    core = json.loads((pack / "core-pack.json").read_text("utf-8"))
    coverage = json.loads((pack / "core-coverage.json").read_text("utf-8"))
    core.update(evidence=[], supplemental_evidence=[{"evidence_id": "original", "company": "600519", "original_path": str(original),
                           "original_sha256": frozen.snapshot.sha256}], peers=[],
                coverage_requirements=coverage["requirements"], token_count={"count": 10})
    for n, metric in enumerate(core["metrics"]):
        f = metric["fact"]
        metric.update(fact_ref=f"F{n}", metric_id=f["metric_id"], period=f["period_end"],
                      period_type=f["period_type"], group="B", label=f["metric_id"], required=True)
    _write_json(pack / "core-pack.json", core)
    _write_json(pack / "next-work.json", {"items": []})
    (pack / "core-pack.md").write_text("offline integration", encoding="utf-8")
    (pack / "evidence-index.jsonl").write_text("", encoding="utf-8")
    pm = json.loads((pack / "manifest.json").read_text("utf-8"))
    pm["pack_version"] = "eight-step-lite-pack-v1.0.4"
    pm["output_hashes"] = {p.name: sha(p) for p in pack.iterdir() if p.name != "manifest.json"}
    _write_json(pack / "manifest.json", pm)
    destination = tmp_path / "packs/600519/2026-09-13/lite-pack-test"
    destination.parent.mkdir(parents=True)
    pack.rename(destination)
    _write_json(tmp_path / "identity.json", {"stockList": [dict(code="600519", zwjc="贵州茅台", orgId="gssh0600519", category="A股")]})
    config = dict(state_root="state", identity_file="identity.json", pack_roots=["packs"],
        projection_roots=[], evidence_roots=[], profile_id="eight-step-lite-v1.0.0",
        business_evidence_sources=[dict(company_id="600519", facts_db=str(facts_path),
            acquisition_db=str(acquisition.db_path), data_root=str(acquisition.data_root), manifest_id=manifest.manifest_id)])
    w = ResearchWorkspace(tmp_path, config)
    state = w.prepare_research("600519", "2026-09-13")
    assert state["snapshot_id"]
    return w, state, request, corpus, store, acquisition, original


def test_profile_real_services_through_report_bridge_and_explicit_citation(context):
    w, state, request, *_ = context
    rid, sid = state["research_id"], state["snapshot_id"]
    ops = operations(w)
    assert set(inspect.signature(ops["build_business_profile"]).parameters) == {"research_id", "profile"}
    profile = ops["build_business_profile"](rid, request)
    assert ops["build_business_profile"](rid, request)["artifact_id"] == profile["artifact_id"]
    assert profile["snapshot_id"] == sid
    assert BusinessProfiles(ResearchWorkspace(w.root, w.config)).get(rid)["profile_id"] == profile["profile_id"]
    drafts = Drafts(w)
    for n in range(1, 9):
        drafts.save_section(rid, sid, n, "仅作接口验收的合成正文。", "接口测试判断", ["F0"])
    drafts.save_conclusion(rid, sid, "中性观察", "仅作接口验收", ["测试"], ["测试"], ["测试"],
                           valuation_unavailable_reason="本测试不提供估值假设")
    uncited = Reports(w).build_report(rid, ["md"])
    report = json.loads((Path(uncited["outputs"]["md"]["path"]).parent / "report.json").read_text("utf-8"))
    assert not any(x["reference"] == profile["artifact_id"] for x in report["request_metadata"]["agent_citations"])
    drafts.save_section(rid, sid, 1, "引用画像的接口测试正文。", "接口测试判断", [profile["artifact_id"]])
    cited = Reports(w).build_report(rid, ["md"])
    report = json.loads((Path(cited["outputs"]["md"]["path"]).parent / "report.json").read_text("utf-8"))
    saved = next(x["detail"] for x in report["request_metadata"]["agent_citations"] if x["reference"] == profile["artifact_id"])
    assert saved["facts"][0]["citations"][0]["snapshot_sha256"]
    assert saved["acceptance"]["human_profile_acceptance"] == "pending"
    assert profile["artifact_id"] in report["claims"][0]["evidence_ids"]
    assert "第1页" in Path(cited["outputs"]["md"]["path"]).read_text("utf-8")


@pytest.mark.parametrize("change", ["value", "manifest", "fake_review", "company", "future"])
def test_supplied_verified_label_never_bypasses_replay(context, change):
    w, state, request, corpus, store, acquisition, _ = context
    profile = build_profile(ProfileInput(**request), store, corpus, acquisition.data_root)
    if change == "value": profile["series"][0]["cells"]["2023"]["value"] = "999"
    elif change == "manifest": profile["manifest_id"] = "unrelated"
    elif change == "fake_review": profile["acceptance"]["human_profile_acceptance"] = "passed"
    elif change == "company": profile["company_id"] = "000858"
    else: profile["as_of"] = "2026-09-14T00:00:00+08:00"
    with pytest.raises(ResearchError):
        w.save_business_profile(state["research_id"], profile)
    assert not w.artifacts(state["research_id"], "business_profile")


def test_original_must_belong_to_frozen_pack_and_be_rechecked(context):
    w, state, request, _, _, _, original = context
    rid = state["research_id"]
    artifact = BusinessProfiles(w).build(rid, request)
    original.write_bytes(b"changed original")
    with pytest.raises(ValueError):
        Drafts(w).save_section(rid, state["snapshot_id"], 1, "正文", "判断", [artifact["artifact_id"]])


def test_profile_cannot_be_rebound_to_another_research_snapshot(context):
    w, state, request, *_ = context
    rid = state["research_id"]
    artifact = BusinessProfiles(w).build(rid, request)
    changed = deepcopy(artifact)
    changed["snapshot_id"] = "other"
    with pytest.raises(ResearchError, match="snapshot_mismatch"):
        BusinessProfiles(w).validate_saved(rid, changed)
    with w.connect() as db:
        payload = json.loads(db.execute("SELECT payload FROM research_artifacts WHERE id=?", (artifact["artifact_id"],)).fetchone()[0])
        payload["facts"][0]["value"] = "999"
        db.execute("UPDATE research_artifacts SET payload=? WHERE id=?", (json.dumps(payload), artifact["artifact_id"]))
    with pytest.raises(ResearchError, match="artifact_integrity_failed"):
        BusinessProfiles(w).get(rid)


def test_matching_company_alone_does_not_admit_unadopted_originals(context):
    w, state, request, *_ = context
    rid = state["research_id"]
    current, revision = w.task(rid)
    pack = Path(current["pack_path"])
    core = json.loads((pack / "core-pack.json").read_text("utf-8"))
    core["supplemental_evidence"] = []
    _write_json(pack / "core-pack.json", core)
    manifest = json.loads((pack / "manifest.json").read_text("utf-8"))
    manifest["output_hashes"]["core-pack.json"] = sha(pack / "core-pack.json")
    _write_json(pack / "manifest.json", manifest)
    # Simulate adoption of a different frozen input that lacks this original.
    current.update(snapshot_id="without-original", manifest_sha256=sha(pack / "manifest.json"))
    w._save(current, revision)
    with pytest.raises(ResearchError, match="original_not_in_research_snapshot"):
        BusinessProfiles(w).build(rid, request)


def test_missing_registration_never_silently_chooses_a_database(context):
    w, state, request, *_ = context
    w.config.pop("business_evidence_sources")
    with pytest.raises(ResearchError, match="source_unavailable"):
        BusinessProfiles(w).build(state["research_id"], request)


def test_existing_artifact_keeps_its_source_when_configuration_changes(context):
    w, state, request, *_ = context
    result = BusinessProfiles(w).build(state["research_id"], request)
    w.config["business_evidence_sources"] = []
    restored = ResearchWorkspace(w.root, w.config)
    assert BusinessProfiles(restored).get(state["research_id"])["profile_id"] == result["profile_id"]


def test_snapshot_change_during_profile_validation_rejects_artifact_write(context, monkeypatch):
    w, state, request, *_ = context
    original_write = w.artifact
    def adopt_then_write(research_id, kind, payload, **kw):
        current, revision = w.task(research_id)
        current["snapshot_id"] = "adopted-after-validation"
        w._save(current, revision)
        return original_write(research_id, kind, payload, **kw)
    monkeypatch.setattr(w, "artifact", adopt_then_write)
    with pytest.raises(ResearchError, match="snapshot_changed_during_artifact_write"):
        BusinessProfiles(w).build(state["research_id"], request)
    assert not w.artifacts(state["research_id"], "business_profile")
