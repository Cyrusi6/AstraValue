from __future__ import annotations

import argparse

from analysis.cli import main as analysis_main


def main() -> int:
    """Compatibility launcher for the registry-driven persisted smoke command."""

    parser = argparse.ArgumentParser(
        description="从版本化来源注册表执行带审计租约的最小在线探针"
    )
    parser.add_argument("--ticker", default="600519", help="六位A股代码")
    parser.add_argument("--source", action="append", dest="sources")
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    forwarded = [
        "smoke-sources",
        "--ticker",
        args.ticker,
        "--db",
        args.db,
        "--data-root",
        args.data_root,
        "--json",
    ]
    for source_id in args.sources or ():
        forwarded.extend(("--source", source_id))
    if args.strict:
        forwarded.append("--strict")
    return analysis_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
