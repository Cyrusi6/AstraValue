from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT_MARKER = ".acquisition-storage-namespace.json"
BINDING_INTENT_SUFFIX = ".acquisition-binding-intent"


class ConsistencyError(RuntimeError):
    pass


def _identity(path: Path) -> str:
    return hashlib.sha256(
        os.path.normcase(str(path.resolve(strict=False))).encode("utf-8")
    ).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConsistencyError(f"{label}不可读: {path.name}") from exc
    if not isinstance(value, dict):
        raise ConsistencyError(f"{label}不是JSON对象")
    return value


def _scalar(connection: sqlite3.Connection, sql: str, parameters: tuple = ()) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return int(row[0]) if row else 0


def _assert_zero(
    connection: sqlite3.Connection,
    label: str,
    sql: str,
    parameters: tuple = (),
) -> None:
    count = _scalar(connection, f"SELECT COUNT(*) FROM ({sql})", parameters)
    if count:
        raise ConsistencyError(f"{label}: {count}条不一致")


def validate(db_path: Path, data_root: Path, *, run_id: str | None = None) -> dict[str, Any]:
    database = db_path.expanduser().resolve()
    root = data_root.expanduser().resolve()
    if not database.is_file():
        raise ConsistencyError("数据库不存在")
    marker_path = root / ROOT_MARKER
    if not marker_path.is_file():
        raise ConsistencyError("data root缺少namespace marker")
    marker = _load_json(marker_path, "namespace marker")
    if marker.get("state") != "bound":
        raise ConsistencyError("namespace marker尚未bound")
    if marker.get("database_identity_hash") != _identity(database):
        raise ConsistencyError("marker database identity不匹配")
    if marker.get("data_root_identity_hash") != _identity(root):
        raise ConsistencyError("marker data-root identity不匹配")

    intent_path = Path(str(database) + BINDING_INTENT_SUFFIX)
    if intent_path.exists():
        intent = _load_json(intent_path, "binding intent")
        if intent.get("bootstrap_stage") != "marker_bound":
            raise ConsistencyError("存在未完成binding intent")
        for key in (
            "namespace_id",
            "binding_nonce",
            "database_identity_hash",
            "data_root_identity_hash",
        ):
            if intent.get(key) != marker.get(key):
                raise ConsistencyError(f"binding intent与marker的{key}冲突")
        if any(
            _looks_absolute(value)
            for value in _walk_strings(intent)
        ):
            raise ConsistencyError("binding intent泄露绝对路径")

    uri = f"file:{database.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 6:
            raise ConsistencyError(f"数据库版本必须为v6，实际为v{version}")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise ConsistencyError(f"SQLite integrity_check失败: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ConsistencyError(f"SQLite外键断裂: {len(foreign_keys)}")

        namespaces = connection.execute(
            "SELECT namespace_id,binding_nonce,database_identity_hash,"
            "data_root_identity_hash,payload FROM storage_namespaces"
        ).fetchall()
        if len(namespaces) != 1:
            raise ConsistencyError("数据库必须恰有一个storage namespace")
        namespace = namespaces[0]
        for key in (
            "namespace_id",
            "binding_nonce",
            "database_identity_hash",
            "data_root_identity_hash",
        ):
            if str(namespace[key]) != str(marker.get(key)):
                raise ConsistencyError(f"数据库namespace与marker的{key}冲突")

        filter_sql = "" if run_id is None else " WHERE run_id=?"
        filter_params: tuple[Any, ...] = () if run_id is None else (run_id,)
        if run_id is not None and not _scalar(
            connection,
            "SELECT COUNT(*) FROM acquisition_runs WHERE run_id=?",
            (run_id,),
        ):
            raise ConsistencyError(f"run不存在: {run_id}")

        _assert_zero(
            connection,
            "plan-to-coverage跨run或跨来源",
            """SELECT 1 FROM physical_query_coverage_links l
               JOIN physical_query_plan_items p ON p.plan_item_id=l.plan_item_id
               JOIN coverage_entries c ON c.coverage_entry_id=l.coverage_entry_id
               WHERE p.run_id<>c.run_id
                  OR p.source_definition_id<>c.source_definition_id
                  OR p.source_definition_version<>c.source_definition_version""",
        )
        _assert_zero(
            connection,
            "attempt-to-plan身份不一致",
            """SELECT 1 FROM acquisition_attempts a
               JOIN physical_query_plan_items p
                 ON p.plan_item_id=a.physical_query_plan_item_id
               WHERE a.run_id<>p.run_id
                  OR a.source_definition_id<>p.source_definition_id
                  OR a.source_definition_version<>p.source_definition_version""",
        )
        _assert_zero(
            connection,
            "discovery observation lineage不一致",
            """SELECT 1 FROM discovery_observations o
               JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id
               WHERE o.physical_query_plan_item_id<>a.physical_query_plan_item_id""",
        )
        _assert_zero(
            connection,
            "discovered resource proof/observation lineage不一致",
            """SELECT 1 FROM discovered_resources r
               LEFT JOIN discovery_proofs p ON p.proof_id=r.proof_id
               WHERE r.proof_id IS NOT NULL AND p.observation_id<>r.discovery_observation_id""",
        )
        _assert_zero(
            connection,
            "resource observation lineage不一致",
            """SELECT 1 FROM resource_observations o
               JOIN acquisition_attempts a ON a.attempt_id=o.attempt_id
               WHERE o.source_definition_id<>a.source_definition_id
                  OR o.source_definition_version<>a.source_definition_version
                  OR (o.disposition IS NOT NULL AND o.snapshot_id IS NULL)""",
        )
        from analysis.acquisition.validators import validate_observation_anchor
        for row in connection.execute("SELECT payload FROM resource_observations"):
            validate_observation_anchor(connection, json.loads(row[0]))
        _assert_zero(
            connection,
            "snapshot/blob身份不一致",
            """SELECT 1 FROM raw_resource_snapshots s
               JOIN content_blobs b ON b.content_blob_id=s.content_blob_id
               WHERE s.content_sha256<>b.sha256
                  OR s.byte_length<>b.byte_length
                  OR s.storage_namespace_id<>b.storage_namespace_id""",
        )
        _assert_zero(
            connection,
            "barrier resolution位置不一致",
            """SELECT 1 FROM barrier_resolutions r
               JOIN checkpoint_barriers b ON b.barrier_id=r.barrier_id
               JOIN acquisition_attempts a ON a.attempt_id=r.resolving_attempt_id
               LEFT JOIN discovery_proofs p ON p.proof_id=r.proof_id
               LEFT JOIN discovery_observations o ON o.observation_id=p.observation_id
               WHERE r.source_definition_id<>b.source_definition_id
                  OR r.source_definition_version<>b.source_definition_version
                  OR r.partition_key<>b.partition_key
                  OR r.work_position<>b.work_position
                  OR COALESCE(r.canonical_resource_id,'')<>COALESCE(b.canonical_resource_id,'')
                  OR CASE WHEN r.proof_id IS NOT NULL AND json_valid(b.work_position)
                          AND json_extract(b.work_position,'$.kind')='discovery'
                     THEN o.attempt_id IS NOT r.resolving_attempt_id
                          OR o.page_ordinal IS NOT json_extract(b.work_position,'$.page')
                          OR o.cursor IS NOT json_extract(b.work_position,'$.cursor')
                     ELSE a.work_position<>b.work_position END""",
        )
        _assert_zero(
            connection,
            "coverage resolution跨run",
            """SELECT 1 FROM coverage_resolutions r
               JOIN coverage_entries c ON c.coverage_entry_id=r.coverage_entry_id
               WHERE r.run_id<>c.run_id""",
        )
        _assert_zero(
            connection,
            "final lease epoch超过控制面epoch",
            """SELECT 1 FROM acquisition_run_events e
               JOIN acquisition_execution_leases l ON l.run_id=e.run_id
               WHERE e.lease_epoch IS NOT NULL AND e.lease_epoch>l.lease_epoch""",
        )

        snapshot_rows = connection.execute(
            "SELECT snapshot_id,content_sha256,byte_length,archived_relative_path "
            "FROM raw_resource_snapshots ORDER BY snapshot_id"
        ).fetchall()
        for row in snapshot_rows:
            relative = Path(str(row["archived_relative_path"]).replace("/", os.sep))
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ConsistencyError(
                    f"snapshot路径越界: {row['snapshot_id']}"
                ) from exc
            if not target.is_file():
                raise ConsistencyError(f"snapshot blob缺失: {row['snapshot_id']}")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != row["content_sha256"] or target.stat().st_size != row["byte_length"]:
                raise ConsistencyError(f"snapshot blob哈希或长度不匹配: {row['snapshot_id']}")

        return {
            "namespace_id": marker["namespace_id"],
            "run_count": _scalar(
                connection,
                f"SELECT COUNT(*) FROM acquisition_runs{filter_sql}",
                filter_params,
            ),
            "plan_item_count": _scalar(connection, "SELECT COUNT(*) FROM physical_query_plan_items"),
            "coverage_count": _scalar(connection, "SELECT COUNT(*) FROM coverage_entries"),
            "attempt_count": _scalar(connection, "SELECT COUNT(*) FROM acquisition_attempts"),
            "snapshot_count": len(snapshot_rows),
            "legacy_sync_result_count": _scalar(connection, "SELECT COUNT(*) FROM sync_results"),
            "legacy_document_count": _scalar(connection, "SELECT COUNT(*) FROM documents"),
        }
    finally:
        connection.close()


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _looks_absolute(value: str) -> bool:
    return (
        len(value) >= 3
        and value[1] == ":"
        and value[2] in {"/", "\\"}
    ) or value.startswith(("/", "\\\\"))


def main() -> int:
    parser = argparse.ArgumentParser(description="验证采集控制面与证据目录一致性")
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        summary = validate(Path(args.db), Path(args.data_root), run_id=args.run_id)
    except (ConsistencyError, sqlite3.Error) as exc:
        print(f"ACQUISITION_CONSISTENCY_FAILED {exc}", file=sys.stderr)
        return 1
    print(
        "ACQUISITION_CONSISTENCY_OK "
        + json.dumps(summary, ensure_ascii=False, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
