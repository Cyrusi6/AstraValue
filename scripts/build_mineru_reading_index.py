"""为成功的 MinerU 解析结果发布独立阅读副本与完整公告索引。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

from analysis.acquisition.mineru import inspect_bundle


def checked_bytes(data_root: Path, relative: str, expected: str) -> bytes:
    path = (data_root / relative).resolve()
    path.relative_to(data_root.resolve())
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("reading_index_hash_mismatch")
    return raw


def publish(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("reading_copy_already_exists_with_different_bytes")
    else:
        path.write_bytes(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--announcement-index", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text("utf-8"))
    results = {r["snapshot_id"]: r for r in report["results"]}
    selected = set(report["selection"]["snapshot_ids"])
    if (len(results) != len(report["results"]) or set(results) != selected
            or any(r["status"] != "completed_with_machine_parse" for r in results.values())):
        raise ValueError("mineru_selection_not_complete")
    with args.announcement_index.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        old_fields = list(reader.fieldnames)
        rows = list(reader)
    if len(rows) != len({r["canonical_resource_id"] for r in rows}):
        raise ValueError("announcement_index_duplicate_canonical")
    if not selected <= {r["snapshot_id"] for r in rows}:
        raise ValueError("selected_snapshot_missing_from_index")
    args.output.mkdir(parents=True, exist_ok=True)
    reviewed = []
    pages_queue = []
    for row in rows:
        row.update(effective_parser="native", effective_text_status=row["text_status"],
                   effective_text_path=row["text_path"], effective_text_sha256=row["text_sha256"],
                   effective_text_reading_path=row["text_reading_path"], mineru_markdown_path="",
                   mineru_layout_path="", mineru_page_count="", mineru_table_count="")
        result = results.get(row["snapshot_id"])
        if result is None:
            continue
        original = Path(row["raw_path"]).read_bytes()
        if hashlib.sha256(original).hexdigest() != result["raw_sha256"]:
            raise ValueError("reading_index_original_hash_mismatch")
        blobs = {kind: checked_bytes(args.data_root, result[kind + "_relative_path"], result[kind + "_sha256"])
                 for kind in ("bundle", "layout", "text", "markdown")}
        layout = json.loads(blobs["layout"])
        if [p["pdf_page_number"] for p in layout["pages"]] != result["parsed_pages"]:
            raise ValueError("reading_index_page_coverage_mismatch")
        name = re.sub(r"[^A-Za-z0-9_-]", "_", row["canonical_resource_id"])
        folder = args.output / "reading-copies" / (name + "_" + result["bundle_sha256"][:12])
        publish(folder / "full.md", blobs["markdown"])
        publish(folder / "pages.txt", blobs["text"])
        publish(folder / "layout.json", blobs["layout"])
        for entry, raw in inspect_bundle(blobs["bundle"], report["config"]).items():
            if re.fullmatch(r"images/[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|webp)", entry, re.I):
                publish(folder / entry, raw)
        row.update(effective_parser="mineru_precision_vlm", effective_text_status="machine_parse_requires_review",
                   effective_text_path=str((args.data_root / result["text_relative_path"]).resolve()),
                   effective_text_sha256=result["text_sha256"],
                   effective_text_reading_path=str((folder / "pages.txt").resolve()),
                   mineru_markdown_path=str((folder / "full.md").resolve()),
                   mineru_layout_path=str((folder / "layout.json").resolve()),
                   mineru_page_count=result["page_count"], mineru_table_count=result["table_count"])
        for page in layout["pages"]:
            pages_queue.append({"snapshot_id": row["snapshot_id"], "title": row["title"],
                "pdf_page_number": page["pdf_page_number"], "review_flags": ";".join(page["review_flags"]),
                "review_status": "pending", "reviewer": "", "reviewed_at": "", "manual_notes": ""})
        reviewed.append(row)
    if len(reviewed) != len(selected):
        raise ValueError("reading_index_selected_count_mismatch")
    fields = old_fields + [k for k in rows[0] if k not in old_fields]
    for filename, values, fieldnames in (("announcement-index-with-mineru.csv", rows, fields),
            ("page-review-queue.csv", pages_queue, list(pages_queue[0]))):
        with (args.output / filename).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(values)
    def link(path: str | Path) -> str:
        return "<" + Path(path).resolve().as_posix() + ">"
    lines = ["# 贵州茅台 MinerU 精准解析阅读入口", "",
        f"{len(reviewed)} 份 PDF、{len(pages_queue)} 页已通过精准 API 解析并保存；请求模型 vlm，实际服务后端与版本见逐份布局。完整目录保留 {len(rows)} 条公告。", "",
        "原始 PDF 和旧解析结果保留。每页均为机器解析、待人工核对；签章、签署日期、表格单位及关键数字请回看原页。Markdown 中保留服务返回的表格与图片。", "",
        f"[完整公告索引]({link(args.output / 'announcement-index-with-mineru.csv')}) · [逐页复核队列]({link(args.output / 'page-review-queue.csv')})", "",
        "| 公告 | 页数 | 表格数 | 阅读入口 |", "| --- | ---: | ---: | --- |"]
    for row in reviewed:
        title = row["title"].replace("|", "／")
        lines.append(f"| {title} | {row['mineru_page_count']} | {row['mineru_table_count']} | "
            f"[原 PDF]({link(row['reading_path'] or row['raw_path'])}) · "
            f"[Markdown 与图片]({link(row['mineru_markdown_path'])}) · "
            f"[逐页文本]({link(row['effective_text_reading_path'])}) |")
    (args.output / "MinerU阅读索引.md").write_text("\n".join(lines) + "\n", "utf-8")
    print(json.dumps({"announcements": len(rows), "parsed_documents": len(reviewed),
        "parsed_pages": len(pages_queue), "table_count": sum(int(r["mineru_table_count"]) for r in reviewed),
        "index": str((args.output / "MinerU阅读索引.md").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
