import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from analysis.structured.materialization_replay import _export_result
from analysis.structured.research_lite import _discover_projection, _selected_records_at, build_lite_pack, materialize_cache
from analysis.structured.research_projection import build_research_projection
from analysis.structured.scope import LITE_PROFILE_ID
from test_materialization import Fixture


def text_row(fixture, dataset, raw, value, **fields):
    return fixture.add(dataset, raw, value, row=fields, rule={
        "standard_field": raw, "multiplier": "1", "nature": "source_text",
        "stored_unit": "text", "original_unit": "text", "unit": "text", "period_kind": "instant",
        "value_kind": "stock", "definition_id": "test-text"})


def source_fixture():
    fixture = Fixture()
    fixture.list_acquisition_coverage = lambda **_: [
        {"company_id": "company:600519", "dataset_id": "repurchase", "status": "no_data",
         "coverage_record_id": "coverage-empty", "query_complete": True, "reason_code": "empty_response"}]
    return fixture


def test_formal_export_carries_scoped_text_and_outcomes_and_excludes_stale_legacy_files(tmp_path):
    fixture = source_fixture()
    fixture.add()
    text_row(fixture, "company_basic", "MAIN_BUSINESS", "生产和销售白酒", ORG_NAME="贵州茅台",
             UNREGISTERED_FIELD="must not leak")
    text_row(fixture, "management_roster", "PERSON_NAME", "test person", POSITION="董事")
    text_row(fixture, "customers_peer", "AMOUNT", 20, RANK=1, TYPE="supplier", SUM_AMOUNT=100)
    research = build_research_projection(fixture, fixture, "run-1", research_profile_id=LITE_PROFILE_ID)
    company = next(row for row in research["records"] if row["dataset_id"] == "company_basic")
    assert company["fields"] == {"ORG_NAME": "贵州茅台", "MAIN_BUSINESS": "生产和销售白酒"}
    assert company["numeric_consumption"] == "only_selected_fact_ids"
    assert company["source_ids"] and company["snapshot_sha256"] and company["available_at"]
    assert len(research["records"]) == 4
    assert research["acquisition_coverage"][0]["query_complete"] is True
    folder = tmp_path / "source" / "600519"
    _export_result(fixture.run(), folder, "ns-1", research_projection=research)
    (folder / "question-coverage.jsonl").write_text('{"state":"ready","stale":true}\n', encoding="utf8")
    (folder / "next-work.json").write_text('{"items":[{"stale":true}]}', encoding="utf8")
    (folder / "coverage-facts.jsonl").write_text('invalid legacy content', encoding="utf8")
    discovered = _discover_projection(tmp_path / "source", "600519", "primary", 0)
    assert discovered["records"] and discovered["facts"]
    assert discovered["coverage"] == [] and discovered["work"] == []
    assert "coverage-facts.jsonl" not in discovered["files"]
    assert "acquisition-coverage.jsonl" in discovered["files"]
    (folder / "normalized-records.jsonl").write_text("", encoding="utf8")
    with pytest.raises(ValueError, match="export_hash_mismatch"):
        _discover_projection(tmp_path / "source", "600519", "primary", 0)


@pytest.mark.parametrize("failure", ["failed_attempt", "wrong_namespace", "denied", "late_record"])
def test_unvalidated_text_is_never_promoted_to_research_input(failure):
    fixture = source_fixture()
    record, _, snapshot = text_row(fixture, "company_basic", "MAIN_BUSINESS", "white spirit")
    options = {}
    if failure == "failed_attempt":
        record["_committed_attempt_outcome"] = "parse_failed"
    elif failure == "wrong_namespace":
        snapshot.storage_namespace_id = "wrong"
    elif failure == "denied":
        snapshot.policy_decision = "denied"
    else:
        options = {"strict_historical": True, "as_of": datetime(2026, 9, 9, tzinfo=timezone.utc)}
    result = build_research_projection(fixture, fixture, "run-1", **options)
    assert not result["records"] and result["gaps"]
    assert result["gaps"][0]["record_version_id"] == record["record_version_id"]


def test_formal_events_keep_original_times_terms_lineage_and_sources_through_bridge(tmp_path):
    from analysis.structured.reporting_bridge import build_report_request

    fixture = source_fixture()
    fixture.add("dividend", "PRETAX_BONUS_RMB", "10", row={"NOTICE_DATE": "2026-08-01", "ASSIGN_PROGRESS": "预案"})
    result = fixture.run()
    assert len(result.events) == 1
    research = build_research_projection(fixture, fixture, "run-1", research_profile_id=LITE_PROFILE_ID)
    _export_result(result, tmp_path / "source" / "600519", "ns-1", research_projection=research)
    built = build_lite_pack(input_root=tmp_path / "source", ticker="600519", as_of=date(2026, 9, 13),
                           output_root=tmp_path / "packs", max_tokens=100000)
    request = build_report_request(built["pack_dir"])
    assert request.events == list(result.events)
    assert set(result.events[0].source_ids).issubset({source.source_id for source in request.sources})
    assert request.events[0].announced_at != request.events[0].available_at
    assert request.events[0].metadata["original_value"] == "10"


def test_cache_upgrades_once_and_rebuilds_missing_required_artifact_without_new_acquisition(tmp_path, monkeypatch):
    import analysis.acquisition.repository as repository_module
    import analysis.structured.materialization as materialization_module
    import analysis.structured.storage as storage_module
    import analysis.structured.interpretation as interpretation_module

    db = tmp_path / "cache.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE storage_namespaces(namespace_id TEXT)")
        connection.execute("INSERT INTO storage_namespaces VALUES ('ns-1')")
        connection.execute("CREATE TABLE structured_run_contexts(run_id TEXT,ticker TEXT)")
        connection.execute("INSERT INTO structured_run_contexts VALUES ('run-1','600519')")
    fixture = source_fixture()
    fixture.add()
    result = fixture.run()
    calls = []

    class Materializer:
        def __init__(self, *_):
            pass

        def materialize(self, run_id, **kwargs):
            calls.append((run_id, kwargs))
            return result

    monkeypatch.setattr(storage_module, "StructuredStorage", lambda *args, **kwargs: fixture)
    monkeypatch.setattr(repository_module, "AcquisitionRepository", lambda *args, **kwargs: fixture)
    monkeypatch.setattr(materialization_module, "StructuredFactMaterializer", Materializer)
    monkeypatch.setattr(interpretation_module, "load_interpretation", lambda *args: {"content_sha256": "contract-v1"})
    output = tmp_path / "output"
    company = output / "600519"
    _export_result(result, company, "ns-1")
    (company / "cache-binding.json").write_text('{"old_cache":true}', encoding="utf8")

    def run(day=date(2026, 9, 13)):
        return materialize_cache(db, tmp_path / "data", output, day, research_profile_id=LITE_PROFILE_ID)

    run()
    run()
    assert len(calls) == 1 and (company / "normalized-records.jsonl").is_file()
    (company / "normalized-records.jsonl").unlink()
    run()
    assert len(calls) == 2
    run(date(2026, 9, 14))
    assert len(calls) == 3
    assert calls[-1][1]["as_of"].date() == date(2026, 9, 14)


def test_fact_only_formal_export_does_not_reuse_legacy_raw_records(tmp_path):
    fixture = source_fixture()
    fixture.add()
    folder = tmp_path / "600519"
    folder.mkdir()
    (folder / "normalized-records.jsonl").write_text('{"company":"wrong","record_id":"stale"}\n', encoding="utf8")
    _export_result(fixture.run(), folder, "ns-1")
    projection = _discover_projection(tmp_path, "600519", "primary", 0)
    assert projection["records"] == []
    assert "normalized-records.jsonl" not in projection["files"]


def test_catalog_prerequisite_rows_are_not_business_records():
    fixture = source_fixture()
    fixture.add()
    fixture.jobs[0]["purpose"] = "report_catalog"
    result = build_research_projection(fixture, fixture, "run-1")
    assert not result["records"]


def test_record_selection_excludes_future_and_only_follows_explicit_revisions():
    base = {"storage_namespace_id": "ns", "dataset_id": "company_basic", "row_key": "company",
            "source_definition_id": "source", "source_definition_version": "1", "fields": {"MAIN_BUSINESS": "old"},
            "record_id": "old", "available_at": "2026-09-01T00:00:00+00:00"}
    new = {**base, "record_id": "new", "available_at": "2026-09-02T00:00:00+00:00",
           "fields": {"MAIN_BUSINESS": "new"}, "supersedes_record_version_id": "old"}
    future = {**new, "record_id": "future", "available_at": "2026-10-02T00:00:00+00:00"}
    conflicts = []
    chosen = _selected_records_at([base, new, future], date(2026, 9, 13), conflicts)
    assert [r["record_id"] for r in chosen] == ["new"] and not conflicts
    new.pop("supersedes_record_version_id")
    chosen = _selected_records_at([base, new], date(2026, 9, 13), conflicts)
    assert len(chosen) == 2 and conflicts[0]["resolved"] is False
