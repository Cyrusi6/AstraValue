from __future__ import annotations

from pathlib import Path

from .models import (
    DimensionalFactRecord,
    EventRecord,
    FactRecord,
    ForecastSnapshot,
    IndustryFactRecord,
    PeerSetVersion,
)
from .registry import PROJECT_ROOT


DEFAULT_DUCKDB_PATH = PROJECT_ROOT / "var" / "timeseries.duckdb"
DEFAULT_PARQUET_ROOT = PROJECT_ROOT / "var" / "parquet"


class TimeSeriesStorageError(RuntimeError):
    pass


class TimeSeriesStore:
    """DuckDB索引加Parquet快照；SQLite仍保存报告和审计对象。"""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DUCKDB_PATH,
        parquet_root: Path | str = DEFAULT_PARQUET_ROOT,
    ) -> None:
        try:
            import duckdb
        except ImportError as exc:
            raise TimeSeriesStorageError("缺少duckdb依赖，无法启用时序事实库") from exc
        self._duckdb = duckdb
        self.db_path = Path(db_path)
        self.parquet_root = Path(parquet_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS facts_timeseries (
                    fact_id VARCHAR PRIMARY KEY,
                    ticker VARCHAR NOT NULL,
                    metric_id VARCHAR NOT NULL,
                    period_start DATE,
                    period_end DATE,
                    disclosed_at TIMESTAMPTZ,
                    as_of TIMESTAMPTZ NOT NULL,
                    value DOUBLE,
                    unit VARCHAR,
                    scope VARCHAR,
                    verification_status VARCHAR,
                    payload JSON NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_records_timeseries (
                    record_key VARCHAR PRIMARY KEY,
                    record_type VARCHAR NOT NULL,
                    record_id VARCHAR NOT NULL,
                    entity_key VARCHAR NOT NULL,
                    metric_id VARCHAR,
                    period_start DATE,
                    period_end DATE,
                    available_at TIMESTAMPTZ NOT NULL,
                    as_of TIMESTAMPTZ NOT NULL,
                    value DOUBLE,
                    unit VARCHAR,
                    verification_status VARCHAR,
                    data_snapshot_id VARCHAR NOT NULL,
                    payload JSON NOT NULL
                )
                """
            )

    def _connect(self):
        connection = self._duckdb.connect(str(self.db_path))
        connection.execute("SET enable_progress_bar=false")
        return connection

    def append_facts(self, facts: list[FactRecord]) -> None:
        if not facts:
            return
        rows = [
            (
                item.fact_id,
                item.ticker,
                item.metric_id,
                item.period_start,
                item.period_end,
                item.disclosed_at,
                item.as_of,
                item.value,
                item.unit,
                item.scope,
                item.verification_status.value,
                item.model_dump_json(),
            )
            for item in facts
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO facts_timeseries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO NOTHING
                """,
                rows,
            )

    def append_research_records(
        self,
        *,
        dimensional_facts: list[DimensionalFactRecord] | None = None,
        events: list[EventRecord] | None = None,
        industry_facts: list[IndustryFactRecord] | None = None,
        forecast_snapshots: list[ForecastSnapshot] | None = None,
        peer_sets: list[PeerSetVersion] | None = None,
    ) -> None:
        rows = self._research_rows(
            dimensional_facts=dimensional_facts or [],
            events=events or [],
            industry_facts=industry_facts or [],
            forecast_snapshots=forecast_snapshots or [],
            peer_sets=peer_sets or [],
        )
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO research_records_timeseries
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_key) DO NOTHING
                """,
                rows,
            )

    def export_research_snapshot(
        self,
        data_snapshot_id: str,
        *,
        dimensional_facts: list[DimensionalFactRecord] | None = None,
        events: list[EventRecord] | None = None,
        industry_facts: list[IndustryFactRecord] | None = None,
        forecast_snapshots: list[ForecastSnapshot] | None = None,
        peer_sets: list[PeerSetVersion] | None = None,
    ) -> Path:
        safe_snapshot = "".join(
            char for char in data_snapshot_id if char.isalnum() or char in "-_"
        )
        if not safe_snapshot:
            raise TimeSeriesStorageError("研究数据快照路径标识清洗为空")
        target_dir = (self.parquet_root / "research").resolve()
        root = self.parquet_root.resolve()
        if root not in target_dir.parents and target_dir != root:
            raise TimeSeriesStorageError("研究数据快照路径越界")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_snapshot}.parquet"
        escaped_path = str(target).replace("'", "''")
        rows = [
            row
            for row in self._research_rows(
                dimensional_facts=dimensional_facts or [],
                events=events or [],
                industry_facts=industry_facts or [],
                forecast_snapshots=forecast_snapshots or [],
                peer_sets=peer_sets or [],
            )
            if row[12] == data_snapshot_id
        ]
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TEMP TABLE snapshot_research AS
                SELECT * FROM research_records_timeseries WHERE 1=0
                """
            )
            if rows:
                connection.executemany(
                    "INSERT INTO snapshot_research VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            connection.execute(
                f"COPY (SELECT * FROM snapshot_research ORDER BY record_type, entity_key, metric_id, period_end, as_of) TO '{escaped_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        return target

    def query_research_records(
        self,
        entity_key: str,
        *,
        record_type: str | None = None,
        metric_id: str | None = None,
        as_of=None,
        limit: int = 500,
    ) -> list[dict]:
        sql = "SELECT * FROM research_records_timeseries WHERE entity_key=?"
        params: list[object] = [entity_key]
        if record_type:
            sql += " AND record_type=?"
            params.append(record_type)
        if metric_id:
            sql += " AND metric_id=?"
            params.append(metric_id)
        if as_of:
            sql += " AND available_at<=?"
            params.append(as_of)
        sql += " ORDER BY COALESCE(period_end, CAST(as_of AS DATE)) DESC, as_of DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            cursor = connection.execute(sql, params)
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def _research_rows(
        *,
        dimensional_facts: list[DimensionalFactRecord],
        events: list[EventRecord],
        industry_facts: list[IndustryFactRecord],
        forecast_snapshots: list[ForecastSnapshot],
        peer_sets: list[PeerSetVersion],
    ) -> list[tuple]:
        rows: list[tuple] = []
        rows.extend(
            (
                f"dimensional_fact:{item.dimensional_fact_id}",
                "dimensional_fact",
                item.dimensional_fact_id,
                item.ticker,
                item.metric_id,
                item.period_start,
                item.period_end,
                item.available_at,
                item.available_at,
                item.value,
                item.unit,
                item.verification_status.value,
                item.data_snapshot_id,
                item.model_dump_json(),
            )
            for item in dimensional_facts
        )
        rows.extend(
            (
                f"event:{item.event_id}",
                "event",
                item.event_id,
                item.ticker,
                item.event_type,
                item.period_start,
                item.period_end,
                item.available_at,
                item.status_updated_at,
                item.amount,
                item.currency,
                item.verification_status.value,
                item.data_snapshot_id,
                item.model_dump_json(),
            )
            for item in events
        )
        rows.extend(
            (
                f"industry_fact:{item.industry_fact_id}",
                "industry_fact",
                item.industry_fact_id,
                item.industry_code,
                item.metric_id,
                item.period_start,
                item.period_end,
                item.available_at,
                item.available_at,
                item.value,
                item.unit,
                item.verification_status.value,
                item.data_snapshot_id,
                item.model_dump_json(),
            )
            for item in industry_facts
        )
        rows.extend(
            (
                f"forecast_snapshot:{item.forecast_snapshot_id}",
                "forecast_snapshot",
                item.forecast_snapshot_id,
                item.ticker,
                item.metric_id,
                item.target_period,
                item.target_period,
                item.available_at,
                item.as_of,
                item.value,
                item.unit,
                item.verification_status.value,
                item.data_snapshot_id,
                item.model_dump_json(),
            )
            for item in forecast_snapshots
        )
        rows.extend(
            (
                f"peer_set:{item.peer_set_id}@{item.version}",
                "peer_set",
                f"{item.peer_set_id}@{item.version}",
                item.target_ticker,
                None,
                None,
                None,
                item.created_at,
                item.as_of,
                None,
                "",
                None,
                item.data_snapshot_id,
                item.model_dump_json(),
            )
            for item in peer_sets
        )
        return rows

    def export_snapshot(
        self,
        ticker: str,
        data_snapshot_id: str,
        facts: list[FactRecord],
    ) -> Path:
        safe_ticker = "".join(char for char in ticker if char.isalnum() or char in "-_")
        safe_snapshot = "".join(char for char in data_snapshot_id if char.isalnum() or char in "-_")
        if not safe_ticker or not safe_snapshot:
            raise TimeSeriesStorageError("快照路径标识清洗为空")
        target_dir = (self.parquet_root / safe_ticker).resolve()
        root = self.parquet_root.resolve()
        if root not in target_dir.parents and target_dir != root:
            raise TimeSeriesStorageError("快照路径越界")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_snapshot}.parquet"
        escaped_path = str(target).replace("'", "''")
        rows = [
            (
                item.fact_id,
                item.ticker,
                item.metric_id,
                item.period_start,
                item.period_end,
                item.disclosed_at,
                item.as_of,
                item.value,
                item.unit,
                item.scope,
                item.verification_status.value,
                item.model_dump_json(),
            )
            for item in facts
            if item.ticker == ticker
        ]
        with self._connect() as connection:
            connection.execute(
                "CREATE TEMP TABLE snapshot_facts AS SELECT * FROM facts_timeseries WHERE 1=0"
            )
            if rows:
                connection.executemany(
                    "INSERT INTO snapshot_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            connection.execute(
                f"COPY (SELECT * FROM snapshot_facts ORDER BY metric_id, period_end, as_of) TO '{escaped_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        return target

    def query(
        self,
        ticker: str,
        metric_id: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        sql = "SELECT * FROM facts_timeseries WHERE ticker=?"
        params: list[object] = [ticker]
        if metric_id:
            sql += " AND metric_id=?"
            params.append(metric_id)
        sql += " ORDER BY COALESCE(period_end, CAST(as_of AS DATE)) DESC, as_of DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            cursor = connection.execute(sql, params)
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
