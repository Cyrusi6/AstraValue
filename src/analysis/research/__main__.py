"""研究工作区的轻量包与证据读取命令。

常规采集和物化仍由 ``analysis.cli structured`` 负责；这里仅提供研究
工作区需要的两个无供应商参数入口，避免重新引入旧的全量下载脚本。
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from analysis.structured.research_lite import build_lite_pack, read_evidence, refresh_lite_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="研究工作区轻量包与按需证据读取")
    commands = parser.add_subparsers(dest="command", required=True)

    lite = commands.add_parser("lite", help="从已物化投影构建轻量研究包")
    lite.add_argument("--input", type=Path, required=True)
    lite.add_argument("--supplement", type=Path, action="append", default=[])
    lite.add_argument("--ticker", action="append", required=True)
    lite.add_argument("--as-of", type=date.fromisoformat, required=True)
    lite.add_argument("--output", type=Path, required=True)
    lite.add_argument("--profile", default="eight-step-lite-v1.0.0")
    lite.add_argument("--max-tokens", type=int)
    lite.add_argument("--execute", action="store_true", help="仅刷新问题触发的期后目录")
    lite.add_argument("--network-output", type=Path)

    evidence = commands.add_parser("evidence", help="按证据 ID 分页读取原文")
    evidence.add_argument("--pack", type=Path, required=True)
    evidence.add_argument("--evidence-id", required=True)
    evidence.add_argument("--page", type=int, default=1)
    evidence.add_argument("--max-tokens", type=int, default=2000)

    args = parser.parse_args(argv)
    if args.command == "evidence":
        print(json.dumps(read_evidence(
            pack_dir=args.pack,
            evidence_id=args.evidence_id,
            page=args.page,
            max_tokens=args.max_tokens,
        ), ensure_ascii=False))
        return 0

    supplements = list(args.supplement)
    if args.execute:
        network_output = args.network_output or args.output / "network-audit" / args.as_of.isoformat()
        for ticker in args.ticker:
            refresh_lite_catalog(
                output_root=network_output,
                ticker=ticker,
                as_of=args.as_of,
                profile_id=args.profile,
            )
        supplements.append(network_output)
    results = []
    for ticker in args.ticker:
        result = build_lite_pack(
            input_root=args.input,
            ticker=ticker,
            as_of=args.as_of,
            output_root=args.output,
            supplements=tuple(supplements),
            profile_id=args.profile,
            max_tokens=args.max_tokens,
        )
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
    return 2 if any(item.get("status") == "budget_exceeded" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
