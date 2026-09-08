from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AnnouncementRecord,
    DimensionalFactRecord,
    DocumentRecord,
    EventRecord,
    FactRecord,
    ForecastSnapshot,
    IndustryFactRecord,
    PeerSetVersion,
    ReportVersion,
    SourceRecord,
    SyncResult,
    VerificationRecord,
    VerificationStatus,
)
from .registry import PROJECT_ROOT
from .acquisition.migrations import MigrationCoordinator, MigrationError


DEFAULT_DB_PATH = PROJECT_ROOT / "var" / "analysis.db"


class StorageError(RuntimeError):
    pass


def _utc_iso(value: datetime) -> str:
    aware = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return aware.isoformat()


class ReportStorage:
    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        *,
        migration_data_root: Path | str | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.db_path = Path(db_path)
        self.migration_data_root = Path(migration_data_root or self.db_path.parent)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=max(self.busy_timeout_ms, 1) / 1000
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        try:
            MigrationCoordinator(
                self.db_path, busy_timeout_ms=self.busy_timeout_ms
            ).migrate(data_root=self.migration_data_root)
        except MigrationError as exc:
            raise StorageError(str(exc)) from exc

    def _initialize_legacy_v5(self) -> None:
        """Frozen reference for the pre-v6 schema; migrations.py is authoritative."""
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    parent_report_id TEXT,
                    ticker TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    data_snapshot_id TEXT NOT NULL,
                    method_bundle_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(ticker, version)
                );
                CREATE INDEX IF NOT EXISTS idx_reports_ticker ON reports(ticker, version DESC);

                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    fact_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    metric_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_facts_ticker_metric ON facts(ticker, metric_id, as_of DESC);

                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_results (
                    sync_result_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sync_results_ticker
                    ON sync_results(ticker, created_at DESC);

                CREATE TABLE IF NOT EXISTS verification_records (
                    verification_id TEXT PRIMARY KEY,
                    sync_result_id TEXT,
                    left_fact_id TEXT NOT NULL,
                    right_fact_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(sync_result_id) REFERENCES sync_results(sync_result_id)
                );
                CREATE INDEX IF NOT EXISTS idx_verification_left
                    ON verification_records(left_fact_id);
                CREATE INDEX IF NOT EXISTS idx_verification_right
                    ON verification_records(right_fact_id);

                CREATE TABLE IF NOT EXISTS dimensional_facts (
                    dimensional_fact_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    metric_id TEXT NOT NULL,
                    dimension_type TEXT NOT NULL,
                    period_end TEXT,
                    available_at TEXT NOT NULL,
                    data_snapshot_id TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dimensional_facts_lookup
                    ON dimensional_facts(ticker, metric_id, dimension_type, period_end DESC);
                CREATE INDEX IF NOT EXISTS idx_dimensional_facts_snapshot
                    ON dimensional_facts(data_snapshot_id);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    root_event_id TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    announced_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    data_snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_lookup
                    ON events(ticker, event_type, announced_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_root
                    ON events(root_event_id, announced_at);
                CREATE INDEX IF NOT EXISTS idx_events_snapshot
                    ON events(data_snapshot_id);

                CREATE TABLE IF NOT EXISTS industry_facts (
                    industry_fact_id TEXT PRIMARY KEY,
                    industry_code TEXT NOT NULL,
                    metric_id TEXT NOT NULL,
                    period_end TEXT,
                    available_at TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    data_snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_industry_facts_lookup
                    ON industry_facts(industry_code, metric_id, period_end DESC);
                CREATE INDEX IF NOT EXISTS idx_industry_facts_snapshot
                    ON industry_facts(data_snapshot_id);

                CREATE TABLE IF NOT EXISTS forecast_snapshots (
                    forecast_snapshot_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    metric_id TEXT NOT NULL,
                    target_period TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    data_snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_lookup
                    ON forecast_snapshots(ticker, metric_id, target_period, as_of DESC);
                CREATE INDEX IF NOT EXISTS idx_forecast_snapshots_snapshot
                    ON forecast_snapshots(data_snapshot_id);

                CREATE TABLE IF NOT EXISTS peer_set_versions (
                    peer_set_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    target_ticker TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    data_snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(peer_set_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_peer_set_versions_lookup
                    ON peer_set_versions(target_ticker, as_of DESC);
                CREATE INDEX IF NOT EXISTS idx_peer_set_versions_snapshot
                    ON peer_set_versions(data_snapshot_id);

                CREATE TABLE IF NOT EXISTS announcements (
                    announcement_record_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    announcement_id TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    announced_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    classified_event_type TEXT,
                    data_snapshot_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_announcements_lookup
                    ON announcements(ticker, announced_at DESC);
                CREATE INDEX IF NOT EXISTS idx_announcements_canonical
                    ON announcements(canonical_key);
                CREATE INDEX IF NOT EXISTS idx_announcements_event_type
                    ON announcements(ticker, classified_event_type, announced_at DESC);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                ("0002_research_data_contracts", datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                ("0003_announcement_index", datetime.now(timezone.utc).isoformat()),
            )
            event_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "canonical_key" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN canonical_key TEXT")
            for row in connection.execute(
                "SELECT event_id, payload FROM events WHERE canonical_key IS NULL OR canonical_key=''"
            ).fetchall():
                event = EventRecord.model_validate_json(row["payload"])
                canonical_key = str(
                    event.metadata.get("canonical_announcement_key")
                    or event.event_id
                )
                connection.execute(
                    "UPDATE events SET canonical_key=? WHERE event_id=?",
                    (canonical_key, event.event_id),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_canonical ON events(canonical_key)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                ("0004_event_snapshot_versions", datetime.now(timezone.utc).isoformat()),
            )
            dimensional_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(dimensional_facts)"
                ).fetchall()
            }
            if "verification_status" not in dimensional_columns:
                connection.execute(
                    "ALTER TABLE dimensional_facts ADD COLUMN verification_status TEXT"
                )
            for row in connection.execute(
                "SELECT dimensional_fact_id, payload FROM dimensional_facts "
                "WHERE verification_status IS NULL OR verification_status=''"
            ).fetchall():
                fact = DimensionalFactRecord.model_validate_json(row["payload"])
                connection.execute(
                    "UPDATE dimensional_facts SET verification_status=? "
                    "WHERE dimensional_fact_id=?",
                    (fact.verification_status.value, row["dimensional_fact_id"]),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dimensional_facts_verification "
                "ON dimensional_facts(ticker, verification_status, available_at DESC)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                (
                    "0005_dimensional_verification_status",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.execute("PRAGMA user_version=5")
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(document_id UNINDEXED, ticker UNINDEXED, title, body, tokenize='unicode61')"
                )
            except sqlite3.OperationalError:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS documents_fts(document_id TEXT PRIMARY KEY, ticker TEXT, title TEXT, body TEXT)"
                )

    def next_version(self, ticker: str) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM reports WHERE ticker=?", (ticker,)).fetchone()
        return int(row["version"]) + 1

    def save_report(self, report: ReportVersion) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                """
                INSERT INTO reports(
                    report_id, parent_report_id, ticker, version, created_at,
                    data_snapshot_id, method_bundle_id, status, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                    report.report_id,
                    report.parent_report_id,
                    report.ticker,
                    report.version,
                    report.created_at.isoformat(),
                    report.data_snapshot_id,
                    report.method_bundle_id,
                    report.status.value,
                    report.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"报告版本不可覆盖: {report.ticker} v{report.version}") from exc

    def get_report(self, report_id: str) -> ReportVersion:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM reports WHERE report_id=?", (report_id,)).fetchone()
        if row is None:
            raise StorageError(f"报告不存在: {report_id}")
        return ReportVersion.model_validate_json(row["payload"])

    def list_reports(self, ticker: str | None = None, limit: int = 100) -> list[ReportVersion]:
        with self._connect() as connection:
            if ticker:
                rows = connection.execute(
                    "SELECT payload FROM reports WHERE ticker=? ORDER BY version DESC LIMIT ?", (ticker, limit)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [ReportVersion.model_validate_json(row["payload"]) for row in rows]

    def save_sources(self, sources: list[SourceRecord]) -> None:
        with self._connect() as connection:
            for source in sources:
                payload = source.model_dump_json()
                existing = connection.execute("SELECT payload FROM sources WHERE source_id=?", (source.source_id,)).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(f"来源记录不可覆盖: {source.source_id}")
                connection.execute("INSERT OR IGNORE INTO sources(source_id, payload) VALUES (?, ?)", (source.source_id, payload))

    def save_facts(self, facts: list[FactRecord]) -> None:
        with self._connect() as connection:
            for fact in facts:
                payload = fact.model_dump_json()
                existing = connection.execute("SELECT payload FROM facts WHERE fact_id=?", (fact.fact_id,)).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(f"事实记录不可覆盖: {fact.fact_id}")
                connection.execute(
                    "INSERT OR IGNORE INTO facts(fact_id, ticker, metric_id, as_of, payload) VALUES (?, ?, ?, ?, ?)",
                    (fact.fact_id, fact.ticker, fact.metric_id, fact.as_of.isoformat(), payload),
                )

    def save_dimensional_facts(self, facts: list[DimensionalFactRecord]) -> None:
        with self._connect() as connection:
            for fact in facts:
                self._validate_verified_sources(
                    connection,
                    fact.verification_status,
                    fact.source_ids,
                    f"维度事实 {fact.dimensional_fact_id}",
                )
                payload = fact.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM dimensional_facts WHERE dimensional_fact_id=?",
                    (fact.dimensional_fact_id,),
                ).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(f"维度事实记录不可覆盖: {fact.dimensional_fact_id}")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO dimensional_facts(
                        dimensional_fact_id, ticker, metric_id, dimension_type,
                        period_end, available_at, data_snapshot_id,
                        verification_status, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.dimensional_fact_id,
                        fact.ticker,
                        fact.metric_id,
                        fact.dimension_type,
                        fact.period_end.isoformat() if fact.period_end else None,
                        fact.available_at.isoformat(),
                        fact.data_snapshot_id,
                        fact.verification_status.value,
                        payload,
                    ),
                )

    def save_events(self, events: list[EventRecord]) -> None:
        with self._connect() as connection:
            for event in events:
                self._validate_verified_sources(
                    connection,
                    event.verification_status,
                    event.source_ids,
                    f"事件 {event.event_id}",
                )
                payload = event.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(f"事件记录不可覆盖: {event.event_id}")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO events(
                        event_id, ticker, event_type, root_event_id, canonical_key,
                        lifecycle_state, announced_at, available_at,
                        data_snapshot_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.ticker,
                        event.event_type,
                        event.root_event_id,
                        str(
                            event.metadata.get("canonical_announcement_key")
                            or event.event_id
                        ),
                        event.lifecycle_state,
                        event.announced_at.isoformat(),
                        event.available_at.isoformat(),
                        event.data_snapshot_id,
                        payload,
                    ),
                )

    def save_industry_facts(self, facts: list[IndustryFactRecord]) -> None:
        with self._connect() as connection:
            for fact in facts:
                self._validate_verified_sources(
                    connection,
                    fact.verification_status,
                    fact.source_ids,
                    f"行业事实 {fact.industry_fact_id}",
                )
                payload = fact.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM industry_facts WHERE industry_fact_id=?",
                    (fact.industry_fact_id,),
                ).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(f"行业事实记录不可覆盖: {fact.industry_fact_id}")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO industry_facts(
                        industry_fact_id, industry_code, metric_id, period_end,
                        available_at, dataset_version, data_snapshot_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.industry_fact_id,
                        fact.industry_code,
                        fact.metric_id,
                        fact.period_end.isoformat() if fact.period_end else None,
                        fact.available_at.isoformat(),
                        fact.dataset_version,
                        fact.data_snapshot_id,
                        payload,
                    ),
                )

    def save_forecast_snapshots(self, snapshots: list[ForecastSnapshot]) -> None:
        with self._connect() as connection:
            for snapshot in snapshots:
                self._validate_verified_sources(
                    connection,
                    snapshot.verification_status,
                    snapshot.source_ids,
                    f"预测快照 {snapshot.forecast_snapshot_id}",
                )
                payload = snapshot.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM forecast_snapshots WHERE forecast_snapshot_id=?",
                    (snapshot.forecast_snapshot_id,),
                ).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(f"预测快照不可覆盖: {snapshot.forecast_snapshot_id}")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO forecast_snapshots(
                        forecast_snapshot_id, ticker, provider, metric_id,
                        target_period, as_of, available_at, data_snapshot_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.forecast_snapshot_id,
                        snapshot.ticker,
                        snapshot.provider,
                        snapshot.metric_id,
                        snapshot.target_period.isoformat(),
                        snapshot.as_of.isoformat(),
                        snapshot.available_at.isoformat(),
                        snapshot.data_snapshot_id,
                        payload,
                    ),
                )

    def save_peer_set_versions(self, peer_sets: list[PeerSetVersion]) -> None:
        with self._connect() as connection:
            for peer_set in peer_sets:
                payload = peer_set.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM peer_set_versions WHERE peer_set_id=? AND version=?",
                    (peer_set.peer_set_id, peer_set.version),
                ).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(
                        f"同行集合版本不可覆盖: {peer_set.peer_set_id}@{peer_set.version}"
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO peer_set_versions(
                        peer_set_id, version, target_ticker, as_of,
                        data_snapshot_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        peer_set.peer_set_id,
                        peer_set.version,
                        peer_set.target_ticker,
                        peer_set.as_of.isoformat(),
                        peer_set.data_snapshot_id,
                        payload,
                    ),
                )

    def save_announcements(self, announcements: list[AnnouncementRecord]) -> None:
        with self._connect() as connection:
            for announcement in announcements:
                payload = announcement.model_dump_json()
                existing = connection.execute(
                    "SELECT payload FROM announcements WHERE announcement_record_id=?",
                    (announcement.announcement_record_id,),
                ).fetchone()
                if existing is not None and existing["payload"] != payload:
                    raise StorageError(
                        f"公告元数据不可覆盖: {announcement.announcement_record_id}"
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO announcements(
                        announcement_record_id, ticker, provider, announcement_id,
                        canonical_key, announced_at, available_at,
                        classified_event_type, data_snapshot_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        announcement.announcement_record_id,
                        announcement.ticker,
                        announcement.provider,
                        announcement.announcement_id,
                        announcement.canonical_key,
                        announcement.announced_at.isoformat(),
                        announcement.available_at.isoformat(),
                        announcement.classified_event_type,
                        announcement.data_snapshot_id,
                        payload,
                    ),
                )

    @staticmethod
    def _validate_verified_sources(
        connection: sqlite3.Connection,
        status: VerificationStatus,
        source_ids: list[str],
        record_label: str,
    ) -> None:
        if status not in {
            VerificationStatus.DUAL_SOURCE,
            VerificationStatus.AUTHORITATIVE_SINGLE,
            VerificationStatus.SUPPLIER_DIRECT,
        }:
            return
        unique_ids = list(dict.fromkeys(source_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        rows = connection.execute(
            f"SELECT source_id, payload FROM sources WHERE source_id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        found = {
            row["source_id"]: SourceRecord.model_validate_json(row["payload"])
            for row in rows
        }
        missing = sorted(set(unique_ids) - set(found))
        if missing:
            raise StorageError(f"{record_label} 引用了尚未保存的来源: {missing}")
        if status == VerificationStatus.DUAL_SOURCE:
            upstream_ids = {
                source.upstream_source_id or source.source_id
                for source in found.values()
            }
            if len(upstream_ids) < 2:
                raise StorageError(
                    f"{record_label} 的来源具有相同上游，不能标记为双源一致"
                )

    def save_research_records(
        self,
        *,
        dimensional_facts: list[DimensionalFactRecord] | None = None,
        events: list[EventRecord] | None = None,
        industry_facts: list[IndustryFactRecord] | None = None,
        forecast_snapshots: list[ForecastSnapshot] | None = None,
        peer_sets: list[PeerSetVersion] | None = None,
    ) -> None:
        self.save_dimensional_facts(dimensional_facts or [])
        self.save_events(events or [])
        self.save_industry_facts(industry_facts or [])
        self.save_forecast_snapshots(forecast_snapshots or [])
        self.save_peer_set_versions(peer_sets or [])

    def save_sync_result(self, result: SyncResult) -> None:
        self.save_sources(result.sources)
        self.save_facts([*result.raw_facts, *result.facts])
        self.save_research_records(
            dimensional_facts=result.dimensional_facts,
            events=result.events,
            industry_facts=result.industry_facts,
            forecast_snapshots=result.forecast_snapshots,
            peer_sets=result.peer_sets,
        )
        for document in result.documents:
            self.save_document(document)
        self.save_announcements(result.announcements)
        with self._connect() as connection:
            payload = result.model_dump_json()
            existing = connection.execute(
                "SELECT payload FROM sync_results WHERE sync_result_id=?",
                (result.sync_result_id,),
            ).fetchone()
            if existing is not None and existing["payload"] != payload:
                raise StorageError(f"同步批次不可覆盖: {result.sync_result_id}")
            connection.execute(
                "INSERT OR IGNORE INTO sync_results(sync_result_id, ticker, as_of, created_at, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    result.sync_result_id,
                    result.ticker,
                    result.as_of.isoformat(),
                    result.created_at.isoformat(),
                    payload,
                ),
            )
            for record in result.verification_records:
                self._insert_verification(connection, record, result.sync_result_id)

    @staticmethod
    def _insert_verification(
        connection: sqlite3.Connection,
        record: VerificationRecord,
        sync_result_id: str | None,
    ) -> None:
        payload = record.model_dump_json()
        existing = connection.execute(
            "SELECT payload FROM verification_records WHERE verification_id=?",
            (record.verification_id,),
        ).fetchone()
        if existing is not None and existing["payload"] != payload:
            raise StorageError(f"核验记录不可覆盖: {record.verification_id}")
        connection.execute(
            "INSERT OR IGNORE INTO verification_records(verification_id, sync_result_id, left_fact_id, right_fact_id, payload) VALUES (?, ?, ?, ?, ?)",
            (
                record.verification_id,
                sync_result_id,
                record.left_fact_id,
                record.right_fact_id,
                payload,
            ),
        )

    def get_sync_result(self, sync_result_id: str) -> SyncResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM sync_results WHERE sync_result_id=?",
                (sync_result_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"同步批次不存在: {sync_result_id}")
        return SyncResult.model_validate_json(row["payload"])

    def latest_sync_result(
        self,
        ticker: str,
        as_of=None,
        required_scope: str | None = None,
        required_records: str | None = None,
    ) -> SyncResult | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM sync_results WHERE ticker=? ORDER BY created_at DESC",
                (ticker,),
            ).fetchall()
        results = [SyncResult.model_validate_json(row["payload"]) for row in rows]
        # Legacy payloads omit this field and retain the model's compatibility
        # default. New acquisition-backed partial/failed runs set it false so an
        # auditable but incomplete run never shadows an older consumable batch.
        results = [item for item in results if item.default_consume_eligible]
        if as_of is not None:
            from datetime import timezone

            cutoff = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)
            results = [
                item
                for item in results
                if (item.as_of.replace(tzinfo=timezone.utc) if item.as_of.tzinfo is None else item.as_of.astimezone(timezone.utc))
                <= cutoff
            ]
        if required_scope is not None:
            results = [
                item
                for item in results
                if (
                    required_scope in item.scopes
                    or (
                        not item.scopes
                        and required_scope == "financials"
                        and bool(item.facts)
                    )
                )
            ]
        if required_records is not None:
            allowed = {
                "facts",
                "dimensional_facts",
                "events",
                "industry_facts",
                "forecast_snapshots",
                "peer_sets",
                "announcements",
            }
            if required_records not in allowed:
                raise StorageError(f"未知同步记录类型: {required_records}")
            results = [
                item for item in results if bool(getattr(item, required_records))
            ]
        return max(results, key=lambda item: item.created_at, default=None)

    def get_fact(self, fact_id: str) -> FactRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            raise StorageError(f"事实不存在: {fact_id}")
        return FactRecord.model_validate_json(row["payload"])

    def get_source(self, source_id: str) -> SourceRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            raise StorageError(f"来源不存在: {source_id}")
        return SourceRecord.model_validate_json(row["payload"])

    def get_dimensional_fact(self, dimensional_fact_id: str) -> DimensionalFactRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM dimensional_facts WHERE dimensional_fact_id=?",
                (dimensional_fact_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"维度事实不存在: {dimensional_fact_id}")
        return DimensionalFactRecord.model_validate_json(row["payload"])

    def list_dimensional_facts(
        self,
        ticker: str,
        metric_id: str | None = None,
        dimension_type: str | None = None,
        as_of: datetime | None = None,
        data_snapshot_id: str | None = None,
        verification_status: VerificationStatus | str | None = None,
        limit: int = 500,
    ) -> list[DimensionalFactRecord]:
        clauses = ["ticker=?"]
        values: list[object] = [ticker]
        if metric_id:
            clauses.append("metric_id=?")
            values.append(metric_id)
        if dimension_type:
            clauses.append("dimension_type=?")
            values.append(dimension_type)
        if as_of:
            clauses.append("available_at<=?")
            values.append(_utc_iso(as_of))
        if data_snapshot_id:
            clauses.append("data_snapshot_id=?")
            values.append(data_snapshot_id)
        if verification_status:
            clauses.append("verification_status=?")
            values.append(
                verification_status.value
                if isinstance(verification_status, VerificationStatus)
                else verification_status
            )
        values.append(limit)
        query = (
            "SELECT payload FROM dimensional_facts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY available_at DESC, dimensional_fact_id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [DimensionalFactRecord.model_validate_json(row["payload"]) for row in rows]

    def get_event(self, event_id: str) -> EventRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"事件不存在: {event_id}")
        return EventRecord.model_validate_json(row["payload"])

    def list_events(
        self,
        ticker: str,
        event_type: str | None = None,
        root_event_id: str | None = None,
        as_of: datetime | None = None,
        data_snapshot_id: str | None = None,
        limit: int = 500,
    ) -> list[EventRecord]:
        clauses = ["ticker=?"]
        values: list[object] = [ticker]
        if event_type:
            clauses.append("event_type=?")
            values.append(event_type)
        if root_event_id:
            clauses.append("root_event_id=?")
            values.append(root_event_id)
        if as_of:
            clauses.append("available_at<=?")
            values.append(_utc_iso(as_of))
        if data_snapshot_id:
            clauses.append("data_snapshot_id=?")
            values.append(data_snapshot_id)
        values.append(limit)
        query = (
            "SELECT payload FROM ("
            "SELECT payload, announced_at, event_id, "
            "ROW_NUMBER() OVER (PARTITION BY canonical_key ORDER BY rowid DESC) AS snapshot_rank "
            "FROM events WHERE "
            + " AND ".join(clauses)
            + ") WHERE snapshot_rank=1 "
            "ORDER BY announced_at DESC, event_id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [EventRecord.model_validate_json(row["payload"]) for row in rows]

    def get_industry_fact(self, industry_fact_id: str) -> IndustryFactRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM industry_facts WHERE industry_fact_id=?",
                (industry_fact_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"行业事实不存在: {industry_fact_id}")
        return IndustryFactRecord.model_validate_json(row["payload"])

    def list_industry_facts(
        self,
        industry_code: str,
        metric_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 500,
    ) -> list[IndustryFactRecord]:
        clauses = ["industry_code=?"]
        values: list[object] = [industry_code]
        if metric_id:
            clauses.append("metric_id=?")
            values.append(metric_id)
        if as_of:
            clauses.append("available_at<=?")
            values.append(_utc_iso(as_of))
        values.append(limit)
        query = (
            "SELECT payload FROM industry_facts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY available_at DESC, industry_fact_id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [IndustryFactRecord.model_validate_json(row["payload"]) for row in rows]

    def get_forecast_snapshot(self, forecast_snapshot_id: str) -> ForecastSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM forecast_snapshots WHERE forecast_snapshot_id=?",
                (forecast_snapshot_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"预测快照不存在: {forecast_snapshot_id}")
        return ForecastSnapshot.model_validate_json(row["payload"])

    def list_forecast_snapshots(
        self,
        ticker: str,
        metric_id: str | None = None,
        as_of: datetime | None = None,
        limit: int = 500,
    ) -> list[ForecastSnapshot]:
        clauses = ["ticker=?"]
        values: list[object] = [ticker]
        if metric_id:
            clauses.append("metric_id=?")
            values.append(metric_id)
        if as_of:
            clauses.append("available_at<=?")
            values.append(_utc_iso(as_of))
        values.append(limit)
        query = (
            "SELECT payload FROM forecast_snapshots WHERE "
            + " AND ".join(clauses)
            + " ORDER BY as_of DESC, forecast_snapshot_id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [ForecastSnapshot.model_validate_json(row["payload"]) for row in rows]

    def get_peer_set_version(self, peer_set_id: str, version: int) -> PeerSetVersion:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM peer_set_versions WHERE peer_set_id=? AND version=?",
                (peer_set_id, version),
            ).fetchone()
        if row is None:
            raise StorageError(f"同行集合版本不存在: {peer_set_id}@{version}")
        return PeerSetVersion.model_validate_json(row["payload"])

    def list_peer_set_versions(
        self,
        target_ticker: str,
        as_of: datetime | None = None,
        limit: int = 100,
    ) -> list[PeerSetVersion]:
        clauses = ["target_ticker=?"]
        values: list[object] = [target_ticker]
        if as_of:
            clauses.append("as_of<=?")
            values.append(_utc_iso(as_of))
        values.append(limit)
        query = (
            "SELECT payload FROM peer_set_versions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY as_of DESC, version DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [PeerSetVersion.model_validate_json(row["payload"]) for row in rows]

    def get_announcement(self, announcement_record_id: str) -> AnnouncementRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM announcements WHERE announcement_record_id=?",
                (announcement_record_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"公告元数据不存在: {announcement_record_id}")
        return AnnouncementRecord.model_validate_json(row["payload"])

    def list_announcements(
        self,
        ticker: str,
        event_type: str | None = None,
        as_of: datetime | None = None,
        data_snapshot_id: str | None = None,
        limit: int = 1000,
    ) -> list[AnnouncementRecord]:
        clauses = ["ticker=?"]
        values: list[object] = [ticker]
        if event_type:
            clauses.append("classified_event_type=?")
            values.append(event_type)
        if as_of:
            clauses.append("available_at<=?")
            values.append(_utc_iso(as_of))
        if data_snapshot_id:
            clauses.append("data_snapshot_id=?")
            values.append(data_snapshot_id)
        values.append(limit)
        query = (
            "SELECT payload FROM ("
            "SELECT payload, announced_at, announcement_record_id, "
            "ROW_NUMBER() OVER (PARTITION BY canonical_key ORDER BY rowid DESC) AS snapshot_rank "
            "FROM announcements WHERE "
            + " AND ".join(clauses)
            + ") WHERE snapshot_rank=1 "
            "ORDER BY announced_at DESC, announcement_record_id LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [AnnouncementRecord.model_validate_json(row["payload"]) for row in rows]

    def fact_lineage(self, fact_id: str) -> dict:
        fact = self.get_fact(fact_id)
        sources = []
        for source_id in fact.source_ids:
            try:
                sources.append(self.get_source(source_id))
            except StorageError:
                continue
        parents = []
        for parent_id in fact.derived_from_fact_ids:
            try:
                parents.append(self.fact_lineage(parent_id))
            except StorageError:
                parents.append({"missing_fact_id": parent_id})
        raw_fact_ids = [
            str(item) for item in fact.metadata.get("consolidated_from_fact_ids", [])
        ]
        raw_facts = []
        for raw_fact_id in raw_fact_ids:
            try:
                raw_facts.append(self.get_fact(raw_fact_id))
            except StorageError:
                continue
        lookup_ids = [fact_id, *raw_fact_ids]
        placeholders = ",".join("?" for _ in lookup_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload FROM verification_records WHERE left_fact_id IN ({placeholders}) OR right_fact_id IN ({placeholders}) ORDER BY verification_id",
                (*lookup_ids, *lookup_ids),
            ).fetchall()
        verifications = [VerificationRecord.model_validate_json(row["payload"]) for row in rows]
        return {
            "fact": fact.model_dump(mode="json"),
            "sources": [item.model_dump(mode="json") for item in sources],
            "derived_from": parents,
            "consolidated_from": [item.model_dump(mode="json") for item in raw_facts],
            "verifications": [item.model_dump(mode="json") for item in verifications],
        }

    def dimensional_fact_lineage(self, dimensional_fact_id: str) -> dict:
        fact = self.get_dimensional_fact(dimensional_fact_id)
        sources, missing_sources = self._resolve_sources(fact.source_ids)
        documents, missing_documents = self._resolve_documents(fact.document_ids)
        supporting_facts, missing_facts = self._resolve_facts(fact.supporting_fact_ids)
        return {
            "dimensional_fact": fact.model_dump(mode="json"),
            "sources": sources,
            "documents": documents,
            "supporting_facts": supporting_facts,
            "missing_references": [
                *missing_sources,
                *missing_documents,
                *missing_facts,
            ],
        }

    def event_lineage(self, event_id: str) -> dict:
        event = self.get_event(event_id)
        sources, missing_sources = self._resolve_sources(event.source_ids)
        document_ids = list(
            dict.fromkeys(
                [
                    *event.document_ids,
                    *(span.document_id for span in event.evidence_spans),
                ]
            )
        )
        documents, missing_documents = self._resolve_documents(document_ids)
        supporting_facts, missing_facts = self._resolve_facts(event.supporting_fact_ids)
        related_events = []
        missing_events = []
        for related_id in dict.fromkeys(
            item
            for item in (event.previous_event_id, event.root_event_id)
            if item and item != event.event_id
        ):
            try:
                related_events.append(self.get_event(related_id).model_dump(mode="json"))
            except StorageError:
                missing_events.append(f"event:{related_id}")
        return {
            "event": event.model_dump(mode="json"),
            "sources": sources,
            "documents": documents,
            "supporting_facts": supporting_facts,
            "related_events": related_events,
            "missing_references": [
                *missing_sources,
                *missing_documents,
                *missing_facts,
                *missing_events,
            ],
        }

    def industry_fact_lineage(self, industry_fact_id: str) -> dict:
        fact = self.get_industry_fact(industry_fact_id)
        sources, missing_sources = self._resolve_sources(fact.source_ids)
        component_facts = []
        missing_components = []
        for component_id in fact.component_fact_ids:
            try:
                component_facts.append(
                    {
                        "kind": "company_fact",
                        "record": self.get_fact(component_id).model_dump(mode="json"),
                    }
                )
                continue
            except StorageError:
                pass
            try:
                component_facts.append(
                    {
                        "kind": "industry_fact",
                        "record": self.get_industry_fact(component_id).model_dump(mode="json"),
                    }
                )
            except StorageError:
                missing_components.append(f"component_fact:{component_id}")
        return {
            "industry_fact": fact.model_dump(mode="json"),
            "sources": sources,
            "component_facts": component_facts,
            "missing_references": [*missing_sources, *missing_components],
        }

    def forecast_snapshot_lineage(self, forecast_snapshot_id: str) -> dict:
        snapshot = self.get_forecast_snapshot(forecast_snapshot_id)
        sources, missing_sources = self._resolve_sources(snapshot.source_ids)
        documents, missing_documents = self._resolve_documents(snapshot.report_ids)
        return {
            "forecast_snapshot": snapshot.model_dump(mode="json"),
            "sources": sources,
            "reports": documents,
            "missing_references": [*missing_sources, *missing_documents],
        }

    def peer_set_lineage(self, peer_set_id: str, version: int) -> dict:
        peer_set = self.get_peer_set_version(peer_set_id, version)
        sources, missing_sources = self._resolve_sources(peer_set.source_ids)
        return {
            "peer_set": peer_set.model_dump(mode="json"),
            "sources": sources,
            "missing_references": missing_sources,
        }

    def announcement_lineage(self, announcement_record_id: str) -> dict:
        announcement = self.get_announcement(announcement_record_id)
        sources, missing_sources = self._resolve_sources(announcement.source_ids)
        documents = []
        missing_documents = []
        if announcement.document_id:
            documents, missing_documents = self._resolve_documents(
                [announcement.document_id]
            )
        events = []
        missing_events = []
        for event_id in announcement.event_ids:
            try:
                events.append(self.get_event(event_id).model_dump(mode="json"))
            except StorageError:
                missing_events.append(f"event:{event_id}")
        return {
            "announcement": announcement.model_dump(mode="json"),
            "sources": sources,
            "documents": documents,
            "events": events,
            "missing_references": [
                *missing_sources,
                *missing_documents,
                *missing_events,
            ],
        }

    def _resolve_sources(self, source_ids: list[str]) -> tuple[list[dict], list[str]]:
        resolved = []
        missing = []
        for source_id in dict.fromkeys(source_ids):
            try:
                resolved.append(self.get_source(source_id).model_dump(mode="json"))
            except StorageError:
                missing.append(f"source:{source_id}")
        return resolved, missing

    def _resolve_documents(self, document_ids: list[str]) -> tuple[list[dict], list[str]]:
        resolved = []
        missing = []
        for document_id in dict.fromkeys(document_ids):
            try:
                resolved.append(self.get_document(document_id).model_dump(mode="json"))
            except StorageError:
                missing.append(f"document:{document_id}")
        return resolved, missing

    def _resolve_facts(self, fact_ids: list[str]) -> tuple[list[dict], list[str]]:
        resolved = []
        missing = []
        for fact_id in dict.fromkeys(fact_ids):
            try:
                resolved.append(self.get_fact(fact_id).model_dump(mode="json"))
            except StorageError:
                missing.append(f"fact:{fact_id}")
        return resolved, missing

    def save_document(self, document: DocumentRecord) -> None:
        with self._connect() as connection:
            payload = document.model_dump_json()
            existing = connection.execute("SELECT payload FROM documents WHERE document_id=?", (document.document_id,)).fetchone()
            if existing is not None and existing["payload"] != payload:
                raise StorageError(f"文档记录不可覆盖: {document.document_id}")
            connection.execute(
                "INSERT OR IGNORE INTO documents(document_id, ticker, payload) VALUES (?, ?, ?)",
                (document.document_id, document.ticker, payload),
            )
            body = Path(document.text_path).read_text(encoding="utf-8", errors="replace")
            connection.execute("DELETE FROM documents_fts WHERE document_id=?", (document.document_id,))
            connection.execute(
                "INSERT INTO documents_fts(document_id, ticker, title, body) VALUES (?, ?, ?, ?)",
                (document.document_id, document.ticker, document.title, body),
            )

    def get_document(self, document_id: str) -> DocumentRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if row is None:
            raise StorageError(f"文档不存在: {document_id}")
        return DocumentRecord.model_validate_json(row["payload"])

    def search_documents(self, query: str, ticker: str | None = None, limit: int = 20) -> list[dict]:
        if not query.strip():
            return []
        with self._connect() as connection:
            try:
                if ticker:
                    rows = connection.execute(
                        "SELECT document_id, ticker, title, snippet(documents_fts, 3, '[', ']', '...', 24) AS snippet FROM documents_fts WHERE documents_fts MATCH ? AND ticker=? LIMIT ?",
                        (query, ticker, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT document_id, ticker, title, snippet(documents_fts, 3, '[', ']', '...', 24) AS snippet FROM documents_fts WHERE documents_fts MATCH ? LIMIT ?",
                        (query, limit),
                    ).fetchall()
            except sqlite3.OperationalError:
                pattern = f"%{query}%"
                if ticker:
                    rows = connection.execute(
                        "SELECT document_id, ticker, title, substr(body, 1, 300) AS snippet FROM documents_fts WHERE (title LIKE ? OR body LIKE ?) AND ticker=? LIMIT ?",
                        (pattern, pattern, ticker, limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT document_id, ticker, title, substr(body, 1, 300) AS snippet FROM documents_fts WHERE title LIKE ? OR body LIKE ? LIMIT ?",
                        (pattern, pattern, limit),
                    ).fetchall()
        return [dict(row) for row in rows]

    def reset_for_tests(self) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        self._initialize()


def report_diff(current: ReportVersion, previous: ReportVersion | None) -> dict:
    if previous is None:
        return {"report_id": current.report_id, "changes": ["初始报告版本"]}
    changes = []
    if current.data_snapshot_id != previous.data_snapshot_id:
        changes.append("数据快照变化")
    if current.method_bundle_id != previous.method_bundle_id:
        changes.append("方法集合变化")
    if current.conclusion.rating != previous.conclusion.rating:
        changes.append(f"评级: {previous.conclusion.rating.value} -> {current.conclusion.rating.value}")
    for field in ("fair_value_low", "fair_value_base", "fair_value_high"):
        old = getattr(previous.conclusion, field)
        new = getattr(current.conclusion, field)
        if old != new:
            changes.append(f"{field}: {old} -> {new}")
    old_facts = {fact.fact_id: fact.model_dump(mode="json") for fact in previous.facts}
    new_facts = {fact.fact_id: fact.model_dump(mode="json") for fact in current.facts}
    if old_facts != new_facts:
        changes.append("事实集合变化")
    if [item.model_dump(mode="json") for item in current.assumptions] != [item.model_dump(mode="json") for item in previous.assumptions]:
        changes.append("情景假设变化")
    return {"report_id": current.report_id, "parent_report_id": previous.report_id, "changes": changes or ["无实质变化"]}
