from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import fitz
import httpx
import pytest

from analysis.documents import parse_research_original
from analysis.research import report_acquisition as acquisition
from analysis.research import report_materials as reports
from analysis.structured.research_lite import _evidence_payload, _load_evidence, lite_periods
from analysis.structured.scope import LITE_PROFILE_ID, load_research_profile
from orchestrator_support import envelope


CUTOFF = date(2026, 9, 13)
TITLE = {"D01": "贵州茅台2025年年度报告", "D02": "贵州茅台2026年半年度报告"}


def pdf(kind):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((35, 40), TITLE[kind], fontname="china-s", fontsize=13)
    page = doc.new_page()
    text = ("经营模式与收入确认。公司主要业务采用销售模式。分产品、分地区及分渠道披露。"
            "生产量与销售量、产能和在建工程。行业经营性信息与行业情况。关联交易及实际控制人。"
            "利润分配与股份回购。股权激励和股份支付。短期借款与使用权受到限制的资金。"
            "内部控制及审计意见。发展战略及可能面临的风险。")
    page.insert_textbox((35, 35, 550, 780), text * 3, fontname="china-s", fontsize=11)
    result = doc.tobytes()
    doc.close()
    return result


def row(kind, **overrides):
    return {"ticker": "600519", "resource_id": "cninfo:" + kind, "title": TITLE[kind],
            "url": "https://static.cninfo.com.cn/finalpage/2026/" + kind + ".PDF",
            "published_at": "2026-04-16T16:00:00+00:00" if kind == "D01" else "2026-08-14T16:00:00+00:00",
            "period": "2025-12-31" if kind == "D01" else "2026-06-30", "document_class": kind,
            "language": "zh", **overrides}


def write_inputs(root, *, kinds=("D01", "D02"), parsed=False):
    root.mkdir(parents=True)
    documents = []
    for kind in kinds:
        path = root / (kind + ".pdf")
        path.write_bytes(pdf(kind))
        document = row(kind, original_path=str(path), original_sha256=reports._sha(path), mime_type="application/pdf")
        if parsed:
            parsed_doc = parse_research_original(path, mime_type="application/pdf", output_dir=root / "parsed")
            document.update(parsed_path=parsed_doc["parsed_path"])
        documents.append(document)
    reports._write(root / "live-documents.json", {"documents": documents})
    return documents


def run(tmp_path, **kwargs):
    return reports.refresh_report_materials(ticker="600519", cutoff=CUTOFF,
        output_root=tmp_path / "output", db_path=tmp_path / "db.sqlite", data_root=tmp_path / "data", **kwargs)


def test_verified_original_and_index_reuse_never_creates_network_backend(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    documents = write_inputs(inputs, parsed=True)
    before = {p: reports._sha(p) for p in inputs.rglob("*") if p.is_file()}
    monkeypatch.setattr(reports, "ReportAcquisition", lambda **_: pytest.fail("unexpected acquisition runtime"))
    result = run(tmp_path, evidence_roots=[inputs])
    assert result["status"] == "completed" and result["query_complete"] and result["materials_ready"]
    assert not result["performed_network_io"]
    assert result["counts"]["reused"] == result["counts"]["parse_reused"] == 2
    assert not result["missing_slots"]
    all_evidence, by_hash, _ = _load_evidence([tmp_path / "output"], "600519", CUTOFF)
    selected, coverage = _evidence_payload(all_evidence, by_hash, load_research_profile(LITE_PROFILE_ID), lite_periods(CUTOFF))
    assert len(selected) == 14 and all(r["state"] == "source_text_available" for r in coverage)
    assert {r["original_sha256"] for r in all_evidence} == {d["original_sha256"] for d in documents}
    assert all(not r["numeric_formula_eligible"] and r["question_answer_status"] == "not_evaluated" for r in all_evidence)
    assert before == {p: reports._sha(p) for p in before}
    evidence_before = reports._sha(tmp_path / "output/document-evidence.jsonl")
    again = run(tmp_path, evidence_roots=[inputs])
    assert again["status"] == "completed" and reports._sha(tmp_path / "output/document-evidence.jsonl") == evidence_before


def test_selection_excludes_summary_english_future_and_chooses_actual_latest(tmp_path):
    candidates = [row("D01"), row("D02"), row("D01", resource_id="cninfo:old", title="贵州茅台2024年年度报告"),
                  row("D01", resource_id="cninfo:summary", title=TITLE["D01"] + "摘要"),
                  row("D01", resource_id="cninfo:english", title=TITLE["D01"] + "（英文版）"),
                  row("D01", resource_id="cninfo:future", title=TITLE["D01"] + "（修订版）", published_at="2026-09-14T00:00:00+08:00"),
                  row("D01", resource_id="cninfo:notice", title="关于2025年年度报告的更正公告")]
    chosen = reports._select([c for r in candidates if (c := reports._candidate(r, "600519", CUTOFF))])
    assert {r["resource_id"] for r in chosen.values()} == {"cninfo:D01", "cninfo:D02"}


def test_hash_corruption_is_rejected_and_other_document_stays_readable(tmp_path):
    inputs = tmp_path / "inputs"
    write_inputs(inputs)
    (inputs / "D01.pdf").write_bytes(b"broken")
    result = run(tmp_path, evidence_roots=[inputs], allow_network=False)
    assert result["status"] == "partial" and not result["materials_ready"]
    assert result["missing_documents"][0]["document_class"] == "D01"
    assert any("hash_mismatch" in f["reason"] for f in result["failures"])
    assert {r["document_class"] for r in _load_evidence([tmp_path / "output"], "600519", CUTOFF)[0]} == {"D02"}


def test_corrupt_parse_index_is_rebuilt_from_verified_original(tmp_path):
    inputs = tmp_path / "inputs"
    docs = write_inputs(inputs, parsed=True)
    parsed_path = Path(docs[0]["parsed_path"])
    parsed = reports._json(parsed_path)
    parsed["units"][1]["text"] = "corrupt cached text"
    reports._write(parsed_path, parsed)
    result = run(tmp_path, evidence_roots=[inputs], allow_network=False)
    assert result["status"] == "completed" and result["counts"]["parsed"] == 1
    assert result["counts"]["parse_reused"] == 1
    assert reports._json(parsed_path)["units"][1]["text"] == "corrupt cached text"


def test_request_only_organizes_registered_slot_and_body_title_is_checked(tmp_path):
    inputs = tmp_path / "inputs"
    write_inputs(inputs)
    result = run(tmp_path, evidence_roots=[inputs], allow_network=False,
                 requirements=[{"requirement_id": "lite.evidence.A.business_model"}])
    assert result["materials_ready"]
    assert {r["route_id"] for r in _load_evidence([tmp_path / "output"], "600519", CUTOFF)[0]} == {"RD01"}
    with pytest.raises(ValueError, match="unknown_reading_route"):
        run(tmp_path, requirements=[{"route_id": "arbitrary"}])
    document = reports._json(inputs / "live-documents.json")["documents"][0]
    document.update(original_path=str(inputs / "D02.pdf"), original_sha256=reports._sha(inputs / "D02.pdf"))
    reports._write(inputs / "live-documents.json", {"documents": [document]})
    mismatch = run(tmp_path / "mismatch", evidence_roots=[inputs], allow_network=False)
    assert not mismatch["materials_ready"]
    assert any("title_body_period_mismatch" in r["reason"] for r in mismatch["failures"])


def test_task_hint_route_and_checkpoint_before_work_keep_prior_outputs(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    write_inputs(inputs)
    result = run(tmp_path, evidence_roots=[inputs], allow_network=False,
                 requirements=[{"task_hint": {"route_ids": ["RD01"]}}])
    assert result["status"] == "completed"
    prior = reports._sha(tmp_path / "output/document-evidence.jsonl")
    monkeypatch.setattr(reports, "_bound", lambda _: (_ for _ in ()).throw(acquisition.ReportCheckpoint("stop")))
    interrupted = run(tmp_path, evidence_roots=[inputs], allow_network=False,
                      requirements=[{"task_hint": {"route_ids": ["RD01"]}}])
    assert interrupted["status"] == "checkpointed" and interrupted["materials_ready"]
    assert reports._sha(tmp_path / "output/document-evidence.jsonl") == prior


@pytest.mark.parametrize("kind,requirement", [("D01", "latest_annual_original"), ("D02", "latest_interim_original")])
def test_default_report_requirements_use_registered_slots_and_check_hints(tmp_path, kind, requirement):
    inputs = tmp_path / "inputs"
    write_inputs(inputs)
    period = row(kind)["period"]
    request = {"requirement_id": "lite.context." + requirement, "period": period,
               "task_hint": {"document_classes": [kind], "report_periods": [period]}}
    result = run(tmp_path, evidence_roots=[inputs], allow_network=False, requirements=[request])
    assert result["status"] == "completed" and result["materials_ready"] and not result["missing_slots"]
    bad = {**request, "task_hint": {"document_classes": ["D03"], "report_periods": [period]}}
    with pytest.raises(ValueError, match="document_class_mismatch"):
        run(tmp_path, requirements=[bad])
    bad = {**request, "task_hint": {"document_classes": [kind], "report_periods": ["2020-12-31"]}}
    with pytest.raises(ValueError, match="period_mismatch"):
        run(tmp_path, requirements=[bad])
    with pytest.raises(ValueError, match="no_registered_reading_requirement"):
        run(tmp_path, requirements=[{**request, "requirement_id": "arbitrary"}])


class FakeBackend:
    calls = []
    fail_once = None
    interrupt_once = None
    empty = False

    def __init__(self, **kwargs):
        self.root = kwargs["data_root"]
        self.requests = []

    def close(self):
        pass

    def catalog(self, *, state, save, **kwargs):
        state["run_id"] = "catalog-run"
        save()
        entries = [{**row(k), "canonical_resource_id": "cninfo:" + k} for k in ("D01", "D02")]
        return {"query_complete": True, "entries": [] if self.empty else entries, "bundle": {"entries": entries}}

    def body(self, *, entry, **kwargs):
        kind = entry["document_class"]
        self.calls.append(kind)
        if kind == self.interrupt_once:
            type(self).interrupt_once = None
            raise acquisition.ReportCheckpoint("fixture_interrupt")
        if kind == self.fail_once:
            type(self).fail_once = None
            raise TimeoutError("fixture_source_timeout")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (kind + ".pdf")
        path.write_bytes(pdf(kind))
        return {"original_path": str(path), "original_sha256": reports._sha(path), "download_status": "downloaded"}


@pytest.fixture
def fake_backend(monkeypatch):
    FakeBackend.calls, FakeBackend.fail_once, FakeBackend.interrupt_once, FakeBackend.empty = [], None, None, False
    monkeypatch.setattr(reports, "ReportAcquisition", FakeBackend)
    return FakeBackend


def test_no_data_query_completes_with_explicit_missing_documents(tmp_path, fake_backend):
    fake_backend.empty = True
    result = run(tmp_path)
    assert result["status"] == "completed" and result["query_complete"]
    assert not result["materials_ready"] and len(result["missing_documents"]) == 2
    assert fake_backend.calls == []


def test_access_challenge_stops_later_network_body_requests(tmp_path, fake_backend, monkeypatch):
    def denied(self, *, entry, **kwargs):
        self.calls.append(entry["document_class"])
        raise ValueError("report_body_failed:upstream_bot_challenge")
    monkeypatch.setattr(fake_backend, "body", denied)
    result = run(tmp_path)
    assert result["status"] == "failed" and result["source_halt"] == "report_source_access_halted"
    assert fake_backend.calls == ["D01"]


def test_refresh_fetches_only_selected_reports_and_resume_keeps_new_success(tmp_path, fake_backend):
    inputs = tmp_path / "inputs"
    write_inputs(inputs, parsed=True)
    first = run(tmp_path, evidence_roots=[inputs])
    assert first["materials_ready"] and not fake_backend.calls
    fake_backend.interrupt_once = "D02"
    refreshed = run(tmp_path, evidence_roots=[inputs], refresh=True)
    assert refreshed["status"] == "checkpointed"
    assert fake_backend.calls == ["D01", "D02"]
    resumed = run(tmp_path, evidence_roots=[inputs], refresh=True)
    assert resumed["status"] == "completed" and resumed["materials_ready"]
    assert fake_backend.calls == ["D01", "D02", "D02"]


@pytest.mark.parametrize("stop", ["failure", "interrupt", "document_bound"])
def test_resume_preserves_successful_body_and_retries_only_unfinished(tmp_path, fake_backend, stop):
    if stop == "failure":
        fake_backend.fail_once = "D01"
    elif stop == "interrupt":
        fake_backend.interrupt_once = "D02"
    first = run(tmp_path, max_documents=1 if stop == "document_bound" else 2)
    assert first["status"] == ("partial" if stop == "failure" else "checkpointed")
    assert not first["materials_ready"]
    successful = "D02" if stop == "failure" else "D01"
    assert {r["document_class"] for r in _load_evidence([tmp_path / "output"], "600519", CUTOFF)[0]} == {successful}
    count = fake_backend.calls.count(successful)
    second = run(tmp_path, max_documents=1 if stop == "document_bound" else 2)
    assert second["status"] == "completed" and second["materials_ready"]
    assert fake_backend.calls.count(successful) == count


@pytest.mark.parametrize("catalog_stop,derive_error", [(None, False), ("checkpoint", False), ("timeout", False),
    ("timeout_twice", False), ("timeout_corrupt_prefix", False), (None, True)])
def test_formal_catalog_and_selected_fetch_create_snapshots_without_other_bodies(tmp_path, monkeypatch, catalog_stop, derive_error):
    calls = []
    actual_create = acquisition.AcquisitionRuntime.create
    interrupted = False
    fail_catalog = bool(catalog_stop and catalog_stop.startswith("timeout"))
    now = datetime(2026, 9, 13, 9, tzinfo=timezone.utc)
    if derive_error:
        def unavailable(*args):
            raise ValueError("fixture_text_derivation_gap")
        monkeypatch.setattr(acquisition, "extract_announcement_text", unavailable)

    class Transport:
        def request(self, work):
            nonlocal interrupted
            calls.append(work)
            if work.url.endswith(".PDF"):
                return envelope(url=work.url, body=pdf(Path(work.url).stem), content_type="application/pdf")
            if work.query_family == "company_bootstrap":
                data = {"stockList": [{"code": "600519", "orgId": "gssh0600519", "zwjc": "贵州茅台"}]}
            else:
                rows = [{"secCode": "600519", "announcementId": k, "announcementTitle": TITLE[k],
                         "announcementTime": int(datetime.fromisoformat(row(k)["published_at"]).timestamp() * 1000),
                         "adjunctUrl": "finalpage/2026/" + k + ".PDF"} for k in ("D01", "D02")]
                rows.extend({**rows[0], "announcementId": "summary" + str(i), "announcementTitle": TITLE["D01"] + "摘要",
                             "adjunctUrl": "finalpage/2026/summary" + str(i) + ".PDF"} for i in range(29))
                lower, upper = work.form_body["seDate"].split("~")
                rows = [r for r in rows if lower <= datetime.fromtimestamp(r["announcementTime"] / 1000,
                                                                          timezone.utc).date().isoformat() <= upper]
                if fail_catalog and work.page == 2:
                    raise httpx.ReadTimeout("fixture_catalog_timeout")
                if catalog_stop == "checkpoint" and work.page == 2 and not interrupted:
                    interrupted = True
                    raise acquisition.ReportCheckpoint("interrupt_before_page_two")
                data = {"announcements": rows[(work.page - 1) * 30:work.page * 30], "totalAnnouncement": len(rows)}
            return envelope(url=work.url, body=json.dumps(data, ensure_ascii=False).encode())

        def close(self):
            pass

    def create(*args, **kwargs):
        runtime = actual_create(*args, **kwargs, workspace_root=tmp_path / "workspace",
                                sleeper=lambda _: None)
        # Only the planning clock moves; repository leases use wall time.
        runtime.clock = lambda: now
        runtime.orchestrator._transport_factory = lambda _: Transport()
        return runtime

    monkeypatch.setattr(acquisition.AcquisitionRuntime, "create", create)
    def catalog_records(run_id):
        with sqlite3.connect(tmp_path / "db.sqlite") as con:
            records = {table: [json.loads(r[0]) for r in con.execute(
                "SELECT payload FROM " + table + " WHERE run_id=?", (run_id,))]
                for table in ("acquisition_runs", "physical_query_plan_items", "acquisition_run_events",
                              "acquisition_attempts", "coverage_resolutions")}
            attempt_ids = {a["attempt_id"] for a in records["acquisition_attempts"]}
            records["proofs"] = [value for r in con.execute("SELECT payload FROM discovery_proofs")
                                  if (value := json.loads(r[0]))["attempt_id"] in attempt_ids]
            snapshot_ids = {p["discovery_snapshot_id"] for p in records["proofs"]}
            records["snapshots"] = [value for r in con.execute("SELECT payload FROM raw_resource_snapshots")
                                    if (value := json.loads(r[0]))["snapshot_id"] in snapshot_ids]
        return records

    first = run(tmp_path)
    if catalog_stop:
        assert first["status"] == ("checkpointed" if catalog_stop == "checkpoint" else "failed")
        assert not first["query_complete"]
        assert not any(w.url.endswith(".PDF") for w in calls)
        page_one_count = sum(w.query_family == "periodic_report" and w.page == 1 for w in calls)
        resume_start = len(calls)
        if fail_catalog:
            state = reports._json(tmp_path / "output/report-materials-state.json")
            failed_run_id = state["catalog"]["run_id"]
            original = catalog_records(failed_run_id)
            assert any(e["event_type"] == "finalized" and e["material_gap_count"] > 0
                       for e in original["acquisition_run_events"])
            original_hashes = {tmp_path / "data" / s["archive_relative_path"]: s["sha256"] for s in original["snapshots"]}
            assert all(reports._sha(path) == digest for path, digest in original_hashes.items())
        now += timedelta(hours=1)
        if catalog_stop == "timeout_corrupt_prefix":
            prefix = next(p for p in original["proofs"] if not p["terminal"])
            snapshot = next(s for s in original["snapshots"] if s["snapshot_id"] == prefix["discovery_snapshot_id"])
            path = tmp_path / "data" / snapshot["archive_relative_path"]
            body = path.read_bytes()
            path.write_bytes(bytes([body[0] ^ 1]) + body[1:])
            broken = run(tmp_path)
            assert broken["status"] == "failed" and not broken["query_complete"] and not broken["materials_ready"]
            assert any(f["reason"] == "report_catalog_prefix_hash_invalid" for f in broken["failures"])
            assert len(calls) == resume_start
            assert catalog_records(failed_run_id) == original
            return
        if catalog_stop == "timeout_twice":
            repeated = run(tmp_path)
            assert repeated["status"] == "failed" and not repeated["query_complete"] and not repeated["materials_ready"]
            assert all(w.query_family == "periodic_report" and w.page == 2 for w in calls[resume_start:])
            now += timedelta(hours=1)
        fail_catalog = False
        first = run(tmp_path)
        assert sum(w.query_family == "periodic_report" and w.page == 1 for w in calls) == page_one_count
        assert all(w.page == 2 and w.query_family == "periodic_report" for w in calls[resume_start:]
                   if not w.url.endswith(".PDF"))
        if catalog_stop.startswith("timeout"):
            state = reports._json(tmp_path / "output/report-materials-state.json")
            recovered = catalog_records(state["catalog"]["run_id"])
            assert len(state["catalog"]["previous_run_ids"]) == (2 if catalog_stop == "timeout_twice" else 1)
            parent = recovered["acquisition_runs"][0]
            assert parent["parent_run_id"] == state["catalog"]["previous_run_ids"][-1]
            assert parent["as_of"] != original["acquisition_runs"][0]["as_of"]
            def windows(records):
                return {(p["query_id"], p["time_start"], p["time_end"], json.dumps(p["normalized_parameters"], sort_keys=True))
                        for p in records["physical_query_plan_items"]}
            assert windows(recovered) == windows(original)
            assert len(recovered["proofs"]) == 1 and recovered["proofs"][0]["page_number"] == 2
            assert catalog_records(failed_run_id) == original
            assert all(reports._sha(path) == digest for path, digest in original_hashes.items())
            referenced = {proof_id for r in recovered["coverage_resolutions"] for proof_id in r["discovery_proof_ids"]}
            assert {p["proof_id"] for p in original["proofs"]} <= referenced
    assert first["status"] == "completed", first
    assert first["materials_ready"] and first["counts"]["fetched"] == 2
    assert [Path(w.url).stem for w in calls if w.url.endswith(".PDF")] == ["D01", "D02"]
    assert all(w.query_id in {"cninfo.company_bootstrap", "cninfo.periodic_report", "cninfo.business_announcement"} for w in calls)
    count = len(calls)
    with sqlite3.connect(tmp_path / "db.sqlite") as con:
        tables = ("acquisition_runs", "acquisition_attempts", "raw_resource_snapshots", "derived_artifacts")
        db_counts = {table: con.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in tables}
    second = run(tmp_path)
    assert second["status"] == "completed" and len(calls) == count
    with sqlite3.connect(tmp_path / "db.sqlite") as con:
        assert {table: con.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in tables} == db_counts
    live = reports._json(tmp_path / "output/live-documents.json")
    assert all(d["snapshot_id"] for d in live["documents"])
    assert all(d.get("text_derivation_error") if derive_error else d.get("derived_artifact_id") for d in live["documents"])
