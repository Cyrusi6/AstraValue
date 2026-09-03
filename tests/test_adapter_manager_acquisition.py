from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from analysis.acquisition.models import AcquisitionMode
from analysis.acquisition.models import (
    AcquisitionAttemptEventType,
    AcquisitionOutcome,
)
from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.adapters.manager import AdapterManager
from analysis.models import SyncRequest


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


class RecordingOrchestrator:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    def execute_run(self, run_id: str):
        self.run_ids.append(run_id)
        return {
            "run_id": run_id,
            "result": "succeeded",
            "coverage_accounted": True,
            "material_gap_count": 0,
            "default_consume_eligible": True,
            "checkpoint_ids": [],
        }


class RecordingLegacyAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SyncRequest]] = []

    def sync(self, ticker: str, options: SyncRequest):
        self.calls.append((ticker, options))
        raise AssertionError("混合scope不得进入legacy adapter")


def _runtime(tmp_path):
    orchestrator = RecordingOrchestrator()
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "data",
        orchestrator_factory=lambda _runtime: orchestrator,
    )
    return runtime, orchestrator


def test_business_model_registry_plan_cannot_be_narrowed_by_provider_list(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    manager = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
    )

    result = manager.sync(
        "600519",
        SyncRequest(
            providers=[],
            scopes=["business_model"],
            acquisition_mode="baseline",
            as_of=NOW,
        ),
    )

    assert len(orchestrator.run_ids) == 1
    assert result.provider_results_authority == "structured_attempts"
    assert result.coverage_accounted is True
    assert set(result.provider_results) == {
        "cninfo.disclosures",
        "sse.disclosures",
        "szse.disclosures",
        "moutai.ir",
    }
    plan_items = runtime.repository.list_plan_items(result.acquisition_run_id)
    assert len({item.plan_item_id for item in plan_items}) == len(plan_items)


def test_business_model_plan_only_is_durable_and_never_consumable(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    result = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
    ).sync(
        "600519",
        SyncRequest(
            providers=["official"],
            scopes=["business_model"],
            acquisition_mode=AcquisitionMode.BASELINE.value,
            acquisition_plan_only=True,
            as_of=NOW,
        ),
    )

    assert orchestrator.run_ids == []
    assert runtime.repository.get_run(result.acquisition_run_id).run_id
    assert result.acquisition_status == "planned"
    assert result.default_consume_eligible is False
    assert result.coverage_accounted is False


def test_business_model_unknown_alias_fails_before_execution(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    manager = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
    )
    with pytest.raises(ValueError, match="未知business_model来源alias"):
        manager.sync(
            "600519",
            SyncRequest(
                providers=["unreviewed-web"],
                scopes=["business_model"],
                acquisition_mode="baseline",
                as_of=NOW,
            ),
        )
    assert orchestrator.run_ids == []


def test_business_model_mixed_with_legacy_scope_fails_before_any_io(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    legacy = RecordingLegacyAdapter()
    manager = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
        legacy_adapter_builders={"official": lambda: legacy},
    )

    with pytest.raises(ValueError, match="不能与legacy同步范围混合执行"):
        manager.sync(
            "600519",
            SyncRequest(
                providers=["official"],
                scopes=["business_model", "financials"],
                acquisition_mode="baseline",
                as_of=NOW,
            ),
        )

    assert orchestrator.run_ids == []
    assert legacy.calls == []
    assert runtime.repository.list_runs(ticker="600519") == []


def test_default_mode_is_baseline_until_safe_checkpoints_exist(tmp_path):
    runtime, _ = _runtime(tmp_path)
    assert runtime.default_business_model_mode("600519") == AcquisitionMode.BASELINE


def test_shared_execution_preserves_coverage_fanout_without_duplicate_io_plan(tmp_path):
    runtime, orchestrator = _runtime(tmp_path)
    result = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
    ).sync(
        "600519",
        SyncRequest(
            providers=[],
            scopes=["business_model"],
            acquisition_mode="baseline",
            acquisition_plan_only=True,
            as_of=NOW,
        ),
    )

    assert orchestrator.run_ids == []
    periodic = next(
        item
        for item in runtime.repository.list_plan_items(result.acquisition_run_id)
        if item.source_definition_id == "cninfo.disclosures"
        and item.query_id == "cninfo.periodic_report"
    )
    links = runtime.repository.list_plan_coverage_links(
        plan_item_id=periodic.plan_item_id
    )
    assert len(links) == 10
    assert len({item.plan_item_id for item in links}) == 1


def test_required_fetch_gap_and_typed_failure_are_not_swallowed_as_provider_text(
    tmp_path,
):
    runtime, _ = _runtime(tmp_path)

    class RestrictedFetchOrchestrator:
        def execute_run(self, run_id: str):
            fetch = SimpleNamespace(
                attempt_id="attempt-restricted-fetch",
                source_definition_id="cninfo.disclosures",
            )
            runtime.repository.list_attempts = lambda **_kwargs: [fetch]
            runtime.repository.list_attempt_events = lambda _attempt_id: [
                SimpleNamespace(
                    event_type=AcquisitionAttemptEventType.OUTCOME_TERMINAL,
                    outcome=AcquisitionOutcome.RESTRICTED,
                )
            ]
            runtime.repository.list_resource_observations = lambda **_kwargs: []
            return {
                "run_id": run_id,
                "result": "partial",
                "coverage_accounted": True,
                "material_gap_count": 1,
                "default_consume_eligible": False,
                "checkpoint_ids": [],
            }

    runtime.orchestrator = RestrictedFetchOrchestrator()
    result = AdapterManager(
        loaded_registry=runtime.loaded_registry,
        acquisition_runtime=runtime,
    ).sync(
        "600519",
        SyncRequest(
            providers=["official"],
            scopes=["business_model"],
            acquisition_mode="baseline",
            as_of=NOW,
        ),
    )

    cninfo_summary = json.loads(result.provider_results["cninfo.disclosures"])
    assert cninfo_summary["kind"] == "attempts"
    assert cninfo_summary["counts"]["restricted"] == 1
    assert result.material_gap_count == 1
    assert result.default_consume_eligible is False
    assert result.acquisition_status == "partial"
    assert "失败:" not in result.provider_results["cninfo.disclosures"]
