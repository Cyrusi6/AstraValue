"""Inspect one immutable candidate; exit 1 for unmet acceptance, 2 for bad input."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analysis.knowledge import KnowledgeService
from analysis.knowledge_release import evaluate_release, prepare_evidence


def release_report(service: KnowledgeService, bundle_id: str, acceptance: dict | None = None) -> dict:
    bundle = service.get_bundle(bundle_id)
    coverage = service.coverage(bundle_id)
    evidence = prepare_evidence(bundle_id, coverage["methods"], bundle["catalog"].get("reviews", []), acceptance)
    expected = [q["question_id"] for q in service.questions["questions"]]
    report = evaluate_release(
        {
            **coverage,
            "ready_question_ids": coverage["available_question_ids"],
            "content_sha256": bundle["content_sha256"],
        },
        evidence,
        expected,
    )
    report["candidate_content_sha256"] = bundle["content_sha256"]
    report["coverage"] = coverage
    report["evidence_counts"] = {k: len(evidence.get(k, [])) for k in ("source_checks", "case_checks", "agent_samples")}
    report["default_changed"] = False
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="检查指定知识候选的54题及分项验收；不修改默认版本")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config/methods/knowledge/catalog.v1.json")
    parser.add_argument("--store", type=Path, default=ROOT / "var/research/knowledge-store")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--acceptance", type=Path, help="实际Agent样例与独立人工抽查记录")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        acceptance = json.loads(args.acceptance.read_text(encoding="utf-8")) if args.acceptance else None
        if acceptance is not None and not isinstance(acceptance, dict):
            raise ValueError("acceptance must be a JSON object")
        service = KnowledgeService.from_catalog(args.catalog, args.store)
        report = release_report(service, args.bundle_id, acceptance)
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if report["passed"] else 1
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "configuration_error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
