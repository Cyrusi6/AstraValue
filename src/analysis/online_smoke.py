from __future__ import annotations

import multiprocessing
import queue
import time
from typing import Any


PROVIDERS = {"official", "akshare", "sina", "baostock", "tushare"}
DEFAULT_PROVIDERS = ("official", "akshare", "sina", "baostock")


def probe_online_sources(
    ticker: str,
    providers: list[str],
    *,
    timeout_seconds: float = 20,
) -> list[dict[str, Any]]:
    unknown = sorted(set(providers) - PROVIDERS)
    if unknown:
        raise ValueError(f"未知适配器: {', '.join(unknown)}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds必须大于0")
    context = multiprocessing.get_context("spawn")
    jobs = []
    for provider in providers:
        result_queue = context.Queue(maxsize=1)
        process = context.Process(target=_probe_provider, args=(provider, ticker, result_queue))
        process.daemon = True
        process.start()
        jobs.append((provider, process, result_queue, time.monotonic()))

    results: list[dict[str, Any]] = []
    for provider, process, result_queue, started_at in jobs:
        remaining = max(0.0, timeout_seconds - (time.monotonic() - started_at))
        process.join(remaining)
        if process.is_alive():
            process.terminate()
            process.join(2)
            results.append(
                {
                    "provider": provider,
                    "status": "timeout",
                    "fact_count": 0,
                    "source_count": 0,
                    "warnings": [f"超过{timeout_seconds:g}秒，已终止探测进程"],
                }
            )
        else:
            try:
                results.append(result_queue.get(timeout=1))
            except queue.Empty:
                results.append(
                    {
                        "provider": provider,
                        "status": "failed",
                        "fact_count": 0,
                        "source_count": 0,
                        "warnings": [f"探测进程退出码{process.exitcode}，未返回结果"],
                    }
                )
        result_queue.close()
        result_queue.join_thread()
    return results


def _probe_provider(provider: str, ticker: str, result_queue) -> None:
    try:
        if provider == "akshare":
            from .adapters.akshare_adapter import AkshareAdapter

            adapter = AkshareAdapter()
        elif provider == "baostock":
            from .adapters.baostock_adapter import BaostockAdapter

            adapter = BaostockAdapter()
        elif provider == "sina":
            from .adapters.sina_adapter import SinaFinanceAdapter

            adapter = SinaFinanceAdapter()
        elif provider == "tushare":
            from .adapters.tushare_adapter import TushareProAdapter

            adapter = TushareProAdapter()
        elif provider == "official":
            from .adapters.official_adapter import OfficialDisclosureAdapter

            adapter = OfficialDisclosureAdapter()
        else:
            raise ValueError(f"未知适配器: {provider}")
        from .models import SyncRequest

        # Metadata-only probing keeps the watchdog lightweight. Full PDF download
        # and parsing are exercised by the normal company sync endpoint.
        options = SyncRequest(
            providers=[provider],
            download_official_documents=provider != "official",
        )
        result = adapter.sync(ticker, options)
        usable = bool(result.facts) or (
            provider == "official" and "仅校验元数据" in result.provider_results.get(provider, "")
        )
        status = "ok" if usable and not result.warnings else "degraded"
        result_queue.put(
            {
                "provider": provider,
                "status": status,
                "fact_count": len(result.facts),
                "source_count": len(result.sources),
                "document_count": len(result.documents),
                "verification_count": len(result.verification_records),
                "provider_result": result.provider_results.get(provider),
                "warnings": result.warnings,
            }
        )
    except RuntimeError as exc:
        message = str(exc)
        status = (
            "unavailable"
            if any(token in message for token in ("未安装", "未配置", "不可用"))
            else "failed"
        )
        result_queue.put(
            {
                "provider": provider,
                "status": status,
                "fact_count": 0,
                "source_count": 0,
                "warnings": [message],
            }
        )
    except Exception as exc:
        result_queue.put(
            {
                "provider": provider,
                "status": "failed",
                "fact_count": 0,
                "source_count": 0,
                "warnings": [f"{type(exc).__name__}: {exc}"],
            }
        )
