"""python -m analysis.business_evidence.cli: offline route/import/query."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from pydantic import ValidationError

from .corpus import FrozenCorpus
from .models import Citation, ReviewBatch
from .routing import DEFAULT_ROUTES, build_routes, load_routes, publish_json
from .store import FactStore


def markdown_table(result: dict) -> str:
    def cell(value):
        return str(value).replace("|", "／").replace("\n", " ")
    lines = ["# 业务事实与出处", "", "记录来自 AI 原文复核，尚未人工黄金签署。current 仅表示当前未被更正且无冲突，不代表商业判断已独立证实。",
             "", f"查询时点：{result['as_of']}", "",
             "|主题|期间|指标及维度|值与单位|事件状态|版本状态|出处|",
             "|---|---|---|---|---|---|---|"]
    for fact in result["facts"]:
        references = []
        for cite in fact["citations"]:
            locator = f"PDF p{cite['page_number']}" if cite["page_number"] else "HTML 正文"
            references.append(f"[{cell(cite['title'])}]({cite['source_url']}) {locator}，{cell(cite['section'])}")
        lines.append("|" + "|".join([",".join(fact["question_ids"]), cell(fact["period"]),
            cell(fact["metric"] + " " + json.dumps(fact["dimensions"], ensure_ascii=False)),
            cell(fact["value"] + " " + fact["unit"]), fact["event_stage"], fact["status"], "；".join(references)]) + "|")
    lines += ["", "JSON 保留完整原文引文、主体、口径、版本、更正关系及 manifest/snapshot/artifact/hash。重复引用不能计为独立证明。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地业务证据章节定位、事实去重与更正")
    commands = parser.add_subparsers(dest="command", required=True)
    route = commands.add_parser("route", help="生成十主题候选页；不会自动生成事实")
    route.add_argument("--config", type=Path, default=DEFAULT_ROUTES)
    route.add_argument("--snapshot", action="append")
    route.add_argument("--text-selection", type=Path, help="snapshot ID 到显式 text artifact ID 的 JSON 映射")
    route.add_argument("--output", type=Path, required=True)
    imp = commands.add_parser("import", help="核对原文后原子导入结构化复核记录")
    imp.add_argument("--review", type=Path, required=True)
    imp.add_argument("--facts-db", type=Path, required=True)
    query = commands.add_parser("query", help="按时点查询事实、更正历史及冲突")
    query.add_argument("--facts-db", type=Path, required=True)
    query.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    query.add_argument("--company")
    query.add_argument("--question", choices=[f"Q{i:02d}" for i in range(1, 11)])
    query.add_argument("--output", type=Path, required=True)
    for sub in (route, imp, query):
        sub.add_argument("--db", type=Path, required=True, help="已有采集数据库，只读")
        sub.add_argument("--data-root", type=Path, required=True)
        sub.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        corpus = FrozenCorpus(args.db, args.data_root, args.manifest)
        if hasattr(args, "facts_db") and args.facts_db.resolve() == args.db.resolve():
            raise ValueError("facts_database_must_be_separate_from_acquisition")
        if args.command == "route":
            selection = json.loads(args.text_selection.read_text("utf-8")) if args.text_selection else None
            result = build_routes(corpus, load_routes(args.config), args.snapshot, selection)
            publish_json(args.output, result)
            summary = {**result["counts"], "output": str(args.output.resolve()), "network_calls": 0}
        elif args.command == "import":
            batch = ReviewBatch.model_validate_json(args.review.read_bytes())
            store = FactStore(args.facts_db, corpus.manifest.storage_namespace_id, create=True)
            summary = store.import_batch(batch, corpus)
        else:
            store = FactStore(args.facts_db, corpus.manifest.storage_namespace_id)
            result = store.query(as_of=args.as_of, company_id=args.company, question_id=args.question)
            # Re-consumption verifies every original manifest, including earlier batches.
            checked = {}
            for fact in result["facts"]:
                for cite in fact["citations"]:
                    mid = cite["manifest_id"]
                    if mid not in checked:
                        checked[mid] = FrozenCorpus(args.db, args.data_root, mid, selection_policy=None)
                    original = Citation.model_validate({key: cite[key] for key in Citation.model_fields})
                    actual = checked[mid].citation(original, fact["company_id"])
                    if actual != {key: value for key, value in cite.items() if key != "citation_id"}:
                        raise ValueError("stored_citation_provenance_changed")
            publish_json(args.output, result)
            md = args.output.with_suffix(".md")
            text = markdown_table(result)
            if md.exists() and md.read_text("utf-8") != text:
                raise ValueError("markdown_output_exists_with_different_content")
            md.write_text(text, "utf-8")
            summary = {"facts": len(result["facts"]), "counts": result["counts"], "output": str(args.output.resolve())}
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except ValidationError as exc:
        print(json.dumps({"status": "failed", "error": "invalid_review_schema", "error_count": exc.error_count(),
                          "details": exc.errors(include_url=False, include_input=False, include_context=False)[:5]}, ensure_ascii=False))
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
