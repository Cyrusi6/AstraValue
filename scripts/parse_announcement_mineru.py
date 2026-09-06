"""使用 MinerU 精准解析 API v4 解析显式选择的已归档公告，可恢复远端任务。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from analysis.acquisition.mineru import extract_snapshot_mineru
from analysis.acquisition.mineru_client import DEFAULT_CONFIG, MinerUClient, MinerUError, load_config, load_token
from analysis.acquisition.runtime import AcquisitionRuntime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-list", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--submit-only", action="store_true")
    parser.add_argument("--retry-upload-once", action="store_true",
                        help="仅对原任务仍等待文件的中断上传补传一次，不重新申请任务")
    parser.add_argument("--network-route", choices=("direct", "clash"), default="direct")
    args = parser.parse_args()
    config = load_config(args.config)
    env_file = args.env_file or (Path(".env.local") if Path(".env.local").is_file() else None)
    token = load_token(env_file)
    selection = json.loads(args.snapshot_list.read_text("utf-8"))
    ids = selection["snapshot_ids"]
    if not ids or len(ids) != len(set(ids)):
        raise MinerUError("snapshot_list_must_be_nonempty_unique")
    args.output.mkdir(parents=True, exist_ok=True)

    def emit(value):
        event = {"at": datetime.now(timezone.utc).isoformat(), **value}
        with (args.output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps(event, ensure_ascii=False), flush=True)

    results = {}
    with AcquisitionRuntime.create(args.db, args.data_root) as runtime:
        if selection["namespace_id"] != runtime.namespace_id:
            raise MinerUError("mineru_selection_namespace_mismatch")
        with MinerUClient(token, config, progress=emit) as client:
            client.route = args.network_route
            # Upload serially; remote parsing overlaps subsequent file uploads.
            for submit_only in ([True] if args.submit_only else [True, False]):
                for snapshot_id in ids:
                    if results.get(snapshot_id, {}).get("status") == "failed":
                        continue
                    try:
                        result = extract_snapshot_mineru(runtime, snapshot_id, client,
                            progress=emit, submit_only=submit_only, retry_upload_once=args.retry_upload_once)
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, MinerUError) else type(exc).__name__
                        result = {"snapshot_id": snapshot_id, "status": "failed", "error": reason}
                    results[snapshot_id] = result
                    emit({"document": result})
                    (args.output / (snapshot_id + ".json")).write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
                    report = {"selection": selection, "config": config, "results": list(results.values()),
                              "unprocessed_snapshot_ids": [sid for sid in ids if sid not in results]}
                    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
                    if result.get("error", "").startswith(("mineru_api_A02", "mineru_http_401", "mineru_http_403",
                        "mineru_http_429", "mineru_api_-60018", "mineru_connection_failed")):
                        return 3
    return 0 if len(results) == len(ids) and all(r["status"] != "failed" for r in results.values()) else 3


if __name__ == "__main__":
    raise SystemExit(main())
