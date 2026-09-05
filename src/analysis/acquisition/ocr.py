"""Versioned, resumable local OCR derived only from verified PDF snapshots."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
from typing import Any, Callable

from .models import PolicyDecision, ResourceRole, canonical_json_bytes

OCR_VERSION = "1.0.0"
PAGE_EXTRACTOR = "announcement-ocr-page"
LAYOUT_EXTRACTOR = "announcement-ocr-layout"
TEXT_EXTRACTOR = "announcement-ocr-text"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OCR_CONFIG = PROJECT_ROOT / "config/data_sources/announcement_ocr.v1.json"


class LocalOcrWorker:
    def __init__(self, python: Path, config_path: Path = DEFAULT_OCR_CONFIG) -> None:
        self.config = json.loads(config_path.read_text("utf-8"))
        self._queue: queue.Queue[str] = queue.Queue()
        self._stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [str(python), "-u", str(PROJECT_ROOT / "scripts/announcement_ocr_worker.py"),
             "--config", str(config_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        def read() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self._queue.put(line)
            self._queue.put("")
        threading.Thread(target=read, daemon=True).start()
        try:
            ready = self._receive()
            if not ready.get("ready"):
                raise ValueError("ocr_worker_not_ready")
            self.provenance = ready["provenance"]
            if self.provenance["config"] != self.config:
                raise ValueError("ocr_worker_config_mismatch")
        except Exception:
            self.close()
            raise

    def _receive(self) -> dict[str, Any]:
        try:
            raw = self._queue.get(timeout=self.config["page_timeout_seconds"])
        except queue.Empty as exc:
            self.process.kill()
            raise TimeoutError("local_ocr_page_timeout") from exc
        if not raw:
            self._stderr.seek(0)
            raise RuntimeError("ocr_worker_exited: " + self._stderr.read()[-1500:].decode("utf-8", "replace"))
        result = json.loads(raw)
        if "error" in result:
            raise RuntimeError("ocr_worker_error: " + result["error"] + ": " + result.get("message", ""))
        return result

    def recognize(self, png_path: Path) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise RuntimeError("ocr_worker_not_running")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"png_path": str(png_path),
            "png_sha256": hashlib.sha256(png_path.read_bytes()).hexdigest()}) + "\n")
        self.process.stdin.flush()
        return self._receive()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self._stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> "LocalOcrWorker":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _read_artifact(runtime: Any, artifact: Any) -> bytes:
    return runtime.blob_store.read_verified_derived(artifact.archive_relative_path,
        expected_sha256=artifact.output_sha256, expected_length=artifact.output_byte_length)


def _freeze(runtime: Any, snapshot_id: str, extractor: str, parameters: dict[str, Any],
            output: bytes, artifact_type: str) -> Any:
    sha = hashlib.sha256(output).hexdigest()
    for artifact in runtime.repository.list_derived_artifacts(snapshot_id):
        if (artifact.extractor_id == extractor and artifact.extractor_version == OCR_VERSION
                and artifact.parameters == parameters and artifact.output_sha256 == sha):
            _read_artifact(runtime, artifact)
            return artifact
    return runtime.snapshot_service.freeze_derived_artifact(parent_snapshot_id=snapshot_id,
        output=output, extractor_id=extractor, extractor_version=OCR_VERSION,
        artifact_type=artifact_type, parameters=parameters)


def _validated_lines(result: dict[str, Any], width: int, height: int) -> list[dict[str, Any]]:
    if result.get("width") != width or result.get("height") != height:
        raise ValueError("ocr_page_dimensions_mismatch")
    for line in result["lines"]:
        confidence = line["confidence"]
        if not isinstance(line["text"], str) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("ocr_invalid_line")
        if len(line["box_px"]) != 4 or any(len(p) != 2 or not all(math.isfinite(v) for v in p)
            or not (0 <= p[0] <= width and 0 <= p[1] <= height) for p in line["box_px"]):
            raise ValueError("ocr_invalid_box")
    return result["lines"]


def extract_snapshot_ocr(runtime: Any, snapshot_id: str, worker: Any,
                         progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    import fitz
    snapshot = runtime.repository.get_raw_resource_snapshot(snapshot_id)
    if snapshot.resource_role != ResourceRole.CONTENT or snapshot.mime_type != "application/pdf":
        raise ValueError("ocr_requires_pdf_content_snapshot")
    definition = runtime.repository.get_source_definition_version(
        snapshot.source_definition_id, snapshot.source_definition_version)
    if definition.license_policy.save_derived_text != PolicyDecision.ALLOWED:
        raise ValueError("derived_text_not_allowed")
    events = runtime.repository.list_snapshot_integrity_events(snapshot_id)
    if events and max(events, key=lambda e: (e.checked_at, e.integrity_event_id)).status.value == "quarantined":
        raise ValueError("snapshot_quarantined")
    content = runtime.snapshot_bytes(snapshot_id)
    if len(content) != snapshot.byte_length or hashlib.sha256(content).hexdigest() != snapshot.sha256:
        raise ValueError("snapshot_integrity_mismatch")
    parameters = {"raw_sha256": snapshot.sha256, "engine": worker.provenance,
                  "renderer": "PyMuPDF", "renderer_version": fitz.VersionBind,
                  "coordinate_system": "rendered-page-top-left; PDF points; one-based PDF page"}
    prior_pages = {canonical_json_bytes(a.parameters): a for a in runtime.repository.list_derived_artifacts(snapshot_id)
                   if a.extractor_id == PAGE_EXTRACTOR and a.extractor_version == OCR_VERSION}
    pages, ocr_pages, review_pages = [], [], []
    temp_root = runtime.data_root / "ocr-work"
    temp_root.mkdir(exist_ok=True)
    with fitz.open(stream=content, filetype="pdf") as doc:
        if not len(doc):
            raise ValueError("empty_pdf_page_tree")
        for page in doc:
            number = page.number + 1
            native = page.get_text("text")
            record = {"pdf_page_number": number, "width_pt": page.rect.width,
                      "height_pt": page.rect.height, "rotation": page.rotation,
                      "cropbox": list(page.cropbox), "native_text": native}
            if native.strip():
                record.update({"method": "native", "text": native, "status": "native_text"})
            else:
                page_parameters = {**parameters, "page": record, "dpi": worker.config["dpi"]}
                artifact = prior_pages.get(canonical_json_bytes(page_parameters))
                reused = artifact is not None
                if reused:
                    result = json.loads(_read_artifact(runtime, artifact))
                else:
                    with tempfile.TemporaryDirectory(dir=temp_root) as scratch:
                        path = Path(scratch) / "page.png"
                        pix = page.get_pixmap(dpi=worker.config["dpi"], alpha=False)
                        pix.save(path)
                        result = worker.recognize(path)
                        _validated_lines(result, pix.width, pix.height)
                        result["render_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                        result["pdf_page_number"] = number
                        result["raw_sha256"] = snapshot.sha256
                    artifact = _freeze(runtime, snapshot_id, PAGE_EXTRACTOR, page_parameters,
                                       canonical_json_bytes(result), "ocr")
                if result["pdf_page_number"] != number or result["raw_sha256"] != snapshot.sha256:
                    raise ValueError("ocr_cached_page_identity_mismatch")
                lines = _validated_lines(result, result["width"], result["height"])
                mapped = [{**line, "box_pt": [[x * page.rect.width / result["width"],
                                              y * page.rect.height / result["height"]] for x, y in line["box_px"]]}
                          for line in lines]
                text = "\n".join(line["text"] if line["text"].strip() else "[OCR detected region without recognized text]"
                                 for line in mapped)
                flags = []
                if not any(line["text"].strip() for line in lines):
                    flags.append("empty_ocr_requires_visual_review")
                if any(not line["text"].strip() for line in lines):
                    flags.append("unrecognized_regions")
                if any(line["confidence"] < worker.config["low_confidence_threshold"] for line in lines):
                    flags.append("low_confidence_regions")
                if any(re.match(r"^\s*[-−]?\s*[,，.]\s*\d", line["text"]) for line in lines):
                    flags.append("malformed_numeric_regions")
                record.update({"method": "local_ocr", "text": text, "lines": mapped,
                    "status": "requires_review" if flags else "ocr_machine_transcription",
                    "review_flags": flags, "page_artifact_id": artifact.derived_artifact_id,
                    "page_artifact_sha256": artifact.output_sha256})
                ocr_pages.append(number)
                if flags:
                    review_pages.append(number)
                if progress:
                    progress({"snapshot_id": snapshot_id, "page_number": number,
                              "reused": reused, "status": record["status"], "flags": flags})
            pages.append(record)
    layout = {"snapshot_id": snapshot_id, "raw_sha256": snapshot.sha256,
              "machine_ocr_is_not_verified_financial_fact": True, "pages": pages}
    layout_artifact = _freeze(runtime, snapshot_id, LAYOUT_EXTRACTOR, parameters,
                              canonical_json_bytes(layout), "ocr")
    header = "机器派生阅读文本；PDF 页码为文件顺序。OCR 数字、表格关系与签章须回看原页。\n"
    text = header + "\n\n".join(f"--- PDF page {p['pdf_page_number']} | {p['method']} | {p['status']} ---\n{p['text']}" for p in pages)
    text_artifact = _freeze(runtime, snapshot_id, TEXT_EXTRACTOR,
                           {**parameters, "layout_artifact_id": layout_artifact.derived_artifact_id}, text.encode("utf-8"), "text")
    return {"snapshot_id": snapshot_id, "raw_sha256": snapshot.sha256, "page_count": len(pages),
            "ocr_pages": ocr_pages, "review_pages": review_pages,
            "layout_artifact_id": layout_artifact.derived_artifact_id,
            "layout_sha256": layout_artifact.output_sha256,
            "text_artifact_id": text_artifact.derived_artifact_id, "text_sha256": text_artifact.output_sha256,
            "text_relative_path": text_artifact.archive_relative_path,
            "layout_relative_path": layout_artifact.archive_relative_path}
