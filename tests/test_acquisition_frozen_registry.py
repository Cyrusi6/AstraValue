from __future__ import annotations

import json

import pytest

from analysis.acquisition.models import (
    AcquisitionPlan,
    AcquisitionRun,
    SourceDefinitionRef,
    canonical_json_sha256,
)
from analysis.acquisition.orchestrator import (
    AcquisitionExecutionError,
    AcquisitionOrchestrator,
)
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH
from analysis.acquisition.repository import AcquisitionNotFoundError
from analysis.acquisition.runtime import AcquisitionRuntime

from orchestrator_support import (
    NullTransport,
    make_runtime,
    no_data_adapter,
    targeted_plan,
)


def test_old_run_recovers_repository_frozen_definition_after_registry_upgrade(
    tmp_path,
) -> None:
    root = tmp_path / "registry-upgrade"
    old_runtime = make_runtime(root, no_data_adapter())
    old_plan = targeted_plan(old_runtime)
    old_definition = old_runtime.source_definition("cninfo.disclosures", "1.0.0")
    old_query = next(
        item
        for item in old_definition.queries
        if item.query_id == "cninfo.periodic_report"
    )
    old_reference = old_plan.run.source_definition_refs[0]
    old_runtime.close()

    payload = json.loads(INITIAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["registry_version"] = "2.0.0"
    upgraded_definition = next(
        item
        for item in payload["definitions"]
        if item["source_definition_id"] == "cninfo.disclosures"
    )
    upgraded_definition["version"] = "2.0.0"
    upgraded_query = next(
        item
        for item in upgraded_definition["queries"]
        if item["query_id"] == "cninfo.periodic_report"
    )
    upgraded_query["endpoint"] = f'{upgraded_query["endpoint"]}/v2'
    upgraded_path = tmp_path / "registry-v2.json"
    upgraded_path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    adapter = no_data_adapter()
    seen_definitions = []

    def resolve_adapter(definition, _transport):
        seen_definitions.append(definition)
        return adapter

    runtime = AcquisitionRuntime.create(
        root / "analysis.db",
        root / "data",
        registry_path=upgraded_path,
        workspace_root=root / "workspace",
        orchestrator_factory=lambda _runtime: None,
    )
    runtime.orchestrator = AcquisitionOrchestrator(
        runtime,
        transport_factory=lambda _definition: NullTransport(),
        adapter_resolver=resolve_adapter,
    )
    try:
        assert runtime.source_definition("cninfo.disclosures").version == "2.0.0"

        result = runtime.orchestrator.execute_run(old_plan.run.run_id)

        assert result.material_gap_count == 0
        assert {item.version for item in seen_definitions} == {"1.0.0"}
        assert adapter.query_calls[0].url == old_query.endpoint
        frozen = runtime.frozen_source_definitions(old_plan.run)
        assert frozen == (old_definition,)
        assert canonical_json_sha256(frozen[0]) == old_reference.content_hash
        assert (
            runtime.manifest_service.source_policy_resolver(
                old_reference.source_definition_id, old_reference.version
            )
            == old_definition
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("tamper", "message"),
    (
        ("registry", "来源注册表content hash不匹配"),
        ("definition", "来源定义content hash不匹配"),
    ),
)
def test_frozen_hash_mismatch_fails_before_lease_or_io(
    tmp_path, tamper: str, message: str
) -> None:
    adapter = no_data_adapter()
    runtime = make_runtime(tmp_path / tamper, adapter)
    plan = targeted_plan(runtime, persist=False)
    run_payload = plan.run.model_dump(mode="python")
    if tamper == "registry":
        run_payload["registry_content_hash"] = "0" * 64
    else:
        reference = plan.run.source_definition_refs[0]
        run_payload["source_definition_refs"] = (
            SourceDefinitionRef(
                source_definition_id=reference.source_definition_id,
                version=reference.version,
                content_hash="0" * 64,
            ),
        )
    tampered_run = AcquisitionRun.model_validate(run_payload)
    tampered_plan = AcquisitionPlan(
        run=tampered_run,
        physical_query_plan_items=plan.physical_query_plan_items,
        coverage_entries=plan.coverage_entries,
        coverage_links=plan.coverage_links,
    )
    try:
        runtime.orchestrator.persist_plan(tampered_plan)

        with pytest.raises(AcquisitionExecutionError, match=message):
            runtime.orchestrator.execute_run(tampered_run.run_id)

        assert adapter.query_calls == []
        with pytest.raises(AcquisitionNotFoundError):
            runtime.repository.get_lease(tampered_run.run_id)
    finally:
        runtime.close()
