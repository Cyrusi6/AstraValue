"""Locate deferred-topic filing passages without inferring events or numbers."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .report_materials import _parse, _verify_report_body
from .workspace import digest, read_json


# Registered reading routes carry literal source text. These narrower terms
# locate the nine requested topics; they never establish a negative claim.
TOPICS = {
    "guarantee": ("RD07", ("担保情况", "重大担保"), "仅限披露期间和担保栏目；其他期间未知"),
    "litigation": ("RD07", ("重大诉讼", "仲裁事项"), "重大诉讼仲裁披露；普通诉讼和其他期间未知"),
    "pledge": ("RD07", ("质押、标记或冻结", "质押或冻结"), "股东期末状态；未知单元格及逐笔质押记录保留未知"),
    "seo": ("RD08", ("证券发行与上市", "证券发行情况"), "报告期证券发行栏目；不能推断历史从未增发"),
    "allotment": ("RD08", ("证券发行与上市", "证券发行情况"), "报告期证券发行栏目；不能推断历史从未配股"),
    "bond_issuance": ("RD08", ("债券相关情况", "公司债券", "债务融资工具"), "公司发行与投资持有债券分开；持有金额不作发行事件"),
    "unlock_peer": ("RD11", ("限售股份变动", "解除限售"), "区分计划与实际解禁；报告期不适用不外推历史"),
    "customers_peer": ("RD02", ("前五名客户", "前五名供应商", "前5名客户"), "客户供应商汇总金额及集中度；不替代逐户实名记录"),
    "goodwill": ("RD05", ("商誉",), "余额、减值均待专题核对；空白与会计政策不补零"),
}


def organize_topic_evidence(output: Path, topics: list[str], periods: list[str]) -> dict:
    """Add exact page text with provenance and return per-topic reading gaps."""
    documents = read_json(output / "live-documents.json").get("documents", [])
    evidence_file = output / "document-evidence.jsonl"
    rows = [json.loads(line) for line in evidence_file.read_text("utf-8").splitlines() if line.strip()]
    found = {topic: [] for topic in topics}
    for document in documents:
        if document.get("period") not in periods or document.get("parse_status") != "parsed":
            continue
        parsed = _parse(document, output)
        _verify_report_body(document, parsed)
        pages = [unit for unit in parsed["units"] if unit.get("kind") == "page"]
        for topic in topics:
            route, terms, _ = TOPICS[topic]
            matched = [unit for unit in pages if any(term in re.sub(r"\s+", "", unit["text"]) for term in terms)
                       and "目录" not in re.sub(r"\s+", "", unit["text"])[:100]]
            for unit in matched[:3]:
                identity = ["deferred-disclosure-v1", topic, document["original_sha256"],
                            parsed["parser_version"], unit["locator"], digest(unit["text"])]
                row = {
                    "evidence_id": "document-evidence-" + digest(identity)[:24],
                    "company": document["ticker"], "period": document["period"],
                    "route_id": route, "deferred_topic": topic,
                    "document_class": document["document_class"],
                    "original_sha256": document["original_sha256"],
                    "original_path": document["original_path"], "original_url": document["url"],
                    "published_at": document["published_at"], "parser_version": parsed["parser_version"],
                    "locator": unit["locator"], "text": unit["text"],
                    "data_nature": "source_text", "consumption_status": "source_text_evidence",
                    "semantic_status": "organized_literal_source_passage",
                    "question_answer_status": "not_evaluated", "numeric_formula_eligible": False,
                }
                rows.append(row)
                found[topic].append({k: row[k] for k in (
                    "evidence_id", "period", "locator", "original_path", "original_sha256", "original_url", "published_at"
                )})
    unique = {row["evidence_id"]: row for row in rows}
    evidence_file.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                                   for _, row in sorted(unique.items())), encoding="utf-8")
    return {
        topic: {
            "api_state": "no_data", "evidence": refs,
            "consumption_status": "source_text_available" if refs else "unknown",
            "numeric_formula_eligible": False, "scope": TOPICS[topic][2],
            "remaining": (["goodwill_balance_and_impairment_unknown"] if topic == "goodwill" else
                          ["topic_passage_missing"] if not refs else ["human_scope_review_pending"]),
        }
        for topic, refs in found.items()
    }
