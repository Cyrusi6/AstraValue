from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from analysis.acquisition.models import AcquisitionRunResult
from analysis.acquisition.orchestrator import AcquisitionExecutionResult
from analysis.acquisition.bootstrap import (
    BINDING_INTENT_SUFFIX,
    ROOT_MARKER_NAME,
    StorageNamespaceMismatch,
)
from analysis.acquisition.repository import LeaseConflictError, StorageBusyError
from analysis.cli import (
    EXIT_INTERNAL,
    EXIT_MATERIAL_GAP,
    EXIT_OK,
    EXIT_RETRYABLE,
    EXIT_USAGE,
    main,
)
from analysis.storage import ReportStorage


NOW = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)


class FakeRepository:
    def list_runs(self, **_kwargs):
        return []

    def get_run(self, run_id):
        return SimpleNamespace(run_id=run_id)

    def list_run_events(self, _run_id):
        return []

    def list_plan_items(self, _run_id):
        return []

    def list_coverage_entries(self, _run_id):
        return []

    def list_plan_coverage_links(self, **_kwargs):
        return []

    def list_attempts(self, **_kwargs):
        return []

    def list_coverage_resolutions(self, _run_id):
        return []


class FakeOrchestrator:
    def __init__(self, *, result=None, error=None):
        self.result = result or {
            "run_id": "run-1",
            "result": "succeeded",
            "coverage_accounted": True,
            "material_gap_count": 0,
            "default_consume_eligible": True,
        }
        self.error = error
        self.execute_calls = []
        self.smoke_calls = []

    def execute_run(self, run_id, *, lease_ttl_seconds=60):
        self.execute_calls.append((run_id, lease_ttl_seconds))
        if self.error:
            raise self.error
        if isinstance(self.result, dict):
            return {**self.result, "run_id": run_id}
        return self.result

    def smoke_sources(self, *, ticker, source_ids):
        self.smoke_calls.append((ticker, source_ids))
        if self.error:
            raise self.error
        return self.result


class FakeRuntime:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.repository = FakeRepository()
        self.plan_calls = []

    def plan_company_run(self, ticker, **kwargs):
        self.plan_calls.append((ticker, kwargs))
        selected_mode = str(getattr(kwargs["mode"], "value", kwargs["mode"]))
        parent_run_id = kwargs.get("parent_run_id")
        if selected_mode == "reconcile" and parent_run_id is None:
            parent_run_id = "resolved-latest-run"
        run = SimpleNamespace(
            run_id="run-1",
            mode=kwargs["mode"],
            run_kind=SimpleNamespace(value="production"),
            parent_run_id=parent_run_id,
            reconcile_target=(
                {"strategy": "earliest_unresolved_gap"}
                if selected_mode == "reconcile"
                else None
            ),
        )
        return SimpleNamespace(
            run=run,
            physical_query_plan_items=(SimpleNamespace(plan_item_id="plan-1"),),
            coverage_entries=(SimpleNamespace(coverage_entry_id="coverage-1"),),
            coverage_links=(SimpleNamespace(plan_item_id="plan-1"),),
        )


def _patch_runtime(monkeypatch, runtime):
    calls = []

    def create(db, data_root):
        calls.append((db, data_root))
        if isinstance(runtime, Exception):
            raise runtime
        return runtime

    monkeypatch.setattr("analysis.cli.AcquisitionRuntime.create", create)
    return calls


@pytest.mark.parametrize("repeat_repair", [False, True])
def test_api_cli_same_target_shared_selector_exact_work_position(tmp_path, monkeypatch, capsys, repeat_repair):
    from fastapi.testclient import TestClient
    from analysis.api import create_app
    from orchestrator_support import later_barrier_runtime, NOW as CUTOFF
    runtime, adapter, parent, expected, state = later_barrier_runtime(tmp_path / "shared")
    if repeat_repair:
        parent = runtime.plan_company_run("600519", mode="reconcile", as_of=CUTOFF,
                                          parent_run_id=parent.run.run_id)
        runtime.orchestrator.execute_run(parent.run.run_id)
        expected = next(p for p in parent.physical_query_plan_items if p.execution_key == expected.execution_key)
    _patch_runtime(monkeypatch, runtime)
    calls_before = len(adapter.query_calls)
    code = main(["acquire", "start", "600519", "--mode", "reconcile", "--from-run",
                 parent.run.run_id, "--as-of", CUTOFF.isoformat(), "--plan-only",
                 "--db", str(tmp_path / "shared" / "analysis.db"),
                 "--data-root", str(tmp_path / "shared" / "data"), "--json"])
    cli_target = json.loads(capsys.readouterr().out)["reconcile_target"]
    assert code == EXIT_OK
    response = TestClient(create_app(acquisition_runtime=runtime)).post(
        "/api/companies/600519/acquisition-runs",
        json={"mode": "reconcile", "parent_run_id": parent.run.run_id, "as_of": CUTOFF.isoformat()},
    )
    assert response.status_code == 201, response.text
    api_target = response.json()["run"]["reconcile_target"]
    assert api_target == cli_target
    assert api_target["range_policy_version"] == "bounded-parent-range-v1"
    if repeat_repair:
        assert api_target["range_floor"] == parent.run.reconcile_target["range_floor"]
    assert api_target["plan_item_id"] == expected.plan_item_id
    assert json.loads(api_target["work_position"])["page"] == 3
    assert len(adapter.query_calls) == calls_before


def test_plan_only_builds_one_runtime_and_does_not_execute(monkeypatch, capsys):
    orchestrator = FakeOrchestrator()
    runtime = FakeRuntime(orchestrator)
    calls = _patch_runtime(monkeypatch, runtime)

    code = main(
        [
            "acquire",
            "start",
            "600519",
            "--mode",
            "baseline",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--plan-only",
            "--json",
        ]
    )

    assert code == EXIT_OK
    assert len(calls) == 1
    assert orchestrator.execute_calls == []
    assert json.loads(capsys.readouterr().out)["plan_only"] is True


def test_execute_exit_codes_and_ttl(monkeypatch, capsys):
    partial = {
        "result": "partial",
        "coverage_accounted": True,
        "material_gap_count": 1,
        "default_consume_eligible": False,
    }
    orchestrator = FakeOrchestrator(result=partial)
    _patch_runtime(monkeypatch, FakeRuntime(orchestrator))
    code = main(
        [
            "acquire",
            "execute",
            "run-x",
            "--lease-ttl-seconds",
            "21",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--json",
        ]
    )
    assert code == EXIT_MATERIAL_GAP
    assert orchestrator.execute_calls == [("run-x", 21)]
    assert json.loads(capsys.readouterr().out)["material_gap_count"] == 1


@pytest.mark.parametrize(
    ("error", "subtype"),
    [
        (LeaseConflictError("run-x", NOW.isoformat()), "active_lease"),
        (StorageBusyError("busy"), "storage_busy"),
    ],
)
def test_active_lease_exit_5_and_storage_busy_exit_5_have_subtype(
    monkeypatch, capsys, error, subtype
):
    _patch_runtime(monkeypatch, FakeRuntime(FakeOrchestrator(error=error)))
    code = main(
        [
            "acquire",
            "execute",
            "run-x",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--json",
        ]
    )
    assert code == EXIT_RETRYABLE
    assert json.loads(capsys.readouterr().out)["subtype"] == subtype


def test_smoke_uses_same_runtime_and_passes_registry_source_ids(monkeypatch, capsys):
    orchestrator = FakeOrchestrator()
    calls = _patch_runtime(monkeypatch, FakeRuntime(orchestrator))
    code = main(
        [
            "smoke-sources",
            "--ticker",
            "300750",
            "--source",
            "szse.disclosures",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--json",
        ]
    )
    assert code == EXIT_OK
    assert calls == [("pilot.db", "pilot-data")]
    assert orchestrator.smoke_calls == [("300750", ["szse.disclosures"])]
    capsys.readouterr()


def test_missing_db_or_data_root_is_argparse_error():
    with pytest.raises(SystemExit) as exc:
        main(["acquire", "execute", "run-x", "--db", "pilot.db"])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("mode", "parent_args", "expected_parent"),
    [
        ("baseline", [], None),
        ("incremental", [], None),
        ("reconcile", ["--from-run", "parent-explicit"], "parent-explicit"),
        ("reconcile", ["--from-latest-run"], "resolved-latest-run"),
    ],
)
def test_mode_and_reconcile_parent_are_explicit(
    monkeypatch, capsys, mode, parent_args, expected_parent
):
    runtime = FakeRuntime(FakeOrchestrator())
    calls = _patch_runtime(monkeypatch, runtime)
    code = main(
        [
            "acquire",
            "start",
            "600519",
            "--mode",
            mode,
            *parent_args,
            "--db",
            "isolated.db",
            "--data-root",
            "isolated-data",
            "--plan-only",
            "--json",
        ]
    )
    assert code == EXIT_OK
    assert calls == [("isolated.db", "isolated-data")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == mode
    assert payload["resolved_parent_run_id"] == expected_parent


def test_namespace_mismatch_is_usage_error_before_execution(monkeypatch, capsys):
    calls = _patch_runtime(
        monkeypatch,
        StorageNamespaceMismatch("database/data-root namespace mismatch"),
    )
    code = main(
        [
            "acquire",
            "execute",
            "run-x",
            "--db",
            "pilot.db",
            "--data-root",
            "wrong-root",
            "--json",
        ]
    )
    assert code == EXIT_USAGE
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["subtype"] == "validation"


def test_internal_failure_uses_exit_code_4(monkeypatch, capsys):
    _patch_runtime(
        monkeypatch,
        FakeRuntime(
            FakeOrchestrator(
                error=RuntimeError("executor failed at /tmp/private/analysis.db")
            )
        ),
    )
    code = main(
        [
            "acquire",
            "execute",
            "run-x",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--json",
        ]
    )
    assert code == EXIT_INTERNAL
    payload = json.loads(capsys.readouterr().out)
    assert payload["subtype"] == "internal_error"
    assert "private" not in payload["detail"]


def test_cli_success_redacts_paths_and_nested_url_secrets(monkeypatch, capsys):
    result = {
        "result": "succeeded",
        "coverage_accounted": True,
        "material_gap_count": 0,
        "default_consume_eligible": True,
        "archived_path": "/tmp/private/blob.pdf",
        "db_path": "/var/lib/astravalue/analysis.db",
        "source_urls": [
            "https://example.test/a?access_token=list-secret-value"
        ],
        "archive_relative_path": "raw/blobs/sha256/aa/aabbcc",
    }
    _patch_runtime(
        monkeypatch,
        FakeRuntime(FakeOrchestrator(result=result)),
    )

    code = main(
        [
            "acquire",
            "execute",
            "run-x",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--json",
        ]
    )

    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["archived_path"] == "[REDACTED_LOCAL_PATH]"
    assert "db_path" not in payload
    assert "list-secret-value" not in json.dumps(payload)
    assert payload["archive_relative_path"] == "raw/blobs/sha256/aa/aabbcc"


def test_execute_serializes_real_result_dataclass(monkeypatch, capsys):
    result = AcquisitionExecutionResult(
        run_id="run-x",
        result=AcquisitionRunResult.SUCCEEDED,
        coverage_accounted=True,
        material_gap_count=0,
        default_consume_eligible=True,
        checkpoint_advanced=True,
        lease_epoch=2,
        outcome_counts={"success": 1},
        attempt_ids=("attempt-1",),
        coverage_resolution_ids=("resolution-1",),
        checkpoint_ids=("checkpoint-1",),
    )
    _patch_runtime(monkeypatch, FakeRuntime(FakeOrchestrator(result=result)))
    code = main(
        [
            "acquire",
            "execute",
            "run-x",
            "--db",
            "pilot.db",
            "--data-root",
            "pilot-data",
            "--json",
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "succeeded"
    assert payload["checkpoint_ids"] == ["checkpoint-1"]


def test_smoke_strict_preserves_material_gap_exit(monkeypatch, capsys):
    result = {
        "result": "partial",
        "coverage_accounted": True,
        "material_gap_count": 1,
        "default_consume_eligible": False,
    }
    _patch_runtime(monkeypatch, FakeRuntime(FakeOrchestrator(result=result)))
    base = [
        "smoke-sources",
        "--ticker",
        "300750",
        "--db",
        "pilot.db",
        "--data-root",
        "pilot-data",
        "--json",
    ]
    assert main(base) == EXIT_OK
    capsys.readouterr()
    assert main([*base, "--strict"]) == EXIT_MATERIAL_GAP
    capsys.readouterr()


def test_backup_no_intent_or_migration_or_runtime(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "analysis.db"
    data_root = tmp_path / "backup-root"
    ReportStorage(db_path, migration_data_root=data_root)
    with sqlite3.connect(db_path) as connection:
        before = {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "migrations": connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
            "namespaces": connection.execute(
                "SELECT COUNT(*) FROM storage_namespaces"
            ).fetchone()[0],
            "runs": connection.execute(
                "SELECT COUNT(*) FROM acquisition_runs"
            ).fetchone()[0],
        }

    def forbidden_runtime(*_args, **_kwargs):
        raise AssertionError("backup must not construct AcquisitionRuntime")

    monkeypatch.setattr("analysis.cli.AcquisitionRuntime.create", forbidden_runtime)
    code = main(
        [
            "acquisition-db",
            "backup",
            "--db",
            str(db_path),
            "--data-root",
            str(data_root),
            "--json",
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["integrity_check"] == "ok"
    assert not Path(str(db_path) + BINDING_INTENT_SUFFIX).exists()
    assert not (data_root / ROOT_MARKER_NAME).exists()
    with sqlite3.connect(db_path) as connection:
        after = {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "migrations": connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0],
            "namespaces": connection.execute(
                "SELECT COUNT(*) FROM storage_namespaces"
            ).fetchone()[0],
            "runs": connection.execute(
                "SELECT COUNT(*) FROM acquisition_runs"
            ).fetchone()[0],
        }
    assert after == before


def test_legacy_validate_command_does_not_construct_acquisition_runtime(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "analysis.cli.AcquisitionRuntime.create",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy command must not construct acquisition runtime")
        ),
    )
    assert main(["validate-methods"]) == EXIT_OK
    assert "METHOD_LIBRARY_OK" in capsys.readouterr().out
