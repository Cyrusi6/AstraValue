from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from analysis.acquisition.runtime import AcquisitionRuntime
from analysis.acquisition.registry import INITIAL_REGISTRY_PATH


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "validate_acquisition_consistency.py"


def _runtime(tmp_path: Path) -> AcquisitionRuntime:
    runtime = AcquisitionRuntime.create(
        tmp_path / "analysis.db",
        tmp_path / "data",
        registry_path=INITIAL_REGISTRY_PATH,
        workspace_root=PROJECT_ROOT,
    )
    runtime.plan_company_run(
        "600519",
        mode="baseline",
        company_name="贵州茅台",
        market="SSE",
        # Keep this consistency fixture intentionally small while placing its
        # run inside the frozen v1 definitions' effective window.  Historical
        # baseline slicing is covered separately by planner tests.
        listing_date=date(2026, 9, 3),
        as_of=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    return runtime


def _run(runtime: AcquisitionRuntime) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(runtime.db_path),
            "--data-root",
            str(runtime.data_root),
        ],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_consistency_m2m_namespace_and_compatibility_ok(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    result = _run(runtime)
    assert result.returncode == 0, result.stderr
    assert "ACQUISITION_CONSISTENCY_OK" in result.stdout
    summary = json.loads(result.stdout.split(" ", 1)[1])
    assert summary["run_count"] == 1
    assert summary["coverage_count"] > summary["plan_item_count"]
    assert summary["legacy_sync_result_count"] == 0


def test_consistency_m2m_corruption_is_nonzero(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with sqlite3.connect(runtime.db_path) as connection:
        row = connection.execute(
            """SELECT c.coverage_entry_id
               FROM coverage_entries c
               JOIN physical_query_coverage_links l
                 ON l.coverage_entry_id=c.coverage_entry_id
               JOIN physical_query_plan_items p ON p.plan_item_id=l.plan_item_id
               WHERE p.source_definition_id='cninfo.disclosures'
               LIMIT 1"""
        ).fetchone()
        assert row is not None
        connection.execute(
            "UPDATE coverage_entries SET source_definition_id='sse.disclosures' "
            "WHERE coverage_entry_id=?",
            (row[0],),
        )
    result = _run(runtime)
    assert result.returncode != 0
    assert "plan-to-coverage" in result.stderr


def test_consistency_binding_intent_pending_is_rejected(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    intent_path = Path(str(runtime.db_path) + ".acquisition-binding-intent")
    payload = json.loads(intent_path.read_text(encoding="utf-8"))
    payload["bootstrap_stage"] = "committed_v6"
    intent_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    result = _run(runtime)
    assert result.returncode != 0
    assert "未完成binding intent" in result.stderr


def test_consistency_discovery_barrier_resolution_and_lease_epoch_empty_graph(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    result = _run(runtime)
    assert result.returncode == 0
    assert '"attempt_count": 0' in result.stdout
    assert '"snapshot_count": 0' in result.stdout
