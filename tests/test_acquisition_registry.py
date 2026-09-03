from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from analysis.acquisition.models import LiveAccessReviewCheck, SourceRegistry
from analysis.acquisition.registry import (
    DEFAULT_QUESTIONS_PATH,
    DEFAULT_REGISTRY_PATH,
    SourceRegistryError,
    SourceRegistryLoader,
)
from analysis.acquisition.runtime import AcquisitionRuntime


def _payload() -> dict:
    return json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _approved_moutai_ir_payload() -> dict:
    payload = _payload()
    payload["registry_version"] = "1.1.0"
    ir = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "moutai.ir"
    )
    ir["version"] = "1.1.0"
    ir["policy_status"] = "enabled"
    ir["enabled"] = True
    ir["access_method"] = "https_api"
    ir["initial_request_allowlist"] = [
        {
            "scheme": "https",
            "host": "ir.example.test",
            "port": 443,
            "path_prefix": "/public/",
        }
    ]
    ir["redirect_allowlist"] = list(ir["initial_request_allowlist"])
    ir["retention_policy"]["content_body"] = "allowed"
    ir["license_policy"].update(
        {
            "automated_access": "allowed",
            "archive_original": "allowed",
            "save_derived_text": "allowed",
            "llm_processing": "allowed",
            "checked_at": "2026-09-03T01:00:00Z",
        }
    )
    ir["live_access_review"].update(
        {
            "status": "approved",
            "completed_checks": [item.value for item in LiveAccessReviewCheck],
            "reviewed_at": "2026-09-03T01:00:00Z",
            "reviewed_by": "fixture-reviewer",
            "evidence_reference": "fixture:policy-review",
        }
    )
    ir["queries"][0]["endpoint"] = "https://ir.example.test/public/list"
    return payload


def test_registry_hash_is_stable_for_semantically_identical_key_order(tmp_path):
    loader = SourceRegistryLoader()
    payload = _payload()
    first = loader.load_registry(_write(tmp_path, payload))
    reversed_payload = dict(reversed(list(payload.items())))
    second_path = tmp_path / "registry-reordered.json"
    second_path.write_text(
        json.dumps(reversed_payload, ensure_ascii=False), encoding="utf-8"
    )
    second = loader.load_registry(second_path)
    assert first.content_hash == second.content_hash
    assert first.canonical_json == second.canonical_json


def test_registry_schema_missing_required_field_fails_before_network(tmp_path):
    payload = _payload()
    del payload["definitions"][0]["upstream_identity"]
    with pytest.raises(SourceRegistryError, match="schema校验失败"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_registry_schema_validator_script_rejects_missing_topic_mapping(tmp_path):
    questions = json.loads(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions["topics"][0]["query_families"] = []
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validate_source_registry.py",
            "--questions",
            str(path),
            "--require-plan-traceability",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "SOURCE_REGISTRY_INVALID" in completed.stderr


def test_registry_redirect_and_initial_allowlist_are_checked_per_hop():
    loaded = SourceRegistryLoader().load_registry()
    cninfo = loaded.definition("cninfo.disclosures")
    SourceRegistryLoader.validate_request_url(
        cninfo,
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
    )
    SourceRegistryLoader.validate_request_url(
        cninfo,
        "https://static.cninfo.com.cn/finalpage/report.pdf",
        redirect=True,
    )
    with pytest.raises(SourceRegistryError, match="allowlist"):
        SourceRegistryLoader.validate_request_url(
            cninfo,
            "https://evil.example/report.pdf",
            redirect=True,
        )


def test_registry_time_semantics_reject_unknown_timezone(tmp_path):
    payload = _payload()
    payload["definitions"][0]["source_timezone"] = "Mars/Olympus"
    with pytest.raises(SourceRegistryError, match="未知来源时区"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_registry_response_limit_must_be_positive_and_ordered(tmp_path):
    payload = _payload()
    payload["definitions"][0]["response_limits"]["max_decompressed_bytes"] = 1
    with pytest.raises(SourceRegistryError, match="上限"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_registry_rate_limit_business_model_concurrency_is_fixed_to_one(tmp_path):
    payload = _payload()
    payload["definitions"][0]["rate_limit"]["max_concurrency"] = 2
    with pytest.raises(SourceRegistryError, match="并发固定为1"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_registry_license_policy_fails_closed_for_enabled_source(tmp_path):
    payload = _payload()
    payload["definitions"][0]["license_policy"]["automated_access"] = "pending"
    with pytest.raises(SourceRegistryError, match="允许自动访问"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_v1_live_access_review_is_machine_readable_and_pending_before_task_9_1():
    loaded = SourceRegistryLoader().load_registry()
    statuses = {
        source_id: loaded.definition(source_id).live_access_review.status.value
        for source_id in (
            "cninfo.disclosures",
            "sse.disclosures",
            "szse.disclosures",
            "moutai.ir",
        )
    }
    assert statuses == {source_id: "pending" for source_id in statuses}


def test_live_access_approval_requires_complete_9_1_checklist_and_signoff(tmp_path):
    payload = _payload()
    review = payload["definitions"][0]["live_access_review"]
    review.update(
        {
            "status": "approved",
            "reviewed_at": "2026-09-03T01:00:00Z",
            "reviewed_by": "fixture-reviewer",
            "evidence_reference": "fixture:policy-review",
        }
    )
    with pytest.raises(SourceRegistryError, match="批准缺少人工审核检查项"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))

    review["completed_checks"] = [item.value for item in LiveAccessReviewCheck]
    loaded = SourceRegistryLoader().load_registry(_write(tmp_path, payload))
    assert loaded.definition("cninfo.disclosures").live_access_review.status.value == (
        "approved"
    )


@pytest.mark.parametrize("initial_identity", ["registry", "definition"])
def test_business_model_v1_initial_ir_contract_cannot_be_enabled_in_place(
    tmp_path,
    initial_identity,
):
    payload = _approved_moutai_ir_payload()
    if initial_identity == "registry":
        payload["registry_version"] = "1.0.0"
    else:
        ir = next(
            item
            for item in payload["definitions"]
            if item["source_definition_id"] == "moutai.ir"
        )
        ir["version"] = "1.0.0"

    with pytest.raises(SourceRegistryError, match="初始.*pending_policy/disabled"):
        SourceRegistryLoader().load_registry(
            _write(tmp_path, payload),
            expect_business_model_v1=4,
        )


def test_business_model_v1_enabled_ir_requires_approved_live_review(tmp_path):
    payload = _approved_moutai_ir_payload()
    ir = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "moutai.ir"
    )
    ir["live_access_review"].update(
        {
            "status": "pending",
            "completed_checks": [],
            "reviewed_at": None,
            "reviewed_by": None,
            "evidence_reference": None,
        }
    )

    with pytest.raises(SourceRegistryError, match="必须完成live access人工审核"):
        SourceRegistryLoader().load_registry(
            _write(tmp_path, payload),
            expect_business_model_v1=4,
        )


def test_business_model_v1_enabled_ir_requires_resolved_license_decisions(tmp_path):
    payload = _approved_moutai_ir_payload()
    ir = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "moutai.ir"
    )
    ir["license_policy"]["llm_processing"] = "pending"

    with pytest.raises(SourceRegistryError, match="许可与LLM决策不得pending"):
        SourceRegistryLoader().load_registry(
            _write(tmp_path, payload),
            expect_business_model_v1=4,
        )


def test_business_model_v1_enabled_ir_requires_dedicated_adapter(tmp_path):
    payload = _approved_moutai_ir_payload()
    ir = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "moutai.ir"
    )
    ir["adapter_key"] = "cninfo"

    with pytest.raises(SourceRegistryError, match="moutai.ir必须使用moutai_ir"):
        SourceRegistryLoader().load_registry(
            _write(tmp_path, payload),
            expect_business_model_v1=4,
        )


@pytest.mark.parametrize("missing_contract", ["allowlist", "endpoint"])
def test_business_model_v1_enabled_ir_requires_fixed_network_contract(
    tmp_path,
    missing_contract,
):
    payload = _approved_moutai_ir_payload()
    ir = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "moutai.ir"
    )
    if missing_contract == "allowlist":
        ir["initial_request_allowlist"] = []
    else:
        ir["queries"][0]["endpoint"] = None

    with pytest.raises(SourceRegistryError, match="schema校验失败"):
        SourceRegistryLoader().load_registry(
            _write(tmp_path, payload),
            expect_business_model_v1=4,
        )


def test_formal_runtime_loads_approved_ir_from_new_registry_version(tmp_path):
    registry_path = _write(tmp_path, _approved_moutai_ir_payload())

    with AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "data",
        registry_path=registry_path,
        orchestrator_factory=lambda _runtime: None,
    ) as runtime:
        ir = runtime.source_definition("moutai.ir")
        assert ir.version == "1.1.0"
        assert ir.enabled is True
        assert ir.live_access_review.status.value == "approved"


def test_registry_unknown_adapter_fails_before_resolution(tmp_path):
    payload = _payload()
    payload["definitions"][0]["adapter_key"] = "arbitrary_browser"
    with pytest.raises(SourceRegistryError, match="未知adapter_key"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_registry_models_are_immutable_versions():
    registry = SourceRegistryLoader().load_registry().registry
    assert isinstance(registry, SourceRegistry)
    with pytest.raises(Exception):
        registry.registry_version = "2.0.0"
