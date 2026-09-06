"""Immutable, resumable MinerU precision parsing of committed PDF snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import os
import stat
import time
from typing import Any, Callable
import zipfile

from .mineru_client import MinerUError
from .models import PolicyDecision, ResourceRole, canonical_json_bytes

VERSION = "1.0.0"
BUNDLE_EXTRACTOR = "mineru-precision-bundle"
LAYOUT_EXTRACTOR = "mineru-precision-layout"
TEXT_EXTRACTOR = "mineru-precision-text"
MARKDOWN_EXTRACTOR = "mineru-precision-markdown"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_artifact(runtime: Any, artifact: Any) -> bytes:
    return runtime.blob_store.read_verified_derived(artifact.archive_relative_path,
        expected_sha256=artifact.output_sha256, expected_length=artifact.output_byte_length)


def _freeze(runtime: Any, snapshot_id: str, extractor: str, parameters: dict,
            raw: bytes, kind: str) -> Any:
    digest = _sha(raw)
    for artifact in runtime.repository.list_derived_artifacts(snapshot_id):
        if (artifact.extractor_id == extractor and artifact.extractor_version == VERSION
                and artifact.parameters == parameters and artifact.output_sha256 == digest):
            read_artifact(runtime, artifact)
            return artifact
    return runtime.snapshot_service.freeze_derived_artifact(parent_snapshot_id=snapshot_id,
        extractor_id=extractor, extractor_version=VERSION, parameters=parameters,
        artifact_type=kind, output=raw)


def verified_pdf(runtime: Any, snapshot_id: str, config: dict) -> tuple[Any, bytes, list[dict]]:
    import fitz
    snapshot = runtime.repository.get_raw_resource_snapshot(snapshot_id)
    if snapshot.resource_role != ResourceRole.CONTENT or snapshot.mime_type != "application/pdf":
        raise MinerUError("mineru_requires_pdf_content_snapshot")
    policy = runtime.repository.get_source_definition_version(
        snapshot.source_definition_id, snapshot.source_definition_version).license_policy
    if policy.save_derived_text != PolicyDecision.ALLOWED or policy.llm_processing != PolicyDecision.ALLOWED:
        raise MinerUError("mineru_processing_not_allowed")
    events = runtime.repository.list_snapshot_integrity_events(snapshot_id)
    if events and max(events, key=lambda e: (e.checked_at, e.integrity_event_id)).status.value == "quarantined":
        raise MinerUError("snapshot_quarantined")
    content = runtime.snapshot_bytes(snapshot_id)
    if len(content) != snapshot.byte_length or _sha(content) != snapshot.sha256:
        raise MinerUError("snapshot_integrity_mismatch")
    if len(content) > config["max_file_bytes"]:
        raise MinerUError("mineru_file_size_limit")
    pages = []
    with fitz.open(stream=content, filetype="pdf") as doc:
        if not 1 <= len(doc) <= config["max_pages"]:
            raise MinerUError("mineru_page_limit_split_required")
        for page in doc:
            pages.append({"pdf_page_number": page.number + 1, "native_text": page.get_text("text"),
                "width_pt": page.rect.width, "height_pt": page.rect.height,
                "rotation": page.rotation, "cropbox": list(page.cropbox)})
    return snapshot, content, pages


def inspect_bundle(raw: bytes, config: dict) -> dict[str, bytes]:
    if len(raw) > config["max_zip_bytes"]:
        raise MinerUError("mineru_zip_size_limit")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            infos = bundle.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > config["max_uncompressed_bytes"]:
                raise MinerUError("mineru_zip_expansion_limit")
            files = {}
            seen = set()
            for info in infos:
                path = PurePosixPath(info.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in info.orig_filename
                        or ":" in info.filename or info.filename in seen
                        or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                    raise MinerUError("mineru_unsafe_zip_entry")
                seen.add(info.filename)
                if not info.is_dir():
                    files[info.filename] = bundle.read(info)
            return files
    except MinerUError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise MinerUError("mineru_invalid_zip") from None


def _one_file(files: dict[str, bytes], predicate: Callable[[str], bool]) -> tuple[str, bytes]:
    matches = [(name, raw) for name, raw in files.items() if predicate(PurePosixPath(name).name)]
    if len(matches) != 1:
        raise MinerUError("mineru_missing_or_ambiguous_output")
    return matches[0]


def _block_text(block: dict) -> str:
    keys = ("text", "table_caption", "table_body", "table_footnote", "image_caption",
            "image_footnote", "chart_caption", "chart_body", "chart_footnote",
            "code_caption", "code_body", "code_footnote", "list_items", "content")
    values = []
    for key in keys:
        value = block.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            values.extend(value)
    return "\n".join(values)


def verify_returned_pdf(files: dict[str, bytes], original: bytes) -> dict:
    import fitz
    origins = [raw for name, raw in files.items() if name.endswith("_origin.pdf")]
    if not origins:
        return {"returned_pdf_present": False}
    if len(origins) != 1:
        raise MinerUError("mineru_ambiguous_returned_pdf")
    returned = origins[0]
    verification = {"returned_pdf_present": True, "returned_pdf_sha256": _sha(returned),
                    "returned_pdf_matches_raw_bytes": returned == original}
    if returned != original:
        # MinerU rewrites PDF metadata/serialization. Its origin.pdf is a
        # derivative, never a replacement for the archived source bytes.
        render_hashes = []
        with fitz.open(stream=original, filetype="pdf") as source, fitz.open(stream=returned, filetype="pdf") as other:
            if len(source) != len(other):
                raise MinerUError("mineru_returned_pdf_page_mismatch")
            for left, right in zip(source, other):
                a, b = left.get_pixmap(dpi=72, alpha=False), right.get_pixmap(dpi=72, alpha=False)
                if (a.width, a.height, _sha(a.samples)) != (b.width, b.height, _sha(b.samples)):
                    raise MinerUError("mineru_returned_pdf_render_mismatch")
                render_hashes.append(_sha(a.samples))
        verification.update(render_match=True, render_dpi=72, renderer_version=fitz.VersionBind,
                            page_render_sha256=render_hashes)
    return verification


def normalize_bundle(files: dict[str, bytes], native_pages: list[dict]) -> tuple[dict, bytes]:
    _, md = _one_file(files, lambda name: name == "full.md")
    _, middle_raw = _one_file(files, lambda name: name == "layout.json" or name.endswith("_middle.json"))
    _, list_raw = _one_file(files, lambda name: name.endswith("_content_list.json") or name == "content_list.json")
    try:
        middle, blocks = json.loads(middle_raw), json.loads(list_raw)
        md.decode("utf-8")
    except (ValueError, UnicodeError):
        raise MinerUError("mineru_invalid_output_encoding_or_json") from None
    if not isinstance(middle, dict) or not isinstance(blocks, list):
        raise MinerUError("mineru_unknown_output_schema")
    # Precision v4 currently serves requested vlm with hybrid 3.4.4. Keep
    # requested model and observed backend separate instead of inventing one.
    if middle.get("_backend") not in ("vlm", "hybrid") or not isinstance(middle.get("_version_name"), str):
        raise MinerUError("mineru_backend_or_version_missing")
    infos = middle.get("pdf_info")
    expected = set(range(len(native_pages)))
    if (not isinstance(infos, list) or len(infos) != len(native_pages)
            or any(not isinstance(p, dict) or type(p.get("page_idx")) is not int for p in infos)
            or {p["page_idx"] for p in infos} != expected):
        raise MinerUError("mineru_page_coverage_mismatch")
    by_page = {p["page_idx"]: p for p in infos}
    grouped: dict[int, list[dict]] = {idx: [] for idx in expected}
    for block in blocks:
        if (not isinstance(block, dict) or type(block.get("page_idx")) is not int
                or block["page_idx"] not in expected or not isinstance(block.get("type"), str)):
            raise MinerUError("mineru_invalid_content_page")
        box = block.get("bbox")
        if box is not None and (not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in box)
                or box[0] > box[2] or box[1] > box[3]):
            raise MinerUError("mineru_invalid_content_box")
        grouped[block["page_idx"]].append(block)
    pages = []
    for index, original in enumerate(native_pages):
        info = by_page[index]
        size = info.get("page_size")
        if (not isinstance(size, list) or len(size) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in size)):
            raise MinerUError("mineru_invalid_page_size")
        if any(abs(a - b) > max(2, b * .02) for a, b in zip(size, [original["width_pt"], original["height_pt"]])):
            raise MinerUError("mineru_page_geometry_mismatch")
        page_blocks = grouped[index]
        texts = [_block_text(block) for block in page_blocks]
        flags = ["machine_output_not_manually_verified"]
        if not any(text.strip() for text in texts):
            flags.append("empty_parse_requires_visual_review")
        if any(b["type"] == "table" for b in page_blocks):
            flags.append("table_values_and_structure_require_review")
        if any(b["type"] in ("image", "chart") for b in page_blocks):
            flags.append("visual_regions_require_review")
        if any(not text.strip() and b["type"] not in ("image", "chart") for b, text in zip(page_blocks, texts)):
            flags.append("unrendered_content_block")
        pages.append({**original, "mineru_page_idx": index, "mineru_page_size": size,
            "method": "mineru_precision_vlm", "status": "machine_parse_requires_review",
            "text": "\n\n".join(texts), "blocks": page_blocks,
            "discarded_blocks": info.get("discarded_blocks", []),
            "review_flags": flags, "confidence": None})
    return {"provider": "mineru-precision", "api_version": "v4", "model_version": "vlm",
            "server_version": middle["_version_name"], "server_backend": middle["_backend"],
            "server_effort": middle.get("_effort"), "pages": pages,
            "coordinate_system": "content bbox 0..1000; page_idx zero-based; pdf_page_number one-based",
            "machine_output_is_not_verified_financial_fact": True}, md


@contextmanager
def _job_lock(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "job.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MinerUError("mineru_job_already_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def extract_snapshot_mineru(runtime: Any, snapshot_id: str, client: Any, *,
                            progress: Callable[[dict], None] | None = None,
                            submit_only: bool = False,
                            retry_upload_once: bool = False,
                            sleep: Callable[[float], None] = time.sleep) -> dict:
    snapshot, content, native_pages = verified_pdf(runtime, snapshot_id, client.config)
    parameters = {"raw_sha256": snapshot.sha256, "config": client.config,
                  "input_mode": "verified_local_pdf_upload", "extractor_version": VERSION}
    identity = {"namespace_id": runtime.namespace_id, "snapshot_id": snapshot_id, **parameters}
    data_id = "av-" + _sha(canonical_json_bytes(identity))[:40]
    folder = runtime.data_root / "mineru-jobs" / data_id
    emit = progress or (lambda event: None)
    with _job_lock(folder):
        state_path = folder / "state.json"
        state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {"identity": identity}
        if state.get("identity") != identity:
            raise MinerUError("mineru_cached_job_identity_mismatch")

        def save(status: str, **changes: Any) -> None:
            state.update(changes, status=status)
            raw = canonical_json_bytes(state)
            temp = state_path.with_suffix(".tmp")
            with temp.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, state_path)
            event = {"at": datetime.now(timezone.utc).isoformat(), "snapshot_id": snapshot_id,
                     "data_id": data_id, "state": status, "batch_id": state.get("batch_id"),
                     "route": getattr(client, "route", "test")}
            with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            emit(event)

        artifacts = runtime.repository.list_derived_artifacts(snapshot_id)
        by_id = {a.derived_artifact_id: a for a in artifacts}
        if state.get("status") == "completed":
            for artifact_id in state["result"]["artifact_ids"]:
                if artifact_id not in by_id:
                    raise MinerUError("mineru_cached_artifact_missing")
                read_artifact(runtime, by_id[artifact_id])
            return state["result"]
        bundles = [a for a in artifacts if a.extractor_id == BUNDLE_EXTRACTOR
                   and a.extractor_version == VERSION and a.parameters.get("job_identity") == identity]
        if bundles:
            bundle = bundles[-1]
            zipped = read_artifact(runtime, bundle)
        else:
            if not state.get("batch_id"):
                if state.get("status") == "allocating":
                    raise MinerUError("mineru_submission_uncertain_manual_reconciliation_required")
                save("allocating")
                try:
                    allocation = client.allocate(data_id)
                except MinerUError as exc:
                    if str(exc) in {"mineru_http_400", "mineru_http_401", "mineru_http_403", "mineru_http_429",
                            "mineru_api_A0202", "mineru_api_A0211", "mineru_api_-500", "mineru_api_-10002",
                            "mineru_api_-60001", "mineru_api_-60009", "mineru_api_-60018", "mineru_connection_failed"}:
                        save("allocation_rejected", allocation_error=str(exc))
                    raise
                save("allocated", **allocation)
            if state["status"] == "allocated":
                save("upload_started", upload_attempts=1)
                client.upload(state["upload_url"], content)
                save("uploaded")
            if state["status"] == "upload_started" and retry_upload_once:
                remote = client.poll(state["batch_id"], data_id)
                if remote["state"] == "waiting-file":
                    if state.get("upload_attempts", 1) >= 2:
                        raise MinerUError("mineru_explicit_upload_retry_exhausted")
                    save("upload_started", upload_attempts=2, upload_recovery_verified_state="waiting-file")
                    client.upload(state["upload_url"], content)
                    save("uploaded")
            if submit_only and state["status"] != "upload_started":
                return {"snapshot_id": snapshot_id, "status": "submitted",
                        "batch_id": state["batch_id"], "page_count": len(native_pages)}
            deadline = time.monotonic() + client.config["poll_timeout_seconds"]
            while True:
                remote = client.poll(state["batch_id"], data_id)
                status = remote["state"]
                if status == "failed":
                    save("remote_failed")
                    raise MinerUError("mineru_remote_parse_failed")
                if status == "done":
                    save("remote_done")
                    zipped = client.download(remote["full_zip_url"])
                    break
                if state.get("status") == "upload_started" and status == "waiting-file":
                    raise MinerUError("mineru_upload_uncertain_state_preserved")
                if state.get("status") != status:
                    save(status)
                if time.monotonic() >= deadline:
                    raise MinerUError("mineru_poll_timeout_state_preserved")
                sleep(client.config["poll_interval_seconds"])
            bundle = _freeze(runtime, snapshot_id, BUNDLE_EXTRACTOR,
                {"job_identity": identity, "batch_id": state["batch_id"], "data_id": data_id}, zipped, "ocr")
            save("bundle_frozen", bundle_artifact_id=bundle.derived_artifact_id)
        files = inspect_bundle(zipped, client.config)
        returned_pdf = verify_returned_pdf(files, content)
        layout, markdown = normalize_bundle(files, native_pages)
        layout.update(snapshot_id=snapshot_id, raw_sha256=snapshot.sha256,
                      bundle_artifact_id=bundle.derived_artifact_id, bundle_sha256=bundle.output_sha256,
                      returned_pdf_verification=returned_pdf)
        common = {**parameters, "bundle_artifact_id": bundle.derived_artifact_id,
                  "server_version": layout["server_version"], "batch_id": state["batch_id"]}
        layout_artifact = _freeze(runtime, snapshot_id, LAYOUT_EXTRACTOR, common,
                                  canonical_json_bytes(layout), "ocr")
        text = "MinerU 精准解析机器派生文本；PDF 页码为文件顺序，数字、表格和签章须回看原页。\n\n"
        text += "\n\n".join(f"--- PDF page {p['pdf_page_number']} | mineru_precision_vlm | requires_review ---\n{p['text']}" for p in layout["pages"])
        text_artifact = _freeze(runtime, snapshot_id, TEXT_EXTRACTOR,
            {**common, "layout_artifact_id": layout_artifact.derived_artifact_id}, text.encode("utf-8"), "text")
        md_artifact = _freeze(runtime, snapshot_id, MARKDOWN_EXTRACTOR, common, markdown, "text")
        result = {"snapshot_id": snapshot_id, "raw_sha256": snapshot.sha256,
            "status": "completed_with_machine_parse", "provider": "mineru-precision", "model_version": "vlm",
            "server_version": layout["server_version"], "server_backend": layout["server_backend"],
            "batch_id": state["batch_id"],
            "page_count": len(native_pages), "parsed_pages": list(range(1, len(native_pages)+1)),
            "native_pages_preserved": sum(bool(p["native_text"].strip()) for p in native_pages),
            "review_pages": list(range(1, len(native_pages)+1)),
            "empty_pages": [p["pdf_page_number"] for p in layout["pages"] if not p["text"].strip()],
            "table_count": sum(b["type"] == "table" for p in layout["pages"] for b in p["blocks"]),
            "artifact_ids": [a.derived_artifact_id for a in (bundle, layout_artifact, text_artifact, md_artifact)]}
        for label, artifact in (("bundle", bundle), ("layout", layout_artifact), ("text", text_artifact), ("markdown", md_artifact)):
            result.update({label + "_artifact_id": artifact.derived_artifact_id,
                           label + "_sha256": artifact.output_sha256,
                           label + "_relative_path": artifact.archive_relative_path})
        save("completed", result=result)
        return result
