from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "golden" / "acquisition_business_model_v1.json"
EXPECTED_SECTIONS = {
    "company_anchor",
    "source_question_time_coverage",
    "physical_query_m2m",
    "discovery_and_required_fetch",
    "observed_non_success_outcomes",
    "snapshot_hash_sample",
    "same_url_version_chains",
}
EXPECTED_QUESTION_PREFIXES = tuple(f"BM.Q{index:02d}." for index in range(1, 11))


def _validate(path: Path) -> tuple[dict, list[str]]:
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"manifest_unreadable:{exc}"]
    if payload.get("schema_version") != "acquisition-golden-v1":
        errors.append("invalid_schema_version")
    if payload.get("change_id") != "business-model-acquisition-v1":
        errors.append("invalid_change_id")
    pilot = payload.get("pilot") or {}
    if pilot.get("ticker") != "600519" or not pilot.get("company_name"):
        errors.append("invalid_pilot")
    if payload.get("status") not in {
        "pending_manual_validation",
        "passed",
        "failed",
    }:
        errors.append("invalid_status")
    question_ids = payload.get("required_question_ids") or []
    if len(question_ids) != 10 or len(set(question_ids)) != 10:
        errors.append("invalid_question_count")
    for prefix in EXPECTED_QUESTION_PREFIXES:
        if sum(str(item).startswith(prefix) for item in question_ids) != 1:
            errors.append(f"missing_question:{prefix}")
    sections = payload.get("review_sections") or []
    section_ids = {item.get("section_id") for item in sections if isinstance(item, dict)}
    if section_ids != EXPECTED_SECTIONS or len(sections) != len(EXPECTED_SECTIONS):
        errors.append("invalid_review_sections")
    for item in sections:
        if not isinstance(item, dict):
            errors.append("invalid_review_section")
            continue
        status = item.get("status")
        if status not in {"pending", "passed", "failed"}:
            errors.append(f"invalid_section_status:{item.get('section_id')}")
        if status == "passed":
            if not item.get("reviewer") or not item.get("reviewed_at"):
                errors.append(f"unsigned_section:{item.get('section_id')}")
            else:
                try:
                    datetime.fromisoformat(str(item["reviewed_at"]).replace("Z", "+00:00"))
                except ValueError:
                    errors.append(f"invalid_review_time:{item.get('section_id')}")
            if not item.get("evidence_refs"):
                errors.append(f"missing_evidence_refs:{item.get('section_id')}")
    text = path.read_text(encoding="utf-8")
    forbidden = ("Authorization:", "Bearer ", "Cookie:", "file://", "browser-profile")
    if any(token in text for token in forbidden):
        errors.append("sensitive_content_detected")
    return payload, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验业务采集人工黄金清单")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    payload, errors = _validate(args.manifest.resolve())
    if errors:
        print("ACQUISITION_GOLDEN_INVALID")
        for error in errors:
            print(error)
        return 2
    sections = payload["review_sections"]
    fully_signed = (
        payload["status"] == "passed"
        and all(item["status"] == "passed" for item in sections)
    )
    if fully_signed:
        print("ACQUISITION_GOLDEN_OK")
        return 0
    print(f"ACQUISITION_GOLDEN_PENDING status={payload['status']}")
    if args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
