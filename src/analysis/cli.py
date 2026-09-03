from __future__ import annotations

import argparse
import json
from pathlib import Path

from .demo import build_demo_request
from .exports import export_report
from .registry import MethodRegistry
from .service import AnalysisService


def main() -> int:
    parser = argparse.ArgumentParser(description="A股八步财报分析本地工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动本地网页与API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

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

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("analysis.api:app", host=args.host, port=args.port, reload=args.reload)
        return 0
    if args.command == "validate-methods":
        errors = MethodRegistry().validate_library()
        if errors:
            print("\n".join(errors))
            return 1
        print("METHOD_LIBRARY_OK")
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())

