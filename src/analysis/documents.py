from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from .models import DocumentIngestRequest, DocumentRecord, SourceRecord
from .registry import PROJECT_ROOT


RAW_ROOT = PROJECT_ROOT / "var" / "raw"
MAX_OFFICIAL_DOCUMENT_BYTES = 80 * 1024 * 1024


def ingest_document(request: DocumentIngestRequest, raw_root: Path | str = RAW_ROOT) -> DocumentRecord:
    source_path = Path(request.path).expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"文件不存在: {source_path}")
    if source_path.suffix.lower() not in {".pdf", ".html", ".htm", ".txt", ".md"}:
        raise ValueError("只支持PDF、HTML、TXT和Markdown")
    content = source_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    ticker = "".join(char for char in request.ticker if char.isalnum() or char in "-_")
    if not ticker:
        raise ValueError("股票代码不能清洗为空")
    destination_dir = Path(raw_root).resolve() / ticker
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{digest[:16]}{source_path.suffix.lower()}"
    if not destination.exists():
        shutil.copy2(source_path, destination)
    text_path = destination.with_suffix(destination.suffix + ".txt")
    text, pages, ocr_used, warnings = _extract_text(destination)
    text_path.write_text(text, encoding="utf-8")
    source = SourceRecord(
        name=request.source_name,
        source_type="official-document",
        upstream_source_id=f"official:{digest}",
        url=request.source_url,
        published_at=request.published_at,
        document_hash=digest,
        authority_level=1,
        notes=request.source_name,
    )
    return DocumentRecord(
        ticker=request.ticker,
        title=request.title,
        archived_path=str(destination),
        text_path=str(text_path),
        sha256=digest,
        source=source,
        page_count=pages,
        ocr_used=ocr_used,
        warnings=warnings,
    )


def ingest_downloaded_document(
    *,
    ticker: str,
    content: bytes,
    title: str,
    source_name: str,
    source_url: str,
    published_at: datetime,
    provider: str,
    announcement_id: str,
    raw_root: Path | str = RAW_ROOT,
    metadata: dict[str, Any] | None = None,
) -> DocumentRecord:
    """Archive a downloaded official PDF and create a deterministic evidence record."""

    if not content.startswith(b"%PDF"):
        raise ValueError("正式公告下载结果不是PDF")
    if len(content) > MAX_OFFICIAL_DOCUMENT_BYTES:
        raise ValueError(f"正式公告超过{MAX_OFFICIAL_DOCUMENT_BYTES // 1024 // 1024}MB安全上限")
    digest = hashlib.sha256(content).hexdigest()
    safe_ticker = "".join(char for char in ticker if char.isalnum() or char in "-_")
    if not safe_ticker:
        raise ValueError("股票代码不能清洗为空")
    destination_dir = Path(raw_root).resolve() / safe_ticker
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{digest[:16]}.pdf"
    if not destination.exists():
        destination.write_bytes(content)
    text_path = destination.with_suffix(".pdf.txt")
    text, pages, ocr_used, warnings = _extract_text(destination)
    if not text_path.exists() or text_path.read_text(encoding="utf-8", errors="replace") != text:
        text_path.write_text(text, encoding="utf-8")
    archived_at = datetime.fromtimestamp(destination.stat().st_mtime, tz=timezone.utc)
    evidence_metadata = {
        "evidence_record_schema": "2",
        "provider": provider,
        "announcement_id": str(announcement_id),
        **(metadata or {}),
    }
    record_digest = _official_evidence_record_digest(
        ticker=ticker,
        content_digest=digest,
        title=title,
        source_name=source_name,
        source_url=source_url,
        published_at=published_at,
        provider=provider,
        announcement_id=str(announcement_id),
        metadata=evidence_metadata,
    )
    source = SourceRecord(
        source_id=f"src-official-v2-{provider}-{record_digest[:20]}",
        name=source_name,
        source_type="official-document",
        upstream_source_id=f"official-document:{digest}",
        url=source_url,
        published_at=published_at,
        retrieved_at=archived_at,
        document_hash=digest,
        authority_level=1,
        notes=title,
        metadata=evidence_metadata,
    )
    return DocumentRecord(
        document_id=f"doc-{safe_ticker}-v2-{provider}-{record_digest[:16]}",
        ticker=ticker,
        title=title,
        archived_path=str(destination),
        text_path=str(text_path),
        sha256=digest,
        source=source,
        page_count=pages,
        ocr_used=ocr_used,
        warnings=warnings,
        extracted_at=archived_at,
        metadata=evidence_metadata,
    )


def _official_evidence_record_digest(
    *,
    ticker: str,
    content_digest: str,
    title: str,
    source_name: str,
    source_url: str,
    published_at: datetime,
    provider: str,
    announcement_id: str,
    metadata: dict[str, Any],
) -> str:
    published = (
        published_at.replace(tzinfo=timezone.utc)
        if published_at.tzinfo is None
        else published_at.astimezone(timezone.utc)
    )
    payload = {
        "schema": "official-evidence-record-v2",
        "ticker": ticker,
        "content_digest": content_digest,
        "title": title,
        "source_name": source_name,
        "source_url": source_url,
        "published_at": published.isoformat(),
        "provider": provider,
        "announcement_id": announcement_id,
        "metadata": metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _extract_text(path: Path) -> tuple[str, int, bool, list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        warnings: list[str] = []
        ocr_used = False
        with fitz.open(path) as document:
            pages = []
            ocr_available = True
            for index, page in enumerate(document, 1):
                page_text = page.get_text("text")
                if len(page_text.strip()) < 20 and ocr_available:
                    try:
                        text_page = page.get_textpage_ocr(language="chi_sim+eng", dpi=180, full=True)
                        page_text = page.get_text("text", textpage=text_page)
                        ocr_used = True
                    except Exception as exc:
                        ocr_available = False
                        warnings.append(f"OCR不可用，扫描页保留为空: {exc}")
                pages.append(f"--- page {index} ---\n{page_text}")
        if not any(item.split("\n", 1)[-1].strip() for item in pages):
            warnings.append("PDF未提取到可检索文本，请人工补充OCR文本")
        return "\n\n".join(pages), len(pages), ocr_used, sorted(set(warnings))
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    return text, 1, False, []
