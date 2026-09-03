from __future__ import annotations

import argparse
import json

from analysis.online_smoke import DEFAULT_PROVIDERS, PROVIDERS, probe_online_sources


def main() -> int:
    parser = argparse.ArgumentParser(description="非阻塞探测免费A股数据适配器")
    parser.add_argument("--ticker", default="600519", help="六位A股代码")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=sorted(PROVIDERS),
        default=list(DEFAULT_PROVIDERS),
    )
    parser.add_argument("--timeout", type=float, default=20, help="每个适配器的最长等待秒数")
    parser.add_argument("--strict", action="store_true", help="任一适配器未成功时返回失败")
    args = parser.parse_args()
    results = probe_online_sources(args.ticker, args.providers, timeout_seconds=args.timeout)
    payload = {
        "ticker": args.ticker,
        "strict": args.strict,
        "results": results,
        "passed": all(item["status"] == "ok" for item in results),
        "note": "默认模式仅监测并显式报告接口状态，不阻塞离线测试。",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
