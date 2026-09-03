from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .migrations import (
    LATEST_SCHEMA_VERSION,
    BackupManifest,
    DatabasePreflight,
    MigrationCoordinator,
    MigrationError,
    create_verified_backup,
    inspect_database,
    validate_preflight,
)


BINDING_INTENT_SUFFIX = ".acquisition-binding-intent"
ROOT_MARKER_NAME = ".acquisition-storage-namespace.json"
LAYOUT_VERSION = 1


class StorageBootstrapError(RuntimeError):
    pass


class StorageNamespaceMismatch(StorageBootstrapError):
    pass


class StorageRepairRequired(StorageBootstrapError):
    pass


class BootstrapLockTimeout(StorageBootstrapError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    namespace: Any
    database_path: Path
    data_root: Path
    created: bool
    backups: tuple[BackupManifest, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _identity(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageRepairRequired(f"invalid storage control file: {path.name}") from exc
    if not isinstance(value, dict):
        raise StorageRepairRequired(f"invalid storage control file: {path.name}")
    return value


class InterProcessFileLock:
    """A crash-releasing one-byte advisory lock stored outside evidence roots."""

    def __init__(self, identity: str, *, timeout_seconds: float = 5.0) -> None:
        lock_root = Path(tempfile.gettempdir()) / "astravalue-acquisition-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        self.path = lock_root / f"{identity}.lock"
        self.timeout_seconds = timeout_seconds
        self._stream: Any = None

    def __enter__(self) -> "InterProcessFileLock":
        deadline = time.monotonic() + self.timeout_seconds
        self._stream = self.path.open("a+b")
        self._stream.seek(0, os.SEEK_END)
        if self._stream.tell() == 0:
            self._stream.write(b"0")
            self._stream.flush()
        while True:
            try:
                self._lock_nonblocking()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise BootstrapLockTimeout(
                        "storage bootstrap lock remained busy past its deadline"
                    )
                time.sleep(0.025)

    def _lock_nonblocking(self) -> None:
        self._stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stream is None:
            return
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None


class StorageBootstrapper:
    """Bind one SQLite database to exactly one evidence root before runtime use."""

    _FAULT_ALIASES = {
        "after_db_intent": "crash_after_db_intent_before_root_marker",
        "after_0005": "crash_after_0005_before_v5_backup",
        "after_v5_backup": "crash_after_v5_backup_before_0006",
        "after_db_namespace_commit": "crash_after_db_namespace_commit",
    }

    def __init__(
        self,
        db_path: Path | str,
        data_root: Path | str,
        *,
        busy_timeout_ms: int = 5_000,
        lock_timeout_seconds: float = 5.0,
        fault_injector: Callable[[str], None] | None = None,
        fault_at: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.data_root = Path(data_root)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self.fault_injector = fault_injector
        self.fault_at = fault_at
        self.database_identity_hash = _identity(self.db_path)
        self.data_root_identity_hash = _identity(self.data_root)
        self.intent_path = Path(str(self.db_path) + BINDING_INTENT_SUFFIX)
        self.marker_path = self.data_root / ROOT_MARKER_NAME

    def preflight(self) -> DatabasePreflight:
        result = inspect_database(self.db_path)
        validate_preflight(result)
        return result

    def backup(self, *, label: str | None = None) -> BackupManifest:
        """Non-migrating backup command: never creates intent, marker, namespace or run."""

        return create_verified_backup(
            self.db_path, self.data_root, label=label or "manual"
        )

    def bootstrap(self) -> BootstrapResult:
        lock_names = sorted(
            (
                f"database-{self.database_identity_hash}",
                f"data-root-{self.data_root_identity_hash}",
            )
        )
        with ExitStack() as stack:
            for name in lock_names:
                stack.enter_context(
                    InterProcessFileLock(
                        name, timeout_seconds=self.lock_timeout_seconds
                    )
                )
            return self._bootstrap_locked()

    initialize = bootstrap

    def _bootstrap_locked(self) -> BootstrapResult:
        preflight = self.preflight()
        intent = _read_json(self.intent_path)
        marker = _read_json(self.marker_path)
        namespace_row = self._read_namespace_row(preflight.version)

        # Inspect both existing control records before writing either side.
        if intent is not None:
            self._validate_pairing(intent, "binding intent")
        if marker is not None:
            self._validate_pairing(marker, "data-root marker")

        if namespace_row is not None:
            return self._resume_bound(namespace_row, intent, marker)

        if marker is not None and marker.get("state") == "bound":
            raise StorageRepairRequired(
                "data-root marker is bound but database namespace row is missing"
            )
        if marker is not None and intent is None:
            raise StorageRepairRequired(
                "pending data-root marker has no authoritative database-side intent"
            )

        created = intent is None
        if intent is None:
            intent = self._new_intent(preflight)
            # The DB-side journal is deliberately durable before creating the root.
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self.intent_path, intent)
            self._fault("after_db_intent")
        else:
            self._validate_resume_state(intent, preflight)

        if marker is None:
            marker = self._marker_from_intent(intent, state="pending")
            _atomic_write_json(self.marker_path, marker)
            self._fault("after_root_pending")
        else:
            self._validate_nonce(intent, marker)
            if marker.get("state") != "pending":
                raise StorageRepairRequired("unexpected root marker stage")

        backups = self._advance_migrations(intent)
        intent = _read_json(self.intent_path)
        assert intent is not None
        current = self.preflight()
        if current.version != LATEST_SCHEMA_VERSION:
            raise StorageRepairRequired("bootstrap did not reach schema v6")

        namespace = self._namespace_from_intent(intent)
        self._commit_namespace(namespace)
        intent = self._advance_intent(intent, "committed_v6")
        self._fault("after_db_namespace_commit")

        bound_marker = self._marker_from_intent(intent, state="bound")
        bound_marker["bootstrap_stage"] = "marker_bound"
        _atomic_write_json(self.marker_path, bound_marker)
        intent = self._advance_intent(intent, "marker_bound")
        self._fault("after_marker_bound")
        return BootstrapResult(
            namespace=self._coerce_namespace(namespace),
            database_path=self.db_path,
            data_root=self.data_root,
            created=created,
            backups=tuple(backups),
        )

    def _resume_bound(
        self,
        namespace: dict[str, Any],
        intent: dict[str, Any] | None,
        marker: dict[str, Any] | None,
    ) -> BootstrapResult:
        self._validate_pairing(namespace, "database namespace")
        if marker is None:
            raise StorageRepairRequired(
                "database namespace is bound but data-root marker is missing"
            )
        self._validate_nonce(namespace, marker)
        if intent is not None:
            self._validate_nonce(namespace, intent)
        if marker.get("state") == "pending":
            if intent is None:
                raise StorageRepairRequired(
                    "pending marker cannot be finalized without its binding intent"
                )
            bound_marker = self._marker_from_intent(intent, state="bound")
            bound_marker["bootstrap_stage"] = "marker_bound"
            _atomic_write_json(self.marker_path, bound_marker)
            if intent.get("bootstrap_stage") != "marker_bound":
                self._advance_intent(intent, "marker_bound")
        elif marker.get("state") != "bound":
            raise StorageRepairRequired("unexpected root marker state")
        return BootstrapResult(
            namespace=self._coerce_namespace(namespace),
            database_path=self.db_path,
            data_root=self.data_root,
            created=False,
        )

    def _advance_migrations(self, initial_intent: dict[str, Any]) -> list[BackupManifest]:
        intent = initial_intent
        source_version = int(intent["source_user_version"])
        stage = str(intent["bootstrap_stage"])
        coordinator = MigrationCoordinator(
            self.db_path, busy_timeout_ms=self.busy_timeout_ms
        )
        backups: list[BackupManifest] = []
        current = self.preflight()

        if source_version == 0:
            if current.version == 0:
                coordinator.bootstrap_fresh()
                self._fault("after_0006")
            elif current.version != 6:
                raise StorageRepairRequired("fresh journal has an unexpected database version")
            return backups

        if source_version == 4:
            if current.version == 4 and stage == "preflight":
                manifest = create_verified_backup(
                    self.db_path, self.data_root, label="pre-v5"
                )
                backups.append(manifest)
                intent = self._record_manifest(intent, "backup_v4", manifest)
                intent = self._advance_intent(intent, "backup_v4_verified")
                stage = "backup_v4_verified"
                self._fault("after_v4_backup")
            if current.version == 4 and stage == "backup_v4_verified":
                coordinator.apply_0005()
                current = self.preflight()
                self._fault("after_0005")
            if current.version == 5 and stage == "backup_v4_verified":
                self._validate_migration_record(current, "0005_dimensional_verification_status")
                intent = self._advance_intent(intent, "migrated_v5")
                stage = "migrated_v5"
            if current.version not in {5, 6}:
                raise StorageRepairRequired("v4 journal reached an unexpected database version")

        current = self.preflight()
        stage = str((_read_json(self.intent_path) or intent)["bootstrap_stage"])
        if current.version == 5 and stage in {"preflight", "migrated_v5"}:
            # preflight is the legitimate direct-v5 path.
            manifest = create_verified_backup(
                self.db_path, self.data_root, label="recovery-v5"
            )
            backups.append(manifest)
            intent = _read_json(self.intent_path) or intent
            intent = self._record_manifest(intent, "backup_v5", manifest)
            intent = self._advance_intent(intent, "backup_v5_verified")
            stage = "backup_v5_verified"
            self._fault("after_v5_backup")
        if current.version == 5 and stage == "backup_v5_verified":
            coordinator.apply_0006()
            current = self.preflight()
            self._fault("after_0006")
        if current.version == 6:
            self._validate_migration_record(current, "0006_business_model_acquisition_v1")
            if source_version == 4 and "backup_v5" not in (_read_json(self.intent_path) or intent).get(
                "stage_manifest_hashes", {}
            ):
                raise StorageRepairRequired("v4 migration cannot skip its v5 recovery point")
            return backups
        raise StorageRepairRequired("migration journal did not reach v6")

    def _new_intent(self, preflight: DatabasePreflight) -> dict[str, Any]:
        now = _utc_now().isoformat()
        return {
            "namespace_id": str(uuid.uuid4()),
            "binding_nonce": secrets_token(),
            "layout_version": LAYOUT_VERSION,
            "database_identity_hash": self.database_identity_hash,
            "data_root_identity_hash": self.data_root_identity_hash,
            "source_database_fingerprint": preflight.database_fingerprint,
            "source_user_version": preflight.version,
            "bootstrap_stage": "preflight",
            "stage_manifest_hashes": {},
            "created_at": now,
            "updated_at": now,
        }

    def _advance_intent(self, intent: dict[str, Any], stage: str) -> dict[str, Any]:
        updated = dict(intent)
        updated["bootstrap_stage"] = stage
        updated["updated_at"] = _utc_now().isoformat()
        _atomic_write_json(self.intent_path, updated)
        marker = _read_json(self.marker_path)
        if marker is not None and marker.get("state") == "pending":
            marker = dict(marker)
            marker["bootstrap_stage"] = stage
            _atomic_write_json(self.marker_path, marker)
        return updated

    def _record_manifest(
        self, intent: dict[str, Any], key: str, manifest: BackupManifest
    ) -> dict[str, Any]:
        updated = dict(intent)
        manifests = dict(updated.get("stage_manifest_hashes", {}))
        manifests[key] = manifest.content_hash
        updated["stage_manifest_hashes"] = manifests
        updated["updated_at"] = _utc_now().isoformat()
        _atomic_write_json(self.intent_path, updated)
        return updated

    def _namespace_from_intent(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "namespace_id": intent["namespace_id"],
            "binding_nonce": intent["binding_nonce"],
            "layout_version": intent["layout_version"],
            "database_identity_hash": intent["database_identity_hash"],
            "data_root_identity_hash": intent["data_root_identity_hash"],
            "created_at": _utc_now().isoformat(),
        }

    def _marker_from_intent(
        self, intent: Mapping[str, Any], *, state: str
    ) -> dict[str, Any]:
        return {
            "marker_version": 1,
            "state": state,
            "namespace_id": intent["namespace_id"],
            "binding_nonce": intent["binding_nonce"],
            "layout_version": intent["layout_version"],
            "database_identity_hash": intent["database_identity_hash"],
            "data_root_identity_hash": intent["data_root_identity_hash"],
            "bootstrap_stage": intent["bootstrap_stage"],
        }

    def _commit_namespace(self, namespace: Mapping[str, Any]) -> None:
        payload = _canonical_json(dict(namespace))
        connection = sqlite3.connect(
            self.db_path,
            timeout=max(self.busy_timeout_ms, 1) / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM storage_namespaces ORDER BY namespace_id"
            ).fetchall()
            if rows:
                if len(rows) != 1 or rows[0]["payload"] != payload:
                    raise StorageNamespaceMismatch(
                        "database is already bound to another storage namespace"
                    )
            else:
                connection.execute(
                    "INSERT INTO storage_namespaces("
                    "namespace_id,binding_nonce,layout_version,database_identity_hash,"
                    "data_root_identity_hash,created_at,payload) VALUES (?,?,?,?,?,?,?)",
                    (
                        namespace["namespace_id"],
                        namespace["binding_nonce"],
                        namespace["layout_version"],
                        namespace["database_identity_hash"],
                        namespace["data_root_identity_hash"],
                        namespace["created_at"],
                        payload,
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _read_namespace_row(self, version: int) -> dict[str, Any] | None:
        if version != 6 or not self.db_path.exists():
            return None
        from urllib.parse import quote

        uri = f"file:{quote(str(self.db_path.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM storage_namespaces ORDER BY namespace_id"
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise StorageRepairRequired("database has multiple storage namespaces")
        return json.loads(rows[0][0])

    def _validate_pairing(self, value: Mapping[str, Any], label: str) -> None:
        if value.get("database_identity_hash") != self.database_identity_hash:
            raise StorageNamespaceMismatch(f"{label} belongs to another database")
        if value.get("data_root_identity_hash") != self.data_root_identity_hash:
            raise StorageNamespaceMismatch(f"{label} belongs to another data root")
        if int(value.get("layout_version", -1)) != LAYOUT_VERSION:
            raise StorageNamespaceMismatch(f"{label} has an incompatible layout version")

    @staticmethod
    def _validate_nonce(left: Mapping[str, Any], right: Mapping[str, Any]) -> None:
        for key in ("namespace_id", "binding_nonce", "layout_version"):
            if left.get(key) != right.get(key):
                raise StorageRepairRequired(f"storage binding {key} conflict")

    def _validate_resume_state(
        self, intent: Mapping[str, Any], preflight: DatabasePreflight
    ) -> None:
        source_version = int(intent["source_user_version"])
        stage = str(intent["bootstrap_stage"])
        if stage == "marker_bound":
            raise StorageRepairRequired(
                "binding intent is complete but database namespace is missing"
            )
        if preflight.version == source_version and stage in {
            "preflight", "backup_v4_verified", "migrated_v5", "backup_v5_verified"
        }:
            if stage in {"preflight", "backup_v4_verified"} and (
                preflight.database_fingerprint
                != intent["source_database_fingerprint"]
            ):
                raise StorageRepairRequired("source database fingerprint changed unexpectedly")
            return
        allowed = {
            (0, "preflight", 6),
            (4, "backup_v4_verified", 5),
            (4, "migrated_v5", 5),
            (4, "backup_v5_verified", 5),
            (4, "backup_v5_verified", 6),
            (5, "preflight", 5),
            (5, "backup_v5_verified", 6),
        }
        if (source_version, stage, preflight.version) not in allowed:
            raise StorageRepairRequired("database is not at the journal's unique next stage")

    @staticmethod
    def _validate_migration_record(preflight: DatabasePreflight, migration_id: str) -> None:
        if migration_id not in preflight.migration_ids:
            raise StorageRepairRequired(f"missing migration record: {migration_id}")

    def _fault(self, stage: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage)
            alias = self._FAULT_ALIASES.get(stage)
            if alias:
                self.fault_injector(alias)
        if self.fault_at is not None and self.fault_at in {
            stage,
            self._FAULT_ALIASES.get(stage),
        }:
            raise RuntimeError(f"injected bootstrap fault: {stage}")

    @staticmethod
    def _coerce_namespace(value: Mapping[str, Any]) -> Any:
        try:
            from .models import StorageNamespace

            return StorageNamespace.model_validate(dict(value))
        except (ImportError, AttributeError):
            return dict(value)


def secrets_token() -> str:
    # UUID plus randomness remains opaque while keeping the sidecar JSON portable.
    import secrets

    return secrets.token_urlsafe(32)


def bootstrap_storage(
    db_path: Path | str,
    data_root: Path | str,
    **kwargs: Any,
) -> BootstrapResult:
    return StorageBootstrapper(db_path, data_root, **kwargs).bootstrap()


def backup_database(
    db_path: Path | str,
    data_root: Path | str,
    *,
    label: str | None = None,
) -> BackupManifest:
    return StorageBootstrapper(db_path, data_root).backup(label=label)


# Public naming used by the design document.
MigrationCoordinator = MigrationCoordinator
