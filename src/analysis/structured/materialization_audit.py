"""独立复核 BaoStock 只读导出：不调用物化器或映射计算函数。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3


def audit(db: Path, data_root: Path, output_dir: Path) -> dict:
    db, data_root, output_dir = db.resolve(), data_root.resolve(), output_dir.resolve()
    if output_dir.is_relative_to(data_root) or db.is_relative_to(output_dir):
        raise ValueError("audit output must be outside source cache")
    summary = json.loads((output_dir/"replay-summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir/"manifest.json").read_text(encoding="utf-8"))
    if Path(summary["db"]).resolve() != db or Path(summary["data_root"]).resolve() != data_root:
        raise ValueError("audit source cache differs from replay")
    for name, entry in summary["exports"].items():
        if _sha(output_dir/name) != entry["sha256"]:
            raise ValueError("export file hash mismatch")
    for name, expected in summary["unchanged_files"].items():
        if _sha(Path(name)) != expected:
            raise ValueError("source file changed since replay")
    counts, missing, snapshot_ids, samples = Counter(), Counter(), set(), {}
    raw_numeric = Counter()
    selected = set(manifest["selected_fact_ids"])
    seen_ids = set()
    run_id = manifest["run_id"]
    with sqlite3.connect(db.as_uri()+"?mode=ro", uri=True) as conn:
        namespaces = conn.execute("SELECT namespace_id FROM storage_namespaces").fetchall()
        if namespaces != [(manifest["source_namespace_id"],)]:
            raise ValueError("audit namespace mismatch")
        jobs = {j["job_id"]:j for (p,) in conn.execute("SELECT payload FROM structured_jobs WHERE run_id=?", (run_id,))
                for j in [json.loads(p)]}
        records = {r["record_version_id"]:r for (p,) in conn.execute(
            "SELECT r.payload FROM structured_records r JOIN structured_jobs j ON r.job_id=j.job_id WHERE j.run_id=?", (run_id,))
            for r in [json.loads(p)]}
        pages = {p["page_id"]:p for (payload,) in conn.execute(
            "SELECT p.payload FROM structured_pages p JOIN structured_jobs j ON p.job_id=j.job_id WHERE j.run_id=?", (run_id,))
            for p in [json.loads(payload)]}
        successful_attempts = {a for (a,) in conn.execute(
            "SELECT attempt_id FROM acquisition_attempt_events WHERE event_type='outcome_terminal' AND outcome='success'")}
        rules = manifest["interpretation_contract"]["rules"]
        for r in records.values():
            dataset = jobs[r["job_id"]]["dataset_id"]
            for raw, value in r["raw_row"].items():
                if dataset+"."+raw not in rules:
                    continue
                try:
                    number = Decimal(str(value))
                    if not number.is_finite():
                        raise ValueError("nonfinite")
                    raw_numeric[(dataset, raw)] += 1
                except (ValueError, ArithmeticError):
                    missing[(dataset, raw)] += 1

        @lru_cache(maxsize=128)
        def snapshot(sid):
            s = json.loads(conn.execute("SELECT payload FROM raw_resource_snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0])
            p = (data_root/s["archive_relative_path"]).resolve()
            if not p.is_relative_to(data_root) or _sha(p) != s["sha256"]:
                raise ValueError("raw snapshot bytes mismatch")
            snapshot_ids.add(sid)
            return s, json.loads(p.read_text(encoding="utf-8"))

        with (output_dir/"facts.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                fact = json.loads(line)
                m, a = fact["metadata"], fact["structured_admission"]
                record = records[m["structured_record_version_id"]]
                job = jobs[record["job_id"]]
                raw = m["structured_field_path"].removeprefix("$.")
                field = json.loads(conn.execute("SELECT payload FROM structured_record_fields WHERE field_value_id=?",
                    (m["field_value_id"],)).fetchone()[0])
                s, body = snapshot(record["snapshot_id"])
                ordinal = record["row_ordinal"]
                value = body["rows"][ordinal][raw]
                dataset = job["dataset_id"]
                page = pages[record["page_id"]]
                # Reviewed official semantics independently encoded here, rather
                # than trusting multiplier/decimal_value in the generated fact.
                multiplier = Decimal("0.01") if dataset == "baostock_daily" and raw in {"turn", "pctChg"} else Decimal(1)
                expected = Decimal(str(value)) * multiplier
                expected_date = (record["observed_at"][:10] if dataset == "baostock_basic" else
                    body["rows"][ordinal]["dividOperateDate"] if dataset == "baostock_adjust" else
                    body["rows"][ordinal]["date" if dataset == "baostock_daily" else "statDate"])
                effective = max(_time(record["available_at"]), _time(record["observed_at"]),
                    _time(s["available_at"]), _time(manifest["interpretation_contract"]["effective_at"]))
                checks = [
                    value == record["raw_row"][raw] == field["value"] == m["original_value"] == a["original_value"],
                    expected == Decimal(m["decimal_value"]) and float(expected) == fact["value"],
                    Decimal(m["multiplier"]) == multiplier,
                    fact["period_end"] == expected_date,
                    m["structured_run_id"] == job["run_id"] == run_id,
                    m["storage_namespace_id"] == s["storage_namespace_id"] == manifest["source_namespace_id"],
                    m["structured_job_id"] == record["job_id"],
                    m["structured_page_id"] == record["page_id"],
                    m["structured_attempt_id"] == page["attempt_id"] and page["attempt_id"] in successful_attempts,
                    page["snapshot_id"] == m["structured_snapshot_id"] == s["snapshot_id"] == a["raw_resource_snapshot_id"],
                    field["record_version_id"] == record["record_version_id"] and field["dataset_id"] == dataset,
                    m["snapshot_sha256"] == s["sha256"] == a["snapshot_sha256"],
                    m["structured_row_key"] == record["row_key"] == a["row_key"],
                    m["structured_field_path"] == field["field_path"] == a["field_path"],
                    m["field_definition_version"] == field["definition_version"],
                    a["field_definition_version"] == manifest["interpretation_contract"]["contract_id"],
                    _time(fact["as_of"]) == _time(a["available_at"]) == effective,
                    m["source_definition_id"] == job["source_definition_id"] == s["source_definition_id"],
                    m["source_definition_version"] == job["source_definition_version"] == s["source_definition_version"],
                    body["proof"]["status"] == "success" and body["proof"]["sdk_code"] == "0",
                    fact["fact_id"] in selected and fact["fact_id"] not in seen_ids,
                    not fact["derived_from_fact_ids"],
                    fact["unit"] == _expected_unit(dataset, raw),
                ]
                if not all(checks):
                    raise ValueError(f"source/value/locator audit failed: {fact['fact_id']}: {[i for i,v in enumerate(checks) if not v]}")
                seen_ids.add(fact["fact_id"])
                counts[(dataset, raw, fact["metric_id"], fact["period_type"])] += 1
                samples.setdefault((dataset, raw), {"fact_id":fact["fact_id"], "original_value":value,
                    "multiplier":str(multiplier), "standard_value":str(expected), "unit":fact["unit"],
                    "record_version_id":record["record_version_id"], "snapshot_id":s["snapshot_id"],
                    "snapshot_sha256":s["sha256"], "row_ordinal":ordinal, "row_key":record["row_key"],
                    "field_path":field["field_path"], "period_end":expected_date})
        by_raw = Counter()
        for (dataset, raw, _, _), n in counts.items():
            by_raw[(dataset, raw)] += n
        # This real-cache audit intentionally fails if any mapped numeric input
        # disappears. Semantic rejections need explicit review, not silent success.
        if by_raw != raw_numeric or seen_ids != selected:
            raise ValueError("numeric input counts differ from selected output")
    result = {"evidence_kind":"independent_existing_cache_numeric_and_locator_audit",
        "materialization_hash":manifest["materialization_hash"], "fact_count_verified":sum(counts.values()),
        "all_mapped_numeric_inputs_accounted_for":True, "snapshot_count_verified":len(snapshot_ids),
        "dataset_metric_period_counts":[dict(dataset_id=d,raw_field=r,metric_id=m,period_type=p,count=n)
            for (d,r,m,p),n in sorted(counts.items())],
        "missing_values":[dict(dataset_id=d,raw_field=r,count=n) for (d,r),n in sorted(missing.items())],
        "samples":[dict(dataset_id=d,raw_field=r,**v) for (d,r),v in sorted(samples.items())],
        "manual_acceptance":False, "current_online_check":False}
    (output_dir/"independent-audit.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return result


def _expected_unit(dataset, raw):
    if raw in {"type", "status", "adjustflag", "tradestatus", "isST"}:
        return "code"
    if raw in {"volume", "totalShare", "liqaShare"}:
        return "shares"
    if raw in {"open", "high", "low", "close", "preclose", "amount", "netProfit", "MBRevenue"}:
        return "CNY"
    if raw == "epsTTM":
        return "CNY_per_share"
    if raw in {"NRTurnDays", "INVTurnDays"}:
        return "days"
    if raw in {"peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"} or dataset == "baostock_operation":
        return "multiple"
    return "ratio"


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.db, args.data_root, args.output_dir)
    print(json.dumps({k:v for k,v in result.items() if k not in {"samples","dataset_metric_period_counts"}},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
