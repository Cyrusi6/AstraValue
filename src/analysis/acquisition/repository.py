from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence, TypeVar

from .migrations import LATEST_SCHEMA_VERSION, MigrationCoordinator


T = TypeVar("T")


class AcquisitionStorageError(RuntimeError):
    pass


class AcquisitionNotFoundError(AcquisitionStorageError):
    pass


class ImmutableRecordError(AcquisitionStorageError):
    pass


class StorageBusyError(AcquisitionStorageError):
    pass


class LeaseConflictError(AcquisitionStorageError):
    def __init__(self, run_id: str, expires_at: str) -> None:
        super().__init__(f"run {run_id} has an active lease until {expires_at}")
        self.run_id = run_id
        self.expires_at = expires_at


class StaleLeaseError(AcquisitionStorageError):
    pass


class CheckpointConflictError(AcquisitionStorageError):
    pass


class BarrierResolutionError(AcquisitionStorageError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"cannot encode {type(value).__name__}")


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        if type(value).__name__ in {"SourceRegistry", "SourceDefinition"}:
            # Preserve the historical canonical payload for immutable v1.0/
            # v1.1 registry rows even after the v1.2 query model gains fields.
            from .models import canonical_json_bytes

            result = json.loads(canonical_json_bytes(value))
        else:
            result = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise TypeError("repository values must be Pydantic models or mappings")
    return json.loads(json.dumps(result, default=_json_default, ensure_ascii=False))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _value(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            value = data[name]
            return value.value if isinstance(value, Enum) else value
    return default


def _require(data: Mapping[str, Any], *names: str) -> Any:
    result = _value(data, *names)
    if result is None or result == "":
        raise AcquisitionStorageError(f"required field is missing: {'|'.join(names)}")
    return result


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AcquisitionStorageError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: Any, *, default: datetime | None = None) -> str:
    if value is None:
        if default is None:
            raise AcquisitionStorageError("required timestamp is missing")
        value = default
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _aware_utc(parsed).isoformat()


def _optional_iso(value: Any) -> str | None:
    return None if value is None else _iso(value)


def _token_hash(owner_token: str) -> str:
    if not owner_token:
        raise AcquisitionStorageError("owner token must not be empty")
    return hashlib.sha256(owner_token.encode("utf-8")).hexdigest()


def _load_model(model_name: str, payload: str) -> Any:
    data = json.loads(payload)
    try:
        from . import models as acquisition_models

        cls = getattr(acquisition_models, model_name)
    except (ImportError, AttributeError):
        return data
    return cls.model_validate(data)


class AcquisitionRepository:
    """SQLite v6 append-only acquisition control-plane repository."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        initialize: bool = False,
        data_root: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if initialize:
            MigrationCoordinator(
                self.db_path, busy_timeout_ms=self.busy_timeout_ms
            ).migrate(data_root=data_root)
        self._assert_v6()

    def _assert_v6(self) -> None:
        if not self.db_path.exists():
            raise AcquisitionStorageError("acquisition database is not initialized")
        with self._connect(readonly=True) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != LATEST_SCHEMA_VERSION:
            raise AcquisitionStorageError(
                f"acquisition repository requires schema v{LATEST_SCHEMA_VERSION}; got v{version}"
            )

    @contextmanager
    def _connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            if readonly:
                from urllib.parse import quote

                uri = f"file:{quote(str(self.db_path.resolve()))}?mode=ro"
                connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=max(self.busy_timeout_ms, 1) / 1000,
                    isolation_level=None,
                )
                connection.execute("PRAGMA query_only=ON")
            else:
                connection = sqlite3.connect(
                    self.db_path,
                    timeout=max(self.busy_timeout_ms, 1) / 1000,
                    isolation_level=None,
                )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            yield connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StorageBusyError("SQLite remained busy past the configured timeout") from exc
            raise
        finally:
            if "connection" in locals():
                connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _insert_immutable(
        connection: sqlite3.Connection,
        table: str,
        key_where: str,
        key_values: Sequence[Any],
        columns: Sequence[str],
        values: Sequence[Any],
        payload: str,
    ) -> bool:
        existing = connection.execute(
            f"SELECT payload FROM {table} WHERE {key_where}", tuple(key_values)
        ).fetchone()
        if existing is not None:
            if existing["payload"] != payload:
                raise ImmutableRecordError(f"{table} record cannot be overwritten")
            return False
        placeholders = ",".join("?" for _ in values)
        try:
            connection.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "unique" in message or "primary key" in message:
                raise ImmutableRecordError(
                    f"{table} immutable identity already exists"
                ) from exc
            raise AcquisitionStorageError(
                f"{table} relational constraint failed"
            ) from exc
        return True

    @staticmethod
    def _get_payload(
        connection: sqlite3.Connection,
        table: str,
        where: str,
        values: Sequence[Any],
        *,
        label: str,
    ) -> str:
        row = connection.execute(
            f"SELECT payload FROM {table} WHERE {where}", tuple(values)
        ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(f"{label} not found")
        return str(row["payload"])

    # Registry and candidates -------------------------------------------------
    def save_source_registry_version(self, registry: Any) -> None:
        data = _payload(registry)
        payload = _canonical_json(data)
        registry_id = str(_require(data, "registry_id"))
        version = str(_require(data, "registry_version", "version"))
        created_at = _iso(_value(data, "created_at", "effective_at"), default=datetime.now(timezone.utc))
        content_hash = str(
            _value(data, "content_hash", "registry_hash")
            or hashlib.sha256(payload.encode("utf-8")).hexdigest()
        )
        with self._write() as connection:
            self._insert_immutable(
                connection,
                "source_registry_versions",
                "registry_id=? AND registry_version=?",
                (registry_id, version),
                (
                    "registry_id", "registry_version", "content_hash",
                    "question_set_version", "effective_at", "created_at", "payload",
                ),
                (
                    registry_id,
                    version,
                    content_hash,
                    _value(data, "question_set_version"),
                    _optional_iso(_value(data, "effective_at")),
                    created_at,
                    payload,
                ),
                payload,
            )
            for definition in (
                *(_value(data, "definitions", default=[]) or []),
                *(_value(data, "legacy_definitions", default=[]) or []),
            ):
                definition_data = _payload(definition)
                self._save_source_definition(
                    connection,
                    definition_data,
                    _canonical_json(definition_data),
                    registry_id=registry_id,
                    registry_version=version,
                )

    save_registry_version = save_source_registry_version

    def get_source_registry_version(self, registry_id: str, version: str | int) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "source_registry_versions",
                "registry_id=? AND registry_version=?",
                (registry_id, str(version)),
                label="source registry version",
            )
        return _load_model("SourceRegistry", payload)

    get_registry_version = get_source_registry_version

    def list_source_registry_versions(
        self, *, registry_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if registry_id:
            clauses.append("registry_id=?")
            args.append(registry_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM source_registry_versions {where} "
                "ORDER BY registry_id, registry_version LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_load_model("SourceRegistry", row["payload"]) for row in rows]

    def save_source_definition_version(self, definition: Any) -> None:
        data = _payload(definition)
        payload = _canonical_json(data)
        definition_id = str(_require(data, "source_definition_id"))
        version = str(_require(data, "source_definition_version", "version"))
        created_at = _iso(
            _value(data, "created_at", "effective_at", "valid_from"),
            default=datetime.now(timezone.utc),
        )
        with self._write() as connection:
            self._save_source_definition(
                connection,
                data,
                payload,
                registry_id=_value(data, "registry_id"),
                registry_version=_value(data, "registry_version"),
            )

    def _save_source_definition(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        *,
        registry_id: str | None,
        registry_version: str | None,
    ) -> None:
        definition_id = str(_require(data, "source_definition_id"))
        version = str(_require(data, "source_definition_version", "version"))
        created_at = _iso(
            _value(data, "created_at", "effective_at", "valid_from"),
            default=datetime.now(timezone.utc),
        )
        self._insert_immutable(
            connection,
            "source_definition_versions",
            "source_definition_id=? AND source_definition_version=?",
            (definition_id, version),
            (
                "source_definition_id", "source_definition_version",
                "registry_id", "registry_version", "upstream_identity",
                "policy_status", "created_at", "payload",
            ),
            (
                definition_id,
                version,
                registry_id,
                registry_version,
                _value(data, "upstream_identity", "upstream_source_identity"),
                _value(data, "policy_status", "status", default="enabled"),
                created_at,
                payload,
            ),
            payload,
        )

    save_source_definition = save_source_definition_version

    def get_source_definition_version(self, definition_id: str, version: str | int) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "source_definition_versions",
                "source_definition_id=? AND source_definition_version=?",
                (definition_id, str(version)),
                label="source definition version",
            )
        return _load_model("SourceDefinition", payload)

    get_source_definition = get_source_definition_version

    def list_source_definition_versions(
        self,
        *,
        definition_id: str | None = None,
        policy_status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if definition_id:
            clauses.append("source_definition_id=?")
            args.append(definition_id)
        if policy_status:
            clauses.append("policy_status=?")
            args.append(policy_status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM source_definition_versions {where} "
                "ORDER BY source_definition_id, source_definition_version LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_load_model("SourceDefinition", row["payload"]) for row in rows]

    list_source_definitions = list_source_definition_versions

    def save_source_candidate(self, candidate: Any) -> None:
        data = _payload(candidate)
        payload = _canonical_json(data)
        candidate_id = str(_require(data, "candidate_id", "source_candidate_id"))
        discovered_at = _iso(
            _value(data, "discovered_at", "created_at"), default=datetime.now(timezone.utc)
        )
        with self._write() as connection:
            self._insert_immutable(
                connection,
                "source_candidates",
                "candidate_id=?",
                (candidate_id,),
                (
                    "candidate_id", "candidate_domain", "review_status",
                    "discovered_at", "created_at", "payload",
                ),
                (
                    candidate_id,
                    _value(data, "candidate_domain", "domain", "host"),
                    _value(data, "review_status", "status", default="pending_review"),
                    discovered_at,
                    _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                    payload,
                ),
                payload,
            )

    def get_source_candidate(self, candidate_id: str) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "source_candidates",
                "candidate_id=?",
                (candidate_id,),
                label="source candidate",
            )
        return _load_model("SourceCandidate", payload)

    def list_source_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]:
        where = "WHERE review_status=?" if status else ""
        args: tuple[Any, ...] = (status,) if status else ()
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM source_candidates {where} "
                "ORDER BY discovered_at, candidate_id LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_load_model("SourceCandidate", row["payload"]) for row in rows]

    # Runs, plans, coverage ---------------------------------------------------
    def save_run(self, run: Any) -> None:
        data = _payload(run)
        payload = _canonical_json(data)
        run_id = str(_require(data, "run_id", "acquisition_run_id"))
        with self._write() as connection:
            self._save_run(connection, data, payload, run_id)

    save_acquisition_run = save_run

    def _save_run(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        run_id: str,
    ) -> None:
        self._insert_immutable(
            connection,
            "acquisition_runs",
            "run_id=?",
            (run_id,),
            (
                "run_id", "ticker", "mode", "run_kind", "as_of", "registry_id",
                "registry_version", "registry_hash", "question_set_version",
                "question_set_hash", "parent_run_id", "created_at", "payload",
            ),
            (
                run_id,
                str(_require(data, "ticker")),
                str(_require(data, "mode")),
                str(_value(data, "run_kind", default="production")),
                _iso(_require(data, "as_of")),
                _value(data, "registry_id"),
                _value(data, "registry_version"),
                str(_value(data, "registry_hash", "registry_content_hash", default="")),
                str(_value(data, "question_set_version", default="")),
                _value(data, "question_set_hash"),
                _value(data, "parent_run_id"),
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def get_run(self, run_id: str) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection, "acquisition_runs", "run_id=?", (run_id,), label="run"
            )
        return _load_model("AcquisitionRun", payload)

    get_acquisition_run = get_run

    def list_runs(
        self,
        *,
        ticker: str | None = None,
        mode: str | None = None,
        run_kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]:
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (("ticker", ticker), ("mode", mode), ("run_kind", run_kind)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM acquisition_runs {where} "
                "ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_load_model("AcquisitionRun", row["payload"]) for row in rows]

    list_acquisition_runs = list_runs

    def append_run_event(
        self, event: Any, *, owner_token: str | None = None
    ) -> None:
        data = _payload(event)
        payload = _canonical_json(data)
        with self._write() as connection:
            epoch = _value(data, "lease_epoch")
            if epoch is not None:
                self._assert_lease(
                    connection,
                    str(_require(data, "run_id")),
                    int(epoch),
                    owner_token=owner_token,
                )
            self._append_run_event(connection, data, payload)

    def _append_run_event(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
    ) -> None:
        event_id = str(_require(data, "event_id", "run_event_id"))
        self._insert_immutable(
            connection,
            "acquisition_run_events",
            "event_id=?",
            (event_id,),
            ("event_id", "run_id", "event_type", "lease_epoch", "occurred_at", "payload"),
            (
                event_id,
                _require(data, "run_id"),
                _require(data, "event_type", "state", "status"),
                _value(data, "lease_epoch"),
                _iso(_value(data, "occurred_at", "created_at", "at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def list_run_events(self, run_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM acquisition_run_events WHERE run_id=? "
                "ORDER BY occurred_at, event_id",
                (run_id,),
            ).fetchall()
        return [_load_model("AcquisitionRunEvent", row["payload"]) for row in rows]

    def save_physical_query_plan_item(self, item: Any) -> None:
        data = _payload(item)
        with self._write() as connection:
            self._save_plan_item(connection, data, _canonical_json(data))

    save_plan_item = save_physical_query_plan_item

    def _save_plan_item(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        item_id = str(_require(data, "plan_item_id", "physical_query_plan_item_id"))
        time_range = _value(data, "time_range", default={}) or {}
        self._insert_immutable(
            connection,
            "physical_query_plan_items",
            "plan_item_id=?",
            (item_id,),
            (
                "plan_item_id", "run_id", "source_definition_id",
                "source_definition_version", "query_id", "execution_key",
                "partition_key", "pagination_fingerprint", "time_start", "time_end",
                "ordinal", "payload",
            ),
            (
                item_id,
                _require(data, "run_id"),
                _require(data, "source_definition_id"),
                str(_require(data, "source_definition_version")),
                _require(data, "query_id"),
                _require(data, "execution_key"),
                _value(data, "partition_key", "query_partition"),
                str(_value(data, "pagination_fingerprint", default="")),
                _optional_iso(_value(data, "time_start", "range_start", default=time_range.get("start"))),
                _optional_iso(_value(data, "time_end", "range_end", default=time_range.get("end"))),
                int(_value(data, "ordinal", "plan_ordinal", default=0)),
                payload,
            ),
            payload,
        )

    def get_physical_query_plan_item(self, plan_item_id: str) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "physical_query_plan_items",
                "plan_item_id=?",
                (plan_item_id,),
                label="physical query plan item",
            )
        return _load_model("PhysicalQueryPlanItem", payload)

    get_plan_item = get_physical_query_plan_item

    def list_physical_query_plan_items(self, run_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM physical_query_plan_items WHERE run_id=? "
                "ORDER BY ordinal, plan_item_id",
                (run_id,),
            ).fetchall()
        return [_load_model("PhysicalQueryPlanItem", row["payload"]) for row in rows]

    list_plan_items = list_physical_query_plan_items

    def save_coverage_entry(self, entry: Any) -> None:
        data = _payload(entry)
        with self._write() as connection:
            self._save_coverage_entry(connection, data, _canonical_json(data))

    def _save_coverage_entry(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        entry_id = str(_require(data, "coverage_entry_id"))
        time_range = _value(data, "time_range", default={}) or {}
        self._insert_immutable(
            connection,
            "coverage_entries",
            "coverage_entry_id=?",
            (entry_id,),
            (
                "coverage_entry_id", "run_id", "source_definition_id",
                "source_definition_version", "question_id", "query_id",
                "time_start", "time_end", "ordinal", "payload",
            ),
            (
                entry_id,
                _require(data, "run_id"),
                _require(data, "source_definition_id"),
                str(_require(data, "source_definition_version")),
                _require(data, "question_id"),
                _require(data, "query_id"),
                _optional_iso(_value(data, "time_start", "range_start", default=time_range.get("start"))),
                _optional_iso(_value(data, "time_end", "range_end", default=time_range.get("end"))),
                int(_value(data, "ordinal", default=0)),
                payload,
            ),
            payload,
        )

    def get_coverage_entry(self, coverage_entry_id: str) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "coverage_entries",
                "coverage_entry_id=?",
                (coverage_entry_id,),
                label="coverage entry",
            )
        return _load_model("CoverageEntry", payload)

    def list_coverage_entries(self, run_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM coverage_entries WHERE run_id=? "
                "ORDER BY ordinal, coverage_entry_id",
                (run_id,),
            ).fetchall()
        return [_load_model("CoverageEntry", row["payload"]) for row in rows]

    def save_physical_query_coverage_link(self, link: Any) -> None:
        data = _payload(link)
        with self._write() as connection:
            self._save_plan_coverage_link(connection, data, _canonical_json(data))

    save_plan_coverage_link = save_physical_query_coverage_link

    def _save_plan_coverage_link(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        plan_item_id = str(_require(data, "plan_item_id", "physical_query_plan_item_id"))
        coverage_entry_id = str(_require(data, "coverage_entry_id"))
        lineage = connection.execute(
            "SELECT p.run_id AS plan_run_id, c.run_id AS coverage_run_id, "
            "p.source_definition_id AS plan_source_id, "
            "c.source_definition_id AS coverage_source_id, "
            "p.source_definition_version AS plan_source_version, "
            "c.source_definition_version AS coverage_source_version "
            "FROM physical_query_plan_items p CROSS JOIN coverage_entries c "
            "WHERE p.plan_item_id=? AND c.coverage_entry_id=?",
            (plan_item_id, coverage_entry_id),
        ).fetchone()
        if lineage is None:
            raise AcquisitionNotFoundError("plan-to-coverage link has a missing endpoint")
        if lineage["plan_run_id"] != lineage["coverage_run_id"]:
            raise AcquisitionStorageError("plan-to-coverage link cannot cross runs")
        if (
            lineage["plan_source_id"] != lineage["coverage_source_id"]
            or lineage["plan_source_version"] != lineage["coverage_source_version"]
        ):
            raise AcquisitionStorageError(
                "plan-to-coverage link must keep source definition identity"
            )
        self._insert_immutable(
            connection,
            "physical_query_coverage_links",
            "plan_item_id=? AND coverage_entry_id=?",
            (plan_item_id, coverage_entry_id),
            ("plan_item_id", "coverage_entry_id", "created_at", "payload"),
            (
                plan_item_id,
                coverage_entry_id,
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def list_physical_query_coverage_links(
        self, *, run_id: str | None = None, plan_item_id: str | None = None,
        coverage_entry_id: str | None = None
    ) -> list[Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if run_id:
            clauses.append("p.run_id=?")
            args.append(run_id)
        if plan_item_id:
            clauses.append("l.plan_item_id=?")
            args.append(plan_item_id)
        if coverage_entry_id:
            clauses.append("l.coverage_entry_id=?")
            args.append(coverage_entry_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT l.payload FROM physical_query_coverage_links l "
                "JOIN physical_query_plan_items p ON p.plan_item_id=l.plan_item_id "
                f"{where} ORDER BY l.plan_item_id, l.coverage_entry_id",
                tuple(args),
            ).fetchall()
        return [_load_model("PhysicalQueryCoverageLink", row["payload"]) for row in rows]

    list_plan_coverage_links = list_physical_query_coverage_links

    def save_plan_bundle(
        self,
        run: Any,
        plan_items: Iterable[Any],
        coverage_entries: Iterable[Any],
        links: Iterable[Any],
    ) -> None:
        run_data = _payload(run)
        with self._write() as connection:
            self._save_run(
                connection,
                run_data,
                _canonical_json(run_data),
                str(_require(run_data, "run_id", "acquisition_run_id")),
            )
            for entry in coverage_entries:
                data = _payload(entry)
                self._save_coverage_entry(connection, data, _canonical_json(data))
            for item in plan_items:
                data = _payload(item)
                self._save_plan_item(connection, data, _canonical_json(data))
            for link in links:
                data = _payload(link)
                self._save_plan_coverage_link(connection, data, _canonical_json(data))

    def append_coverage_resolution(
        self,
        resolution: Any,
        *,
        connection: sqlite3.Connection | None = None,
        owner_token: str | None = None,
        run_id: str | None = None,
    ) -> None:
        data = _payload(resolution)
        if connection is not None:
            self._append_coverage_resolution(connection, data, _canonical_json(data))
            return
        with self._write() as own:
            epoch = _value(data, "lease_epoch")
            if epoch is not None:
                coverage_id = str(_require(data, "coverage_entry_id"))
                row = own.execute(
                    "SELECT run_id FROM coverage_entries WHERE coverage_entry_id=?",
                    (coverage_id,),
                ).fetchone()
                effective_run = run_id or (row["run_id"] if row is not None else None)
                if effective_run is None:
                    raise AcquisitionNotFoundError(
                        f"coverage entry not found: {coverage_id}"
                    )
                self._assert_lease(
                    own, str(effective_run), int(epoch), owner_token=owner_token
                )
            self._append_coverage_resolution(own, data, _canonical_json(data))

    def _append_coverage_resolution(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        resolution_id = str(_require(data, "resolution_id", "coverage_resolution_id"))
        coverage_entry_id = str(_require(data, "coverage_entry_id"))
        coverage = connection.execute(
            "SELECT run_id FROM coverage_entries WHERE coverage_entry_id=?",
            (coverage_entry_id,),
        ).fetchone()
        if coverage is None:
            raise AcquisitionNotFoundError(
                f"coverage entry not found: {coverage_entry_id}"
            )
        run_id = str(_value(data, "run_id", default=coverage["run_id"]))
        if run_id != coverage["run_id"]:
            raise AcquisitionStorageError("coverage resolution references another run")
        self._insert_immutable(
            connection,
            "coverage_resolutions",
            "resolution_id=?",
            (resolution_id,),
            (
                "resolution_id", "run_id", "coverage_entry_id", "resolution_status",
                "lease_epoch", "created_at", "payload",
            ),
            (
                resolution_id,
                run_id,
                coverage_entry_id,
                _require(data, "resolution_status", "status"),
                _value(data, "lease_epoch"),
                _iso(_value(data, "created_at", "resolved_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def list_coverage_resolutions(self, run_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM coverage_resolutions WHERE run_id=? "
                "ORDER BY created_at, resolution_id",
                (run_id,),
            ).fetchall()
        return [_load_model("CoverageResolution", row["payload"]) for row in rows]

    # Lease ------------------------------------------------------------------
    def claim_lease(
        self,
        run_id: str,
        *,
        owner_token: str | None = None,
        now: datetime | None = None,
        ttl_seconds: int = 60,
    ) -> tuple[Any, str]:
        token = owner_token or secrets.token_urlsafe(32)
        current_time = _aware_utc(now or datetime.now(timezone.utc))
        expires = current_time + timedelta(seconds=ttl_seconds)
        if ttl_seconds <= 0:
            raise AcquisitionStorageError("lease TTL must be positive")
        token_hash = _token_hash(token)
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM acquisition_runs WHERE run_id=?", (run_id,)
            ).fetchone() is None:
                raise AcquisitionNotFoundError(f"run not found: {run_id}")
            row = connection.execute(
                "SELECT * FROM acquisition_execution_leases WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None:
                active = row["released_at"] is None and datetime.fromisoformat(
                    row["expires_at"]
                ) > current_time
                if active:
                    raise LeaseConflictError(run_id, row["expires_at"])
                epoch = int(row["lease_epoch"]) + 1
                connection.execute(
                    "UPDATE acquisition_execution_leases SET owner_token_hash=?, "
                    "lease_epoch=?, acquired_at=?, heartbeat_at=?, expires_at=?, released_at=NULL "
                    "WHERE run_id=? AND lease_epoch=?",
                    (
                        token_hash,
                        epoch,
                        current_time.isoformat(),
                        current_time.isoformat(),
                        expires.isoformat(),
                        run_id,
                        int(row["lease_epoch"]),
                    ),
                )
                event_type = "lease_reclaimed"
            else:
                epoch = 1
                connection.execute(
                    "INSERT INTO acquisition_execution_leases("
                    "run_id, owner_token_hash, lease_epoch, acquired_at, heartbeat_at, expires_at, released_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        run_id,
                        token_hash,
                        epoch,
                        current_time.isoformat(),
                        current_time.isoformat(),
                        expires.isoformat(),
                    ),
                )
                event_type = "lease_claimed"
            self._append_internal_lease_event(
                connection, run_id, event_type, epoch, current_time, expires
            )
            lease = self._lease_dict(connection, run_id)
        return self._coerce_lease(lease), token

    claim_execution_lease = claim_lease

    def renew_lease(
        self,
        run_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        now: datetime | None = None,
        ttl_seconds: int = 60,
    ) -> Any:
        current_time = _aware_utc(now or datetime.now(timezone.utc))
        expires = current_time + timedelta(seconds=ttl_seconds)
        if ttl_seconds <= 0:
            raise AcquisitionStorageError("lease TTL must be positive")
        with self._write() as connection:
            self._assert_lease(
                connection,
                run_id,
                lease_epoch,
                owner_token=owner_token,
                at=current_time,
            )
            changed = connection.execute(
                "UPDATE acquisition_execution_leases SET heartbeat_at=?, expires_at=? "
                "WHERE run_id=? AND lease_epoch=? AND owner_token_hash=? AND released_at IS NULL",
                (
                    current_time.isoformat(),
                    expires.isoformat(),
                    run_id,
                    lease_epoch,
                    _token_hash(owner_token),
                ),
            ).rowcount
            if changed != 1:
                raise StaleLeaseError("lease changed while renewing")
            self._append_internal_lease_event(
                connection, run_id, "lease_renewed", lease_epoch, current_time, expires
            )
            lease = self._lease_dict(connection, run_id)
        return self._coerce_lease(lease)

    renew_execution_lease = renew_lease

    def release_lease(
        self,
        run_id: str,
        *,
        owner_token: str,
        lease_epoch: int,
        now: datetime | None = None,
    ) -> Any:
        current_time = _aware_utc(now or datetime.now(timezone.utc))
        with self._write() as connection:
            self._assert_lease(
                connection,
                run_id,
                lease_epoch,
                owner_token=owner_token,
                at=current_time,
            )
            changed = connection.execute(
                "UPDATE acquisition_execution_leases SET released_at=? "
                "WHERE run_id=? AND lease_epoch=? AND owner_token_hash=? AND released_at IS NULL",
                (
                    current_time.isoformat(),
                    run_id,
                    lease_epoch,
                    _token_hash(owner_token),
                ),
            ).rowcount
            if changed != 1:
                raise StaleLeaseError("lease changed while releasing")
            self._append_internal_lease_event(
                connection, run_id, "lease_released", lease_epoch, current_time, current_time
            )
            lease = self._lease_dict(connection, run_id)
        return self._coerce_lease(lease)

    release_execution_lease = release_lease

    def get_lease(self, run_id: str) -> Any:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM acquisition_execution_leases WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise AcquisitionNotFoundError(f"lease not found: {run_id}")
            lease = dict(row)
        return self._coerce_lease(lease)

    get_execution_lease = get_lease

    @staticmethod
    def _lease_dict(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM acquisition_execution_leases WHERE run_id=?", (run_id,)
        ).fetchone()
        assert row is not None
        return dict(row)

    @staticmethod
    def _coerce_lease(data: Mapping[str, Any]) -> Any:
        try:
            from .models import AcquisitionExecutionLease

            return AcquisitionExecutionLease.model_validate(dict(data))
        except (ImportError, AttributeError):
            return dict(data)

    @staticmethod
    def _append_internal_lease_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        epoch: int,
        occurred_at: datetime,
        expires_at: datetime,
    ) -> None:
        event_id = str(uuid.uuid4())
        payload = _canonical_json(
            {
                "event_id": event_id,
                "run_id": run_id,
                "event_type": event_type,
                "lease_epoch": epoch,
                "occurred_at": occurred_at.isoformat(),
                "metadata": {"lease_expires_at": expires_at.isoformat()},
            }
        )
        connection.execute(
            "INSERT INTO acquisition_run_events("
            "event_id, run_id, event_type, lease_epoch, occurred_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, run_id, event_type, epoch, occurred_at.isoformat(), payload),
        )

    @staticmethod
    def _assert_lease(
        connection: sqlite3.Connection,
        run_id: str,
        lease_epoch: int,
        *,
        owner_token: str | None = None,
        at: datetime | None = None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM acquisition_execution_leases WHERE run_id=?", (run_id,)
        ).fetchone()
        current = _aware_utc(at or datetime.now(timezone.utc))
        if (
            row is None
            or int(row["lease_epoch"]) != int(lease_epoch)
            or row["released_at"] is not None
            or datetime.fromisoformat(row["expires_at"]) <= current
            or (owner_token is not None and row["owner_token_hash"] != _token_hash(owner_token))
        ):
            raise StaleLeaseError(f"stale or expired lease for run {run_id}")
        return row

    # Attempts ---------------------------------------------------------------
    def save_attempt(self, attempt: Any, *, owner_token: str | None = None) -> None:
        data = _payload(attempt)
        with self._write() as connection:
            self._save_attempt(connection, data, _canonical_json(data), owner_token=owner_token)

    save_acquisition_attempt = save_attempt

    def _save_attempt(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        *,
        owner_token: str | None,
    ) -> None:
        attempt_id = str(_require(data, "attempt_id", "acquisition_attempt_id"))
        run_id = str(_require(data, "run_id"))
        epoch = int(_require(data, "lease_epoch"))
        self._assert_lease(connection, run_id, epoch, owner_token=owner_token)
        plan_item_id = str(_require(data, "physical_query_plan_item_id", "plan_item_id"))
        plan = connection.execute(
            "SELECT run_id, source_definition_id, source_definition_version, "
            "execution_key, query_id, payload "
            "FROM physical_query_plan_items WHERE plan_item_id=?",
            (plan_item_id,),
        ).fetchone()
        if plan is None or plan["run_id"] != run_id:
            raise AcquisitionStorageError("attempt must reference a plan item in the same run")
        plan_payload = json.loads(plan["payload"])
        expected_fields = {
            "source_definition_id": plan["source_definition_id"],
            "source_definition_version": str(plan["source_definition_version"]),
            "execution_key": plan["execution_key"],
            "attempt_kind": str(_value(plan_payload, "attempt_kind", default="discovery")),
        }
        for field, expected in expected_fields.items():
            actual = str(_require(data, field))
            if actual != expected:
                raise AcquisitionStorageError(
                    f"attempt {field} does not match its physical plan item"
                )
        if expected_fields["attempt_kind"] == "discovery" and str(
            _require(data, "query_id")
        ) != str(plan["query_id"]):
            raise AcquisitionStorageError(
                "discovery attempt query_id does not match its physical plan item"
            )
        planned_resource_id = _value(plan_payload, "discovered_resource_id")
        if planned_resource_id is not None and _value(
            data, "discovered_resource_id"
        ) != planned_resource_id:
            raise AcquisitionStorageError(
                "fetch attempt resource does not match its physical plan item"
            )
        supersedes = _value(data, "supersedes_attempt_id")
        if supersedes:
            prior = connection.execute(
                "SELECT retry_group_id, retry_ordinal FROM acquisition_attempts WHERE attempt_id=?",
                (supersedes,),
            ).fetchone()
            if prior is None:
                raise AcquisitionStorageError("superseded attempt does not exist")
            if str(_value(data, "retry_group_id")) != prior["retry_group_id"]:
                raise AcquisitionStorageError("superseding attempt must keep retry group")
            if int(_value(data, "retry_ordinal", default=0)) <= int(prior["retry_ordinal"]):
                raise AcquisitionStorageError("superseding attempt must advance retry ordinal")
        self._insert_immutable(
            connection,
            "acquisition_attempts",
            "attempt_id=?",
            (attempt_id,),
            (
                "attempt_id", "run_id", "physical_query_plan_item_id",
                "source_definition_id", "source_definition_version", "attempt_kind",
                "execution_key", "retry_group_id", "retry_ordinal",
                "work_position", "supersedes_attempt_id", "lease_epoch", "started_at", "payload",
            ),
            (
                attempt_id,
                run_id,
                plan_item_id,
                plan["source_definition_id"],
                str(plan["source_definition_version"]),
                _require(data, "attempt_kind", "kind"),
                plan["execution_key"],
                _require(data, "retry_group_id"),
                int(_value(data, "retry_ordinal", default=0)),
                str(_require(data, "work_position")),
                supersedes,
                epoch,
                _iso(_value(data, "started_at", "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def get_attempt(self, attempt_id: str) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "acquisition_attempts",
                "attempt_id=?",
                (attempt_id,),
                label="attempt",
            )
        return _load_model("AcquisitionAttempt", payload)

    def list_attempts(
        self,
        *,
        run_id: str | None = None,
        plan_item_id: str | None = None,
        source_definition_id: str | None = None,
        retry_group_id: str | None = None,
        limit: int | None = 500,
        offset: int = 0,
    ) -> list[Any]:
        """Read a display page, or the complete selection with ``limit=None``.

        Runtime decisions and exports must explicitly request the complete
        selection.  A larger numeric limit is still a lossy display page.
        """
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (
            ("run_id", run_id),
            ("physical_query_plan_item_id", plan_item_id),
            ("source_definition_id", source_definition_id),
            ("retry_group_id", retry_group_id),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM acquisition_attempts {where} "
                "ORDER BY started_at, retry_group_id, retry_ordinal, attempt_id LIMIT ? OFFSET ?",
                (*args, -1 if limit is None else limit, offset),
            ).fetchall()
        return [_load_model("AcquisitionAttempt", row["payload"]) for row in rows]

    def append_attempt_event(self, event: Any, *, owner_token: str | None = None) -> None:
        data = _payload(event)
        with self._write() as connection:
            self._append_attempt_event(
                connection, data, _canonical_json(data), owner_token=owner_token
            )

    def _append_attempt_event(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        *,
        owner_token: str | None,
    ) -> None:
        event_id = str(_require(data, "event_id", "attempt_event_id"))
        attempt_id = str(_require(data, "attempt_id"))
        attempt = connection.execute(
            "SELECT run_id FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise AcquisitionNotFoundError(f"attempt not found: {attempt_id}")
        epoch = int(_require(data, "lease_epoch"))
        self._assert_lease(
            connection, attempt["run_id"], epoch, owner_token=owner_token
        )
        outcome = _value(data, "outcome")
        event_type = str(_require(data, "event_type", "lifecycle"))
        if event_type in {"terminal", "outcome"} and outcome is not None:
            event_type = "outcome_terminal"
        self._insert_immutable(
            connection,
            "acquisition_attempt_events",
            "event_id=?",
            (event_id,),
            (
                "event_id", "attempt_id", "event_type", "outcome", "reason_code",
                "lease_epoch", "occurred_at", "payload",
            ),
            (
                event_id,
                attempt_id,
                event_type,
                outcome,
                _value(data, "reason_code"),
                epoch,
                _iso(_value(data, "occurred_at", "created_at", "ended_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def list_attempt_events(self, attempt_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM acquisition_attempt_events WHERE attempt_id=? "
                "ORDER BY occurred_at, event_id",
                (attempt_id,),
            ).fetchall()
        return [_load_model("AcquisitionAttemptEvent", row["payload"]) for row in rows]

    def append_attempt_segment(self, segment: Any, *, owner_token: str | None = None) -> None:
        data = _payload(segment)
        with self._write() as connection:
            self._append_attempt_segment(
                connection, data, _canonical_json(data), owner_token=owner_token
            )

    def _append_attempt_segment(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        *,
        owner_token: str | None,
    ) -> None:
        segment_id = str(_require(data, "segment_id", "attempt_segment_id"))
        attempt_id = str(_require(data, "attempt_id"))
        attempt = connection.execute(
            "SELECT run_id FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise AcquisitionNotFoundError(f"attempt not found: {attempt_id}")
        epoch = int(_require(data, "lease_epoch"))
        self._assert_lease(
            connection, attempt["run_id"], epoch, owner_token=owner_token
        )
        work_position = _value(data, "work_position", "position", default={})
        work_json = (
            json.dumps(work_position, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if not isinstance(work_position, str)
            else work_position
        )
        self._insert_immutable(
            connection,
            "acquisition_attempt_segments",
            "segment_id=?",
            (segment_id,),
            (
                "segment_id", "attempt_id", "segment_ordinal", "work_position",
                "lease_epoch", "committed_at", "payload",
            ),
            (
                segment_id,
                attempt_id,
                int(_require(data, "segment_ordinal", "ordinal")),
                work_json,
                epoch,
                _iso(_value(data, "committed_at", "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def list_attempt_segments(self, attempt_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM acquisition_attempt_segments WHERE attempt_id=? "
                "ORDER BY segment_ordinal, segment_id",
                (attempt_id,),
            ).fetchall()
        return [_load_model("AcquisitionAttemptSegment", row["payload"]) for row in rows]

    # Discovery --------------------------------------------------------------
    def commit_discovery_bundle(
        self,
        observation: Any,
        proof: Any,
        resources: Iterable[Any],
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        observation_data = _payload(observation)
        proof_data = _payload(proof)
        resource_data = [_payload(item) for item in resources]
        with self._write() as connection:
            attempt_id = str(_require(observation_data, "attempt_id"))
            run_id = self._attempt_run_id(connection, attempt_id)
            epoch_value = lease_epoch or _value(observation_data, "lease_epoch")
            if epoch_value is None:
                attempt = connection.execute(
                    "SELECT lease_epoch FROM acquisition_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                epoch_value = attempt["lease_epoch"] if attempt is not None else None
            epoch = int(epoch_value or 0)
            self._assert_lease(connection, run_id, epoch, owner_token=owner_token)
            self._save_discovery_observation(
                connection, observation_data, _canonical_json(observation_data), epoch
            )
            self._save_discovery_proof(connection, proof_data, _canonical_json(proof_data))
            for resource in resource_data:
                self._save_discovered_resource(
                    connection, resource, _canonical_json(resource)
                )

    def save_discovery_observation(
        self,
        observation: Any,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        """Append one discovery observation under the current run lease.

        This is the no-new-snapshot path used when a retained discovery page has
        the same canonical identity and content hash as an existing snapshot.
        """

        data = _payload(observation)
        with self._write() as connection:
            attempt_id = str(_require(data, "attempt_id"))
            run_id = self._attempt_run_id(connection, attempt_id)
            epoch_value = lease_epoch or _value(data, "lease_epoch")
            if epoch_value is None:
                attempt = connection.execute(
                    "SELECT lease_epoch FROM acquisition_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                epoch_value = attempt["lease_epoch"] if attempt is not None else None
            epoch = int(epoch_value or 0)
            self._assert_lease(connection, run_id, epoch, owner_token=owner_token)
            self._save_discovery_observation(
                connection, data, _canonical_json(data), epoch
            )

    def _save_discovery_observation(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        epoch: int,
    ) -> None:
        observation_id = str(_require(data, "observation_id", "discovery_observation_id"))
        attempt_id = str(_require(data, "attempt_id"))
        attempt = connection.execute(
            "SELECT physical_query_plan_item_id, source_definition_id, "
            "source_definition_version, attempt_kind FROM acquisition_attempts "
            "WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise AcquisitionNotFoundError(f"attempt not found: {attempt_id}")
        if attempt["attempt_kind"] != "discovery":
            raise AcquisitionStorageError(
                "discovery observation must reference a discovery attempt"
            )
        expected = {
            "physical_query_plan_item_id": attempt["physical_query_plan_item_id"],
            "source_definition_id": attempt["source_definition_id"],
            "source_definition_version": str(attempt["source_definition_version"]),
        }
        for field, expected_value in expected.items():
            actual = _value(data, field, "plan_item_id" if field == "physical_query_plan_item_id" else field)
            if actual is not None and str(actual) != str(expected_value):
                raise AcquisitionStorageError(
                    f"discovery observation {field} does not match its attempt"
                )
        self._insert_immutable(
            connection,
            "discovery_observations",
            "observation_id=?",
            (observation_id,),
            (
                "observation_id", "attempt_id", "physical_query_plan_item_id",
                "snapshot_id", "page_ordinal", "cursor", "observed_at",
                "retrieved_at", "lease_epoch", "payload",
            ),
            (
                observation_id,
                attempt_id,
                attempt["physical_query_plan_item_id"],
                _value(data, "snapshot_id", "raw_resource_snapshot_id"),
                _value(data, "page_ordinal", "page_number"),
                _value(data, "cursor"),
                _iso(_value(data, "observed_at", "created_at"), default=datetime.now(timezone.utc)),
                _optional_iso(_value(data, "retrieved_at")),
                epoch,
                payload,
            ),
            payload,
        )

    def _save_discovery_proof(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        proof_id = str(_require(data, "proof_id", "discovery_proof_id"))
        observation_id = str(
            _require(data, "observation_id", "discovery_observation_id")
        )
        observation = connection.execute(
            "SELECT attempt_id, physical_query_plan_item_id FROM discovery_observations "
            "WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if observation is None:
            raise AcquisitionNotFoundError(
                f"discovery observation not found: {observation_id}"
            )
        for field, expected in (
            ("attempt_id", observation["attempt_id"]),
            ("physical_query_plan_item_id", observation["physical_query_plan_item_id"]),
        ):
            actual = _value(data, field, "plan_item_id" if field == "physical_query_plan_item_id" else field)
            if actual is not None and str(actual) != str(expected):
                raise AcquisitionStorageError(
                    f"discovery proof {field} does not match its observation"
                )
        self._insert_immutable(
            connection,
            "discovery_proofs",
            "proof_id=?",
            (proof_id,),
            (
                "proof_id", "observation_id", "response_sha256", "byte_length",
                "schema_version", "terminal_proof", "created_at", "payload",
            ),
            (
                proof_id,
                observation_id,
                _require(data, "response_sha256", "sha256"),
                int(_require(data, "byte_length", "response_byte_length", "content_length")),
                str(_require(data, "schema_version", "parser_schema_version")),
                int(bool(_value(data, "terminal_proof", "terminal", "is_terminal", default=False))),
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def _save_discovered_resource(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        resource_id = str(_require(data, "discovered_resource_id", "resource_id"))
        proof_id = str(_require(data, "proof_id", "discovery_proof_id"))
        observation_id = str(
            _require(data, "discovery_observation_id", "observation_id")
        )
        lineage = connection.execute(
            "SELECT p.observation_id AS proof_observation_id, o.attempt_id, "
            "a.source_definition_id, a.source_definition_version "
            "FROM discovery_proofs p JOIN discovery_observations o "
            "ON o.observation_id=p.observation_id JOIN acquisition_attempts a "
            "ON a.attempt_id=o.attempt_id WHERE p.proof_id=?",
            (proof_id,),
        ).fetchone()
        if lineage is None:
            raise AcquisitionNotFoundError(f"discovery proof not found: {proof_id}")
        expected_fields = {
            "discovery_observation_id": lineage["proof_observation_id"],
            "discovery_attempt_id": lineage["attempt_id"],
            "source_definition_id": lineage["source_definition_id"],
            "source_definition_version": str(lineage["source_definition_version"]),
        }
        if observation_id != expected_fields["discovery_observation_id"]:
            raise AcquisitionStorageError(
                "discovered resource observation does not match its proof"
            )
        for field, expected in expected_fields.items():
            actual = _value(data, field)
            if actual is not None and str(actual) != str(expected):
                raise AcquisitionStorageError(
                    f"discovered resource {field} does not match its proof lineage"
                )
        self._insert_immutable(
            connection,
            "discovered_resources",
            "discovered_resource_id=?",
            (resource_id,),
            (
                "discovered_resource_id", "proof_id", "discovery_observation_id",
                "canonical_resource_id", "row_hash", "row_ordinal", "required_fetch",
                "created_at", "payload",
            ),
            (
                resource_id,
                proof_id,
                observation_id,
                _require(data, "canonical_resource_id"),
                _require(data, "row_hash"),
                _value(data, "row_ordinal", "row_number"),
                int(bool(_value(data, "required_fetch", default=False))),
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def get_discovery_observation(self, observation_id: str) -> Any:
        return self._get_typed("discovery_observations", "observation_id", observation_id, "DiscoveryObservation")

    def get_discovery_proof(self, proof_id: str) -> Any:
        return self._get_typed("discovery_proofs", "proof_id", proof_id, "DiscoveryProof")

    def list_discovered_resources(self, observation_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM discovered_resources WHERE discovery_observation_id=? "
                "ORDER BY row_ordinal, discovered_resource_id",
                (observation_id,),
            ).fetchall()
        return [_load_model("DiscoveredResource", row["payload"]) for row in rows]

    def get_discovered_resource(self, discovered_resource_id: str) -> Any:
        return self._get_typed(
            "discovered_resources", "discovered_resource_id",
            discovered_resource_id, "DiscoveredResource",
        )

    def list_discovery_observations(self, attempt_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM discovery_observations WHERE attempt_id=? "
                "ORDER BY page_ordinal, observation_id",
                (attempt_id,),
            ).fetchall()
        return [_load_model("DiscoveryObservation", row["payload"]) for row in rows]

    def list_discovery_proofs(self, attempt_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT p.payload FROM discovery_proofs p "
                "JOIN discovery_observations o ON o.observation_id=p.observation_id "
                "WHERE o.attempt_id=? ORDER BY o.page_ordinal, p.proof_id",
                (attempt_id,),
            ).fetchall()
        return [_load_model("DiscoveryProof", row["payload"]) for row in rows]

    # Raw resources and evidence --------------------------------------------
    def save_content_blob(self, blob: Any, *, connection: sqlite3.Connection | None = None) -> None:
        data = _payload(blob)
        if connection is not None:
            self._save_content_blob(connection, data, _canonical_json(data))
            return
        with self._write() as own:
            self._save_content_blob(own, data, _canonical_json(data))

    def get_content_blob(self, content_blob_id: str) -> Any:
        return self._get_typed(
            "content_blobs", "content_blob_id", content_blob_id, "ContentBlob"
        )

    def _save_content_blob(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        blob_id = str(_require(data, "content_blob_id", "blob_id"))
        self._insert_immutable(
            connection,
            "content_blobs",
            "content_blob_id=?",
            (blob_id,),
            (
                "content_blob_id", "sha256", "byte_length", "relative_path",
                "storage_namespace_id", "created_at", "payload",
            ),
            (
                blob_id,
                _require(data, "sha256", "content_sha256"),
                int(_require(data, "byte_length")),
                _require(data, "relative_path", "archive_relative_path", "archived_relative_path"),
                _require(data, "storage_namespace_id", "namespace_id"),
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def save_raw_resource_snapshot(
        self, snapshot: Any, *, connection: sqlite3.Connection | None = None
    ) -> None:
        data = _payload(snapshot)
        if connection is not None:
            self._save_raw_snapshot(connection, data, _canonical_json(data))
            return
        with self._write() as own:
            self._save_raw_snapshot(own, data, _canonical_json(data))

    save_raw_snapshot = save_raw_resource_snapshot

    def _save_raw_snapshot(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        snapshot_id = str(_require(data, "snapshot_id", "raw_resource_snapshot_id"))
        existing = connection.execute(
            "SELECT payload FROM raw_resource_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload"] != payload:
                raise ImmutableRecordError(
                    "raw_resource_snapshots record cannot be overwritten"
                )
            return
        role = str(_require(data, "resource_role"))
        canonical_id = _value(data, "canonical_resource_id")
        query_page = _value(data, "query_page_canonical")
        version = int(_value(data, "version", "content_version", default=1))
        identity_row = connection.execute(
            "SELECT snapshot_id, payload FROM raw_resource_snapshots "
            "WHERE source_definition_id=? AND source_definition_version=? "
            "AND resource_role=? AND canonical_resource_id IS ? "
            "AND query_page_canonical IS ? AND version=?",
            (
                _require(data, "source_definition_id"),
                str(_require(data, "source_definition_version")),
                role,
                canonical_id,
                query_page,
                version,
            ),
        ).fetchone()
        if identity_row is not None and identity_row["snapshot_id"] != snapshot_id:
            raise ImmutableRecordError(
                "raw snapshot source/canonical/version identity already exists"
            )
        previous = connection.execute(
            "SELECT snapshot_id, version FROM raw_resource_snapshots "
            "WHERE source_definition_id=? AND source_definition_version=? "
            "AND resource_role=? AND canonical_resource_id IS ? "
            "AND query_page_canonical IS ? ORDER BY version DESC LIMIT 1",
            (
                _require(data, "source_definition_id"),
                str(_require(data, "source_definition_version")),
                role,
                canonical_id,
                query_page,
            ),
        ).fetchone()
        supersedes = _value(data, "supersedes_snapshot_id")
        if version == 1:
            if previous is not None or supersedes is not None:
                raise AcquisitionStorageError(
                    "first raw snapshot version cannot supersede an existing version"
                )
        elif (
            previous is None
            or int(previous["version"]) != version - 1
            or previous["snapshot_id"] != supersedes
        ):
            raise AcquisitionStorageError(
                "raw snapshot version must directly supersede the current version"
            )
        self._insert_immutable(
            connection,
            "raw_resource_snapshots",
            "snapshot_id=?",
            (snapshot_id,),
            (
                "snapshot_id", "resource_role", "source_definition_id",
                "source_definition_version", "creating_observation_id", "content_blob_id",
                "canonical_resource_id", "upstream_material_id", "canonical_url",
                "physical_query_plan_item_id", "query_page_canonical", "page_ordinal",
                "cursor", "content_sha256", "byte_length", "available_at",
                "archived_relative_path", "storage_namespace_id", "version",
                "supersedes_snapshot_id", "created_at", "payload",
            ),
            (
                snapshot_id,
                role,
                _require(data, "source_definition_id"),
                str(_require(data, "source_definition_version")),
                _require(data, "creating_observation_id"),
                _require(data, "content_blob_id", "blob_id"),
                canonical_id,
                _value(data, "upstream_material_id"),
                _value(data, "canonical_url"),
                _value(data, "physical_query_plan_item_id", "plan_item_id"),
                query_page,
                _value(data, "page_ordinal", "page_number"),
                _value(data, "cursor"),
                _require(data, "content_sha256", "sha256"),
                int(_require(data, "byte_length")),
                _iso(_require(data, "available_at")),
                _require(data, "archived_relative_path", "archive_relative_path", "relative_path"),
                _require(data, "storage_namespace_id", "namespace_id"),
                version,
                supersedes,
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def save_resource_observation(
        self,
        observation: Any,
        *,
        connection: sqlite3.Connection | None = None,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        data = _payload(observation)
        if connection is not None:
            self._save_resource_observation(
                connection, data, _canonical_json(data), owner_token, lease_epoch
            )
            return
        with self._write() as own:
            self._save_resource_observation(
                own, data, _canonical_json(data), owner_token, lease_epoch
            )

    def _save_resource_observation(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        owner_token: str | None,
        lease_epoch: int | None,
    ) -> None:
        observation_id = str(_require(data, "observation_id", "resource_observation_id"))
        attempt_id = str(_require(data, "attempt_id"))
        attempt_row = connection.execute(
            "SELECT run_id, source_definition_id, source_definition_version, "
            "attempt_kind, payload FROM acquisition_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if attempt_row is None:
            raise AcquisitionNotFoundError(f"attempt not found: {attempt_id}")
        if attempt_row["attempt_kind"] != "fetch":
            raise AcquisitionStorageError(
                "resource observation must reference a fetch attempt"
            )
        run_id = str(attempt_row["run_id"])
        for field, expected in (
            ("source_definition_id", attempt_row["source_definition_id"]),
            ("source_definition_version", str(attempt_row["source_definition_version"])),
        ):
            if str(_require(data, field)) != str(expected):
                raise AcquisitionStorageError(
                    f"resource observation {field} does not match its fetch attempt"
                )
        attempt_payload = json.loads(attempt_row["payload"])
        for field in ("discovered_resource_id", "parent_discovery_attempt_id"):
            expected = _value(attempt_payload, field)
            if expected is not None and _value(data, field) != expected:
                raise AcquisitionStorageError(
                    f"resource observation {field} does not match its fetch attempt"
                )
        epoch_value = lease_epoch or _value(data, "lease_epoch")
        if epoch_value is None:
            attempt_epoch = connection.execute(
                "SELECT lease_epoch FROM acquisition_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            epoch_value = attempt_epoch["lease_epoch"] if attempt_epoch is not None else None
        epoch = int(epoch_value or 0)
        self._assert_lease(connection, run_id, epoch, owner_token=owner_token)
        from .validators import validate_observation_anchor
        try:
            validate_observation_anchor(connection, dict(data))
        except ValueError as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        self._insert_immutable(
            connection,
            "resource_observations",
            "observation_id=?",
            (observation_id,),
            (
                "observation_id", "attempt_id", "discovered_resource_id",
                "source_definition_id", "source_definition_version", "snapshot_id",
                "disposition", "http_status", "etag", "last_modified",
                "validator_snapshot_id", "observed_at", "retrieved_at", "reason_code",
                "lease_epoch", "payload",
            ),
            (
                observation_id,
                attempt_id,
                _value(data, "discovered_resource_id"),
                _require(data, "source_definition_id"),
                str(_require(data, "source_definition_version")),
                _value(data, "snapshot_id", "raw_resource_snapshot_id"),
                _value(data, "disposition"),
                _value(data, "http_status"),
                _value(data, "etag"),
                _value(data, "last_modified"),
                _value(data, "validator_snapshot_id", "validator_source_snapshot_id"),
                _iso(_value(data, "observed_at", "created_at"), default=datetime.now(timezone.utc)),
                _optional_iso(_value(data, "retrieved_at")),
                _value(data, "reason_code"),
                epoch,
                payload,
            ),
            payload,
        )

    def commit_snapshot_bundle(
        self,
        blob: Any,
        snapshot: Any,
        observation: Any,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        blob_data, snapshot_data, observation_data = map(
            _payload, (blob, snapshot, observation)
        )
        with self._write() as connection:
            self._validate_snapshot_bundle(
                blob_data, snapshot_data, observation_data, expected_role="content"
            )
            self._save_content_blob(connection, blob_data, _canonical_json(blob_data))
            self._save_raw_snapshot(connection, snapshot_data, _canonical_json(snapshot_data))
            self._save_resource_observation(
                connection,
                observation_data,
                _canonical_json(observation_data),
                owner_token,
                lease_epoch,
            )

    def commit_discovery_snapshot_bundle(
        self,
        blob: Any,
        snapshot: Any,
        discovery_observation: Any,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        blob_data, snapshot_data, observation_data = map(
            _payload, (blob, snapshot, discovery_observation)
        )
        with self._write() as connection:
            attempt_id = str(_require(observation_data, "attempt_id"))
            run_id = self._attempt_run_id(connection, attempt_id)
            epoch_value = lease_epoch or _value(observation_data, "lease_epoch")
            if epoch_value is None:
                attempt_row = connection.execute(
                    "SELECT lease_epoch FROM acquisition_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                epoch_value = attempt_row["lease_epoch"] if attempt_row is not None else None
            epoch = int(epoch_value or 0)
            self._assert_lease(connection, run_id, epoch, owner_token=owner_token)
            self._validate_snapshot_bundle(
                blob_data,
                snapshot_data,
                observation_data,
                expected_role="discovery_response",
            )
            self._save_content_blob(connection, blob_data, _canonical_json(blob_data))
            self._save_raw_snapshot(connection, snapshot_data, _canonical_json(snapshot_data))
            self._save_discovery_observation(
                connection, observation_data, _canonical_json(observation_data), epoch
            )

    commit_retained_discovery_snapshot = commit_discovery_snapshot_bundle

    @staticmethod
    def _validate_snapshot_bundle(
        blob: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        observation: Mapping[str, Any],
        *,
        expected_role: str,
    ) -> None:
        if str(_require(snapshot, "resource_role")) != expected_role:
            raise AcquisitionStorageError(
                f"snapshot bundle must contain a {expected_role} snapshot"
            )
        pairs = (
            (
                _require(snapshot, "content_blob_id", "blob_id"),
                _require(blob, "content_blob_id", "blob_id"),
                "content blob id",
            ),
            (
                _require(snapshot, "content_sha256", "sha256"),
                _require(blob, "sha256", "content_sha256"),
                "content hash",
            ),
            (
                int(_require(snapshot, "byte_length")),
                int(_require(blob, "byte_length")),
                "byte length",
            ),
            (
                _require(snapshot, "storage_namespace_id", "namespace_id"),
                _require(blob, "storage_namespace_id", "namespace_id"),
                "storage namespace",
            ),
            (
                _require(snapshot, "creating_observation_id"),
                _require(observation, "observation_id", "resource_observation_id"),
                "creating observation",
            ),
            (
                _require(snapshot, "snapshot_id", "raw_resource_snapshot_id"),
                _require(observation, "snapshot_id", "raw_resource_snapshot_id"),
                "snapshot observation",
            ),
            (
                _require(snapshot, "source_definition_id"),
                _require(observation, "source_definition_id"),
                "source definition",
            ),
            (
                str(_require(snapshot, "source_definition_version")),
                str(_require(observation, "source_definition_version")),
                "source definition version",
            ),
        )
        for actual, expected, label in pairs:
            if actual != expected:
                raise AcquisitionStorageError(
                    f"snapshot bundle {label} does not match"
                )
        if expected_role == "discovery_response" and str(
            _require(snapshot, "physical_query_plan_item_id", "plan_item_id")
        ) != str(
            _require(observation, "physical_query_plan_item_id", "plan_item_id")
        ):
            raise AcquisitionStorageError(
                "discovery snapshot plan item does not match its observation"
            )

    def get_raw_resource_snapshot(self, snapshot_id: str) -> Any:
        return self._get_typed(
            "raw_resource_snapshots", "snapshot_id", snapshot_id, "RawResourceSnapshot"
        )

    get_raw_snapshot = get_raw_resource_snapshot

    def find_raw_resource_snapshot(
        self,
        *,
        resource_role: str | None = None,
        query_page_canonical: str | None = None,
        source_definition_id: str | None = None,
        source_definition_version: str | int | None = None,
        canonical_resource_id: str | None = None,
        sha256: str | None = None,
    ) -> Any | None:
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (
            ("resource_role", resource_role),
            ("query_page_canonical", query_page_canonical),
            ("source_definition_id", source_definition_id),
            ("source_definition_version", None if source_definition_version is None else str(source_definition_version)),
            ("canonical_resource_id", canonical_resource_id),
            ("content_sha256", sha256),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        if not clauses:
            raise AcquisitionStorageError("snapshot lookup requires at least one selector")
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload FROM raw_resource_snapshots WHERE "
                + " AND ".join(clauses)
                + " ORDER BY version DESC, created_at DESC, snapshot_id DESC LIMIT 1",
                tuple(args),
            ).fetchone()
        return None if row is None else _load_model("RawResourceSnapshot", row["payload"])

    find_raw_snapshot = find_raw_resource_snapshot

    def list_content_snapshots_by_hash(self, namespace_id: str, sha256: str) -> list[Any]:
        """Complete local candidates; consumers must still validate bytes and policy."""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM raw_resource_snapshots WHERE storage_namespace_id=? "
                "AND resource_role='content' AND content_sha256=? ORDER BY created_at,snapshot_id",
                (namespace_id, sha256),
            ).fetchall()
        return [_load_model("RawResourceSnapshot", row["payload"]) for row in rows]

    def list_resource_observations(
        self,
        *,
        attempt_id: str | None = None,
        snapshot_id: str | None = None,
        limit: int | None = 500,
        offset: int = 0,
    ) -> list[Any]:
        clauses: list[str] = []
        args: list[Any] = []
        if attempt_id:
            clauses.append("attempt_id=?")
            args.append(attempt_id)
        if snapshot_id:
            clauses.append("snapshot_id=?")
            args.append(snapshot_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM resource_observations {where} "
                "ORDER BY observed_at, observation_id LIMIT ? OFFSET ?",
                (*args, -1 if limit is None else limit, offset),
            ).fetchall()
        return [_load_model("ResourceObservation", row["payload"]) for row in rows]

    def get_resource_observation(self, observation_id: str) -> Any:
        return self._get_typed(
            "resource_observations", "observation_id", observation_id, "ResourceObservation"
        )

    def completed_resource_observations(
        self, *, run_id: str, source_definition_id: str,
        source_definition_version: str, canonical_resource_id: str,
    ) -> list[Any]:
        """Read all successful observations for one exact run/source/resource."""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT o.payload FROM resource_observations o "
                "JOIN discovered_resources r ON r.discovered_resource_id=o.discovered_resource_id "
                "JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id "
                "WHERE a.run_id=? AND o.source_definition_id=? "
                "AND o.source_definition_version=? AND r.canonical_resource_id=? "
                "AND o.snapshot_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM acquisition_attempt_events e WHERE e.attempt_id=a.attempt_id "
                "AND e.event_type='outcome_terminal' AND e.outcome IN ('success','unchanged')) "
                "ORDER BY o.observed_at DESC, o.observation_id DESC",
                (run_id, source_definition_id, str(source_definition_version), canonical_resource_id),
            ).fetchall()
        return [_load_model("ResourceObservation", row["payload"]) for row in rows]

    def append_snapshot_integrity_event(self, event: Any) -> None:
        data = _payload(event)
        payload = _canonical_json(data)
        event_id = str(_require(data, "integrity_event_id", "event_id"))
        with self._write() as connection:
            self._insert_immutable(
                connection,
                "snapshot_integrity_events",
                "integrity_event_id=?",
                (event_id,),
                (
                    "integrity_event_id", "snapshot_id", "integrity_status",
                    "checked_at", "reason_code", "payload",
                ),
                (
                    event_id,
                    _require(data, "snapshot_id", "parent_snapshot_id"),
                    _require(data, "integrity_status", "status"),
                    _iso(_value(data, "checked_at", "created_at"), default=datetime.now(timezone.utc)),
                    _value(data, "reason_code"),
                    payload,
                ),
                payload,
            )

    def list_snapshot_integrity_events(self, snapshot_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM snapshot_integrity_events WHERE snapshot_id=? "
                "ORDER BY checked_at, integrity_event_id",
                (snapshot_id,),
            ).fetchall()
        return [_load_model("SnapshotIntegrityEvent", row["payload"]) for row in rows]

    def save_derived_artifact(self, artifact: Any) -> None:
        data = _payload(artifact)
        payload = _canonical_json(data)
        artifact_id = str(_require(data, "derived_artifact_id", "artifact_id"))
        with self._write() as connection:
            snapshot_id = str(_require(data, "snapshot_id", "parent_snapshot_id"))
            snapshot = connection.execute(
                "SELECT storage_namespace_id FROM raw_resource_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise AcquisitionNotFoundError(f"raw snapshot not found: {snapshot_id}")
            namespace_id = _value(data, "storage_namespace_id", "namespace_id")
            if namespace_id is not None and namespace_id != snapshot["storage_namespace_id"]:
                raise AcquisitionStorageError(
                    "derived artifact namespace does not match its parent snapshot"
                )
            self._insert_immutable(
                connection,
                "derived_artifacts",
                "derived_artifact_id=?",
                (artifact_id,),
                (
                    "derived_artifact_id", "snapshot_id", "extractor_id",
                    "extractor_version", "output_hash", "created_at", "payload",
                ),
                (
                    artifact_id,
                    snapshot_id,
                    _require(data, "extractor_id", "extractor_name"),
                    str(_require(data, "extractor_version")),
                    _require(data, "output_hash", "output_sha256", "sha256"),
                    _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                    payload,
                ),
                payload,
            )

    def get_derived_artifact(self, artifact_id: str) -> Any:
        return self._get_typed(
            "derived_artifacts", "derived_artifact_id", artifact_id, "DerivedArtifact"
        )

    def list_derived_artifacts(self, snapshot_id: str) -> list[Any]:
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM derived_artifacts WHERE snapshot_id=? "
                "ORDER BY created_at, derived_artifact_id",
                (snapshot_id,),
            ).fetchall()
        return [_load_model("DerivedArtifact", row["payload"]) for row in rows]

    def save_evidence_manifest(self, manifest: Any, items: Iterable[Any] = ()) -> None:
        data = _payload(manifest)
        payload = _canonical_json(data)
        manifest_id = str(_require(data, "manifest_id", "evidence_manifest_id"))
        explicit_items = [_payload(item) for item in items]
        if explicit_items:
            item_data = self._normalize_manifest_items(explicit_items)
        else:
            item_data = []
            for ordinal, item in enumerate(_value(data, "items", default=[]) or []):
                normalized = _payload(item)
                normalized.setdefault("item_type", "snapshot")
                normalized.setdefault("item_id", normalized.get("snapshot_id"))
                normalized.setdefault("disposition", "included")
                normalized.setdefault("ordinal", ordinal)
                item_data.append(normalized)
            base = len(item_data)
            for ordinal, item in enumerate(_value(data, "exclusions", default=[]) or []):
                normalized = _payload(item)
                normalized.setdefault("item_type", normalized.get("object_type", "coverage"))
                normalized.setdefault("item_id", normalized.get("object_id"))
                normalized.setdefault("disposition", "excluded")
                normalized.setdefault("ordinal", base + ordinal)
                item_data.append(normalized)
        with self._write() as connection:
            self._insert_immutable(
                connection,
                "evidence_manifests",
                "manifest_id=?",
                (manifest_id,),
                ("manifest_id", "run_id", "manifest_hash", "as_of", "created_at", "payload"),
                (
                    manifest_id,
                    _require(data, "run_id"),
                    _require(data, "manifest_hash", "content_hash"),
                    _iso(_require(data, "as_of")),
                    _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                    payload,
                ),
                payload,
            )
            for ordinal, item in enumerate(item_data):
                item_payload = _canonical_json(item)
                item_type = str(_require(item, "item_type", "kind"))
                item_id = str(_require(item, "item_id", "snapshot_id", "derived_artifact_id"))
                self._insert_immutable(
                    connection,
                    "evidence_manifest_items",
                    "manifest_id=? AND item_type=? AND item_id=?",
                    (manifest_id, item_type, item_id),
                    ("manifest_id", "item_type", "item_id", "disposition", "ordinal", "payload"),
                    (
                        manifest_id,
                        item_type,
                        item_id,
                        _value(item, "disposition", default="included"),
                        int(_value(item, "ordinal", default=ordinal)),
                        item_payload,
                    ),
                    item_payload,
                )

    @staticmethod
    def _normalize_manifest_items(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized_items: list[dict[str, Any]] = []
        for ordinal, item in enumerate(items):
            normalized = dict(item)
            if _value(normalized, "snapshot_id") is not None:
                normalized.setdefault("item_type", "snapshot")
                normalized.setdefault("item_id", normalized["snapshot_id"])
                normalized.setdefault("disposition", "included")
            elif _value(normalized, "object_type") is not None:
                normalized.setdefault("item_type", normalized["object_type"])
                normalized.setdefault("item_id", normalized.get("object_id"))
                normalized.setdefault("disposition", "excluded")
            normalized.setdefault("ordinal", ordinal)
            normalized_items.append(normalized)
        return normalized_items

    def get_evidence_manifest(self, manifest_id: str) -> Any:
        return self._get_typed(
            "evidence_manifests", "manifest_id", manifest_id, "EvidenceSnapshotManifest"
        )

    def list_evidence_manifests(
        self, *, run_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[Any]:
        where = "WHERE run_id=?" if run_id else ""
        args: tuple[Any, ...] = (run_id,) if run_id else ()
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM evidence_manifests {where} "
                "ORDER BY created_at, manifest_id LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_load_model("EvidenceSnapshotManifest", row["payload"]) for row in rows]

    def save_storage_namespace(
        self, namespace: Any, *, connection: sqlite3.Connection | None = None
    ) -> None:
        data = _payload(namespace)
        if connection is not None:
            self._save_storage_namespace(connection, data, _canonical_json(data))
            return
        with self._write() as own:
            self._save_storage_namespace(own, data, _canonical_json(data))

    def _save_storage_namespace(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        namespace_id = str(_require(data, "namespace_id"))
        self._insert_immutable(
            connection,
            "storage_namespaces",
            "namespace_id=?",
            (namespace_id,),
            (
                "namespace_id", "binding_nonce", "layout_version",
                "database_identity_hash", "data_root_identity_hash", "created_at", "payload",
            ),
            (
                namespace_id,
                _require(data, "binding_nonce"),
                int(_require(data, "layout_version")),
                _require(data, "database_identity_hash"),
                _require(data, "data_root_identity_hash"),
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def get_storage_namespace(self, namespace_id: str | None = None) -> Any:
        with self._connect(readonly=True) as connection:
            if namespace_id is None:
                rows = connection.execute(
                    "SELECT payload FROM storage_namespaces ORDER BY created_at, namespace_id"
                ).fetchall()
                if len(rows) != 1:
                    raise AcquisitionStorageError(
                        "database must contain exactly one storage namespace"
                    )
                payload = rows[0]["payload"]
            else:
                payload = self._get_payload(
                    connection,
                    "storage_namespaces",
                    "namespace_id=?",
                    (namespace_id,),
                    label="storage namespace",
                )
        return _load_model("StorageNamespace", payload)

    # Checkpoints and atomic finalize ----------------------------------------
    def save_checkpoint(
        self,
        checkpoint: Any,
        *,
        expected_parent_version: int | None,
        owner_token: str | None = None,
        run_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        data = _payload(checkpoint)
        with self._write() as connection:
            self._save_checkpoint(
                connection,
                data,
                _canonical_json(data),
                expected_parent_version=expected_parent_version,
                owner_token=owner_token,
                run_id_override=run_id,
                lease_epoch_override=lease_epoch,
            )

    append_checkpoint = save_checkpoint

    def _save_checkpoint(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        *,
        expected_parent_version: int | None,
        owner_token: str | None,
        run_id_override: str | None = None,
        lease_epoch_override: int | None = None,
    ) -> None:
        checkpoint_id = str(_require(data, "checkpoint_id"))
        run_value = run_id_override or _value(data, "run_id", "latest_successful_run_id")
        if run_value is None:
            raise AcquisitionStorageError("checkpoint write requires its creating run_id")
        run_id = str(run_value)
        epoch_value = lease_epoch_override or _value(data, "lease_epoch", "finalized_lease_epoch")
        if epoch_value is None:
            raise AcquisitionStorageError("checkpoint write requires lease_epoch")
        epoch = int(epoch_value)
        self._assert_lease(connection, run_id, epoch, owner_token=owner_token)
        run_row = connection.execute(
            "SELECT ticker, question_set_version, payload FROM acquisition_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise AcquisitionNotFoundError(f"run not found: {run_id}")
        key = (
            str(_require(data, "ticker")),
            str(_require(data, "source_definition_id")),
            str(_require(data, "source_definition_version")),
            str(_require(data, "question_set_version")),
        )
        if key[0] != run_row["ticker"] or key[3] != run_row["question_set_version"]:
            raise AcquisitionStorageError(
                "checkpoint ticker/question set does not match its creating run"
            )
        run_payload = json.loads(run_row["payload"])
        source_refs = {
            (
                str(_require(reference, "source_definition_id")),
                str(_require(reference, "version", "source_definition_version")),
            )
            for reference in _value(run_payload, "source_definition_refs", default=())
        }
        if (key[1], key[2]) not in source_refs:
            raise AcquisitionStorageError(
                "checkpoint source definition is not frozen by its creating run"
            )
        latest = connection.execute(
            "SELECT checkpoint_id, checkpoint_version FROM source_checkpoints "
            "WHERE ticker=? AND source_definition_id=? AND source_definition_version=? "
            "AND question_set_version=? ORDER BY checkpoint_version DESC LIMIT 1",
            key,
        ).fetchone()
        actual_parent = None if latest is None else int(latest["checkpoint_version"])
        if actual_parent != expected_parent_version:
            raise CheckpointConflictError(
                f"checkpoint parent changed: expected {expected_parent_version}, got {actual_parent}"
            )
        version = int(_require(data, "checkpoint_version", "version"))
        expected_version = 1 if actual_parent is None else actual_parent + 1
        if version != expected_version:
            raise CheckpointConflictError(
                f"checkpoint version must be {expected_version}, got {version}"
            )
        parent_id = _value(data, "parent_checkpoint_id")
        if latest is not None and parent_id != latest["checkpoint_id"]:
            raise CheckpointConflictError("checkpoint parent id is not the current version")
        if latest is None and parent_id is not None:
            raise CheckpointConflictError("first checkpoint cannot have a parent")
        self._insert_immutable(
            connection,
            "source_checkpoints",
            "checkpoint_id=?",
            (checkpoint_id,),
            (
                "checkpoint_id", "ticker", "source_definition_id",
                "source_definition_version", "question_set_version",
                "checkpoint_version", "parent_checkpoint_id", "safe_through",
                "source_lower_bound", "run_id", "lease_epoch", "created_at", "payload",
            ),
            (
                checkpoint_id,
                *key,
                version,
                parent_id,
                self._position_json(_value(data, "safe_through", "source_safe_through")),
                self._position_json(_value(data, "source_lower_bound")),
                run_id,
                epoch,
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )
        for barrier in _value(data, "barriers", "opening_barriers", default=[]) or []:
            barrier_data = _payload(barrier)
            if "checkpoint_id" not in barrier_data:
                barrier_data["checkpoint_id"] = checkpoint_id
            self._ensure_barrier_query_semantics(connection, barrier_data)
            self._save_barrier(connection, barrier_data, _canonical_json(barrier_data))

    def _save_barrier(
        self, connection: sqlite3.Connection, data: Mapping[str, Any], payload: str
    ) -> None:
        barrier_id = str(_require(data, "barrier_id"))
        work = _value(data, "work_position", default="")
        work_json = work if isinstance(work, str) else json.dumps(work, sort_keys=True, separators=(",", ":"))
        checkpoint_id = str(_require(data, "checkpoint_id"))
        checkpoint = connection.execute(
            "SELECT source_definition_id, source_definition_version FROM source_checkpoints "
            "WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        if checkpoint is None:
            raise AcquisitionNotFoundError(f"checkpoint not found: {checkpoint_id}")
        opening_attempt_id = str(_require(data, "opening_attempt_id"))
        opening = connection.execute(
            "SELECT a.source_definition_id, a.source_definition_version, "
            "a.retry_group_id, a.work_position, p.partition_key "
            "FROM acquisition_attempts a JOIN physical_query_plan_items p "
            "ON p.plan_item_id=a.physical_query_plan_item_id WHERE a.attempt_id=?",
            (opening_attempt_id,),
        ).fetchone()
        if opening is None:
            raise AcquisitionNotFoundError(
                f"opening attempt not found: {opening_attempt_id}"
            )
        # A paginated attempt starts at one position but can fail on a later
        # page. Its durable terminal event owns the actual blocking position.
        terminal = connection.execute(
            "SELECT payload FROM acquisition_attempt_events WHERE attempt_id=? "
            "AND event_type='outcome_terminal'", (opening_attempt_id,),
        ).fetchone()
        opening_position = opening["work_position"]
        if terminal is not None:
            opening_position = json.loads(terminal["payload"]).get(
                "protocol_summary", {}
            ).get("work_position", opening_position)
        expected = {
            "source_definition_id": checkpoint["source_definition_id"],
            "source_definition_version": str(checkpoint["source_definition_version"]),
            "partition_key": opening["partition_key"],
            "work_position": opening_position,
            "retry_group_id": opening["retry_group_id"],
        }
        if (
            opening["source_definition_id"] != checkpoint["source_definition_id"]
            or str(opening["source_definition_version"])
            != str(checkpoint["source_definition_version"])
        ):
            raise AcquisitionStorageError(
                "opening barrier attempt does not match checkpoint source"
            )
        for field, expected_value in expected.items():
            actual = _value(data, field)
            if field == "work_position" and actual is not None and not isinstance(actual, str):
                actual = json.dumps(actual, sort_keys=True, separators=(",", ":"))
            if str(actual) != str(expected_value):
                raise AcquisitionStorageError(
                    f"opening barrier {field} does not match its exact work position"
                )
        self._insert_immutable(
            connection,
            "checkpoint_barriers",
            "barrier_id=?",
            (barrier_id,),
            (
                "barrier_id", "checkpoint_id", "source_definition_id",
                "source_definition_version", "partition_key", "work_position",
                "canonical_resource_id", "retry_group_id", "opening_attempt_id",
                "query_semantics_hash", "created_at", "payload",
            ),
            (
                barrier_id,
                checkpoint_id,
                expected["source_definition_id"],
                expected["source_definition_version"],
                expected["partition_key"],
                work_json,
                _value(data, "canonical_resource_id"),
                expected["retry_group_id"],
                opening_attempt_id,
                _require(data, "query_semantics_hash"),
                _iso(_value(data, "created_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def get_checkpoint(self, checkpoint_id: str) -> Any:
        return self._get_typed(
            "source_checkpoints", "checkpoint_id", checkpoint_id, "SourceCheckpoint"
        )

    def checkpoint_run_history(self, checkpoint_id: str) -> tuple[Any, ...]:
        """Read the exact CAS ancestry, never infer history from recent runs."""
        run_ids, seen = [], set()
        child_version = None
        identity = None
        with self._connect(readonly=True) as connection:
            while checkpoint_id:
                if checkpoint_id in seen:
                    raise CheckpointConflictError("checkpoint ancestry cycle")
                seen.add(checkpoint_id)
                row = connection.execute(
                    "SELECT run_id,payload FROM source_checkpoints WHERE checkpoint_id=?",
                    (checkpoint_id,),
                ).fetchone()
                if row is None:
                    raise AcquisitionNotFoundError(checkpoint_id)
                checkpoint = _load_model("SourceCheckpoint", row["payload"])
                key = (checkpoint.ticker, checkpoint.source_definition_id,
                       checkpoint.source_definition_version, checkpoint.question_set_version)
                if identity is not None and key != identity:
                    raise CheckpointConflictError("checkpoint ancestry identity mismatch")
                identity = key
                if child_version is not None and checkpoint.checkpoint_version != child_version - 1:
                    raise CheckpointConflictError("checkpoint ancestry version gap")
                child_version = checkpoint.checkpoint_version
                run_ids.append(row["run_id"])
                checkpoint_id = checkpoint.parent_checkpoint_id
        return tuple(self.get_run(run_id) for run_id in dict.fromkeys(run_ids))

    def latest_checkpoint(
        self,
        ticker: str,
        source_definition_id: str,
        source_definition_version: str | int,
        question_set_version: str,
    ) -> Any | None:
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT payload FROM source_checkpoints WHERE ticker=? "
                "AND source_definition_id=? AND source_definition_version=? "
                "AND question_set_version=? ORDER BY checkpoint_version DESC LIMIT 1",
                (
                    ticker,
                    source_definition_id,
                    str(source_definition_version),
                    question_set_version,
                ),
            ).fetchone()
        return None if row is None else _load_model("SourceCheckpoint", row["payload"])

    def get_checkpoint_barrier(self, barrier_id: str) -> dict[str, Any]:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection,
                "checkpoint_barriers",
                "barrier_id=?",
                (barrier_id,),
                label="checkpoint barrier",
            )
        return json.loads(payload)

    def list_checkpoint_barriers(
        self,
        *,
        checkpoint_id: str | None = None,
        source_definition_id: str | None = None,
        partition_key: str | None = None,
        retry_group_id: str | None = None,
        unresolved_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (
            ("b.checkpoint_id", checkpoint_id),
            ("b.source_definition_id", source_definition_id),
            ("b.partition_key", partition_key),
            ("b.retry_group_id", retry_group_id),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        if unresolved_only:
            clauses.append("r.barrier_id IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT b.payload FROM checkpoint_barriers b LEFT JOIN barrier_resolutions r "
                "ON r.barrier_id=b.barrier_id "
                f"{where} ORDER BY b.created_at, b.barrier_id",
                tuple(args),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def append_barrier_resolution(
        self,
        resolution: Any,
        *,
        owner_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        data = _payload(resolution)
        with self._write() as connection:
            self._append_barrier_resolution(
                connection,
                data,
                _canonical_json(data),
                owner_token=owner_token,
                lease_epoch=lease_epoch,
            )

    def open_barrier(self, barrier: Mapping[str, Any] | Any) -> None:
        data = _payload(barrier)
        with self._write() as connection:
            self._ensure_barrier_query_semantics(connection, data)
            self._save_barrier(connection, data, _canonical_json(data))

    @staticmethod
    def _ensure_barrier_query_semantics(
        connection: sqlite3.Connection, data: dict[str, Any]
    ) -> None:
        opening_attempt_id = str(_require(data, "opening_attempt_id"))
        row = connection.execute(
            "SELECT a.physical_query_plan_item_id, p.query_id, p.execution_key, "
            "p.partition_key, p.time_start, p.time_end, p.pagination_fingerprint "
            "FROM acquisition_attempts a JOIN physical_query_plan_items p "
            "ON p.plan_item_id=a.physical_query_plan_item_id WHERE a.attempt_id=?",
            (opening_attempt_id,),
        ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(
                f"opening attempt not found: {opening_attempt_id}"
            )
        semantics = json.dumps(dict(row), sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(
            semantics.encode("utf-8")
        ).hexdigest()
        supplied = _value(data, "query_semantics_hash")
        if supplied is not None and supplied != expected:
            raise AcquisitionStorageError(
                "opening barrier query semantics hash does not match its plan"
            )
        data["query_semantics_hash"] = expected

    def _append_barrier_resolution(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        payload: str,
        *,
        owner_token: str | None,
        lease_epoch: int | None,
    ) -> None:
        resolution_id = str(_require(data, "barrier_resolution_id", "resolution_id"))
        barrier_id = str(_require(data, "barrier_id", "opening_barrier_id"))
        barrier = connection.execute(
            "SELECT * FROM checkpoint_barriers WHERE barrier_id=?", (barrier_id,)
        ).fetchone()
        if barrier is None:
            raise BarrierResolutionError("opening barrier does not exist")
        if connection.execute(
            "SELECT 1 FROM barrier_resolutions WHERE barrier_id=?", (barrier_id,)
        ).fetchone() is not None:
            existing = connection.execute(
                "SELECT payload FROM barrier_resolutions WHERE barrier_id=?", (barrier_id,)
            ).fetchone()
            if existing["payload"] == payload:
                return
            raise BarrierResolutionError("barrier already has an immutable resolution")
        attempt_id = str(_require(data, "resolving_attempt_id"))
        attempt = connection.execute(
            "SELECT run_id, source_definition_id, source_definition_version, retry_group_id "
            "FROM acquisition_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise BarrierResolutionError("resolving attempt does not exist")
        epoch = int(lease_epoch or _require(data, "lease_epoch"))
        self._assert_lease(
            connection, attempt["run_id"], epoch, owner_token=owner_token
        )
        opening_attempt_id = str(_require(data, "opening_attempt_id"))
        if opening_attempt_id != barrier["opening_attempt_id"]:
            raise BarrierResolutionError("resolution opening attempt does not match barrier")
        opening = connection.execute(
            "SELECT a.*, p.partition_key, p.query_id, p.time_start, p.time_end, "
            "p.pagination_fingerprint FROM acquisition_attempts a "
            "JOIN physical_query_plan_items p ON p.plan_item_id=a.physical_query_plan_item_id "
            "WHERE a.attempt_id=?",
            (opening_attempt_id,),
        ).fetchone()
        resolving_plan = connection.execute(
            "SELECT a.*, p.partition_key, p.query_id, p.time_start, p.time_end, "
            "p.pagination_fingerprint FROM acquisition_attempts a "
            "JOIN physical_query_plan_items p ON p.plan_item_id=a.physical_query_plan_item_id "
            "WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if opening is None or resolving_plan is None:
            raise BarrierResolutionError("barrier attempt lineage is incomplete")
        semantic_columns = (
            "source_definition_id", "source_definition_version", "partition_key",
            "query_id", "time_start", "time_end", "pagination_fingerprint",
        )
        if any(str(opening[name]) != str(resolving_plan[name]) for name in semantic_columns):
            raise BarrierResolutionError("resolving attempt has incompatible query semantics")
        if int(resolving_plan["retry_ordinal"]) <= int(opening["retry_ordinal"]):
            raise BarrierResolutionError(
                "barrier must be resolved by a later retry attempt"
            )
        fields = (
            ("source_definition_id", attempt["source_definition_id"]),
            ("source_definition_version", str(attempt["source_definition_version"])),
            ("partition_key", resolving_plan["partition_key"]),
            ("work_position", resolving_plan["work_position"]),
            ("canonical_resource_id", barrier["canonical_resource_id"]),
            ("query_semantics_hash", barrier["query_semantics_hash"]),
        )
        for field, attempt_default in fields:
            expected = barrier[field]
            actual = _value(data, field, default=attempt_default)
            if field == "work_position" and not isinstance(actual, str):
                actual = json.dumps(actual, sort_keys=True, separators=(",", ":"))
            if actual != expected:
                raise BarrierResolutionError(f"resolution does not match barrier {field}")
        resolution_retry_group = _value(data, "retry_group_id")
        if resolution_retry_group != attempt["retry_group_id"]:
            raise BarrierResolutionError("resolution retry group does not match attempt")
        if barrier["retry_group_id"] and attempt["retry_group_id"] != barrier["retry_group_id"]:
            raise BarrierResolutionError("resolution attempt has an incompatible retry group")
        outcome = connection.execute(
            "SELECT outcome FROM acquisition_attempt_events WHERE attempt_id=? "
            "AND outcome IN ('success','unchanged','no_data')",
            (attempt_id,),
        ).fetchone()
        if outcome is None:
            raise BarrierResolutionError("resolving attempt lacks a successful terminal outcome")
        proof_id = _value(data, "proof_id", "discovery_proof_id")
        snapshot_id = _value(data, "snapshot_id")
        observation_id = _value(data, "observation_id", "resource_observation_id")
        if proof_id is not None:
            proof = connection.execute(
                "SELECT o.attempt_id, o.page_ordinal, o.cursor FROM discovery_proofs p JOIN discovery_observations o "
                "ON o.observation_id=p.observation_id WHERE p.proof_id=?",
                (proof_id,),
            ).fetchone()
            if proof is None or proof["attempt_id"] != attempt_id:
                raise BarrierResolutionError(
                    "barrier discovery proof does not belong to the resolving attempt"
                )
            try:
                position = json.loads(barrier["work_position"])
            except (ValueError, TypeError):
                position = None
            if isinstance(position, dict) and position.get("kind") == "discovery":
                if (position.get("page") != proof["page_ordinal"]
                        or position.get("cursor") != proof["cursor"]):
                    raise BarrierResolutionError("discovery proof does not cover the exact barrier page/cursor")
            elif resolving_plan["work_position"] != barrier["work_position"]:
                raise BarrierResolutionError("discovery proof does not cover the exact barrier position")
        elif snapshot_id is not None and observation_id is not None:
            observation = connection.execute(
                "SELECT attempt_id, snapshot_id FROM resource_observations "
                "WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if (
                observation is None
                or observation["attempt_id"] != attempt_id
                or observation["snapshot_id"] != snapshot_id
            ):
                raise BarrierResolutionError(
                    "barrier snapshot observation does not belong to the resolving attempt"
                )
        else:
            raise BarrierResolutionError(
                "barrier resolution requires a discovery proof or snapshot observation"
            )
        self._insert_immutable(
            connection,
            "barrier_resolutions",
            "barrier_resolution_id=?",
            (resolution_id,),
            (
                "barrier_resolution_id", "barrier_id", "resolving_attempt_id",
                "source_definition_id", "source_definition_version", "partition_key",
                "work_position", "canonical_resource_id", "query_semantics_hash",
                "proof_id", "snapshot_id", "observation_id", "created_at", "payload",
            ),
            (
                resolution_id,
                barrier_id,
                attempt_id,
                barrier["source_definition_id"],
                barrier["source_definition_version"],
                barrier["partition_key"],
                barrier["work_position"],
                barrier["canonical_resource_id"],
                barrier["query_semantics_hash"],
                proof_id,
                snapshot_id,
                observation_id,
                _iso(_value(data, "created_at", "resolved_at"), default=datetime.now(timezone.utc)),
                payload,
            ),
            payload,
        )

    def list_barrier_resolutions(self, *, barrier_id: str | None = None) -> list[Any]:
        where = "WHERE barrier_id=?" if barrier_id else ""
        args: tuple[Any, ...] = (barrier_id,) if barrier_id else ()
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                f"SELECT payload FROM barrier_resolutions {where} "
                "ORDER BY created_at, barrier_resolution_id",
                args,
            ).fetchall()
        return [_load_model("BarrierResolution", row["payload"]) for row in rows]

    def finalize_run(
        self,
        run_id: str,
        *,
        run_event: Any,
        coverage_resolutions: Iterable[Any] = (),
        checkpoints: Iterable[tuple[Any, int | None]] = (),
        opening_barriers: Iterable[Any] = (),
        barrier_resolutions: Iterable[Any] = (),
        owner_token: str,
        lease_epoch: int,
    ) -> None:
        event_data = _payload(run_event)
        coverage_data = [_payload(item) for item in coverage_resolutions]
        checkpoint_data = [(_payload(item), parent) for item, parent in checkpoints]
        opening_barrier_data = [_payload(item) for item in opening_barriers]
        barrier_data = [_payload(item) for item in barrier_resolutions]
        with self._write() as connection:
            self._assert_lease(
                connection, run_id, lease_epoch, owner_token=owner_token
            )
            if str(_require(event_data, "run_id")) != run_id:
                raise AcquisitionStorageError("final run event references another run")
            if str(_require(event_data, "event_type", "state", "status")) != "finalized":
                raise AcquisitionStorageError(
                    "finalize_run requires a finalized run event"
                )
            if int(_require(event_data, "lease_epoch")) != int(lease_epoch):
                raise StaleLeaseError(
                    "finalized run event lease epoch does not match the owner"
                )
            existing_terminal = connection.execute(
                "SELECT event_id, payload FROM acquisition_run_events "
                "WHERE run_id=? AND event_type='finalized'",
                (run_id,),
            ).fetchone()
            if existing_terminal is not None:
                if existing_terminal["payload"] == _canonical_json(event_data):
                    raise AcquisitionStorageError("run was already finalized")
                raise ImmutableRecordError("finalized run event cannot be superseded")

            checkpoint_ids = {
                str(_require(item, "checkpoint_id")): item
                for item, _ in checkpoint_data
            }
            resolving_barrier_ids = {
                str(_require(item, "barrier_id", "opening_barrier_id"))
                for item in barrier_data
            }
            for barrier in opening_barrier_data:
                checkpoint_id = str(_require(barrier, "checkpoint_id"))
                checkpoint = checkpoint_ids.get(checkpoint_id)
                if checkpoint is None:
                    raise AcquisitionStorageError(
                        "opening barrier must be committed with its checkpoint"
                    )
                unresolved = set(
                    _value(checkpoint, "unresolved_barrier_ids", default=()) or ()
                )
                unresolved.update(
                    barrier_id
                    for partition in _value(checkpoint, "partitions", default=()) or ()
                    for barrier_id in (
                        _value(partition, "unresolved_barrier_ids", default=()) or ()
                    )
                )
                barrier_id = str(_require(barrier, "barrier_id"))
                if (
                    barrier_id not in unresolved
                    and barrier_id not in resolving_barrier_ids
                ):
                    raise AcquisitionStorageError(
                        "opening barrier is neither unresolved nor atomically resolved"
                    )
            for item in coverage_data:
                self._append_coverage_resolution(
                    connection, item, _canonical_json(item)
                )
            for item, expected_parent in checkpoint_data:
                self._save_checkpoint(
                    connection,
                    item,
                    _canonical_json(item),
                    expected_parent_version=expected_parent,
                    owner_token=owner_token,
                    run_id_override=run_id,
                    lease_epoch_override=lease_epoch,
                )
            for item in opening_barrier_data:
                self._ensure_barrier_query_semantics(connection, item)
                self._save_barrier(connection, item, _canonical_json(item))
            # A protocol retry may fail and recover within one executor call.
            # Persist its checkpoint and immutable opening barrier before the
            # resolution so both audit records can commit atomically while the
            # resulting checkpoint exposes only currently unresolved gaps.
            for item in barrier_data:
                self._append_barrier_resolution(
                    connection,
                    item,
                    _canonical_json(item),
                    owner_token=owner_token,
                    lease_epoch=lease_epoch,
                )
            for checkpoint_id, checkpoint in checkpoint_ids.items():
                unresolved = set(
                    _value(checkpoint, "unresolved_barrier_ids", default=()) or ()
                )
                unresolved.update(
                    barrier_id
                    for partition in _value(checkpoint, "partitions", default=()) or ()
                    for barrier_id in (
                        _value(partition, "unresolved_barrier_ids", default=()) or ()
                    )
                )
                for barrier_id in unresolved:
                    row = connection.execute(
                        "SELECT b.barrier_id, r.barrier_resolution_id "
                        "FROM checkpoint_barriers b LEFT JOIN barrier_resolutions r "
                        "ON r.barrier_id=b.barrier_id WHERE b.barrier_id=?",
                        (barrier_id,),
                    ).fetchone()
                    if row is None:
                        raise AcquisitionStorageError(
                            f"checkpoint references unknown barrier: {barrier_id}"
                        )
                    if row["barrier_resolution_id"] is not None:
                        raise AcquisitionStorageError(
                            f"checkpoint keeps a resolved barrier open: {barrier_id}"
                        )
            self._append_run_event(connection, event_data, _canonical_json(event_data))

    # Helpers ----------------------------------------------------------------
    @staticmethod
    def _attempt_run_id(connection: sqlite3.Connection, attempt_id: str) -> str:
        row = connection.execute(
            "SELECT run_id FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise AcquisitionNotFoundError(f"attempt not found: {attempt_id}")
        return str(row["run_id"])

    @staticmethod
    def _position_json(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _get_typed(self, table: str, key: str, value: str, model_name: str) -> Any:
        with self._connect(readonly=True) as connection:
            payload = self._get_payload(
                connection, table, f"{key}=?", (value,), label=model_name
            )
        return _load_model(model_name, payload)


# Short compatibility aliases used by callers while the acquisition package is adopted.
Repository = AcquisitionRepository
NotFoundError = AcquisitionNotFoundError
