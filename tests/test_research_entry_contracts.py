"""CLI and independent audit consumers share the current projection contracts."""
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _script("validate_eight_step_lite")
gap_script = _script("run_research_gap")


def _json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf8")


def _projection(directory, facts, dimensions=(), selected=None):
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rows in (("facts.jsonl", facts), ("dimensional-facts.jsonl", dimensions)):
        path = directory / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf8")
        files[name] = {"path": str(path), "sha256": validator.file_hash(path)}
    path = directory / "manifest.json"
    _json(path, {"materialization_version": "test-v1", "fact_count": len(facts),
                 "dimensional_fact_count": len(dimensions),
                 "selected_fact_ids": selected if selected is not None else [f["fact_id"] for f in facts],
                 "selected_dimensional_fact_ids": [f["dimensional_fact_id"] for f in dimensions]})
    return {"files": files, "manifests": [{"path": str(path), "sha256": validator.file_hash(path)}]}


def _combine(primary, extra):
    primary["files"].update({"runs/incremental/" + k: v for k, v in extra["files"].items()})
    primary["manifests"].extend(extra["manifests"])
    return {"source_inputs": [primary]}


def test_independent_validator_reads_selected_facts_dimensions_and_runs(tmp_path):
    source = _projection(tmp_path / "600519", [{"fact_id": "old", "value": 1},
        {"fact_id": "current", "value": 2}], [{"dimensional_fact_id": "segment", "value": 3}], ["current"])
    extra = _projection(tmp_path / "600519/runs/incremental", [{"fact_id": "new-quarter", "value": 4}])
    assert set(validator.source_fact_map(_combine(source, extra))) == {"current", "segment", "new-quarter"}


def test_independent_validator_keeps_legacy_projection_compatibility(tmp_path):
    path = tmp_path / "coverage-facts.jsonl"
    path.write_text('{"fact_id":"legacy","value":1}\n', encoding="utf8")
    manifest = {"source_inputs": [{"files": {path.name: {"path": str(path), "sha256": validator.file_hash(path)}}}]}
    assert validator.source_fact_map(manifest)["legacy"]["value"] == 1


@pytest.mark.parametrize("change,reason", [
    ("fact_hash", "source_projection_changed"),
    ("manifest_hash", "source_projection_changed"),
    ("missing_manifest", "source_materialization_manifest_missing"),
    ("missing_dimensions", "source_materialization_file_missing"),
    ("missing_selected", "source_materialization_incomplete"),
    ("duplicate", "source_materialization_incomplete"),
])
def test_independent_validator_rejects_incomplete_or_changed_sources(tmp_path, change, reason):
    facts = [{"fact_id": "f", "value": 1}]
    source = _projection(tmp_path, facts * (2 if change == "duplicate" else 1),
                         selected=["missing"] if change == "missing_selected" else ["f"])
    if change == "fact_hash":
        (tmp_path / "facts.jsonl").write_text("", encoding="utf8")
    elif change == "manifest_hash":
        (tmp_path / "manifest.json").write_text("{}", encoding="utf8")
    elif change == "missing_manifest":
        source["manifests"] = []
    elif change == "missing_dimensions":
        del source["files"]["dimensional-facts.jsonl"]
    with pytest.raises(AssertionError, match=reason):
        validator.source_fact_map({"source_inputs": [source]})


def test_independent_validator_rejects_immutable_conflict_across_runs(tmp_path):
    first = _projection(tmp_path / "600519", [{"fact_id": "same", "value": 1}])
    second = _projection(tmp_path / "600519/runs/incremental", [{"fact_id": "same", "value": 2}])
    with pytest.raises(AssertionError, match="immutable_fact_conflict"):
        validator.source_fact_map(_combine(first, second))


def test_validator_reports_missing_derived_input_as_validation_failure():
    with pytest.raises(AssertionError, match="source_fact_missing"):
        validator.verify_derived({"metrics": [{"fact": {"fact_id": "lite-derived-margin",
            "derived_from_fact_ids": ["missing", "also-missing"]}}]}, {})


def _gaps():
    return {"profile_id": "eight-step-lite-v1.0.0", "company": "600519", "items": [
        {"requirement_id": "reading", "stage": "document_reading", "acquire_allowed": False},
        {"requirement_id": "unresolved", "stage": "acquisition", "acquire_allowed": True},
        {"requirement_id": "other", "stage": "acquisition", "acquire_allowed": True, "dataset_id": "market_cap"},
        {"requirement_id": "dividend", "stage": "acquisition", "acquire_allowed": True,
         "dataset_id": "dividend", "period": "2025-12-31"},
    ]}


def test_gap_selector_uses_current_profile_and_skips_unresolved_nonacquisition_items():
    profile, selected, skipped = gap_script.select_acquisition_gaps(_gaps(), "600519", "dividend")
    assert profile["profile_id"] == "eight-step-lite-v1.0.0"
    assert [item["requirement_id"] for item in selected] == ["dividend"]
    assert {item["reason"] for item in skipped} == {"not_acquisition_task", "other_dataset",
        "dataset_identity_required:use_workspace_request_materials"}


def test_gap_selector_does_not_revive_retired_scope_schema():
    with pytest.raises(ValueError, match="current_lite_next_work_required"):
        gap_script.select_acquisition_gaps({"scope_sha256": "old", "company": "600519", "items": []},
                                           "600519", "dividend")


@pytest.mark.parametrize("outcome", ["unfinished", "empty", "exception"])
def test_gap_execution_preserves_failure_and_complete_empty_status(tmp_path, monkeypatch, outcome):
    _json(tmp_path / "next-work.json", _gaps())
    _json(tmp_path / "stocks.json", {"stockList": [{"code": "600519", "category": "A股", "zwjc": "贵州茅台", "orgId": "mt"}]})
    calls = []
    state = {"summary": {"updated": [], "empty": ["job"] if outcome == "empty" else [],
                          "unfinished": [{"job_id": "job", "state": "failed", "reason": "source_unavailable"}]
                          if outcome == "unfinished" else []}}

    class Runtime:
        def __init__(self, acquisition):
            pass

        def plan(self, identities, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(run_ids=["run-1"])

        def execute(self, run_id, **kwargs):
            if outcome == "exception":
                raise ValueError("provider_unavailable")
            return {"attempted_job_ids": []}

        def status(self, run_id):
            return state

    @contextmanager
    def acquisition(*args, **kwargs):
        yield object()

    monkeypatch.setattr(gap_script.AcquisitionRuntime, "create", acquisition)
    monkeypatch.setattr(gap_script, "StructuredDataRuntime", Runtime)
    code = gap_script.main(["--gap-file", str(tmp_path / "next-work.json"), "--stock-list", str(tmp_path / "stocks.json"),
        "--output", str(tmp_path / "output"), "--ticker", "600519", "--dataset", "dividend", "--as-of", "2026-09-27", "--execute"])
    saved = json.loads((tmp_path / "output/gap-runs/600519-dividend-2026-09-27.json").read_text(encoding="utf8"))
    assert code == (0 if outcome == "empty" else 1)
    assert saved["executed"] is True
    assert saved["state"] == ("execution_failed" if outcome == "exception" else state)
    if outcome == "exception":
        assert saved["error"] == {"type": "ValueError", "reason": "provider_unavailable"}
    assert calls[0]["research_profile_id"] == "eight-step-lite-v1.0.0"
    assert calls[0]["report_periods"] == ["2025-12-31"]
    assert calls[0]["dataset_ids"] == ["dividend"]


def test_cli_materialize_exports_without_persisting_and_retains_summary(tmp_path, monkeypatch, capsys):
    from analysis import cli
    calls = []

    class Service:
        def materialize(self, run_id, **kwargs):
            calls.append((run_id, kwargs))
            return {"persisted": False, "exports": {"facts.jsonl": {"path": str(kwargs["output_dir"] / "facts.jsonl")}}}

    monkeypatch.setattr(cli, "_create_structured_service", lambda *args: Service())
    target = tmp_path / "materialized/600519/runs/run-1"
    assert cli.main(["structured", "materialize", "run-1", "--db", "bound.db", "--data-root", "data",
        "--output-dir", str(target), "--no-persist", "--summary", "--json"]) == 0
    assert calls[0][1]["output_dir"] == target
    assert calls[0][1]["persist"] is False
    assert calls[0][1]["include_records"] is False
    assert json.loads(capsys.readouterr().out)["exports"]["facts.jsonl"]["path"] == cli.REDACTED_LOCAL_PATH


def test_documented_cli_export_to_lite_chain_uses_offline_acquisition_fixture(tmp_path, monkeypatch, capsys):
    from datetime import date, datetime, timezone
    import httpx
    from analysis import cli
    from analysis.acquisition.runtime import AcquisitionRuntime
    from analysis.structured.service import StructuredDataService
    from analysis.structured.research_lite import build_lite_pack

    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"success": True, "code": 0, "result": {"data": [{
            "SECUCODE": "600519.SH", "TRADE_DATE": "2026-09-27", "CLOSE_PRICE": 1500,
            "TOTAL_MARKET_CAP": 100000, "TOTAL_SHARES": 100}], "count": 1, "pages": 1}})

    class Gate:
        @contextmanager
        def hold(self, *_args, **_kwargs):
            yield SimpleNamespace()

    db, data = tmp_path / "bound.db", tmp_path / "data"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with AcquisitionRuntime.create(db, data, http_client=client, sleeper=lambda _: None) as runtime:
            runtime.source_gate = Gate()
            service = StructuredDataService.from_runtime(runtime)
            plan = service.plan("600519", mode="baseline", datasets=["market_cap"],
                research_profile_id="eight-step-lite-v1.0.0", as_of=datetime(2026, 9, 27, tzinfo=timezone.utc))
            run_id = plan["run_ids"][0]
            assert service.runtime.execute(run_id)["status"]["succeeded"] == 1
            request_count, original_db_hash = len(requests), validator.file_hash(db)
            monkeypatch.setattr(cli, "_create_structured_service", lambda *args: service)
            projection_root = tmp_path / "materialized"
            target = projection_root / "600519/runs" / run_id
            assert cli.main(["structured", "materialize", run_id, "--db", str(db), "--data-root", str(data),
                "--research-profile", "eight-step-lite-v1.0.0", "--as-of", "2026-09-27T00:00:00+00:00",
                "--output-dir", str(target), "--no-persist", "--summary", "--json"]) == 0
            summary = json.loads(capsys.readouterr().out)
            assert summary["persisted"] is False and summary["fact_count"] > 0
            assert (target / "facts.jsonl").is_file()
            assert (target / "manifest.json").is_file()
            result = build_lite_pack(input_root=projection_root, ticker="600519", as_of=date(2026, 9, 27),
                                     output_root=tmp_path / "packs")
            pack = Path(result["pack_dir"])
            assert pack.parent == tmp_path / "packs/600519/2026-09-27"
            manifest = json.loads((pack / "manifest.json").read_text(encoding="utf8"))
            facts = validator.source_fact_map(manifest)
            assert any(f["metric_id"] == "total_market_cap" and f["value"] == 100000 for f in facts.values())
            assert len(requests) == request_count
            assert validator.file_hash(db) == original_db_hash
