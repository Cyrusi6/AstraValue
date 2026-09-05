"""Versioned plain-text derivation from committed announcement snapshots."""
from __future__ import annotations

import codecs
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .models import PolicyDecision, ResourceRole


EXTRACTOR_ID = "announcement-text"
EXTRACTOR_VERSION = "1.1.0"
_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([a-zA-Z0-9_-]+)", re.I)


def decode_html(content: bytes) -> tuple[str, str]:
    """Respect HTML declarations; never conceal broken Chinese with replacement."""
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig"), "utf-8-sig"
    match = _CHARSET.search(content[:8192])
    if match:
        declared = match.group(1).decode("ascii").lower().replace("_", "-")
        encoding = {
            "utf-8": "utf-8", "utf8": "utf-8", "gb2312": "gb18030",
            "gbk": "gb18030", "gb18030": "gb18030", "big5": "big5",
        }.get(declared)
        if encoding is None:
            raise ValueError("unsupported_html_encoding")
        return content.decode(encoding), encoding
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return content.decode("gb18030"), "gb18030"


def validate_cninfo_html_response(content: bytes) -> None:
    """Check bounded HTML envelope structure before publishing content evidence.

    This is a response-schema check, not the downstream text extraction.  The
    HTTP/challenge classifier must run first; ordinary errors must also fail.
    """
    prefix = content[:131072]
    if not re.search(rb"<html(?:\s|>)", prefix, re.I):
        raise ValueError("invalid_html_document")
    if not re.search(rb"<(?:body|pre)(?:\s|>)", prefix, re.I):
        raise ValueError("invalid_html_document")
    if not re.search(rb"</(?:html|body)>\s*$", content.rstrip(), re.I):
        raise ValueError("incomplete_html_document")
    # Validate all bytes within the transport's size limit, including the tail.
    decoded, _ = decode_html(content)
    title = re.search(r"<title[^>]*>(.*?)</title>", decoded[:131072], re.I | re.S)
    if title and re.search(r"(?:\b(?:404|50[0-9]|error|not found|forbidden)\b|页面不存在|访问出错|系统错误)", title.group(1), re.I):
        raise ValueError("html_error_page")
    if len(content) < 200:
        raise ValueError("invalid_html_document")


class _PlainHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"head", "script", "style", "noscript", "template"}:
            self.suppressed.append(tag)
        if not self.suppressed and tag in {"p", "div", "br", "tr", "h1", "h2", "pre", "li"}:
            self.parts.append("\n")
        elif not self.suppressed and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed:
            if tag == self.suppressed[-1]:
                self.suppressed.pop()
        elif tag in {"p", "div", "tr", "h1", "h2", "pre", "li", "title"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def html_text(content: bytes) -> tuple[str, str]:
    decoded, encoding = decode_html(content)
    parser = _PlainHTML()
    parser.feed(decoded)
    parser.close()
    lines = [re.sub(r"[\t\xa0 \r\f\v]+", " ", line).strip()
             for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line), encoding


@dataclass(frozen=True)
class ExtractedAnnouncement:
    snapshot_id: str
    derived_artifact_id: str
    sha256: str
    character_count: int
    page_count: int
    empty_page_count: int
    encoding: str


def extract_announcement_text(runtime: Any, snapshot_id: str) -> ExtractedAnnouncement:
    """Only a committed, verified content snapshot can enter this extractor."""
    snapshot = runtime.repository.get_raw_resource_snapshot(snapshot_id)
    if snapshot.resource_role != ResourceRole.CONTENT:
        raise ValueError("announcement_text_requires_content_snapshot")
    definition = runtime.repository.get_source_definition_version(
        snapshot.source_definition_id, snapshot.source_definition_version,
    )
    if definition.license_policy.save_derived_text != PolicyDecision.ALLOWED:
        raise ValueError("derived_text_not_allowed")
    integrity = runtime.repository.list_snapshot_integrity_events(snapshot_id)
    if integrity and max(integrity, key=lambda e: (e.checked_at, e.integrity_event_id)).status.value == "quarantined":
        raise ValueError("snapshot_quarantined")
    content = runtime.snapshot_bytes(snapshot_id)
    if len(content) != snapshot.byte_length or hashlib.sha256(content).hexdigest() != snapshot.sha256:
        raise ValueError("snapshot_integrity_mismatch")
    for prior in runtime.repository.list_derived_artifacts(snapshot_id):
        if prior.extractor_id == EXTRACTOR_ID and prior.extractor_version == EXTRACTOR_VERSION:
            prior_bytes = runtime.blob_store.read_verified_derived(
                prior.archive_relative_path, expected_sha256=prior.output_sha256,
                expected_length=prior.output_byte_length,
            )
            return ExtractedAnnouncement(snapshot_id, prior.derived_artifact_id,
                prior.output_sha256, len(prior_bytes.decode("utf-8")),
                int(prior.parameters["page_count"]), int(prior.parameters["empty_page_count"]),
                str(prior.parameters["encoding"]))
    mime = snapshot.mime_type.split(";", 1)[0].lower()
    if mime == "text/html":
        validate_cninfo_html_response(content)
        text, encoding = html_text(content)
        pages, empty = 1, int(not text.strip())
    elif mime == "application/pdf":
        import fitz
        if not content.startswith(b"%PDF-"):
            raise ValueError("invalid_pdf_header")
        with fitz.open(stream=content, filetype="pdf") as document:
            page_texts = [page.get_text("text") for page in document]
        pages, empty = len(page_texts), sum(not t.strip() for t in page_texts)
        if not pages:
            raise ValueError("empty_pdf_page_tree")
        text = "\n\n".join(f"--- page {index} ---\n{value}" for index, value in enumerate(page_texts, 1))
        encoding = "pdf-unicode"
    else:
        raise ValueError("unsupported_announcement_mime")
    if empty == pages:
        raise ValueError("announcement_text_empty_requires_review")
    encoded = text.encode("utf-8")
    artifact = runtime.snapshot_service.freeze_derived_artifact(
        parent_snapshot_id=snapshot_id, output=encoded, extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION, artifact_type="text",
        parameters={"encoding": encoding, "page_count": pages, "empty_page_count": empty,
                    "ocr": False},
    )
    return ExtractedAnnouncement(snapshot_id, artifact.derived_artifact_id,
                                 hashlib.sha256(encoded).hexdigest(), len(text), pages, empty, encoding)
