"""Explicit bounded supervisor for a frozen structured repair manifest.

This command never waits for a baseline, polls a process, installs a scheduled
task, or creates an incremental run.  An operator invokes it only after the
original supervisor has exited.  All preflight checks happen before the SDK is
imported or a structured runtime is opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _git(workspace: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=workspace, text=True, encoding="utf-8"
    ).strip()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        raise ValueError("supervisor PID must be positive")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _active_leases(db: Path) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect(
        "file:" + db.resolve().as_posix() + "?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT run_id,lease_epoch,acquired_at,expires_at "
            "FROM acquisition_execution_leases WHERE released_at IS NULL "
            "AND expires_at>? ORDER BY run_id",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _database_namespace(db: Path) -> str:
    connection = sqlite3.connect(
        "file:" + db.resolve().as_posix() + "?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        rows = connection.execute(
            "SELECT namespace_id FROM storage_namespaces ORDER BY namespace_id"
        ).fetchall()
        if len(rows) != 1:
            raise SystemExit("database must contain exactly one storage namespace")
        return str(rows[0][0])
    finally:
        connection.close()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def preflight(args: argparse.Namespace) -> tuple[dict[str, Any], Any]:
    workspace = Path(args.workspace).resolve()
    db = Path(args.db).resolve()
    data_root = Path(args.data_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    if not db.is_file():
        raise SystemExit("repair database is absent")
    if not data_root.is_dir():
        raise SystemExit("repair data root is absent")
    if not manifest_path.is_file():
        raise SystemExit("repair manifest is absent")
    head = _git(workspace, "rev-parse", "HEAD")
    if head != args.expected_revision:
        raise SystemExit("workspace revision does not match the repair pin")
    if _git(workspace, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("workspace has tracked modifications")
    if _pid_alive(args.supervisor_pid):
        raise SystemExit("original supervisor is still running")
    active = _active_leases(db)
    if active:
        raise SystemExit("active execution leases are present")
    namespace = _database_namespace(db)
    if namespace != args.expected_namespace:
        raise SystemExit("database namespace does not match the repair pin")

    src = workspace / "src"
    sys.path.insert(0, str(src))
    from analysis.structured.repair import load_repair_manifest

    manifest = load_repair_manifest(manifest_path)
    if manifest.code_revision != args.expected_revision:
        raise SystemExit("manifest code revision does not match the repair pin")
    if manifest.storage_namespace_id != namespace:
        raise SystemExit("manifest namespace does not match the database")
    if manifest.manifest_sha256 != args.expected_manifest_sha256:
        raise SystemExit("manifest hash does not match the repair pin")
    return (
        {
            "schema_version": "structured-repair-supervisor-preflight.v1",
            "workspace_revision": head,
            "database": str(db),
            "data_root": str(data_root),
            "storage_namespace_id": namespace,
            "manifest_id": manifest.manifest_id,
            "manifest_sha256": manifest.manifest_sha256,
            "manifest_file_sha256": _sha256_file(manifest_path),
            "original_supervisor_pid": args.supervisor_pid,
            "original_supervisor_running": False,
            "active_leases": [],
            "preflight_passed": True,
            "performed_supplier_io": False,
        },
        manifest,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-namespace", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--supervisor-pid", required=True, type=int)
    parser.add_argument("--max-jobs-per-round", type=int, default=1000)
    parser.add_argument("--max-rounds", type=int, choices=(1, 2), default=2)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--label", default="structured-repair")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    if args.max_jobs_per_round < 1:
        parser.error("--max-jobs-per-round must be positive")

    frozen, manifest = preflight(args)
    evidence_dir = Path(args.evidence_dir).resolve()
    if args.preflight_only:
        print(json.dumps(frozen, ensure_ascii=False, sort_keys=True))
        return 0

    # Imports below this boundary may initialize the bound structured runtime;
    # no provider or production write is reachable before every preflight passes.
    import baostock as bs
    from analysis.structured.service import StructuredDataService

    invocation = {
        **frozen,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "max_jobs_per_round": args.max_jobs_per_round,
        "max_rounds": args.max_rounds,
        "manual_acceptance": "pending_independent",
    }
    _write_json(evidence_dir / f"{args.label}-invocation.json", invocation)
    service = StructuredDataService.create(
        Path(args.db).resolve(), Path(args.data_root).resolve(), sdk=bs
    )
    rounds: list[dict[str, Any]] = []
    try:
        for ordinal in range(1, args.max_rounds + 1):
            result = service.runtime.repair_execute(
                manifest,
                expected_code_revision=args.expected_revision,
                max_jobs_per_round=args.max_jobs_per_round,
            )
            rounds.append({"round": ordinal, **result})
            if result.get("source_status") == "source_unavailable":
                break
            if not result.get("attempted_job_ids"):
                break
            if int((result.get("counts") or {}).get("ready", 0)) == 0:
                break
        final = service.runtime.repair_status(
            manifest, expected_code_revision=args.expected_revision
        )
    finally:
        service.close()
    summary = {
        "schema_version": "structured-repair-supervisor-result.v1",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "manifest_id": manifest.manifest_id,
        "manifest_sha256": manifest.manifest_sha256,
        "rounds": rounds,
        "final_status": final,
        "manual_acceptance": "pending_independent",
        "installed_scheduler": False,
        "monitored_baseline": False,
    }
    _write_json(evidence_dir / f"{args.label}-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    source_unavailable = any(
        item.get("source_status") == "source_unavailable" for item in rounds
    )
    return 2 if source_unavailable else 0


if __name__ == "__main__":
    raise SystemExit(main())
