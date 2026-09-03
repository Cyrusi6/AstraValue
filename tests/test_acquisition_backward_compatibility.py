from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from analysis.acquisition.migrations import MigrationCoordinator
from analysis.exports import render_html, render_markdown
from analysis.models import DocumentRecord, ReportVersion, SourceRecord, SyncResult
from analysis.reporting import ReportBuilder
from analysis.storage import ReportStorage


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


def _create_v4_shell(path: Path) -> None:
    """Create the smallest supported v4 shape before the public v5 step."""

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE dimensional_facts (
                dimensional_fact_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                dimension_type TEXT NOT NULL,
                period_end TEXT,
                available_at TEXT NOT NULL,
                data_snapshot_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            PRAGMA user_version=4;
            """
        )


@pytest.fixture(params=("fresh", "v4", "v5", "v6"))
def supported_database(request, tmp_path):
    """Frozen supported-version matrix used by the compatibility contract."""

    state = request.param
    path = tmp_path / f"{state}.db"
    if state != "fresh":
        _create_v4_shell(path)
    if state in {"v5", "v6"}:
        MigrationCoordinator(path).apply_0005()
    if state == "v6":
        MigrationCoordinator(path).migrate(data_root=tmp_path / "setup-data")
    before = path.read_bytes() if path.exists() else b""
    return state, path, before


@pytest.fixture
def frozen_legacy_payloads(demo_request):
    """Pre-acquisition JSON payloads with no v6-only reference fields."""

    source_payload = {
        "source_id": "legacy-source",
        "name": "旧权威来源",
        "url": "https://example.invalid/notice.pdf",
        "retrieved_at": NOW.isoformat(),
        "document_hash": "a" * 64,
    }
    document_payload = {
        "document_id": "legacy-document",
        "ticker": "600519",
        "title": "旧文档",
        "archived_path": "raw/legacy.pdf",
        "text_path": "raw/legacy.pdf.txt",
        "sha256": "a" * 64,
        "source": source_payload,
    }
    sync_payload = {
        "sync_result_id": "legacy-sync",
        "ticker": "600519",
        "provider_results": {"official": "历史自由文本"},
        "as_of": NOW.isoformat(),
        "created_at": NOW.isoformat(),
    }
    report = ReportBuilder().build(demo_request).model_copy(
        update={"report_id": "legacy-report-v5", "created_at": NOW}
    )
    report_payload = report.model_dump(mode="json")
    for source in report_payload["audit"]["sources"]:
        for field in (
            "source_definition_id",
            "source_definition_version",
            "raw_resource_snapshot_id",
            "canonical_resource_id",
            "available_at",
        ):
            source.pop(field, None)
    return {
        "source": json.dumps(source_payload, ensure_ascii=False, separators=(",", ":")),
        "document": json.dumps(document_payload, ensure_ascii=False, separators=(",", ":")),
        "sync": json.dumps(sync_payload, ensure_ascii=False, separators=(",", ":")),
        "report": json.dumps(report_payload, ensure_ascii=False, separators=(",", ":")),
    }


def _database_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def test_supported_fresh_v4_v5_v6_matrix_reaches_v6_without_rewriting_v6(
    supported_database, tmp_path
):
    state, path, before = supported_database

    MigrationCoordinator(path).migrate(data_root=tmp_path / "migration-data")

    assert _database_version(path) == 6
    if state == "v6":
        assert path.read_bytes() == before


def test_frozen_legacy_sync_source_and_document_payloads_keep_defaults():
    source_payload = {
        "source_id": "legacy-source",
        "name": "旧权威来源",
        "url": "https://example.invalid/notice.pdf",
        "retrieved_at": NOW.isoformat(),
        "document_hash": "a" * 64,
    }
    document_payload = {
        "document_id": "legacy-document",
        "ticker": "600519",
        "title": "旧文档",
        "archived_path": "raw/legacy.pdf",
        "text_path": "raw/legacy.pdf.txt",
        "sha256": "a" * 64,
        "source": source_payload,
    }
    sync_payload = {
        "sync_result_id": "legacy-sync",
        "ticker": "600519",
        "provider_results": {"official": "历史自由文本"},
        "as_of": NOW.isoformat(),
        "created_at": NOW.isoformat(),
    }

    source = SourceRecord.model_validate(source_payload)
    document = DocumentRecord.model_validate(document_payload)
    sync = SyncResult.model_validate(sync_payload)

    assert source.raw_resource_snapshot_id is None
    assert document.raw_resource_snapshot_id is None
    assert document.document_version == 1
    assert sync.acquisition_status == "legacy_unassessed"
    assert sync.provider_results_authority == "legacy_unassessed"
    assert sync.provider_results == sync_payload["provider_results"]


def test_v5_frozen_models_and_report_exports_are_byte_stable_after_v6_migration(
    tmp_path, frozen_legacy_payloads
):
    db_path = tmp_path / "legacy-models-v5.db"
    _create_v4_shell(db_path)
    MigrationCoordinator(db_path).apply_0005()
    source = SourceRecord.model_validate_json(frozen_legacy_payloads["source"])
    document = DocumentRecord.model_validate_json(frozen_legacy_payloads["document"])
    sync = SyncResult.model_validate_json(frozen_legacy_payloads["sync"])
    report = ReportVersion.model_validate_json(frozen_legacy_payloads["report"])
    before_exports = {
        "md": render_markdown(report),
        "html": render_html(report),
    }

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO sources(source_id,payload) VALUES (?,?)",
            (source.source_id, frozen_legacy_payloads["source"]),
        )
        connection.execute(
            "INSERT INTO documents(document_id,ticker,payload) VALUES (?,?,?)",
            (document.document_id, document.ticker, frozen_legacy_payloads["document"]),
        )
        connection.execute(
            "INSERT INTO sync_results(sync_result_id,ticker,as_of,created_at,payload) "
            "VALUES (?,?,?,?,?)",
            (
                sync.sync_result_id,
                sync.ticker,
                sync.as_of.isoformat(),
                sync.created_at.isoformat(),
                frozen_legacy_payloads["sync"],
            ),
        )
        connection.execute(
            "INSERT INTO reports(report_id,parent_report_id,ticker,version,created_at,"
            "data_snapshot_id,method_bundle_id,status,payload) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                report.report_id,
                report.parent_report_id,
                report.ticker,
                report.version,
                report.created_at.isoformat(),
                report.data_snapshot_id,
                report.method_bundle_id,
                report.status.value,
                frozen_legacy_payloads["report"],
            ),
        )
    raw_before = {}
    with sqlite3.connect(db_path) as connection:
        for table, key, value in (
            ("sources", "source_id", source.source_id),
            ("documents", "document_id", document.document_id),
            ("sync_results", "sync_result_id", sync.sync_result_id),
            ("reports", "report_id", report.report_id),
        ):
            raw_before[table] = connection.execute(
                f"SELECT payload FROM {table} WHERE {key}=?", (value,)
            ).fetchone()[0]

    MigrationCoordinator(db_path).migrate(data_root=tmp_path / "migration-data")

    with sqlite3.connect(db_path) as connection:
        raw_after = {
            table: connection.execute(
                f"SELECT payload FROM {table} WHERE {key}=?", (value,)
            ).fetchone()[0]
            for table, key, value in (
                ("sources", "source_id", source.source_id),
                ("documents", "document_id", document.document_id),
                ("sync_results", "sync_result_id", sync.sync_result_id),
                ("reports", "report_id", report.report_id),
            )
        }
    expected_payloads = {
        "sources": frozen_legacy_payloads["source"],
        "documents": frozen_legacy_payloads["document"],
        "sync_results": frozen_legacy_payloads["sync"],
        "reports": frozen_legacy_payloads["report"],
    }
    assert raw_before == expected_payloads
    assert raw_after == expected_payloads

    storage = ReportStorage(db_path)
    assert storage.get_source(source.source_id).raw_resource_snapshot_id is None
    assert storage.get_document(document.document_id).raw_resource_snapshot_id is None
    loaded_sync = storage.get_sync_result(sync.sync_result_id)
    assert loaded_sync.acquisition_status == "legacy_unassessed"
    assert loaded_sync.provider_results == sync.provider_results
    loaded_report = storage.get_report(report.report_id)
    assert render_markdown(loaded_report) == before_exports["md"]
    assert render_html(loaded_report) == before_exports["html"]


def test_report_payload_roundtrip_is_unchanged_by_acquisition_models(service, demo_request):
    report = service.create_report(demo_request)
    payload = report.model_dump(mode="json")
    loaded = service.storage.get_report(report.report_id)
    assert loaded.model_dump(mode="json") == payload


def test_v6_reopen_is_noop_for_legacy_payload(service):
    result = SyncResult(
        sync_result_id="legacy-v6-reopen",
        ticker="600519",
        provider_results={"official": "do not reinterpret"},
        as_of=NOW,
        created_at=NOW,
    )
    service.storage.save_sync_result(result)
    before = service.storage.get_sync_result(result.sync_result_id).model_dump(mode="json")

    assert MigrationCoordinator(service.storage.db_path).migrate() == ()
    after = service.storage.get_sync_result(result.sync_result_id).model_dump(mode="json")

    assert after == before
    assert after["acquisition_status"] == "legacy_unassessed"
