"""只读回放已绑定的历史缓存，不引导迁移、不创建运行、不写投影。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from analysis.acquisition.bootstrap import ROOT_MARKER_NAME
from analysis.acquisition.repository import AcquisitionRepository
from .materialization import StructuredFactMaterializer
from .storage import StructuredStorage


def replay(db: Path, data_root: Path, run_id: str, *, profile_memory: bool = False) -> dict:
    db, data_root = db.resolve(), data_root.resolve()
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        namespaces = connection.execute("SELECT * FROM storage_namespaces").fetchall()
        if len(namespaces) != 1:
            raise ValueError("expected one bound namespace")
        namespace = dict(namespaces[0])
        marker = json.loads((data_root / ROOT_MARKER_NAME).read_text(encoding="utf-8"))
        for key, path in (("database_identity_hash", db), ("data_root_identity_hash", data_root)):
            identity = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
            if namespace[key] != identity or marker.get(key) != identity:
                raise ValueError("cache binding mismatch")
        if (marker.get("state") != "bound" or marker.get("namespace_id") != namespace["namespace_id"]
                or marker.get("binding_nonce") != namespace["binding_nonce"]):
            raise ValueError("cache namespace marker mismatch")
        snapshots = connection.execute("SELECT DISTINCT s.archive_relative_path,s.sha256 FROM "
            "(SELECT json_extract(payload,'$.archive_relative_path') AS archive_relative_path,"
            "json_extract(payload,'$.sha256') AS sha256,snapshot_id FROM raw_resource_snapshots) s "
            "JOIN structured_records r ON r.snapshot_id=s.snapshot_id "
            "JOIN structured_jobs j ON j.job_id=r.job_id WHERE j.run_id=?", (run_id,)).fetchall()
    snapshot_digest = hashlib.sha256()
    for snapshot in sorted(snapshots, key=lambda r: r[0]):
        path = (data_root / snapshot["archive_relative_path"]).resolve()
        if not path.is_relative_to(data_root):
            raise ValueError("snapshot path escaped the bound root")
        actual = _hash_file(path)
        if actual != snapshot["sha256"]:
            raise ValueError("raw snapshot hash mismatch")
        snapshot_digest.update((snapshot["archive_relative_path"] + ":" + actual + "\n").encode())
    before = {str(p): _hash_file(p) for p in (db, db.with_name(db.name + "-wal"), data_root / ROOT_MARKER_NAME) if p.exists()}
    storage = StructuredStorage(db, namespace["namespace_id"], initialize=False)
    materializer = StructuredFactMaterializer(storage, AcquisitionRepository(db, initialize=False))
    durations, summaries = [], []
    if profile_memory:
        import tracemalloc
        tracemalloc.start()
    for _ in range(2):
        started = time.perf_counter()
        result = materializer.materialize(run_id)
        durations.append(round(time.perf_counter() - started, 3))
        summaries.append(result.to_mapping())
        del result
    peak_bytes = None
    if profile_memory:
        peak_bytes = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    after = {p: _hash_file(Path(p)) for p in before}
    if before != after or summaries[0] != summaries[1]:
        raise ValueError("cache changed or replay was nondeterministic")
    if any(_hash_file(data_root / s["archive_relative_path"]) != s["sha256"] for s in snapshots):
        raise ValueError("raw snapshot changed during replay")
    return {**summaries[0], "evidence_kind": "existing_cache_readonly_replay",
        "db": str(db), "data_root": str(data_root), "namespace_id": namespace["namespace_id"],
        "snapshot_count_verified": len(snapshots), "snapshot_inventory_hash": snapshot_digest.hexdigest(),
        "unchanged_files": before, "repeat_hash_equal": True, "elapsed_seconds": durations,
        "snapshot_files_unchanged": True, "peak_tracemalloc_bytes": peak_bytes,
        "persisted": False, "manual_acceptance": False, "current_online_check": False}


def _hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--profile-memory", action="store_true")
    args = parser.parse_args()
    print(json.dumps(replay(args.db, args.data_root, args.run_id, profile_memory=args.profile_memory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
