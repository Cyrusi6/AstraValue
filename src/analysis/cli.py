from __future__ import annotations

import argparse
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .demo import build_demo_request
from .exports import export_report
from .registry import MethodRegistry
from .service import AnalysisService
from .acquisition.bootstrap import (
    BootstrapLockTimeout,
    StorageBootstrapError,
    StorageBootstrapper,
    StorageNamespaceMismatch,
)
from .acquisition.models import AcquisitionMode, AcquisitionRunKind
from .acquisition.planner import AcquisitionPlanningError
from .acquisition.registry import SourceRegistryError
from .acquisition.repository import (
    AcquisitionNotFoundError,
    LeaseConflictError,
    StaleLeaseError,
    StorageBusyError,
)
from .acquisition.runtime import AcquisitionRuntime
from .acquisition.security import (
    REDACTED_LOCAL_PATH,
    is_sensitive_name,
    looks_like_absolute_local_path,
    redact_absolute_local_paths,
    redact_url,
)
from .acquisition.snapshots import SnapshotPipelineError


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MATERIAL_GAP = 3
EXIT_INTERNAL = 4
EXIT_RETRYABLE = 5


def _add_bound_storage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--json", action="store_true", dest="json_output")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def _lease_ttl(value: str) -> int:
    parsed = int(value)
    if not 5 <= parsed <= 3600:
        raise argparse.ArgumentTypeError("lease TTL必须在5到3600秒之间")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A股八步财报分析本地工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动本地网页与API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--acquisition-db")
    serve.add_argument("--acquisition-data-root")

    subparsers.add_parser("validate-methods", help="检查文档、配置、实现和测试引用")

    demo = subparsers.add_parser("demo", help="生成虚构公司的端到端示例")
    demo.add_argument("--output-dir", default="outputs/demo")
    demo.add_argument("--formats", nargs="+", default=["md", "html", "xlsx", "pdf"])

    export = subparsers.add_parser("export", help="导出已保存报告")
    export.add_argument("report_id")
    export.add_argument("format", choices=["md", "html", "xlsx", "pdf"])
    export.add_argument("--output-dir", default="var/exports")

    list_command = subparsers.add_parser("list", help="列出本地报告")
    list_command.add_argument("--ticker")

    acquire = subparsers.add_parser("acquire", help="可审计公司资料采集")
    acquire_commands = acquire.add_subparsers(dest="acquire_command", required=True)

    acquire_start = acquire_commands.add_parser("start", help="创建并执行采集运行")
    acquire_start.add_argument("ticker")
    acquire_start.add_argument(
        "--mode", required=True, choices=[item.value for item in AcquisitionMode]
    )
    acquire_start.add_argument("--as-of")
    acquire_start.add_argument("--company-name")
    acquire_start.add_argument("--market", choices=["SSE", "SZSE"])
    acquire_start.add_argument("--listing-date")
    acquire_start.add_argument("--prospectus-date")
    acquire_start.add_argument("--from-run")
    acquire_start.add_argument("--from-latest-run", action="store_true")
    acquire_start.add_argument("--plan-only", action="store_true")
    _add_bound_storage_arguments(acquire_start)

    acquire_list = acquire_commands.add_parser("list", help="列出采集运行")
    acquire_list.add_argument("--ticker")
    acquire_list.add_argument("--mode", choices=[item.value for item in AcquisitionMode])
    acquire_list.add_argument(
        "--run-kind", choices=[item.value for item in AcquisitionRunKind]
    )
    acquire_list.add_argument("--limit", type=_positive_int, default=100)
    _add_bound_storage_arguments(acquire_list)

    acquire_show = acquire_commands.add_parser("show", help="显示采集运行")
    acquire_show.add_argument("run_id")
    _add_bound_storage_arguments(acquire_show)

    acquire_execute = acquire_commands.add_parser("execute", help="执行或恢复采集运行")
    acquire_execute.add_argument("run_id")
    acquire_execute.add_argument("--lease-ttl-seconds", type=_lease_ttl, default=60)
    _add_bound_storage_arguments(acquire_execute)

    supplement = acquire_commands.add_parser("supplement", help="针对已终结运行的实际缺口调用补充来源")
    supplement.add_argument("--from-run", required=True)
    supplement.add_argument("--coverage-entry", required=True)
    supplement.add_argument("--source", required=True)
    supplement.add_argument("--plan-only", action="store_true")
    _add_bound_storage_arguments(supplement)

    smoke = subparsers.add_parser("smoke-sources", help="执行注册表驱动的最小在线探针")
    smoke.add_argument("--ticker", required=True)
    smoke.add_argument("--source", action="append", dest="sources")
    smoke.add_argument("--strict", action="store_true")
    _add_bound_storage_arguments(smoke)

    acquisition_db = subparsers.add_parser("acquisition-db", help="采集数据库维护")
    acquisition_db_commands = acquisition_db.add_subparsers(
        dest="acquisition_db_command", required=True
    )
    acquisition_backup = acquisition_db_commands.add_parser(
        "backup", help="只读预检并生成已验证备份"
    )
    acquisition_backup.add_argument("--label")
    _add_bound_storage_arguments(acquisition_backup)

    structured = subparsers.add_parser("structured", help="结构化字段计划、执行与查询")
    structured_commands = structured.add_subparsers(
        dest="structured_command", required=True
    )

    structured_plan = structured_commands.add_parser("plan", help="生成零网络计划预览")
    structured_plan.add_argument("ticker")
    structured_plan.add_argument(
        "--mode", choices=["baseline", "incremental", "due"], default="incremental"
    )
    structured_plan.add_argument(
        "--company-scope",
        choices=["company-only", "company-with-peers", "peer-set"],
        default="company-only",
    )
    structured_plan.add_argument("--dataset", action="append", dest="datasets")
    structured_plan.add_argument("--as-of")
    _add_bound_storage_arguments(structured_plan)

    for name, help_text in (
        ("run", "执行一轮已持久化任务"),
        ("resume", "从未完成范围恢复一轮任务"),
        ("status", "查询运行与全部任务状态"),
    ):
        command = structured_commands.add_parser(name, help=help_text)
        command.add_argument("run_id")
        _add_bound_storage_arguments(command)

    structured_materialize = structured_commands.add_parser(
        "materialize", help="将已提交结构化记录物化为报告事实"
    )
    structured_materialize.add_argument("run_id")
    structured_materialize.add_argument("--as-of")
    structured_materialize.add_argument("--strict-historical", action="store_true")
    structured_materialize.add_argument("--interpretation-contract", help="显式选择有证据的后补字段解释版本")
    structured_materialize.add_argument("--no-persist", action="store_true")
    structured_materialize.add_argument("--summary", action="store_true", help="仅输出计数、缺口汇总和投影定位")
    _add_bound_storage_arguments(structured_materialize)

    structured_repair_plan = structured_commands.add_parser(
        "repair-plan", help="只读生成终态失败补采 manifest"
    )
    structured_repair_plan.add_argument("run_id")
    structured_repair_plan.add_argument(
        "--dataset", action="append", dest="datasets", required=True
    )
    structured_repair_plan.add_argument(
        "--reason", action="append", dest="reasons", required=True
    )
    structured_repair_plan.add_argument("--revision", required=True)
    structured_repair_plan.add_argument("--output", required=True)
    _add_bound_storage_arguments(structured_repair_plan)

    structured_repair_run = structured_commands.add_parser(
        "repair-run", help="显式执行一轮有界终态失败补采"
    )
    structured_repair_run.add_argument("manifest")
    structured_repair_run.add_argument("--revision", required=True)
    structured_repair_run.add_argument(
        "--max-jobs", type=_positive_int, default=25
    )
    _add_bound_storage_arguments(structured_repair_run)

    structured_repair_status = structured_commands.add_parser(
        "repair-status", help="只读核对补采 manifest 状态"
    )
    structured_repair_status.add_argument("manifest")
    structured_repair_status.add_argument("--revision", required=True)
    _add_bound_storage_arguments(structured_repair_status)

    structured_records = structured_commands.add_parser("records", help="分页查询结构化记录")
    structured_records.add_argument("--run-id")
    structured_records.add_argument("--dataset-id")
    structured_records.add_argument("--limit", type=_positive_int, default=500)
    structured_records.add_argument("--offset", type=int, default=0)
    _add_bound_storage_arguments(structured_records)

    structured_reading = structured_commands.add_parser("reading-tasks", help="分页查询阅读任务")
    structured_reading.add_argument("--run-id")
    structured_reading.add_argument("--limit", type=_positive_int, default=500)
    structured_reading.add_argument("--offset", type=int, default=0)
    _add_bound_storage_arguments(structured_reading)

    structured_resolve = structured_commands.add_parser("resolve", help="从本地主数据解析公司身份")
    structured_resolve.add_argument("query")
    structured_resolve.add_argument("--market")
    structured_resolve.add_argument("--as-of")
    _add_bound_storage_arguments(structured_resolve)

    structured_profile = structured_commands.add_parser("profile", help="查询行业画像状态")
    structured_profile.add_argument("ticker")
    _add_bound_storage_arguments(structured_profile)

    structured_peers = structured_commands.add_parser("peers", help="查询有界同行选择")
    structured_peers.add_argument("ticker")
    structured_peers.add_argument(
        "--company-scope",
        choices=["company-only", "company-with-peers", "peer-set"],
        default="company-with-peers",
    )
    _add_bound_storage_arguments(structured_peers)

    structured_coverage = structured_commands.add_parser("coverage", help="分页查询逐题覆盖")
    structured_coverage.add_argument("snapshot_id")
    structured_coverage.add_argument("--limit", type=_positive_int, default=500)
    structured_coverage.add_argument("--offset", type=int, default=0)
    _add_bound_storage_arguments(structured_coverage)

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        if bool(args.acquisition_db) != bool(args.acquisition_data_root):
            parser.error("serve启用采集时必须同时提供--acquisition-db与--acquisition-data-root")
        if args.acquisition_db:
            from .api import create_app

            runtime = AcquisitionRuntime.create(
                args.acquisition_db,
                args.acquisition_data_root,
            )
            uvicorn.run(
                create_app(acquisition_runtime=runtime),
                host=args.host,
                port=args.port,
            )
        else:
            uvicorn.run("analysis.api:app", host=args.host, port=args.port, reload=args.reload)
        return 0
    if args.command == "validate-methods":
        errors = MethodRegistry().validate_library()
        if errors:
            print("\n".join(errors))
            return 1
        print("METHOD_LIBRARY_OK")
        return 0

    if args.command == "acquisition-db":
        try:
            bootstrapper = StorageBootstrapper(args.db, args.data_root)
            manifest = bootstrapper.backup(label=args.label)
            _emit(_safe_json_value(manifest), json_output=args.json_output)
            return EXIT_OK
        except (ValueError, SourceRegistryError, StorageNamespaceMismatch) as exc:
            return _emit_error(EXIT_USAGE, "validation", exc, args.json_output)
        except (StorageBusyError, BootstrapLockTimeout) as exc:
            return _emit_error(EXIT_RETRYABLE, "storage_busy", exc, args.json_output)
        except Exception as exc:
            return _emit_error(EXIT_INTERNAL, "internal_error", exc, args.json_output)

    if args.command in {"acquire", "smoke-sources"}:
        return _run_acquisition_command(args)

    if args.command == "structured":
        return _run_structured_command(args)

    service = AnalysisService()
    if args.command == "demo":
        report = service.create_report(build_demo_request())
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        report_json = output_dir / "report.json"
        report_json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        paths = [report_json]
        for fmt in args.formats:
            paths.append(export_report(report, fmt, output_dir))
        print(json.dumps({"report_id": report.report_id, "outputs": [str(item) for item in paths]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "export":
        report = service.storage.get_report(args.report_id)
        print(export_report(report, args.format, args.output_dir))
        return 0
    if args.command == "list":
        rows = service.storage.list_reports(args.ticker)
        print(json.dumps([{"report_id": item.report_id, "ticker": item.ticker, "version": item.version, "status": item.status.value} for item in rows], ensure_ascii=False, indent=2))
        return 0
    return 1


def _create_structured_service(db_path: str, data_root: str) -> Any:
    from .structured.service import StructuredDataService

    return StructuredDataService.create(db_path, data_root)


def _run_structured_command(args: argparse.Namespace) -> int:
    try:
        service = _create_structured_service(args.db, args.data_root)
        command = args.structured_command
        if command == "repair-run" and getattr(service, "runtime", None) is not None:
            if service.runtime.sdk is None:
                import baostock as bs

                service.runtime.sdk = bs
        if command == "plan":
            value = service.plan(
                args.ticker,
                mode=args.mode,
                company_scope=args.company_scope,
                datasets=tuple(args.datasets or ()),
                as_of=_parse_datetime(args.as_of) if args.as_of else None,
            )
        elif command == "run":
            value = service.run(args.run_id)
        elif command == "resume":
            value = service.resume(args.run_id)
        elif command == "status":
            value = service.status(args.run_id)
        elif command == "materialize":
            value = service.materialize(
                args.run_id,
                as_of=_parse_datetime(args.as_of) if args.as_of else None,
                strict_historical=bool(args.strict_historical),
                interpretation_contract=args.interpretation_contract,
                persist=not bool(args.no_persist),
                include_records=not bool(args.summary),
            )
        elif command == "repair-plan":
            value = service.repair_plan(
                args.run_id,
                datasets=tuple(args.datasets),
                reasons=tuple(args.reasons),
                code_revision=args.revision,
                output=args.output,
            )
        elif command == "repair-run":
            value = service.repair_run(
                args.manifest,
                code_revision=args.revision,
                max_jobs_per_round=args.max_jobs,
            )
        elif command == "repair-status":
            value = service.repair_status(
                args.manifest, code_revision=args.revision
            )
        elif command == "records":
            value = service.records(
                run_id=args.run_id,
                dataset_id=args.dataset_id,
                limit=args.limit,
                offset=args.offset,
            )
        elif command == "reading-tasks":
            value = service.reading_tasks(
                run_id=args.run_id, limit=args.limit, offset=args.offset
            )
        elif command == "resolve":
            value = service.resolve_company(
                args.query,
                market=args.market,
                as_of=date.fromisoformat(args.as_of) if args.as_of else None,
            )
        elif command == "profile":
            value = service.industry_profile(args.ticker)
        elif command == "peers":
            value = service.peer_candidates(
                args.ticker, company_scope=args.company_scope
            )
        elif command == "coverage":
            value = service.coverage(
                args.snapshot_id, limit=args.limit, offset=args.offset
            )
        else:
            raise ValueError(f"unknown structured command: {command}")
        _emit(_safe_json_value(value), json_output=args.json_output)
        return EXIT_OK
    except (ValueError, SourceRegistryError, StorageNamespaceMismatch) as exc:
        return _emit_error(EXIT_USAGE, "validation", exc, args.json_output)
    except (StorageBusyError, BootstrapLockTimeout) as exc:
        return _emit_error(EXIT_RETRYABLE, "storage_busy", exc, args.json_output)
    except Exception as exc:
        return _emit_error(EXIT_INTERNAL, "structured_error", exc, args.json_output)


def _run_acquisition_command(args: argparse.Namespace) -> int:
    try:
        # Exactly one composition root is built for this invocation.
        runtime = AcquisitionRuntime.create(args.db, args.data_root)
        if args.command == "smoke-sources":
            return _run_smoke(runtime, args)
        if args.acquire_command == "list":
            rows = runtime.repository.list_runs(
                ticker=args.ticker,
                mode=args.mode,
                run_kind=args.run_kind,
                limit=args.limit,
            )
            _emit([_safe_json_value(item) for item in rows], json_output=args.json_output)
            return EXIT_OK
        if args.acquire_command == "show":
            _emit(_run_detail(runtime, args.run_id), json_output=args.json_output)
            return EXIT_OK
        if args.acquire_command == "execute":
            result = _execute(runtime, args.run_id, args.lease_ttl_seconds)
            _emit(_safe_json_value(result), json_output=args.json_output)
            return _result_exit_code(result)
        if args.acquire_command == "supplement":
            from .acquisition.supplement import plan_supplement
            plan = plan_supplement(runtime, parent_run_id=args.from_run,
                coverage_entry_id=args.coverage_entry, source_definition_id=args.source)
            if args.plan_only:
                _emit(_safe_json_value(plan), json_output=args.json_output)
                return EXIT_OK
            result = _execute(runtime, plan.run.run_id, 60)
            _emit(_safe_json_value(result), json_output=args.json_output)
            return _result_exit_code(result)
        if args.acquire_command == "start":
            if args.from_run and args.from_latest_run:
                raise ValueError("--from-run与--from-latest-run不能同时使用")
            mode = AcquisitionMode(args.mode)
            if mode != AcquisitionMode.RECONCILE and (
                args.from_run or args.from_latest_run
            ):
                raise ValueError("只有reconcile可使用--from-run/--from-latest-run")
            if mode == AcquisitionMode.RECONCILE and not (
                args.from_run or args.from_latest_run
            ):
                raise ValueError("reconcile必须指定--from-run或--from-latest-run")
            plan = runtime.plan_company_run(
                args.ticker,
                mode=mode,
                as_of=_parse_datetime(args.as_of),
                company_name=args.company_name,
                market=args.market,
                listing_date=_parse_date(args.listing_date),
                prospectus_date=_parse_date(args.prospectus_date),
                parent_run_id=args.from_run,
                persist=True,
            )
            response = {
                "run_id": plan.run.run_id,
                "mode": plan.run.mode.value,
                "run_kind": plan.run.run_kind.value,
                "resolved_parent_run_id": plan.run.parent_run_id,
                "reconcile_target": plan.run.reconcile_target,
                "physical_query_count": len(plan.physical_query_plan_items),
                "coverage_entry_count": len(plan.coverage_entries),
                "coverage_link_count": len(plan.coverage_links),
                "plan_only": bool(args.plan_only),
                "coverage_accounted": False,
                "material_gap_count": None,
                "checkpoint_advanced": False,
            }
            if args.plan_only:
                _emit(response, json_output=args.json_output)
                return EXIT_OK
            result = _execute(runtime, plan.run.run_id, 60)
            merged = {**response, **_safe_json_value(result)}
            _emit(merged, json_output=args.json_output)
            return _result_exit_code(merged)
        raise ValueError("未知acquire子命令")
    except (LeaseConflictError, StaleLeaseError) as exc:
        return _emit_error(EXIT_RETRYABLE, "active_lease", exc, args.json_output)
    except (StorageBusyError, BootstrapLockTimeout) as exc:
        return _emit_error(EXIT_RETRYABLE, "storage_busy", exc, args.json_output)
    except (
        ValueError,
        AcquisitionPlanningError,
        SourceRegistryError,
        StorageNamespaceMismatch,
        StorageBootstrapError,
        AcquisitionNotFoundError,
    ) as exc:
        return _emit_error(EXIT_USAGE, "validation", exc, args.json_output)
    except SnapshotPipelineError as exc:
        return _emit_error(EXIT_INTERNAL, "integrity_error", exc, args.json_output)
    except Exception as exc:
        return _emit_error(EXIT_INTERNAL, "internal_error", exc, args.json_output)


def _execute(runtime: AcquisitionRuntime, run_id: str, lease_ttl_seconds: int) -> Any:
    if runtime.orchestrator is None:
        raise RuntimeError("采集执行器尚不可用")
    execute = getattr(runtime.orchestrator, "execute_run", None) or getattr(
        runtime.orchestrator, "execute", None
    )
    if execute is None:
        raise RuntimeError("采集执行器接口不可用")
    return execute(run_id, lease_ttl_seconds=lease_ttl_seconds)


def _run_smoke(runtime: AcquisitionRuntime, args: argparse.Namespace) -> int:
    if runtime.orchestrator is None:
        raise RuntimeError("采集执行器尚不可用")
    smoke = getattr(runtime.orchestrator, "smoke_sources", None) or getattr(
        runtime.orchestrator, "execute_smoke", None
    )
    if smoke is None:
        raise RuntimeError("采集执行器未实现smoke_sources")
    result = smoke(ticker=args.ticker, source_ids=args.sources)
    payload = _safe_json_value(result)
    _emit(payload, json_output=args.json_output)
    code = _result_exit_code(payload)
    if not args.strict and code == EXIT_MATERIAL_GAP:
        return EXIT_OK
    return code


def _run_detail(runtime: AcquisitionRuntime, run_id: str) -> dict[str, Any]:
    repository = runtime.repository
    run = repository.get_run(run_id)
    return {
        "run": _safe_json_value(run),
        "events": [_safe_json_value(item) for item in repository.list_run_events(run_id)],
        "plan": [
            _safe_json_value(item) for item in repository.list_plan_items(run_id)
        ],
        "coverage": [
            _safe_json_value(item) for item in repository.list_coverage_entries(run_id)
        ],
        "coverage_links": [
            _safe_json_value(item)
            for item in repository.list_plan_coverage_links(run_id=run_id)
        ],
        "attempts": [
            _safe_json_value(item) for item in repository.list_attempts(run_id=run_id, limit=None)
        ],
        "coverage_resolutions": [
            _safe_json_value(item)
            for item in repository.list_coverage_resolutions(run_id)
        ],
    }


def _result_exit_code(value: Any) -> int:
    payload = _safe_json_value(value)
    if not isinstance(payload, dict):
        return EXIT_INTERNAL
    if payload.get("error") in {"active_lease", "storage_busy"}:
        return EXIT_RETRYABLE
    if payload.get("internal_error") or payload.get("integrity_error"):
        return EXIT_INTERNAL
    eligible = payload.get("default_consume_eligible")
    gaps = payload.get("material_gap_count")
    if eligible is False or (isinstance(gaps, int) and gaps > 0):
        return EXIT_MATERIAL_GAP
    return EXIT_OK


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of必须包含时区")
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _safe_json_value(value: Any, *, field_name: str | None = None) -> Any:
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    elif hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = {
            item.name: getattr(value, item.name)
            for item in fields(value)
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "owner_token",
                "owner_token_hash",
                "database_path",
                "db_path",
                "data_root",
            } or lowered.endswith("absolute_path") or is_sensitive_name(lowered):
                continue
            result[str(key)] = _safe_json_value(item, field_name=lowered)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item, field_name=field_name) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.name
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        if field_name and "url" in field_name and value.startswith(
            ("http://", "https://")
        ):
            return redact_url(value)
        if looks_like_absolute_local_path(value):
            return REDACTED_LOCAL_PATH
    return value


def _emit(value: Any, *, json_output: bool) -> None:
    payload = _safe_json_value(value)
    if json_output or isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(payload)


def _emit_error(code: int, subtype: str, exc: Exception, json_output: bool) -> int:
    _emit(
        {
            "ok": False,
            "exit_code": code,
            "error": subtype,
            "subtype": subtype,
            "detail": _safe_error_detail(exc),
        },
        json_output=json_output,
    )
    return code


def _safe_error_detail(exc: Exception) -> str:
    return redact_absolute_local_paths(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
