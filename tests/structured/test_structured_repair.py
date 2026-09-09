from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.structured.repair import (
    RepairManifest,
    RepairManifestError,
    RepairManifestItem,
    load_repair_manifest,
    plan_repair,
    write_repair_manifest,
)
from analysis.structured.service import StructuredDataService
from analysis.structured.storage import StructuredNamespaceMismatch
from scripts import supervise_structured_repair


NOW = datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)
REVISION = "d" * 40


class NoWaitSourceGate:
    def __init__(self):
        self.holds = []

    @contextmanager
    def hold(self, *args, **kwargs):
        self.holds.append((args, kwargs))
        yield SimpleNamespace()


def _runtime(tmp_path):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, json={"error": "unused"})
        )
    )
    runtime = AcquisitionRuntime.create(
        tmp_path / "isolated.db",
        tmp_path / "data",
        http_client=client,
        clock=lambda: datetime.now(timezone.utc),
        monotonic_clock=time.monotonic,
        sleeper=lambda _seconds: None,
    )
    runtime.source_gate = NoWaitSourceGate()
    return runtime, client


class ResultSet:
    def __init__(self, fields, rows, *, error_code="0", error_msg="success"):
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = tuple(fields)
        self._rows = iter(rows)
        self._current = None

    def next(self):
        try:
            self._current = next(self._rows)
        except StopIteration:
            return False
        return True

    def get_row_data(self):
        return self._current


class CalendarSdk:
    def __init__(self, *, login_ok: bool):
        self.login_ok = login_ok
        self.login_count = 0
        self.logout_count = 0
        self.query_count = 0
        self.fail_queries: set[int] = set()
        self.raise_queries: set[int] = set()

    def login(self):
        self.login_count += 1
        return SimpleNamespace(
            error_code="0" if self.login_ok else "-1",
            error_msg="success" if self.login_ok else "blocked",
        )

    def logout(self):
        self.logout_count += 1
        return SimpleNamespace(error_code="0", error_msg="success")

    def query_trade_dates(self, **_params):
        self.query_count += 1
        if self.query_count in self.raise_queries:
            raise RuntimeError("query exploded")
        if self.query_count in self.fail_queries:
            return ResultSet((), (), error_code="-2", error_msg="query failed")
        return ResultSet(
            ("calendar_date", "is_trading_day"),
            (("2026-01-05", "1"),),
        )


def _manifest_item(**updates):
    value = {
        "job_id": "job-1",
        "plan_item_id": "plan-1",
        "company_id": "company:600519",
        "ticker": "600519.SH",
        "dataset_id": "baostock_calendar",
        "source_definition_id": "structured-baostock-v1",
        "source_definition_version": "1.0.0",
        "purpose": "date_range",
        "scope_key": "2026",
        "time_start": "2026-01-01T00:00:00+00:00",
        "time_end": "2027-01-01T00:00:00+00:00",
        "baseline_attempt_count": 2,
        "baseline_max_attempts": 2,
        "baseline_failure_reasons": ("ProtocolError:BaoStock login failed: blocked",),
    }
    value.update(updates)
    return RepairManifestItem.model_validate(value)


def _manifest(*items, **updates):
    value = {
        "run_id": "run-1",
        "storage_namespace_id": "namespace-1",
        "frozen_context_hash": "0" * 64,
        "code_revision": REVISION,
        "dataset_filters": ("baostock_calendar",),
        "reason_filters": ("ProtocolError:BaoStock login failed: blocked",),
        "items": items or (_manifest_item(),),
    }
    value.update(updates)
    return RepairManifest.create(**value)


def test_repair_manifest_is_content_addressed_and_rejects_invalid_inputs(tmp_path):
    manifest = _manifest()
    path = tmp_path / "repair.json"
    write_repair_manifest(path, manifest)
    assert load_repair_manifest(path) == manifest
    with pytest.raises(RepairManifestError, match="different content"):
        write_repair_manifest(
            path,
            _manifest(_manifest_item(scope_key="another-scope")),
        )

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["items"][0]["scope_key"] = "forged"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RepairManifestError, match="hash mismatch"):
        load_repair_manifest(path)

    with pytest.raises(ValidationError):
        RepairManifest.model_validate(
            {**manifest.model_dump(mode="json"), "schema_version": "wrong"}
        )
    with pytest.raises(ValidationError):
        RepairManifest.create(
            run_id="",
            storage_namespace_id="namespace-1",
            frozen_context_hash="0" * 64,
            code_revision=REVISION,
            dataset_filters=("baostock_calendar",),
            reason_filters=("failure",),
            items=(),
        )
    with pytest.raises(ValidationError, match="duplicate jobs"):
        _manifest(_manifest_item(), _manifest_item())
    with pytest.raises(ValidationError, match="not terminally exhausted"):
        _manifest(_manifest_item(baseline_attempt_count=1))
    with pytest.raises(ValidationError):
        _manifest(max_additional_attempts=3)


def test_repair_plan_is_exact_idempotent_and_empty_plan_does_no_network(tmp_path):
    acquisition_runtime, client = _runtime(tmp_path)
    sdk = CalendarSdk(login_ok=False)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("baostock_calendar",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        service.run(run_id)
        service.run(run_id)
        reason = "ProtocolError:BaoStock login failed: blocked"
        first = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=(reason,),
            code_revision=REVISION,
        )
        repeated = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=(reason,),
            code_revision=REVISION,
        )
        assert first == repeated
        assert len(first.items) == 1
        assert first.items[0].baseline_attempt_count == 2
        assert first.storage_namespace_id == acquisition_runtime.namespace_id
        assert sdk.login_count == 2

        forged_item = first.items[0].model_copy(
            update={"baseline_failure_reasons": ("forged-reason",)}
        )
        forged = RepairManifest.create(
            run_id=first.run_id,
            storage_namespace_id=first.storage_namespace_id,
            frozen_context_hash=first.frozen_context_hash,
            code_revision=first.code_revision,
            dataset_filters=first.dataset_filters,
            reason_filters=("forged-reason",),
            items=(forged_item,),
        )
        with pytest.raises(RepairManifestError, match="failure reasons changed"):
            service.runtime.repair_status(
                forged, expected_code_revision=REVISION
            )

        empty = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=("another exact reason",),
            code_revision=REVISION,
        )
        assert empty.items == ()
        assert sdk.login_count == 2
    finally:
        acquisition_runtime.close()
        client.close()


def test_repair_plan_excludes_every_non_failed_scheduler_state():
    states = ("succeeded", "no_data", "pending", "retryable", "partial", "failed")
    jobs = [
        {
            "job_id": f"job-{state}",
            "run_id": "run-1",
            "plan_item_id": f"plan-{state}",
            "company_id": "company:600519",
            "ticker": "600519.SH",
            "dataset_id": "baostock_calendar",
            "source_definition_id": "structured-baostock-v1",
            "source_definition_version": "1.0.0",
            "purpose": "date_range",
            "scope_key": state,
            "time_start": None,
            "time_end": None,
            "max_attempts": 2,
        }
        for state in states
    ]
    context = SimpleNamespace(
        storage_namespace_id="namespace-1", content_hash="0" * 64
    )
    bridge = SimpleNamespace(
        prepare_execution=lambda _run_id: context,
        status=lambda _run_id: SimpleNamespace(
            jobs=tuple(
                SimpleNamespace(job_id=f"job-{state}", state=state)
                for state in states
            )
        ),
    )
    storage = SimpleNamespace(list_jobs=lambda _run_id, limit=None: jobs)
    attempts = [
        SimpleNamespace(
            attempt_id=f"attempt-failed-{ordinal}",
            physical_query_plan_item_id="plan-failed",
            retry_ordinal=ordinal,
        )
        for ordinal in range(2)
    ]
    repository = SimpleNamespace(
        list_attempts=lambda **_kwargs: attempts,
        list_attempt_events=lambda _attempt_id: [
            SimpleNamespace(outcome="parse_failed", reason_code="exact-reason")
        ],
    )
    manifest = plan_repair(
        bridge=bridge,
        storage=storage,
        repository=repository,
        run_id="run-1",
        dataset_filters=("baostock_calendar",),
        reason_filters=("exact-reason",),
        code_revision=REVISION,
    )
    assert [item.job_id for item in manifest.items] == ["job-failed"]


def test_repair_login_probe_failure_creates_zero_attempts(tmp_path):
    acquisition_runtime, client = _runtime(tmp_path)
    sdk = CalendarSdk(login_ok=False)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("baostock_calendar",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        service.run(run_id)
        service.run(run_id)
        reason = "ProtocolError:BaoStock login failed: blocked"
        manifest = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=(reason,),
            code_revision=REVISION,
        )
        before = len(acquisition_runtime.repository.list_attempts(run_id=run_id, limit=None))
        result = service.runtime.repair_execute(
            manifest, expected_code_revision=REVISION
        )
        after = len(acquisition_runtime.repository.list_attempts(run_id=run_id, limit=None))
        assert result["source_status"] == "source_unavailable"
        assert result["fallback_required"] is True
        assert result["attempted_job_ids"] == []
        assert before == after
        assert sdk.login_count == 3
        assert sdk.logout_count == 0
    finally:
        acquisition_runtime.close()
        client.close()


def test_repair_reuses_one_session_and_stops_after_success(tmp_path):
    acquisition_runtime, client = _runtime(tmp_path)
    sdk = CalendarSdk(login_ok=False)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("baostock_calendar",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        # Exhaust two calendar jobs under the ordinary two-attempt contract.
        for _ in range(4):
            service.run(run_id)
        reason = "ProtocolError:BaoStock login failed: blocked"
        manifest = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=(reason,),
            code_revision=REVISION,
        )
        assert len(manifest.items) == 2

        sdk.login_ok = True
        before_login = sdk.login_count
        before_holds = len(acquisition_runtime.source_gate.holds)
        result = service.runtime.repair_execute(
            manifest, expected_code_revision=REVISION, max_jobs_per_round=2
        )
        assert len(result["attempted_job_ids"]) == 2
        assert result["counts"]["succeeded"] == 2
        assert sdk.login_count == before_login + 1
        assert sdk.logout_count == 1
        assert sdk.query_count == 2
        assert len(acquisition_runtime.source_gate.holds) == before_holds + 3

        repeated = service.runtime.repair_execute(
            manifest, expected_code_revision=REVISION, max_jobs_per_round=2
        )
        assert repeated["attempted_job_ids"] == []
        assert sdk.login_count == before_login + 1
        assert len(acquisition_runtime.repository.list_attempts(run_id=run_id, limit=None)) == 6
        # Ordinary resume still never reclaims the other terminal failures.
        assert all(
            item.job_id not in {entry.job_id for entry in manifest.items}
            for item in service.runtime.bridge.resume_candidates(run_id)
        )
    finally:
        acquisition_runtime.close()
        client.close()


def test_repair_budget_exhaustion_and_finally_logout(tmp_path):
    acquisition_runtime, client = _runtime(tmp_path)
    sdk = CalendarSdk(login_ok=False)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("baostock_calendar",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        service.run(run_id)
        service.run(run_id)
        manifest = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=("ProtocolError:BaoStock login failed: blocked",),
            code_revision=REVISION,
        )
        sdk.login_ok = True
        sdk.raise_queries = {1, 2}
        first = service.runtime.repair_execute(manifest, expected_code_revision=REVISION)
        second = service.runtime.repair_execute(manifest, expected_code_revision=REVISION)
        third = service.runtime.repair_execute(manifest, expected_code_revision=REVISION)
        assert first["counts"]["ready"] == 1
        assert second["counts"]["exhausted"] == 1
        assert third["attempted_job_ids"] == []
        assert sdk.login_count == 4
        assert sdk.logout_count == 2
        assert len(acquisition_runtime.repository.list_attempts(run_id=run_id, limit=None)) == 4
    finally:
        acquisition_runtime.close()
        client.close()


def test_repair_recovers_from_interrupted_attempt_using_database_budget(
    tmp_path, monkeypatch
):
    acquisition_runtime, client = _runtime(tmp_path)
    sdk = CalendarSdk(login_ok=False)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-only",
            datasets=("baostock_calendar",),
            as_of=NOW,
        )
        run_id = planned["run_ids"][0]
        service.run(run_id)
        service.run(run_id)
        manifest = service.runtime.repair_plan(
            run_id,
            dataset_filters=("baostock_calendar",),
            reason_filters=("ProtocolError:BaoStock login failed: blocked",),
            code_revision=REVISION,
        )
        sdk.login_ok = True
        real_execute_sdk = service.runtime._execute_sdk

        def interrupted(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated process interruption")

        monkeypatch.setattr(service.runtime, "_execute_sdk", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.runtime.repair_execute(
                manifest, expected_code_revision=REVISION
            )
        attempts = acquisition_runtime.repository.list_attempts(
            run_id=run_id, limit=None
        )
        assert len(attempts) == 3
        assert sdk.logout_count == 1

        monkeypatch.setattr(service.runtime, "_execute_sdk", real_execute_sdk)
        recovered = service.runtime.repair_execute(
            manifest, expected_code_revision=REVISION
        )
        assert recovered["counts"]["succeeded"] == 1
        attempts = acquisition_runtime.repository.list_attempts(
            run_id=run_id, limit=None
        )
        assert [attempt.retry_ordinal for attempt in attempts] == [0, 1, 2, 3]
    finally:
        acquisition_runtime.close()
        client.close()


def test_shared_calendar_snapshot_repair_replays_without_another_query(tmp_path, monkeypatch):
    acquisition_runtime, client = _runtime(tmp_path)
    sdk = CalendarSdk(login_ok=True)
    try:
        service = StructuredDataService.from_runtime(acquisition_runtime, sdk=sdk)
        planned = service.plan(
            "600519",
            mode="baseline",
            company_scope="company-with-peers",
            datasets=("baostock_calendar",),
            as_of=NOW,
        )
        first_run, second_run = planned["run_ids"][:2]
        assert service.run(first_run)["status"]["succeeded"] == 1

        real_commit = service.storage.commit_page_bundle

        def reject_projection(*args, **kwargs):
            raise StructuredNamespaceMismatch("forced lineage failure")

        monkeypatch.setattr(service.storage, "commit_page_bundle", reject_projection)
        service.run(second_run)
        service.run(second_run)
        assert service.status(second_run)["failed"] == 1
        queries_after_failure = sdk.query_count
        monkeypatch.setattr(service.storage, "commit_page_bundle", real_commit)

        manifest = service.runtime.repair_plan(
            second_run,
            dataset_filters=("baostock_calendar",),
            reason_filters=("StructuredNamespaceMismatch:forced lineage failure",),
            code_revision=REVISION,
        )
        assert len(manifest.items) == 1
        result = service.runtime.repair_execute(
            manifest, expected_code_revision=REVISION
        )
        assert result["counts"]["succeeded"] == 1
        assert sdk.query_count == queries_after_failure
        assert result["source_probe_performed"] is True
        assert result["query_io_count"] == 0
        item = result["items"][0]
        assert item["page_count"] == 1
        assert item["record_count"] == 1
        assert item["coverage_status"] == "complete"
        assert item["pagination_issues"] == []
    finally:
        acquisition_runtime.close()
        client.close()


def test_repair_supervisor_preflight_then_isolated_end_to_end(tmp_path, monkeypatch, capsys):
    acquisition_runtime, client = _runtime(tmp_path)
    failing_sdk = CalendarSdk(login_ok=False)
    service = StructuredDataService.from_runtime(acquisition_runtime, sdk=failing_sdk)
    planned = service.plan(
        "600519",
        mode="baseline",
        company_scope="company-only",
        datasets=("baostock_calendar",),
        as_of=NOW,
    )
    run_id = planned["run_ids"][0]
    service.run(run_id)
    service.run(run_id)
    manifest = service.runtime.repair_plan(
        run_id,
        dataset_filters=("baostock_calendar",),
        reason_filters=("ProtocolError:BaoStock login failed: blocked",),
        code_revision=REVISION,
    )
    manifest_path = tmp_path / "evidence" / "repair.json"
    write_repair_manifest(manifest_path, manifest)
    namespace = acquisition_runtime.namespace_id
    db = acquisition_runtime.db_path
    data_root = acquisition_runtime.data_root
    service.close()
    acquisition_runtime.close()
    client.close()

    def fake_git(_workspace, *args):
        return REVISION if args == ("rev-parse", "HEAD") else ""

    monkeypatch.setattr(supervise_structured_repair, "_git", fake_git)
    monkeypatch.setattr(supervise_structured_repair, "_pid_alive", lambda _pid: True)
    success_sdk = CalendarSdk(login_ok=True)
    monkeypatch.setitem(sys.modules, "baostock", success_sdk)
    arguments = [
        "--workspace",
        str(tmp_path),
        "--db",
        str(db),
        "--data-root",
        str(data_root),
        "--manifest",
        str(manifest_path),
        "--expected-revision",
        REVISION,
        "--expected-namespace",
        namespace,
        "--expected-manifest-sha256",
        manifest.manifest_sha256,
        "--supervisor-pid",
        "424242",
        "--evidence-dir",
        str(tmp_path / "repair-output"),
        "--max-rounds",
        "1",
    ]
    with pytest.raises(SystemExit, match="still running"):
        supervise_structured_repair.main(arguments)
    assert success_sdk.login_count == success_sdk.query_count == 0

    monkeypatch.setattr(supervise_structured_repair, "_pid_alive", lambda _pid: False)
    assert supervise_structured_repair.main(arguments) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["installed_scheduler"] is False
    assert output["monitored_baseline"] is False
    assert output["final_status"]["counts"]["succeeded"] == 1
    assert success_sdk.login_count == success_sdk.logout_count == 1
    assert success_sdk.query_count == 1
    written = json.loads(
        (tmp_path / "repair-output" / "structured-repair-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert written["manual_acceptance"] == "pending_independent"
