"""Run bounded structured-data samples against an explicitly isolated store.

Raw responses remain under the caller-provided data root and are never written
to Git.  The checkpoint makes a second invocation reuse completed samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analysis.structured.protocols import (
    BaoStockQuery,
    EastmoneyRequest,
    ProtocolFamily,
    execute_baostock_queries,
    parse_eastmoney_response,
    parse_em_f_company_type_response,
)
from analysis.structured.service import StructuredDataService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--force-step",
        action="append",
        default=[],
        help="rerun one named sample while reusing every other completed checkpoint",
    )
    parser.add_argument(
        "--industry-matrix",
        action="store_true",
        help="also sample bank, life/non-life insurance, broker and utility statements",
    )
    args = parser.parse_args(argv)

    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    service = StructuredDataService.create(args.db, args.data_root)
    checkpoint_path = output / "checkpoint.json"
    checkpoint = {} if args.force else _read_json(checkpoint_path)
    started = datetime.now(timezone.utc)
    evidence: dict[str, Any] = dict(checkpoint.get("evidence", {}))
    reused: list[str] = []

    def run_step(name: str, function: Callable[[], dict[str, Any]]) -> None:
        if name not in args.force_step and evidence.get(name, {}).get("terminal"):
            reused.append(name)
            return
        try:
            evidence[name] = function()
        except Exception as exc:  # every isolated source failure remains visible
            evidence[name] = {
                "terminal": True,
                "status": "failed",
                "error_type": type(exc).__name__,
                "diagnostic": str(exc)[:1000],
            }
        _write_json_atomic(
            checkpoint_path,
            {"version": 1, "evidence": evidence, "updated_at": datetime.now(timezone.utc)},
        )

    client_options: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0),
        "follow_redirects": False,
        "trust_env": False,
        "headers": {"User-Agent": "Mozilla/5.0 AstraValue structured sample/1.0"},
    }
    if args.proxy:
        client_options["proxy"] = args.proxy
    last_request_at = [0.0]

    def http_get(
        request: EastmoneyRequest,
        *,
        company_type_html: bool = False,
        company: str = "贵州茅台",
        ticker: str = "600519.SH",
        sample_fields: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        wait = 3.0 - (time.monotonic() - last_request_at[0])
        if wait > 0:
            time.sleep(wait)
        with httpx.Client(**client_options) as client:
            response = client.get(request.endpoint, params=request.params)
        last_request_at[0] = time.monotonic()
        name = hashlib.sha256(
            json.dumps(
                {"endpoint": request.endpoint, "params": request.params},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        raw_path = output / f"http-{name}.bin"
        raw_path.write_bytes(response.content)
        if company_type_html:
            parsed = parse_em_f_company_type_response(
                request,
                status_code=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type"),
            )
            result = {
                "company_type": parsed.company_type,
                "proof": _jsonable(asdict(parsed.proof)),
            }
        else:
            parsed = parse_eastmoney_response(
                request,
                status_code=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type"),
            )
            result = {
                "row_count": len(parsed.rows),
                "declared_total": parsed.declared_total,
                "declared_pages": parsed.declared_pages,
                "terminal_page": parsed.terminal,
                "sample_row_keys": sorted(parsed.rows[0].keys()) if parsed.rows else [],
                "proof": _jsonable(asdict(parsed.proof)),
            }
            if sample_fields:
                selected_rows = _sample_current_and_historical_rows(parsed.rows)
                result["key_rows"] = [
                    {key: row.get(key) for key in sample_fields}
                    for row in selected_rows
                ]
        return {
            "terminal": True,
            "status": parsed.proof.status.value,
            "company": company,
            "ticker": ticker,
            "endpoint": request.endpoint,
            "parameters": dict(request.params),
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "raw_relative_path": raw_path.relative_to(output).as_posix(),
            "byte_length": len(response.content),
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
            **result,
        }

    pagination_request = EastmoneyRequest(
        ProtocolFamily.EM_S,
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        {
            "reportName": "RPT_F10_FN_SEGMENTSV",
            "columns": "ALL",
            "filter": '(SECUCODE="600519.SH")',
            "pageNumber": "1",
            "pageSize": "500",
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "source": "HSF10",
            "client": "PC",
        },
    )
    run_step("eastmoney_segments_page_1", lambda: http_get(pagination_request))

    company_type_request = EastmoneyRequest(
        ProtocolFamily.EM_F,
        "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
        {"type": "web", "code": "SH600519"},
    )
    run_step(
        "eastmoney_financial_company_type",
        lambda: http_get(company_type_request, company_type_html=True),
    )
    company_type = evidence.get("eastmoney_financial_company_type", {}).get("company_type")
    if company_type:
        catalog_request = EastmoneyRequest(
            ProtocolFamily.EM_F,
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/zcfzbDateAjaxNew",
            {"companyType": str(company_type), "reportDateType": "0", "code": "SH600519"},
        )
        run_step("eastmoney_balance_catalog", lambda: http_get(catalog_request))
        dates = _extract_report_dates(output, evidence.get("eastmoney_balance_catalog", {}))
        if dates:
            report_request = EastmoneyRequest(
                ProtocolFamily.EM_F,
                "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/zcfzbAjaxNew",
                {
                    "companyType": str(company_type),
                    "reportDateType": "0",
                    "reportType": "1",
                    "dates": dates[0],
                    "code": "SH600519",
                },
            )
            run_step("eastmoney_balance_current_period", lambda: http_get(report_request))

    def baostock_sample() -> dict[str, Any]:
        import baostock as bs

        queries = (
            BaoStockQuery(
                "query_history_k_data_plus",
                {
                    "code": "sh.600519",
                    "fields": "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
                    "start_date": "2025-12-01",
                    "end_date": "2025-12-31",
                    "frequency": "d",
                    "adjustflag": "3",
                },
            ),
            BaoStockQuery(
                "query_profit_data",
                {"code": "sh.600519", "year": 2025, "quarter": 3},
            ),
        )
        results = execute_baostock_queries(bs, queries, max_rows_per_query=10_000)
        payload = [
            {
                "method": item.query.method,
                "params": dict(item.query.params),
                "rows": list(item.rows),
                "proof": _jsonable(asdict(item.proof)),
            }
            for item in results
        ]
        raw_path = output / "baostock-results.json"
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "terminal": True,
            "status": "success"
            if all(item.proof.status.value in {"success", "empty"} for item in results)
            else "partial",
            "company": "贵州茅台",
            "ticker": "sh.600519",
            "sdk_version": getattr(bs, "__version__", "unknown"),
            "raw_relative_path": raw_path.relative_to(output).as_posix(),
            "response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "queries": [
                {
                    "method": item.query.method,
                    "status": item.proof.status.value,
                    "rows_yielded": item.proof.rows_yielded,
                    "sdk_code": item.proof.sdk_code,
                    "result_set_exhausted": item.proof.result_set_exhausted,
                    "http_status": item.proof.http_status,
                }
                for item in results
            ],
        }

    run_step("baostock_market_and_financial", baostock_sample)

    if args.industry_matrix:
        representatives = (
            ("bank", "招商银行", "600036.SH", "SH600036", "sh.600036"),
            ("life_insurance", "中国人寿", "601628.SH", "SH601628", "sh.601628"),
            ("nonlife_insurance", "中国人保", "601319.SH", "SH601319", "sh.601319"),
            ("securities", "中信证券", "600030.SH", "SH600030", "sh.600030"),
            ("utility", "长江电力", "600900.SH", "SH600900", "sh.600900"),
        )
        metric_fields = {
            "bank": (
                "REPORT_DATE",
                "NET_INTEREST_MARGIN",
                "NON_PERFORMING_LOAN",
                "CAPITAL_PROVISIONS_SUM",
                "NEWCAPITALADER",
                "FIRST_ADEQUACY_RATIO",
                "LIQUIDITY_COVERAGE_RATIO",
                "NET_FUNDING_RATIO",
            ),
            "life_insurance": (
                "REPORT_DATE",
                "NHJZ_CURRENT_AMT",
                "NBV_LIFE",
                "NBV_RATE",
                "SOLVENCY_AR",
                "TOTAL_ROI",
            ),
            "nonlife_insurance": (
                "REPORT_DATE",
                "NHJZ_CURRENT_AMT",
                "NBV_LIFE",
                "NBV_RATE",
                "SOLVENCY_AR",
                "TOTAL_ROI",
            ),
            "securities": (
                "REPORT_DATE",
                "RISK_COVERAGE",
                "NET_CAPITAL_LIABILITIES",
            ),
        }
        statements = (
            (
                "balance",
                "zcfzbDateAjaxNew",
                "zcfzbAjaxNew",
                ("REPORT_DATE", "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY"),
            ),
            (
                "income",
                "lrbDateAjaxNew",
                "lrbAjaxNew",
                (
                    "REPORT_DATE",
                    "OPERATE_INCOME",
                    "TOTAL_OPERATE_INCOME",
                    "PARENT_NETPROFIT",
                    "NETPROFIT",
                ),
            ),
            (
                "cashflow",
                "xjllbDateAjaxNew",
                "xjllbAjaxNew",
                ("REPORT_DATE", "NETCASH_OPERATE"),
            ),
        )
        for profile_id, company_name, ticker, provider_code, baostock_code in representatives:
            prefix = f"industry_{profile_id}"
            type_request = EastmoneyRequest(
                ProtocolFamily.EM_F,
                "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
                {"type": "web", "code": provider_code},
            )
            run_step(
                f"{prefix}_company_type",
                lambda request=type_request, name=company_name, code=ticker: http_get(
                    request, company_type_html=True, company=name, ticker=code
                ),
            )
            resolved_type = evidence.get(f"{prefix}_company_type", {}).get("company_type")
            if not resolved_type:
                continue
            if profile_id in metric_fields:
                metric_request = EastmoneyRequest(
                    ProtocolFamily.EM_M,
                    "https://datacenter.eastmoney.com/securities/api/data/get",
                    {
                        "type": "RPT_F10_FINANCE_MAINFINADATA",
                        "sty": "APP_F10_MAINFINADATA",
                        "filter": f'(SECUCODE="{ticker}")',
                        "p": "1",
                        "ps": "500",
                        "sr": "-1",
                        "st": "REPORT_DATE",
                        "source": "HSF10",
                        "client": "PC",
                    },
                )
                run_step(
                    f"{prefix}_financial_metrics",
                    lambda request=metric_request, name=company_name, code=ticker, selected=metric_fields[profile_id]: http_get(
                        request,
                        company=name,
                        ticker=code,
                        sample_fields=selected,
                    ),
                )
            for statement, catalog_suffix, report_suffix, fields in statements:
                catalog_request = EastmoneyRequest(
                    ProtocolFamily.EM_F,
                    f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/{catalog_suffix}",
                    {
                        "companyType": str(resolved_type),
                        "reportDateType": "0",
                        "code": provider_code,
                    },
                )
                catalog_step = f"{prefix}_{statement}_catalog"
                run_step(
                    catalog_step,
                    lambda request=catalog_request, name=company_name, code=ticker: http_get(
                        request, company=name, ticker=code
                    ),
                )
                catalog_dates = _extract_report_dates(output, evidence.get(catalog_step, {}))
                selected_dates = _current_and_historical(catalog_dates)
                if not selected_dates:
                    continue
                report_request = EastmoneyRequest(
                    ProtocolFamily.EM_F,
                    f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/{report_suffix}",
                    {
                        "companyType": str(resolved_type),
                        "reportDateType": "0",
                        "reportType": "1",
                        "dates": ",".join(selected_dates),
                        "code": provider_code,
                    },
                )
                run_step(
                    f"{prefix}_{statement}_current_and_history",
                    lambda request=report_request, name=company_name, code=ticker, selected=fields: http_get(
                        request,
                        company=name,
                        ticker=code,
                        sample_fields=selected,
                    ),
                )

        def baostock_identity_matrix() -> dict[str, Any]:
            import baostock as bs

            queries = tuple(
                BaoStockQuery("query_stock_basic", {"code": item[4]})
                for item in representatives
            )
            results = execute_baostock_queries(bs, queries, max_rows_per_query=100)
            rows = [
                {
                    "requested_code": item.query.params["code"],
                    "status": item.proof.status.value,
                    "sdk_code": item.proof.sdk_code,
                    "result_set_exhausted": item.proof.result_set_exhausted,
                    "rows": list(item.rows),
                }
                for item in results
            ]
            raw_path = output / "baostock-industry-identities.json"
            raw_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "terminal": True,
                "status": "success"
                if all(item.proof.status.value == "success" for item in results)
                else "partial",
                "market_identity_checked_separately": True,
                "raw_relative_path": raw_path.relative_to(output).as_posix(),
                "response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "queries": [
                    {
                        "code": item.query.params["code"],
                        "status": item.proof.status.value,
                        "rows_yielded": item.proof.rows_yielded,
                    }
                    for item in results
                ],
            }

        run_step("baostock_industry_market_identities", baostock_identity_matrix)
    finished = datetime.now(timezone.utc)
    summary = {
        "schema_version": "structured-live-sample.v1",
        "started_at": started,
        "finished_at": finished,
        "database_name": Path(args.db).name,
        "data_root_name": Path(args.data_root).name,
        "storage_namespace_id": service.storage.storage_namespace_id,
        "isolated": True,
        "full_history_executed": False,
        "production_data_modified": False,
        "manual_acceptance": "pending",
        "checkpoint_reused_steps": reused,
        "evidence": evidence,
    }
    _write_json_atomic(output / "summary.json", summary)
    print(json.dumps(_jsonable(summary), ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") in {"success", "empty"} for item in evidence.values()) else 3


def _extract_report_dates(output: Path, item: dict[str, Any]) -> list[str]:
    path = item.get("raw_relative_path")
    if not path:
        return []
    try:
        payload = json.loads((output / path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    result: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            raw = row.get("REPORT_DATE") or row.get("REPORTDATE") or row.get("date")
        else:
            raw = row
        if raw:
            result.append(str(raw)[:10])
    return list(dict.fromkeys(result))


def _current_and_historical(values: list[str]) -> list[str]:
    if not values:
        return []
    current = values[0]
    current_year = int(current[:4])
    historical = next(
        (item for item in values[1:] if int(item[:4]) <= current_year - 3),
        values[-1] if len(values) > 1 else None,
    )
    return [current] if historical is None else [current, historical]


def _sample_current_and_historical_rows(
    rows: tuple[dict[str, Any], ...],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    current = rows[0]
    current_date = str(current.get("REPORT_DATE") or current.get("date") or "")[:10]
    try:
        cutoff_year = int(current_date[:4]) - 3
    except ValueError:
        return list(rows[:2])
    historical = next(
        (
            row
            for row in rows[1:]
            if str(row.get("REPORT_DATE") or row.get("date") or "")[:4].isdigit()
            and int(str(row.get("REPORT_DATE") or row.get("date"))[:4]) <= cutoff_year
        ),
        rows[-1] if len(rows) > 1 else None,
    )
    return [current] if historical is None else [current, historical]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, value: Any) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(_jsonable(value), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
