"""Service contracts; runtime isolation itself is exercised by sandbox tests."""
import copy
import json
from pathlib import Path

import pytest

from analysis.research.custom_python import CustomPython, compare_results, get_exploration, get_validated_exploration
from analysis.research.workspace import ResearchError, digest, sha
from test_research_workspace import workspace


CODE = """values = {x['metric_id']: float(x['value']) for x in data['tables']['financials']['rows']}
result = {'ratio': values['operating_income'] / values['net_profit'], 'unit': 'ratio'}
"""
INDEPENDENT = """values = dict((x['metric_id'], Decimal(x['value'])) for x in data['tables']['financials']['rows'])
denominator = values.get('net_profit')
if denominator == 0:
    raise ZeroDivisionError('net profit is zero')
result = {'ratio': float(values.get('operating_income') / denominator), 'unit': 'ratio'}
"""


@pytest.fixture
def prepared(workspace):
    pack = next((workspace.root / "packs/600519").rglob("core-pack.json"))
    body = json.loads(pack.read_text(encoding="utf-8"))
    body["metrics"][0]["fact"].update(currency="CNY", scope="consolidated")
    other = copy.deepcopy(body["metrics"][0])
    other.update(metric_id="net_profit", label="合并净利润", fact_ref="F2")
    other["fact"].update(fact_id="600519-profit", value="50")
    body["metrics"].append(other)
    pack.write_text(json.dumps(body), encoding="utf-8")
    manifest_path = pack.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["output_hashes"][pack.name] = sha(pack)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    rid = workspace.prepare_research("600519", "2025-01-01")["research_id"]
    return workspace, rid


@pytest.fixture
def runtime(monkeypatch):
    """Provide deterministic protocol replies, never exec user source in the host tests."""
    calls = []
    def run(code, data, *, mode, output_dir, limits=None, image=None):
        calls.append({"code": code, "data": copy.deepcopy(data), "mode": mode, "limits": limits})
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "code.py").write_text(code, encoding="utf-8")
        (output_dir / "input.json").write_text(json.dumps(data), encoding="utf-8")
        value = {"status": "succeeded", "error": None, "retryable": False,
                 "execution": {"runtime": "fake-docker", "input_unchanged": True, "limits": limits}, "files": {}}
        if "always_fail" in code:
            value.update(status="failed", error={"code": "python_error", "exception_type": "ValueError", "message": "test failure"})
        elif "unavailable" in code:
            value.update(status="unavailable", error={"code": "docker_unavailable", "exception_type": None, "message": "test unavailable"})
        else:
            rows = data["tables"]["financials"]["rows"]
            numbers = {row["metric_id"]: float(row["value"]) for row in rows}
            if numbers["net_profit"] == 0:
                value.update(status="failed", error={"code": "python_error", "exception_type": "ZeroDivisionError", "message": "test zero denominator"})
            else:
                result = {"ratio": numbers["operating_income"] / numbers["net_profit"], "unit": "ratio"}
                if "bad_result" in code:
                    result["ratio"] += 1
                if "nondeterministic" in code:
                    result["ratio"] += len(calls)
                if "nonfinite_output" in code:
                    result = {"ratio": "NaN"}
                if "large_output" in code:
                    result = {"rows": [{"number": i, "value": i / 10} for i in range(300)]}
                name = "result.json" if mode == "calculation" else "chart-data.json"
                (output_dir / name).write_text(json.dumps(result), encoding="utf-8")
                if mode == "chart":
                    (output_dir / "figure.png").write_bytes(b"test-png")
        (output_dir / "run.json").write_text(json.dumps({k: v for k, v in value.items() if k != "files"}), encoding="utf-8")
        value["files"] = {path.name: {"path": str(path), "sha256": sha(path), "size_bytes": path.stat().st_size} for path in output_dir.iterdir()}
        return value
    monkeypatch.setattr("analysis.research.custom_python.python_sandbox.run_python", run)
    return calls


def run(service, rid, **overrides):
    return service.run_python_analysis(rid, **{
        "purpose": "比较收入与利润", "code": CODE, "inputs": {"financials": {"fact_refs": ["F1", "F2"]}},
        "definition": "营业收入 / 合并净利润", "applicability": "正利润公司的收入利润倍数，仅用于方法探索", "output_unit": "ratio", "output_period": "2024-12-31",
        **overrides})


def cases():
    # Rows sort by period then metric, hence net_profit row 0 and revenue row 1.
    return [{"name": "zero_profit", "reason": "分母为零须拒绝", "overrides": [{"path": ["tables", "financials", "rows", 0, "value"], "value": "0"}], "expected_error": "ZeroDivisionError"},
            {"name": "equal_amounts", "reason": "相同输入的比率应为一", "overrides": [{"path": ["tables", "financials", "rows", 1, "value"], "value": "50"}], "expected_result": {"ratio": 1.0, "unit": "ratio"}}]


def validate(service, rid, result, **overrides):
    return service.validate_python_analysis(rid, result["analysis_id"], **{
        "validation_code": INDEPENDENT, "validation_reason": "采用 Decimal 独立复算，核对相同期间和 CNY 口径，除法得到无量纲比率", "boundary_cases": cases(), **overrides})


def test_success_stays_exploration_and_is_reproducibly_bound(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    before = w.pack(rid)[0]["manifest_sha256"]
    result = run(service, rid, assumptions={"growth": {"value": 0.2, "reason": "探索盈利增长情景"}})
    assert result["status"] == "succeeded" and result["validation_status"] == "unverified"
    assert result["promotion_state"] == "not_promoted"
    assert runtime[0]["data"]["assumptions"]["growth"]["value"] == 0.2
    assert runtime[0]["data"]["tables"]["financials"]["rows"][0]["fact_ref"] == "F2"
    assert "path" not in json.dumps(result["files"])
    assert not w.artifacts(rid, "calculation")
    with pytest.raises(ResearchError, match="validation_required"):
        get_validated_exploration(w, rid, result["exploration_id"])
    restored = CustomPython(type(w)(w.root, w.config))
    same = run(restored, rid, purpose="换一种措辞", assumptions={"growth": {"value": 0.2, "reason": "探索盈利增长情景"}})
    assert same["analysis_id"] == result["analysis_id"] and len(runtime) == 1
    assert restored.get_python_analysis(rid, result["analysis_id"], include_code=True)["code"] == CODE
    assert w.pack(rid)[0]["manifest_sha256"] == before


def test_explicit_metric_period_selectors_match_fact_selectors(prepared, runtime):
    w, rid = prepared
    result = run(CustomPython(w), rid, inputs={"financials": {"metric_ids": ["net_profit", "operating_income"], "periods": ["2024-12-31"], "period_type": "cumulative"}})
    assert result["status"] == "succeeded"
    with pytest.raises(ResearchError, match="missing_or_ambiguous"):
        run(CustomPython(w), rid, inputs={"financials": {"metric_ids": ["net_profit"], "periods": ["2023-12-31"], "period_type": "cumulative"}})


@pytest.mark.parametrize("inputs,error", [
    ({}, "selectors_required"),
    ({"financials": {"fact_refs": []}}, "reference_list"),
    ({"financials": {"fact_refs": ["F404"]}}, "unknown_current_fact"),
    ({"financials": {"fact_refs": ["F1"], "path": "C:/secrets.csv"}}, "unknown_input_selector"),
    ({"financials": {"fact_refs": ["F1"], "values": [100]}}, "unknown_input_selector"),
    ({"financials": {"metric_ids": ["net_profit"]}}, "requires_periods"),
])
def test_no_raw_historical_numbers_paths_or_implicit_periods(prepared, runtime, inputs, error):
    w, rid = prepared
    with pytest.raises(ResearchError, match=error):
        run(CustomPython(w), rid, inputs=inputs)
    assert not runtime


def test_assumptions_are_explicit_and_require_reasons(prepared, runtime):
    w, rid = prepared
    with pytest.raises(ResearchError, match="assumption_reason"):
        run(CustomPython(w), rid, assumptions={"growth": {"value": 0.2}})
    with pytest.raises(ResearchError, match="finite_json"):
        run(CustomPython(w), rid, assumptions={"growth": {"value": float("inf"), "reason": "x"}})
    assert not runtime


def test_independent_repeat_and_boundaries_create_separate_validation(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    result = run(service, rid)
    verified = validate(service, rid, result)
    assert verified["validation_status"] == "validated" and verified["execution_count"] == 6
    assert verified["promotion_state"] == "not_promoted"
    record = get_validated_exploration(w, rid, result["exploration_id"])
    assert record["result"]["ratio"] == 2 and record["code"] == CODE
    assert record["validation"]["validation_code"] == INDEPENDENT
    assert record["validation"]["executions"][0]["result"]["ratio"] == 2
    assert record["validation"]["input_definitions"][0]["scope"] == "consolidated"
    assert not w.artifacts(rid, "calculation")
    assert validate(service, rid, result)["validation_id"] == verified["validation_id"]
    assert len(runtime) == 7
    assert len(w.artifacts(rid, "exploration")) == 1


def test_same_code_or_comments_are_not_independent_validation(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    with pytest.raises(ResearchError, match="independent_validation"):
        validate(service, rid, result, validation_code="# independent\n" + CODE)
    with pytest.raises(ResearchError, match="must_use_selected_data"):
        validate(service, rid, result, validation_code="result = {'ratio': 2, 'unit': 'ratio'}")
    assert len(runtime) == 1


def test_wrong_independent_result_and_nondeterminism_are_rejected(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    failed = validate(service, rid, result, validation_code=INDEPENDENT + "\nbad_result = True")
    assert failed["validation_status"] == "failed"
    assert failed["execution_count"] == 2
    with pytest.raises(ResearchError, match="validation_required"):
        get_validated_exploration(w, rid, result["exploration_id"])
    unstable = run(service, rid, code=CODE + "\nnondeterministic = True")
    failed = validate(service, rid, unstable)
    assert failed["validation_status"] == "failed" and failed["execution_count"] == 1


def test_validation_metadata_and_boundary_provenance_are_required(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    bad_cases = cases()
    bad_cases[0]["overrides"][0]["path"][-1] = "unit"
    with pytest.raises(ResearchError, match="not_units_periods"):
        validate(service, rid, result, boundary_cases=bad_cases)
    with pytest.raises(ResearchError, match="success_and_failure"):
        validate(service, rid, result, boundary_cases=[cases()[0], {**cases()[0], "name": "zero2"}])
    with pytest.raises(ResearchError, match="tolerance_out_of_range"):
        validate(service, rid, result, relative_tolerance=0.1)
    assert len(runtime) == 1


def test_undefined_scope_is_not_eligible_for_validation(prepared, runtime):
    w, rid = prepared
    state, revision = w.task(rid)
    pack = Path(state["pack_path"])
    body = json.loads((pack / "core-pack.json").read_text(encoding="utf-8"))
    body["metrics"][0]["fact"].pop("scope")
    (pack / "core-pack.json").write_text(json.dumps(body), encoding="utf-8")
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["output_hashes"]["core-pack.json"] = sha(pack / "core-pack.json")
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    state["manifest_sha256"] = sha(pack / "manifest.json")
    w._save(state, revision)
    service = CustomPython(w); result = run(service, rid)
    with pytest.raises(ResearchError, match="input_unit_period_currency_scope_required"):
        validate(service, rid, result)


def test_failure_budget_and_retry_are_durable(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    source = CODE + "\nalways_fail = True"
    result = run(service, rid, code=source)
    assert result["status"] == "failed" and result["failure_count"] == 1
    assert run(service, rid, code=source)["attempt"] == 1
    assert len(runtime) == 1
    assert run(service, rid, code=source + "\n# cosmetic change", retry=True)["failure_count"] == 2
    assert run(service, rid, code=source, retry=True)["failure_count"] == 3
    with pytest.raises(ResearchError, match="budget_exhausted"):
        run(service, rid, code=source, retry=True, purpose="改措辞重试", output_unit="CNY")
    assert len(runtime) == 3


def test_engine_unavailable_does_not_exhaust_code_failure_budget(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    for _ in range(4):
        result = run(service, rid, code=CODE + "\nunavailable = True", retry=True)
        assert result["status"] == "unavailable" and result["failure_count"] == 0
    assert len(runtime) == 4


def test_chart_is_available_to_review_not_numeric_evidence(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    result = run(service, rid, mode="chart")
    assert result["chart_id"] and result["status"] == "succeeded"
    chart = w.artifacts(rid, "chart")[0]
    assert chart["exploration_id"] == result["exploration_id"]
    assert chart["template"] == "custom_python" and "visual_review_required" in chart["validation"]
    with pytest.raises(ResearchError, match="not_numeric_evidence"):
        get_validated_exploration(w, rid, result["exploration_id"])
    with pytest.raises(ResearchError, match="visual_review"):
        validate(service, rid, result)


def test_file_and_database_tampering_block_reuse(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    exploration = get_exploration(w, rid, result["exploration_id"])
    result_path = Path(exploration["files"]["result.json"]["path"])
    original = result_path.read_bytes()
    result_path.write_text('{"ratio": 999}', encoding="utf-8")
    with pytest.raises(ResearchError, match="integrity_failed"):
        service.get_python_analysis(rid, result["analysis_id"])
    result_path.write_bytes(original)
    with w.connect() as con:
        payload = json.loads(con.execute("SELECT payload FROM research_artifacts WHERE id=?", (result["exploration_id"],)).fetchone()[0])
        payload["definition"] = "altered"
        con.execute("UPDATE research_artifacts SET payload=? WHERE id=?", (json.dumps(payload), result["exploration_id"]))
    with pytest.raises(ResearchError, match="record_integrity_failed"):
        get_exploration(w, rid, result["exploration_id"])


def test_validation_file_tampering_blocks_report_getter(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    validate(service, rid, result)
    record = get_validated_exploration(w, rid, result["exploration_id"])
    source = Path(record["validation"]["executions"][1]["files"]["code.py"]["path"])
    source.write_text("result = 2", encoding="utf-8")
    with pytest.raises(ResearchError, match="integrity_failed"):
        get_validated_exploration(w, rid, result["exploration_id"])


def test_other_company_cannot_read_analysis_or_select_calculation(prepared, runtime):
    from analysis.research.calculations import Calculations
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    other = w.prepare_research("000858", "2025-01-01")["research_id"]
    with pytest.raises(ResearchError, match="unknown_active_python_analysis"):
        service.get_python_analysis(other, result["analysis_id"])
    formal = Calculations(w).calculate(rid, "ratio", {"numerator": "F1", "denominator": "F2"})
    with pytest.raises(ResearchError, match="unknown_active_python_calculation"):
        service._select(other, {"formal": {"calculation_ids": [formal["artifact_id"]]}}, {})


def test_formal_and_validated_sources_accepted_but_unvalidated_blocked(prepared, runtime):
    from analysis.research.calculations import Calculations
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    with pytest.raises(ResearchError, match="validation_required"):
        service._select(rid, {"prior": {"exploration_ids": [result["exploration_id"]]}}, {})
    validate(service, rid, result)
    _, data, provenance = service._select(rid, {"prior": {"exploration_ids": [result["exploration_id"]]}}, {})
    assert data["tables"]["prior"]["rows"][0]["result"]["ratio"] == 2
    assert provenance["exploration_validation_ids"]
    calc = Calculations(w).calculate(rid, "ratio", {"numerator": "F1", "denominator": "F2"})
    _, data, provenance = service._select(rid, {"formal": {"calculation_ids": [calc["artifact_id"]]}}, {})
    assert data["tables"]["formal"]["rows"][0]["result"]["value"] == "2"


def test_large_results_are_readable_by_bounded_nested_pages(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid, code=CODE + "\nlarge_output = True")
    entry = result["result_entries"][0]
    assert entry["value"]["read_path"] == ["rows"]
    selected = service.get_python_analysis(rid, result["analysis_id"], result_path=["rows"], page=2, page_size=10)
    assert selected["result_entries"][0]["value"] == {"number": 10, "value": 1.0}
    assert selected["next_page"] == 3
    scalar = service.get_python_analysis(rid, result["analysis_id"], result_path=["rows", 10, "value"])
    assert scalar["result_entries"] == [{"key": "value", "value": 1.0}]


def test_full_structured_comparison_preserves_labels_units_and_finite_values():
    assert compare_results({"value": "1.0000000001", "unit": "ratio"}, {"value": 1, "unit": "ratio"})["passed"]
    assert not compare_results({"value": 1, "unit": "CNY"}, {"value": 1, "unit": "ratio"})["passed"]
    assert not compare_results({"a": [1, 2]}, {"a": [1, 2, 3]})["passed"]
    assert not compare_results({"value": True}, {"value": 1})["passed"]
    with pytest.raises(ResearchError, match="nonfinite"):
        compare_results({"value": "NaN"}, {"value": 1})


def test_semantic_unit_change_is_a_new_unvalidated_analysis(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    ratio = run(service, rid)
    other = run(service, rid, output_unit="CNY")
    assert ratio["analysis_id"] != other["analysis_id"]
    assert other["output_unit"] == "CNY" and other["validation_status"] == "unverified"
    assert len(runtime) == 2


def test_output_contract_rejection_is_a_durable_budgeted_failure(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    for expected in range(1, 4):
        result = run(service, rid, code=CODE + "\nnonfinite_output = True", retry=True)
        assert result["status"] == "failed" and result["failure_count"] == expected
        assert result["error"]["code"] == "output_contract_rejected"
    assert len(w.artifacts(rid, "python_run")) == 3
    with pytest.raises(ResearchError, match="budget_exhausted"):
        run(service, rid, code=CODE + "\nnonfinite_output = True", retry=True)
    assert not w.artifacts(rid, "exploration")


def test_per_field_units_require_coverage_at_validation(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    result = run(service, rid, output_unit={"ratio": "ratio"})
    assert validate(service, rid, result)["validation_status"] == "validated"
    absent = run(service, rid, output_unit={"other": "CNY"})
    with pytest.raises(ResearchError, match="output_units_missing:ratio"):
        validate(service, rid, absent)


def test_explicit_retry_can_restore_good_verifier_after_later_failure(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w); result = run(service, rid)
    first = validate(service, rid, result)
    assert first["validation_status"] == "validated"
    failed = validate(service, rid, result, validation_code=INDEPENDENT + "\nbad_result = True")
    assert failed["validation_status"] == "failed"
    assert service.get_python_analysis(rid, result["analysis_id"])["validation_status"] == "failed"
    assert validate(service, rid, result)["validation_status"] == "failed"
    restored = validate(service, rid, result, retry=True)
    assert restored["validation_status"] == "validated" and restored["validation_id"] != first["validation_id"]
    assert get_validated_exploration(w, rid, result["exploration_id"])["validation_id"] == restored["validation_id"]


def test_execution_identity_preserves_readable_docstrings():
    from analysis.research.custom_python import _code_signature
    a = "def f():\n    '''1'''\n    return int(f.__doc__)\nresult={'v':f()}"
    b = a.replace("'''1'''", "'''2'''")
    assert _code_signature(a) != _code_signature(b)
    assert _code_signature(a, ignore_docstrings=True) == _code_signature(b, ignore_docstrings=True)


def test_result_cache_binds_exact_code_even_when_ast_is_same(prepared, runtime):
    w, rid = prepared
    service = CustomPython(w)
    first = run(service, rid)
    changed = run(service, rid, code="# source may inspect line numbers\n" + CODE)
    assert changed["analysis_id"] != first["analysis_id"] and len(runtime) == 2
    assert service.get_python_analysis(rid, changed["analysis_id"], include_code=True)["code"].startswith("# source")
