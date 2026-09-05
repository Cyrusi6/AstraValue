"""补全显式 snapshot 清单中的空文本页；只执行本地 OCR。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from analysis.acquisition.ocr import LocalOcrWorker, extract_snapshot_ocr
from analysis.acquisition.runtime import AcquisitionRuntime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-list", type=Path, required=True)
    parser.add_argument("--ocr-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selection = json.loads(args.snapshot_list.read_text("utf-8"))
    ids = selection["snapshot_ids"]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("snapshot_list_must_be_nonempty_unique")
    args.output.mkdir(parents=True, exist_ok=True)
    def emit(value):
        event = {"at": datetime.now(timezone.utc).isoformat(), **value}
        with (args.output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps(event, ensure_ascii=False), flush=True)
    results = []
    with AcquisitionRuntime.create(args.db, args.data_root) as runtime:
        if selection["namespace_id"] != runtime.namespace_id:
            raise ValueError("ocr_selection_namespace_mismatch")
        with LocalOcrWorker(args.ocr_python.resolve()) as worker:
            (args.output / "environment.json").write_text(json.dumps(worker.provenance, indent=2), "utf-8")
            for snapshot_id in ids:
                try:
                    result = extract_snapshot_ocr(runtime, snapshot_id, worker, progress=emit)
                    result["status"] = "completed_with_machine_ocr"
                except Exception as exc:
                    result = {"snapshot_id": snapshot_id, "status": "failed", "error": str(exc)}
                    emit(result)
                    results.append(result)
                    # A crashed or timed-out worker needs an explicit resumed
                    # invocation; do not record the rest as attempted failures.
                    if worker.process.poll() is not None:
                        break
                    continue
                emit({"document": result})
                results.append(result)
                target = args.output / (snapshot_id + ".json")
                target.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    report = {"selection": selection, "results": results,
              "unprocessed_snapshot_ids": ids[len(results):]}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return 0 if len(results) == len(ids) and all(r["status"] != "failed" for r in results) else 3


if __name__ == "__main__":
    raise SystemExit(main())
