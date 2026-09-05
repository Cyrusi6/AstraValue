import hashlib
import json

import fitz
import pytest

from analysis.acquisition.models import SnapshotIntegrityEvent, SnapshotIntegrityStatus
from analysis.acquisition.ocr import extract_snapshot_ocr, PAGE_EXTRACTOR
from test_acquisition_html_content import _runtime
from orchestrator_support import ScenarioAdapter, discovery_result, envelope, resource, targeted_plan


class Worker:
    config = {"dpi": 200, "low_confidence_threshold": .9}
    provenance = {"test_engine": "synthetic", "config": config}

    def __init__(self, fail_on=None):
        self.calls = 0
        self.fail_on = fail_on

    def recognize(self, path):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("fixture interrupted")
        pix = fitz.Pixmap(str(path))
        return {"width": pix.width, "height": pix.height, "lines": [
            {"text": "净额 -12.30", "confidence": .98, "box_px": [[10,10],[100,10],[100,30],[10,30]]},
            {"text": "签章", "confidence": .4, "box_px": [[10,40],[100,40],[100,60],[10,60]]},
        ]}


def prepared(tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((60,60), "Original native page 1")
    doc.new_page()
    doc.new_page()
    body = doc.tobytes()
    doc.close()
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url),
        lambda _, work: discovery_result(work, resources=(resource(),), declared_total=1),
        lambda work: envelope(url=work.resource.url, body=body, content_type="application/pdf"))
    runtime = _runtime(tmp_path, adapter)
    plan = targeted_plan(runtime)
    runtime.orchestrator.execute_run(plan.run.run_id)
    snapshot = runtime.repository.find_raw_resource_snapshot(resource_role="content", canonical_resource_id=resource().canonical_resource_id)
    return runtime, snapshot, body


def test_ocr_preserves_raw_native_text_page_numbers_layout_and_resume(tmp_path):
    runtime, snapshot, body = prepared(tmp_path)
    interrupted = Worker(fail_on=2)
    with pytest.raises(RuntimeError, match="interrupted"):
        extract_snapshot_ocr(runtime, snapshot.snapshot_id, interrupted)
    frozen = runtime.repository.list_derived_artifacts(snapshot.snapshot_id)
    assert len(frozen) == 1 and frozen[0].extractor_id == PAGE_EXTRACTOR
    worker = Worker()
    result = extract_snapshot_ocr(runtime, snapshot.snapshot_id, worker)
    assert worker.calls == 1
    assert result["ocr_pages"] == [2,3] and result["review_pages"] == [2,3]
    layout = json.loads((runtime.data_root / result["layout_relative_path"]).read_bytes())
    assert [p["pdf_page_number"] for p in layout["pages"]] == [1,2,3]
    assert layout["pages"][0]["native_text"] == layout["pages"][0]["text"] == "Original native page 1\n"
    assert layout["pages"][1]["lines"][0]["text"] == "净额 -12.30"
    assert layout["pages"][1]["lines"][0]["box_pt"][0][0] == pytest.approx(10*595/1653)
    assert runtime.snapshot_bytes(snapshot.snapshot_id) == body
    all_before = runtime.repository.list_derived_artifacts(snapshot.snapshot_id)
    assert extract_snapshot_ocr(runtime, snapshot.snapshot_id, worker) == result
    assert worker.calls == 1
    assert runtime.repository.list_derived_artifacts(snapshot.snapshot_id) == all_before
    assert frozen[0] in all_before
    assert hashlib.sha256(body).hexdigest() == result["raw_sha256"]


@pytest.mark.parametrize("failure", ["permission", "quarantine", "raw_hash", "cached_hash"])
def test_ocr_rejects_unusable_evidence_before_worker_calls(tmp_path, failure):
    runtime, snapshot, _ = prepared(tmp_path)
    if failure == "permission":
        get = runtime.repository.get_source_definition_version
        runtime.repository.get_source_definition_version = lambda *args: get(*args).model_copy(update={
            "license_policy": get(*args).license_policy.model_copy(update={"save_derived_text": "denied"})})
    elif failure == "quarantine":
        runtime.repository.append_snapshot_integrity_event(SnapshotIntegrityEvent(snapshot_id=snapshot.snapshot_id,
            status=SnapshotIntegrityStatus.QUARANTINED, reason_code="fixture"))
    elif failure == "raw_hash":
        (runtime.data_root / snapshot.archive_relative_path).write_bytes(b"damaged")
    else:
        extract_snapshot_ocr(runtime, snapshot.snapshot_id, Worker())
        artifact = next(a for a in runtime.repository.list_derived_artifacts(snapshot.snapshot_id) if a.extractor_id == PAGE_EXTRACTOR)
        (runtime.data_root / artifact.archive_relative_path).write_bytes(b"damaged")
    worker = Worker()
    with pytest.raises(Exception):
        extract_snapshot_ocr(runtime, snapshot.snapshot_id, worker)
    assert worker.calls == 0


def test_zero_ocr_is_review_required_and_not_claimed_blank(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    class Empty(Worker):
        def recognize(self, path):
            result = super().recognize(path)
            result["lines"] = []
            return result
    result = extract_snapshot_ocr(runtime, snapshot.snapshot_id, Empty())
    layout = json.loads((runtime.data_root / result["layout_relative_path"]).read_bytes())
    assert layout["pages"][1]["review_flags"] == ["empty_ocr_requires_visual_review"]
    assert result["review_pages"] == [2,3]
