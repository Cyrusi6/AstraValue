import hashlib
import json
from dataclasses import replace

import pytest

from analysis.acquisition.content import extract_announcement_text, html_text
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH
from orchestrator_support import ScenarioAdapter, discovery_result, envelope, make_runtime, resource, targeted_plan
from test_acquisition_cninfo_adapter import _nullable_work, _parse_payload


def _html(encoding="gb2312"):
    return (f'<html><head><meta charset="{encoding}"><title>样本公司2001年年度报告摘要</title></head>'
            '<body><h1>样本公司2001年年度报告摘要</h1><script>ignore_script()</script>'
            '<p>营业收入 &amp; 净利润</p><table><tr><td>收入</td><td>123</td></tr></table>'
            '<p>' + '这是公开公告的摘要正文。' * 30 + '</p></body></html>').encode(encoding)


def _runtime(root, adapter):
    payload = json.loads(INITIAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["definitions"][0]["license_policy"]["save_derived_text"] = "allowed"
    path = root / "fixture-registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return make_runtime(root / "runtime", adapter, registry_path=path)


@pytest.mark.parametrize("schema,expected", [("2", "application/pdf"), ("3", "text/html")])
def test_html_attachment_contract_is_frozen_by_discovery_schema(schema, expected):
    row = {"announcementId": "test-html", "announcementTitle": "年度报告摘要",
           "announcementTime": 1000000000000, "adjunctUrl": "finalpage/2002/summary.html"}
    result = _parse_payload({"announcements": [row], "totalAnnouncement": 1},
                            _nullable_work(parser_schema_version=schema))
    assert result.resources[0].metadata["expected_mime_types"] == (expected,)


@pytest.mark.parametrize("encoding", ["utf-8", "gb2312", "gbk"])
def test_chinese_html_is_archived_verified_then_derived(tmp_path, encoding):
    body = _html(encoding)
    html_resource = replace(resource(url="https://static.cninfo.com.cn/finalpage/summary.html"),
                            metadata={"expected_mime_types": ("text/html",)})
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url),
        lambda _, work: discovery_result(work, resources=(html_resource,), declared_total=1),
        lambda work: envelope(url=work.resource.url, body=body, content_type="text/html"))
    runtime = _runtime(tmp_path, adapter)
    plan = targeted_plan(runtime)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.outcome_counts == {"success": 2}
    snapshot = runtime.repository.find_raw_resource_snapshot(resource_role="content",
        source_definition_id="cninfo.disclosures", canonical_resource_id=html_resource.canonical_resource_id)
    assert runtime.snapshot_bytes(snapshot.snapshot_id) == body
    assert snapshot.sha256 == hashlib.sha256(body).hexdigest()
    derived = extract_announcement_text(runtime, snapshot.snapshot_id)
    artifact = runtime.repository.list_derived_artifacts(snapshot.snapshot_id)[0]
    text_bytes = (runtime.data_root / artifact.archive_relative_path).read_bytes()
    text = text_bytes.decode("utf-8")
    assert "2001年年度报告摘要" in text and "营业收入 & 净利润" in text
    assert "ignore_script" not in text and "\ufffd" not in text
    assert hashlib.sha256(text_bytes).hexdigest() == derived.sha256 == artifact.output_sha256
    assert derived.page_count == 1 and derived.empty_page_count == 0
    assert extract_announcement_text(runtime, snapshot.snapshot_id) == derived
    from analysis.acquisition.materials import classify_snapshot_material
    classification = classify_snapshot_material(runtime, snapshot.snapshot_id, title="样本公司2001年年度报告摘要")
    assert classification.material_type == "periodic_summary"
    assert classify_snapshot_material(runtime, snapshot.snapshot_id, title="样本公司2001年年度报告摘要") == classification
    runtime.close()


@pytest.mark.parametrize("body,headers,outcome,reason", [
    (_html(), {"x-tengine-error": "denied by bot"}, "restricted", "upstream_bot_challenge"),
    (b'<html><body>captcha</body></html>', {}, "restricted", "challenge_page_detected"),
    (b'<html><head><title>404 Not Found</title></head><body>' + b'error ' * 80 + b'</body></html>', {}, "parse_failed", "html_error_page"),
    (_html()[:-17], {}, "parse_failed", "incomplete_html_document"),
], ids=["challenge_header", "captcha", "ordinary_error", "truncated"])
def test_html_permission_does_not_admit_challenges_or_error_pages(tmp_path, body, headers, outcome, reason):
    html_resource = replace(resource(url="https://static.cninfo.com.cn/finalpage/summary.html"),
                            metadata={"expected_mime_types": ("text/html",)})
    adapter = ScenarioAdapter(lambda work: envelope(url=work.url),
        lambda _, work: discovery_result(work, resources=(html_resource,), declared_total=1),
        lambda work: envelope(url=work.resource.url, body=body, content_type="text/html", headers=headers))
    runtime = make_runtime(tmp_path, adapter)
    plan = targeted_plan(runtime)
    result = runtime.orchestrator.execute_run(plan.run.run_id)
    assert result.outcome_counts == {"success": 1, outcome: 1}
    attempts = runtime.repository.list_attempts(run_id=plan.run.run_id, limit=None)
    events = runtime.repository.list_attempt_events(attempts[-1].attempt_id)
    assert events[-1].reason_code == reason
    assert runtime.repository.find_raw_resource_snapshot(resource_role="content",
        source_definition_id="cninfo.disclosures", canonical_resource_id=html_resource.canonical_resource_id) is None
    runtime.close()


def test_explicit_invalid_chinese_encoding_is_not_silently_replaced():
    with pytest.raises(UnicodeDecodeError):
        html_text(b'<html><meta charset="utf-8"><body>\xff</body></html>')


def test_long_html_with_invalid_tail_is_rejected_before_archiving():
    from analysis.acquisition.content import validate_cninfo_html_response
    body = b'<html><head><meta charset="utf-8"></head><body>' + b'a'*140000 + b'\xff</body></html>'
    with pytest.raises(UnicodeDecodeError):
        validate_cninfo_html_response(body)


def test_html_head_metadata_cannot_confirm_a_conflicting_body_type():
    from analysis.acquisition.materials import classify_material
    body = '<html><head><title>招股说明书</title></head><body><h1>上市公告书</h1></body></html>'.encode()
    text, _ = html_text(body)
    result = classify_material("招股说明书", text=text)
    assert result.title_type == "prospectus" and result.content_type == "listing_announcement"
    assert result.evidence_status == "requires_review"
