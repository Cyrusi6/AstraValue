from __future__ import annotations

import argparse
import json

from analysis.golden import validate_golden_manifest
from analysis.registry import PROJECT_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="校验A股黄金样本清单与人工核对记录")
    parser.add_argument(
        "--manifest",
        default=str(PROJECT_ROOT / "data" / "golden" / "manifest.json"),
        help="黄金样本清单路径",
    )
    parser.add_argument("--strict", action="store_true", help="要求所有样本已完成验收")
    args = parser.parse_args()
    result = validate_golden_manifest(args.manifest, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
