from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analysis.governance.golden import validate_governance_golden


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验治理开发期人工黄金清单")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "golden" / "governance_management_v1.json",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    result = validate_governance_golden(args.manifest, strict=args.strict)
    if result["status"] == "validated" and result["passed"]:
        print("GOVERNANCE_GOLDEN_OK")
    else:
        print("pending_manual_validation")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
