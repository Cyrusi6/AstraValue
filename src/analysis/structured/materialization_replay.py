"""只读回放已绑定的历史缓存，不引导迁移、不创建运行、不写投影。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from analysis.acquisition.bootstrap import ROOT_MARKER_NAME
from analysis.acquisition.repository import AcquisitionRepository
from .materialization import StructuredFactMaterializer
from .storage import StructuredStorage
from .storage import canonical_json


def replay(db: Path, data_root: Path, run_id: str, *, profile_memory: bool = False,
           interpretation_contract: str | None = None, output_dir: Path | None = None,
           as_of: datetime | None = None, strict_historical: bool = False) -> dict:
    db, data_root = db.resolve(), data_root.resolve()
    if output_dir is not None:
        output_dir = output_dir.resolve()
        if output_dir.is_relative_to(data_root) or db.is_relative_to(output_dir):
            raise ValueError("replay output must be separate from the source cache")
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
        attempts_before = connection.execute("SELECT count(*) FROM acquisition_attempts").fetchone()[0]
        dataset_records = dict(connection.execute("SELECT j.dataset_id,count(r.record_version_id) FROM structured_jobs j "
            "LEFT JOIN structured_records r ON r.job_id=j.job_id WHERE j.run_id=? GROUP BY j.dataset_id", (run_id,)).fetchall())
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
        result = materializer.materialize(run_id, interpretation_contract=interpretation_contract,
            as_of=as_of, strict_historical=strict_historical)
        durations.append(round(time.perf_counter() - started, 3))
        summaries.append(result.to_mapping())
        if len(summaries) == 2 and output_dir is not None:
            exports = _export_result(result, output_dir, namespace["namespace_id"])
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
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        attempts_after = connection.execute("SELECT count(*) FROM acquisition_attempts").fetchone()[0]
    if attempts_after != attempts_before:
        raise ValueError("acquisition attempts changed during replay")
    summary = {**summaries[0], "evidence_kind": "existing_cache_readonly_replay",
        "db": str(db), "data_root": str(data_root), "namespace_id": namespace["namespace_id"],
        "source_namespace_id": namespace["namespace_id"], "projection_namespace_id": None,
        "acquisition_attempts_before": attempts_before, "acquisition_attempts_after": attempts_after,
        "new_acquisition_attempts": attempts_after - attempts_before,
        "dataset_record_counts": dataset_records,
        "snapshot_count_verified": len(snapshots), "snapshot_inventory_hash": snapshot_digest.hexdigest(),
        "unchanged_files": before, "repeat_hash_equal": True, "elapsed_seconds": durations,
        "snapshot_files_unchanged": True, "peak_tracemalloc_bytes": peak_bytes,
        "persisted": False, "manual_acceptance": False, "current_online_check": False}
    if output_dir is not None:
        summary["exports"] = exports
        (output_dir / "replay-summary.json").write_text(canonical_json(summary)+"\n", encoding="utf-8")
    return summary


def _export_result(result, output_dir, namespace):
    """Stable standalone evidence files; these are not a rebound projection DB."""
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = frozenset(result.selected_fact_ids)
    counts, selected_counts, samples = Counter(), Counter(), {}
    for rule in (result.interpretation_contract or {}).get("rules", {}).values():
        counts[(rule["dataset_id"], rule["standard_field"], rule["period_kind"])] = 0
    from .consumption import is_fact_consumable
    with (output_dir / "facts.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for f in result.facts:
            stream.write(canonical_json(f.model_dump(mode="json"))+"\n")
            key = (f.metadata.get("structured_dataset_id", "derived"), f.metric_id, f.period_type)
            counts[key] += 1
            if is_fact_consumable(f, materialization_selected_ids=selected):
                selected_counts[key] += 1
            if not f.derived_from_fact_ids:
                # This checks serialization/conversion; independent source-locator
                # and semantic-unit acceptance is performed by the audit command.
                expected = Decimal(str(f.metadata["original_value"])) * Decimal(f.metadata["multiplier"])
                if expected != Decimal(f.metadata["decimal_value"]) or float(expected) != f.value:
                    raise ValueError("exported numeric value cannot be recomputed")
                samples.setdefault(key, f.model_dump(mode="json"))
    for name, items in (("field-gaps", result.field_gaps), ("sources", result.sources),
                        ("dimensional-facts", result.dimensional_facts), ("events", result.events)):
        with (output_dir / (name+".jsonl")).open("w", encoding="utf-8", newline="\n") as stream:
            for item in items:
                stream.write(canonical_json(item.model_dump(mode="json") if hasattr(item, "model_dump") else item)+"\n")
    coverage = [{"dataset_id":k[0], "metric_id":k[1], "period_type":k[2],
        "fact_count":v, "consumable_count":selected_counts[k]} for k,v in sorted(counts.items())]
    payloads = {"manifest": {**result.to_mapping(), "source_namespace_id":namespace,
        "projection_namespace_id":None, "projection_kind":"readonly_standalone_files"},
        "coverage":coverage, "samples":[samples[k] for k in sorted(samples)]}
    for name, payload in payloads.items():
        (output_dir / (name+".json")).write_text(canonical_json(payload)+"\n", encoding="utf-8")
    files = ["facts.jsonl", "field-gaps.jsonl", "sources.jsonl", "dimensional-facts.jsonl", "events.jsonl",
             "manifest.json", "coverage.json", "samples.json"]
    return {name: {"path":str(output_dir / name), "sha256":_hash_file(output_dir / name)} for name in files}


def _hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--profile-memory", action="store_true")
    parser.add_argument("--interpretation-contract")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--strict-historical", action="store_true")
    args = parser.parse_args()
    summary = replay(args.db, args.data_root, args.run_id, profile_memory=args.profile_memory,
        interpretation_contract=args.interpretation_contract, output_dir=args.output_dir,
        as_of=args.as_of, strict_historical=args.strict_historical)
    # Full selected IDs and frozen interpretation live in the output manifest.
    print(json.dumps({k:v for k,v in summary.items() if k not in {
        "selected_fact_ids", "selected_dimensional_fact_ids", "interpretation_contract"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
