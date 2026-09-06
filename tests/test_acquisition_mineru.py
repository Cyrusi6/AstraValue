import hashlib
import io
import json
import zipfile

import fitz
import httpx
import pytest

from analysis.acquisition.mineru import extract_snapshot_mineru, inspect_bundle, normalize_bundle, BUNDLE_EXTRACTOR
from analysis.acquisition.mineru_client import MinerUClient, MinerUError, load_config, load_token
from analysis.acquisition.models import SnapshotIntegrityEvent, SnapshotIntegrityStatus
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH
from orchestrator_support import ScenarioAdapter, discovery_result, envelope, resource, targeted_plan, make_runtime


def prepared(tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((60, 60), "Original native page 1")
    doc.new_page()
    doc.new_page()
    body = doc.tobytes()
    doc.close()
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url),
        lambda _, work: discovery_result(work, resources=(resource(),), declared_total=1),
        lambda work: envelope(url=work.resource.url, body=body, content_type="application/pdf"))
    registry = json.loads(INITIAL_REGISTRY_PATH.read_text("utf-8"))
    registry["definitions"][0]["license_policy"].update(save_derived_text="allowed", llm_processing="allowed")
    path = tmp_path / "fixture-registry.json"
    path.write_text(json.dumps(registry), "utf-8")
    runtime = make_runtime(tmp_path / "runtime", adapter, registry_path=path)
    runtime.orchestrator.execute_run(targeted_plan(runtime).run.run_id)
    snapshot = runtime.repository.find_raw_resource_snapshot(resource_role="content",
        canonical_resource_id=resource().canonical_resource_id)
    return runtime, snapshot, body


def output_files():
    return {
        "full.md": b"# Machine output",
        "layout.json": json.dumps({"_backend": "vlm", "_version_name": "fixture-2.5",
            "pdf_info": [{"page_idx": p, "page_size": [595, 842]} for p in range(3)]}).encode(),
        "fixture_content_list.json": json.dumps([
            {"page_idx": 0, "type": "text", "text": "Parsed native page", "bbox": [10, 10, 900, 100]},
            {"page_idx": 1, "type": "table", "table_body": "<table><tr><td>净额</td><td>-12.30</td></tr></table>"},
            {"page_idx": 2, "type": "image", "sub_type": "seal", "image_caption": ["签章"]},
        ], ensure_ascii=False).encode("utf-8"),
    }


def zip_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buffer.getvalue()


class Client:
    config = load_config()
    route = "fixture"

    def __init__(self):
        self.calls = []
        self.files = output_files()
        self.fail_poll = False

    def allocate(self, data_id):
        self.calls.append("allocate")
        self.data_id = data_id
        return {"batch_id": "fixture-batch", "upload_url": "https://mineru.oss-cn-shanghai.aliyuncs.com/test"}

    def upload(self, url, content):
        assert content.startswith(b"%PDF-")
        self.calls.append("upload")

    def poll(self, batch, data_id):
        self.calls.append("poll")
        if self.fail_poll:
            raise MinerUError("mineru_transport_timeout_state_preserved")
        return {"state": "done", "full_zip_url": "https://cdn-mineru.openxlab.org.cn/test.zip"}

    def download(self, url):
        self.calls.append("download")
        return zip_bytes(self.files)


def test_mineru_preserves_original_pages_tables_artifacts_and_resumes(tmp_path):
    runtime, snapshot, raw = prepared(tmp_path)
    client = Client()
    result = extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    layout = json.loads((runtime.data_root / result["layout_relative_path"]).read_bytes())
    assert result["parsed_pages"] == [1, 2, 3]
    assert result["native_pages_preserved"] == 1 and result["table_count"] == 1
    assert layout["pages"][0]["native_text"] == "Original native page 1\n"
    assert layout["pages"][0]["text"] == "Parsed native page"
    assert "-12.30" in layout["pages"][1]["text"]
    assert layout["pages"][2]["confidence"] is None
    assert result["review_pages"] == [1, 2, 3]
    old_artifacts = runtime.repository.list_derived_artifacts(snapshot.snapshot_id)
    old_calls = client.calls[:]
    assert extract_snapshot_mineru(runtime, snapshot.snapshot_id, client) == result
    assert client.calls == old_calls
    assert runtime.repository.list_derived_artifacts(snapshot.snapshot_id) == old_artifacts
    assert runtime.snapshot_bytes(snapshot.snapshot_id) == raw
    assert result["raw_sha256"] == hashlib.sha256(raw).hexdigest()


def test_remote_poll_interruption_does_not_resubmit_or_upload(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    client.fail_poll = True
    with pytest.raises(MinerUError, match="timeout"):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    client.fail_poll = False
    extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert client.calls.count("allocate") == client.calls.count("upload") == 1


def test_submission_response_loss_is_not_automatically_retried(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    def uncertain(data_id):
        client.calls.append("allocate")
        raise MinerUError("mineru_transport_timeout_state_preserved")
    client.allocate = uncertain
    with pytest.raises(MinerUError, match="timeout"):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    with pytest.raises(MinerUError, match="submission_uncertain"):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert client.calls == ["allocate"]


def test_explicit_resume_after_auth_rejection_is_not_an_uncertain_submission(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    original_allocate = client.allocate
    def rejected(data_id):
        raise MinerUError("mineru_api_A0211")
    client.allocate = rejected
    with pytest.raises(MinerUError, match="A0211"):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    client.allocate = original_allocate
    result = extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert result["status"] == "completed_with_machine_parse"
    assert client.calls.count("upload") == 1


def test_interrupted_upload_can_be_explicitly_retried_once_on_same_job(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    client.upload_failed = True
    original_upload = client.upload
    def upload(url, raw):
        original_upload(url, raw)
        if client.upload_failed:
            raise MinerUError("mineru_transport_failed_state_preserved")
    client.upload = upload
    original_poll = client.poll
    client.poll = lambda *args: {"state": "waiting-file"} if client.upload_failed else original_poll(*args)
    with pytest.raises(MinerUError):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    with pytest.raises(MinerUError, match="upload_uncertain"):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    def recovered_upload(url, raw):
        original_upload(url, raw)
        client.upload_failed = False
    client.upload = recovered_upload
    extract_snapshot_mineru(runtime, snapshot.snapshot_id, client, retry_upload_once=True)
    assert client.calls.count("allocate") == 1 and client.calls.count("upload") == 2


def test_returned_pdf_serialization_change_retains_original_and_verifies_render(tmp_path):
    runtime, snapshot, raw = prepared(tmp_path)
    client = Client()
    with fitz.open(stream=raw, filetype="pdf") as doc:
        doc.set_metadata({"producer": "synthetic metadata rewrite"})
        client.files["fixture_origin.pdf"] = doc.tobytes()
    result = extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    layout = json.loads((runtime.data_root / result["layout_relative_path"]).read_bytes())
    check = layout["returned_pdf_verification"]
    assert check["returned_pdf_matches_raw_bytes"] is False and check["render_match"] is True
    assert runtime.snapshot_bytes(snapshot.snapshot_id) == raw


@pytest.mark.parametrize("failure", ["permission", "llm_permission", "quarantine", "raw_hash", "cached_hash"])
def test_mineru_rejects_unusable_evidence_before_network(tmp_path, failure):
    runtime, snapshot, _ = prepared(tmp_path)
    if "permission" in failure:
        get = runtime.repository.get_source_definition_version
        field = "llm_processing" if failure == "llm_permission" else "save_derived_text"
        runtime.repository.get_source_definition_version = lambda *args: get(*args).model_copy(update={
            "license_policy": get(*args).license_policy.model_copy(update={field: "denied"})})
    elif failure == "quarantine":
        runtime.repository.append_snapshot_integrity_event(SnapshotIntegrityEvent(snapshot_id=snapshot.snapshot_id,
            status=SnapshotIntegrityStatus.QUARANTINED, reason_code="fixture"))
    elif failure == "raw_hash":
        (runtime.data_root / snapshot.archive_relative_path).write_bytes(b"damaged")
    else:
        result = extract_snapshot_mineru(runtime, snapshot.snapshot_id, Client())
        (runtime.data_root / result["bundle_relative_path"]).write_bytes(b"damaged")
    client = Client()
    with pytest.raises(Exception):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert not client.calls


@pytest.mark.parametrize("defect", ["missing_page", "duplicate_page", "wrong_page", "box", "wrong_backend"])
def test_missing_pages_and_contract_changes_do_not_publish_text(tmp_path, defect):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    layout = json.loads(client.files["layout.json"])
    blocks = json.loads(client.files["fixture_content_list.json"])
    if defect == "missing_page":
        layout["pdf_info"].pop()
    elif defect == "duplicate_page":
        layout["pdf_info"][2]["page_idx"] = 1
    elif defect == "wrong_page":
        blocks[0]["page_idx"] = 4
    elif defect == "box":
        blocks[0]["bbox"] = [-1, 0, 200, 100]
    else:
        layout["_backend"] = "pipeline"
    client.files["layout.json"] = json.dumps(layout).encode()
    client.files["fixture_content_list.json"] = json.dumps(blocks).encode()
    with pytest.raises(MinerUError):
        extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert {a.extractor_id for a in runtime.repository.list_derived_artifacts(snapshot.snapshot_id)} == {BUNDLE_EXTRACTOR}


def test_empty_page_remains_explicitly_unverified(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    client.files["fixture_content_list.json"] = b"[]"
    result = extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert result["empty_pages"] == [1, 2, 3]
    layout = json.loads((runtime.data_root / result["layout_relative_path"]).read_bytes())
    assert "empty_parse_requires_visual_review" in layout["pages"][0]["review_flags"]


def test_requested_vlm_can_return_observed_hybrid_backend(tmp_path):
    runtime, snapshot, _ = prepared(tmp_path)
    client = Client()
    middle = json.loads(client.files["layout.json"])
    middle.update(_backend="hybrid", _version_name="3.4.4", _effort="medium")
    client.files["layout.json"] = json.dumps(middle).encode()
    result = extract_snapshot_mineru(runtime, snapshot.snapshot_id, client)
    assert result["model_version"] == "vlm" and result["server_backend"] == "hybrid"


@pytest.mark.parametrize("name", ["../evil.json", "/evil.json", "C:/evil.json", "a\\evil.json"])
def test_unsafe_zip_paths_are_rejected(name):
    raw = zip_bytes({name: b"{}"})
    if "\\" in name:
        raw = raw.replace(name.replace("\\", "/").encode(), name.encode())
    with pytest.raises(MinerUError, match="unsafe_zip"):
        inspect_bundle(raw, load_config())


def test_client_uses_precision_vlm_and_never_sends_token_to_storage():
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.host == "mineru.net":
            assert request.headers["authorization"] == "Bearer test-secret"
            payload = json.loads(request.content)
            assert payload["model_version"] == "vlm"
            assert payload["files"][0]["is_ocr"] is True
            return httpx.Response(200, json={"code": 0, "data": {"batch_id": "batch-1",
                "file_urls": ["https://mineru.oss-cn-shanghai.aliyuncs.com/file?signature=secret"]}})
        assert "authorization" not in request.headers
        assert "content-type" not in request.headers
        return httpx.Response(200, content=b"ok")
    with MinerUClient("test-secret", load_config(), transport=httpx.MockTransport(handle)) as client:
        allocated = client.allocate("test-id")
        client.upload(allocated["upload_url"], b"PDF")
        client.download("https://cdn-mineru.openxlab.org.cn/result.zip")
        with pytest.raises(MinerUError, match="unapproved"):
            client.download("https://evil.example/result.zip")
    assert len(requests) == 3


@pytest.mark.parametrize("status", [401, 403, 429, 302, 500])
def test_http_failures_do_not_retry_or_leak_tokens(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.example"}, content=b"test-secret")
    with MinerUClient("test-secret", load_config(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(MinerUError) as error:
            client.allocate("test-id")
    assert "test-secret" not in str(error.value) and len(calls) == 1


def test_token_file_parsing_does_not_execute_or_interpolate(tmp_path, monkeypatch):
    monkeypatch.delenv("MINERU_API", raising=False)
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    path = tmp_path / ".env.local"
    path.write_text('IGNORED=$(not-executed)\nMINERU_API="Bearer example.token"\n', "utf-8")
    assert load_token(path) == "example.token"


def test_legacy_native_extraction_no_longer_invokes_local_ocr(tmp_path, monkeypatch):
    from analysis.documents import _extract_text
    doc = fitz.open()
    doc.new_page()
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    monkeypatch.setattr(fitz.Page, "get_textpage_ocr", lambda *a, **k: pytest.fail("local OCR must be removed"))
    _, pages, ocr_used, warnings = _extract_text(path)
    assert pages == 1 and not ocr_used and any("MinerU" in w for w in warnings)
