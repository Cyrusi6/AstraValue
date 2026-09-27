from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import KnowledgeService


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="本地八步知识候选读取；不接收公司事实")
    parser.add_argument("--catalog", type=Path, default=root / "config/methods/knowledge/catalog.v1.json")
    parser.add_argument("--store", type=Path, default=root / "var/research/knowledge-store")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build"); build.add_argument("--bundle-id")
    sub.add_parser("validate")
    read = sub.add_parser("read"); read.add_argument("question_id"); read.add_argument("--bundle-id"); read.add_argument("--industry"); read.add_argument("--detail", action="store_true"); read.add_argument("--include-drafts", action="store_true"); read.add_argument("--max-chars", type=int)
    read.add_argument("--business-type"); read.add_argument("--period-type"); read.add_argument("--model-purpose")
    coverage = sub.add_parser("coverage"); coverage.add_argument("--bundle-id")
    identity = sub.add_parser("identity"); identity.add_argument("method_id")
    change = sub.add_parser("change"); change.add_argument("kind", choices=["source", "rule", "input", "method"]); change.add_argument("object_id"); change.add_argument("reason"); change.add_argument("--change-id")
    expand = sub.add_parser("expand"); expand.add_argument("reference", help="JSON reference returned by read")
    args = parser.parse_args()
    try:
        service = KnowledgeService.from_catalog(args.catalog, args.store)
        if args.command == "validate": result = service.validate_candidate()
        elif args.command == "build": result = service.build_candidate(bundle_id=args.bundle_id)
        elif args.command == "read": result = service.read(args.question_id, bundle_id=args.bundle_id, context={key: getattr(args, key) for key in ["industry", "business_type", "period_type", "model_purpose"] if getattr(args, key)}, detail=args.detail, include_drafts=args.include_drafts, max_chars=args.max_chars)
        elif args.command == "coverage": result = service.coverage(args.bundle_id)
        elif args.command == "identity": result = {"method_id": args.method_id, "content_sha256": service.method_identity(args.method_id)}
        elif args.command == "change": result = service.record_change(args.kind, args.object_id, args.reason, args.change_id)
        else: result = service.expand(json.loads(args.reference))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("valid") is False or result.get("status") in {"unknown_question", "unknown_version"} else 0
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "configuration_error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
