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
    INITIAL_REGISTRY_PATH,
    POLICY_APPROVED_REGISTRY_PATH,
    REVIEWED_REGISTRY_PATH,
    SourceRegistryError,
    SourceRegistryLoader,
)
from analysis.acquisition.runtime import AcquisitionRuntime


INITIAL_REGISTRY_CANONICAL_SHA256 = (
    "51e8af5e394b8ca82a073bf7fdf4de19ee03fcf07c47dbd0f813e3a74856c084"
)
REVIEWED_REGISTRY_CANONICAL_SHA256 = (
    "0ed28a38172c96dae1c6dba0b4ff4d7134c10731d8ea170b33f7254a163fb696"
)
POLICY_APPROVED_REGISTRY_CANONICAL_SHA256 = (
    "c1cf7ecaa627bee3e1b34c97276181c2c78dc06914c3b8e0b42cca0ad17eaa0a"
)


def _payload() -> dict:
    return json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def _initial_payload() -> dict:
    return json.loads(INITIAL_REGISTRY_PATH.read_text(encoding="utf-8"))


def _reviewed_payload() -> dict:
    return json.loads(REVIEWED_REGISTRY_PATH.read_text(encoding="utf-8"))


def _policy_approved_payload() -> dict:
    return json.loads(POLICY_APPROVED_REGISTRY_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _complete_live_review(definition: dict, *, status: str) -> None:
    definition["live_access_review"].update(
        {
            "status": status,
            "completed_checks": [item.value for item in LiveAccessReviewCheck],
            "reviewed_at": "2026-09-03T01:00:00Z",
            "reviewed_by": "fixture-reviewer",
            "evidence_reference": "fixture:policy-review",
        }
    )


def _approved_moutai_ir_payload() -> dict:
    payload = _policy_approved_payload()
    payload["registry_version"] = "1.2.0"
    ir = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "moutai.ir"
    )
    ir["version"] = "1.2.0"
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
    _complete_live_review(ir, status="approved")
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


def test_registry_redirect_and_initial_allowlist_are_checked_per_hop(tmp_path):
    payload = _initial_payload()
    payload["registry_version"] = "1.0.1"
    cninfo_payload = payload["definitions"][0]
    cninfo_payload["version"] = "1.0.1"
    _complete_live_review(cninfo_payload, status="approved")
    loaded = SourceRegistryLoader().load_registry(_write(tmp_path, payload))
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


def test_registry_request_url_requires_approved_live_review():
    cninfo = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH).definition(
        "cninfo.disclosures"
    )
    with pytest.raises(SourceRegistryError, match="未获live access人工批准"):
        SourceRegistryLoader.validate_request_url(
            cninfo,
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
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
    payload = _initial_payload()
    payload["definitions"][0]["license_policy"]["automated_access"] = "pending"
    with pytest.raises(SourceRegistryError, match="允许自动访问"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_pending_policy_source_cannot_retain_network_authority(tmp_path):
    payload = _reviewed_payload()
    definition = payload["definitions"][0]
    definition["live_access_review"].update(
        {
            "status": "pending",
            "completed_checks": [],
            "reviewed_at": None,
            "reviewed_by": None,
            "evidence_reference": None,
        }
    )
    definition["initial_request_allowlist"] = [
        {
            "scheme": "https",
            "host": "www.cninfo.com.cn",
            "port": 443,
            "path_prefix": "/new/hisAnnouncement/query",
        }
    ]
    with pytest.raises(SourceRegistryError, match="不得预置可联网allowlist"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_v1_1_records_rejected_technical_review_without_network_authority():
    loaded = SourceRegistryLoader().load_registry(REVIEWED_REGISTRY_PATH)
    statuses = {
        source_id: loaded.definition(source_id).live_access_review.status.value
        for source_id in (
            "cninfo.disclosures",
            "sse.disclosures",
            "szse.disclosures",
            "moutai.ir",
        )
    }
    assert statuses == {source_id: "rejected" for source_id in statuses}
    assert loaded.registry.registry_version == "1.1.0"
    assert {
        loaded.definition(source_id).version for source_id in statuses
    } == {"1.1.0"}
    assert all(
        loaded.definition(source_id).policy_status.value == "pending_policy"
        and loaded.definition(source_id).enabled is False
        and loaded.definition(source_id).access_method == "disabled"
        for source_id in statuses
    )
    for source_id in statuses:
        definition = loaded.definition(source_id)
        assert definition.initial_request_allowlist == ()
        assert definition.redirect_allowlist == ()
        assert all(query.endpoint is None for query in definition.queries)
        assert set(definition.live_access_review.completed_checks) == set(
            LiveAccessReviewCheck
        )
        assert definition.live_access_review.reviewed_at is not None
        assert definition.live_access_review.reviewed_by
        assert definition.live_access_review.evidence_reference


def test_review_registry_preserves_unchanged_legacy_definition_versions():
    initial = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH)
    reviewed = SourceRegistryLoader().load_registry(REVIEWED_REGISTRY_PATH)
    initial_legacy = {
        definition.source_definition_id: definition.model_dump(mode="json")
        for definition in initial.registry.legacy_definitions
    }
    reviewed_legacy = {
        definition.source_definition_id: definition.model_dump(mode="json")
        for definition in reviewed.registry.legacy_definitions
    }
    assert reviewed_legacy == initial_legacy


def test_initial_registry_and_definitions_remain_immutable_pending_versions():
    loaded = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH)
    assert loaded.registry.registry_version == "1.0.0"
    assert loaded.content_hash == INITIAL_REGISTRY_CANONICAL_SHA256
    for source_id in (
        "cninfo.disclosures",
        "sse.disclosures",
        "szse.disclosures",
        "moutai.ir",
    ):
        definition = loaded.definition(source_id)
        assert definition.version == "1.0.0"
        assert definition.live_access_review.status.value == "pending"


def test_reviewed_registry_hash_and_definitions_remain_immutable():
    loaded = SourceRegistryLoader().load_registry(REVIEWED_REGISTRY_PATH)
    assert loaded.registry.registry_version == "1.1.0"
    assert loaded.content_hash == REVIEWED_REGISTRY_CANONICAL_SHA256
    assert {
        definition.version for definition in loaded.registry.definitions
    } == {"1.1.0"}


def test_v1_2_registry_hash_and_internal_approval_remain_immutable():
    loaded = SourceRegistryLoader().load_registry(POLICY_APPROVED_REGISTRY_PATH)
    assert loaded.registry.registry_version == "1.2.0"
    assert loaded.content_hash == POLICY_APPROVED_REGISTRY_CANONICAL_SHA256
    approved_ids = {
        definition.source_definition_id
        for definition in loaded.registry.definitions
        if definition.live_access_review.status.value == "approved"
    }
    assert approved_ids == {
        "cninfo.disclosures",
        "sse.disclosures",
        "szse.disclosures",
    }
    for source_id in approved_ids:
        definition = loaded.definition(source_id)
        assert definition.version == "1.2.0"
        assert definition.enabled is True
        assert definition.policy_status.value == "enabled"
        assert definition.license_policy.automated_access.value == "allowed"
        assert definition.license_policy.archive_original.value == "allowed"
        assert definition.license_policy.save_derived_text.value == "allowed"
        assert definition.license_policy.llm_processing.value == "allowed"
    ir = loaded.definition("moutai.ir")
    assert ir.version == "1.2.0"
    assert ir.live_access_review.status.value == "rejected"
    assert ir.policy_status.value == "pending_policy"
    assert ir.enabled is False
    assert ir.initial_request_allowlist == ()
    assert all(query.endpoint is None for query in ir.queries)


def test_default_v1_3_changes_only_the_observed_sse_schema_contract():
    previous = SourceRegistryLoader().load_registry(POLICY_APPROVED_REGISTRY_PATH)
    current = SourceRegistryLoader().load_registry()

    assert current.registry.registry_version == "1.3.0"
    for source_id in ("cninfo.disclosures", "szse.disclosures", "moutai.ir"):
        assert current.definition(source_id) == previous.definition(source_id)

    old_sse = previous.definition("sse.disclosures")
    sse = current.definition("sse.disclosures")
    query = sse.queries[0]
    assert sse.version == "1.3.0"
    assert sse.initial_request_allowlist == old_sse.initial_request_allowlist
    assert sse.redirect_allowlist == old_sse.redirect_allowlist
    assert sse.rate_limit == old_sse.rate_limit
    assert sse.retry_policy == old_sse.retry_policy
    assert sse.license_policy == old_sse.license_policy
    assert sse.incremental_policy.checkpoint_compatible_from_versions == ()
    assert query.execution_key.endswith(".v1.3")
    assert query.discovery_schema.schema_version == "2"
    assert query.discovery_schema.resource_fields["title"] == (
        "pageHelp.data[].title"
    )


def test_pre_v1_2_post_queries_keep_legacy_form_encoding():
    loaded = SourceRegistryLoader().load_registry(INITIAL_REGISTRY_PATH)
    cninfo = loaded.definition("cninfo.disclosures")
    post_query = next(
        query for query in cninfo.queries if query.request_method == "POST"
    )
    assert post_query.request_encoding == "form"
    sse = loaded.definition("sse.disclosures")
    get_query = next(query for query in sse.queries if query.request_method == "GET")
    assert get_query.request_encoding == "query"


def test_v1_2_request_contracts_are_explicit_and_source_specific():
    loaded = SourceRegistryLoader().load_registry(POLICY_APPROVED_REGISTRY_PATH)
    cninfo = loaded.definition("cninfo.disclosures")
    bootstrap = next(
        query for query in cninfo.queries if query.query_id == "cninfo.company_bootstrap"
    )
    periodic = next(
        query for query in cninfo.queries if query.query_id == "cninfo.periodic_report"
    )
    assert bootstrap.request_method == "GET"
    assert bootstrap.request_encoding == "query"
    assert periodic.request_method == "POST"
    assert periodic.request_encoding == "form"
    assert periodic.pagination.total_path == "totalAnnouncement"
    assert periodic.parameter_bindings["stock"].source_query_id == (
        "cninfo.company_bootstrap"
    )

    sse = loaded.definition("sse.disclosures").queries[0]
    assert sse.endpoint.endswith("/queryCompanyStatementNew.do")
    assert sse.request_encoding == "query"
    assert sse.parameter_template["reportType2"] == "DQBG"

    szse = loaded.definition("szse.disclosures")
    assert {query.request_encoding for query in szse.queries} == {"json"}
    assert all(
        isinstance(query.parameter_template["stock"], list)
        for query in szse.queries
    )


def test_v1_2_fixed_headers_reject_credentials_before_network(tmp_path):
    payload = _payload()
    payload["definitions"][0]["queries"][0]["fixed_headers"][
        "Authorization"
    ] = "Bearer fixture-secret"
    with pytest.raises(SourceRegistryError, match="fixed_headers"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_parameter_binding_can_only_reference_an_earlier_query(tmp_path):
    payload = _payload()
    periodic = payload["definitions"][0]["queries"][1]
    periodic["parameter_bindings"]["stock"]["source_query_id"] = (
        "cninfo.periodic_report"
    )
    with pytest.raises(SourceRegistryError, match="此前执行"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_live_access_approval_requires_complete_9_1_checklist_and_signoff(tmp_path):
    payload = _payload()
    review = payload["definitions"][0]["live_access_review"]
    review.update(
        {
            "status": "approved",
            "completed_checks": [],
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


def test_live_access_rejection_requires_complete_checklist_and_signoff(tmp_path):
    payload = _reviewed_payload()
    review = payload["definitions"][0]["live_access_review"]
    review["reviewed_by"] = None
    with pytest.raises(SourceRegistryError, match="拒绝必须记录复核人"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


def test_live_access_rejection_cannot_retain_network_authority(tmp_path):
    payload = _initial_payload()
    definition = payload["definitions"][0]
    _complete_live_review(definition, status="rejected")
    with pytest.raises(SourceRegistryError, match="live access拒绝来源"):
        SourceRegistryLoader().load_registry(_write(tmp_path, payload))


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
        assert ir.version == "1.2.0"
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
