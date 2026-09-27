"""Report admission tests; the synthetic executions below are not financial validation evidence."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_research_workspace import workspace
from analysis.research.charts import Charts
from analysis.research.drafts import Drafts
from analysis.research.reports import (
    apply_agent_research, exploration_value, referenced_custom_artifacts,
    render_agent_html, render_agent_markdown,
)
from analysis.research.workspace import ResearchError, sha


@pytest.fixture
def custom_records(workspace, monkeypatch, tmp_path):
    from PIL import Image
    from analysis.research import custom_python
    state = workspace.prepare_research("600519", "2025-01-01")
    rid, sid = state["research_id"], state["snapshot_id"]
    image = tmp_path / "figure.png"
    Image.new("RGB", (80, 50), "white").save(image)
    code = tmp_path / "code.py"
    code.write_text("# isolated fixture only\n", encoding="utf-8")
    inputs = tmp_path / "input.json"
    inputs.write_text('{"tables": {}, "snapshot_id": "' + sid + '"}', encoding="utf-8")
    values = {"cash_conversion": "0.85"}
    common = {"definition": "累计现金转换比率", "applicability": "正利润期间；不代表未来现金流",
              "output_unit": "ratio", "output_period": "2020—2024", "result": values,
              "input_path": str(inputs), "input_sha256": sha(inputs), "code_path": str(code),
              "code_sha256": sha(code), "code": code.read_text(encoding="utf-8"),
              "provenance": {"input_fact_refs": ["F1"], "calculation_ids": [],
                             "exploration_ids": [], "snapshot_id": sid},
              "files": {"figure.png": {"path": str(image), "sha256": sha(image)},
                        "code.py": {"path": str(code), "sha256": sha(code)}},
              "promotion_state": "not_promoted", "validation_status": "unverified"}
    run = workspace.artifact(rid, "exploration", {**common, "mode": "chart"})
    calc = workspace.artifact(rid, "exploration", {**common, "mode": "calculation"})
    chart = workspace.artifact(rid, "chart", {
        "template": "custom_python", "title": "测试现金转换图", "data": values,
        "image_path": str(image), "image_sha256": sha(image),
        "exploration_id": run["artifact_id"], "template_version": "custom-python-v1",
        "validation": "data_bound; visual_review_required",
    })
    records = {run["artifact_id"]: run, calc["artifact_id"]: calc}
    validations = {}

    def get_run(w, research_id, eid):
        current, _, _ = w.pack(research_id)
        item = records.get(eid)
        if not item or item["research_id"] != research_id or item["snapshot_id"] != current["snapshot_id"]:
            raise ResearchError("exploration_not_in_active_snapshot")
        return deepcopy(item)

    def get_validated(w, research_id, eid):
        item = get_run(w, research_id, eid)
        if item["mode"] != "calculation" or eid not in validations:
            raise ResearchError("exploration_validation_required")
        validation = w.artifact(research_id, "exploration_validation", {
            "exploration_id": eid, "executions": [], **validations[eid]})
        return {**item, "validation_status": "validated", "validation_id": validation["artifact_id"],
                "validation": validation}

    monkeypatch.setattr(custom_python, "get_exploration", get_run)
    monkeypatch.setattr(custom_python, "get_validated_exploration", get_validated)
    return SimpleNamespace(w=workspace, rid=rid, sid=sid, chart=chart, calc=calc, run=run,
                           image=image, validations=validations, get_validated=get_validated)


def section(markdown, refs=None, number=1):
    return dict(number=number, markdown=markdown, judgment="测试判断", evidence_refs=refs or ["F1"],
                counter_evidence=[], unknowns=[], invalidation=[], contract_version="v1")


def test_custom_chart_requires_actual_view_then_explicit_review(custom_records):
    r = custom_records
    c = Charts(r.w)
    cid = r.chart["artifact_id"]
    with pytest.raises(ResearchError, match="must_be_viewed"):
        c.review_custom_chart(r.rid, cid, "坐标轴可读", "底表与输入一致")
    with pytest.raises(ResearchError, match="review_required"):
        c.report_chart(r.rid, cid)
    view = c.view_chart(r.rid, cid)
    assert view["sha256"] == r.chart["image_sha256"]
    with pytest.raises(ResearchError, match="visual_and_data_review"):
        c.review_custom_chart(r.rid, cid, "", "底表与输入一致")
    review = c.review_custom_chart(r.rid, cid, "坐标轴可读且单位明确", "绘图数据与所选期间一致")
    admitted = c.report_chart(r.rid, cid)
    assert admitted["custom_execution"]["code"] == "# isolated fixture only\n"
    assert admitted["custom_review"]["artifact_id"] == review["artifact_id"]
    c.review_custom_chart(r.rid, cid, "发现标签重叠", "数据一致", approved=False)
    with pytest.raises(ResearchError, match="review_required"):
        c.report_chart(r.rid, cid)
    c.review_custom_chart(r.rid, cid, "复核后图形可用", "数据一致")
    assert c.report_chart(r.rid, cid)["custom_review"]["approved"] is True


def test_only_referenced_charts_gate_report_and_tamper_invalidates_review(custom_records):
    r = custom_records
    # An unfinished custom chart and its missing review do not affect unrelated prose.
    assert referenced_custom_artifacts(r.w, r.rid, {"sections": [section("常规正文")]}) == ([], [])
    referenced = {"sections": [section("{{chart:" + r.chart["artifact_id"] + "}}")]}
    with pytest.raises(ResearchError, match="review_required"):
        referenced_custom_artifacts(r.w, r.rid, referenced)
    c = Charts(r.w)
    c.view_chart(r.rid, r.chart["artifact_id"])
    c.review_custom_chart(r.rid, r.chart["artifact_id"], "已查看", "已核对")
    assert len(referenced_custom_artifacts(r.w, r.rid, referenced)[0]) == 1
    r.image.write_bytes(b"modified-image")
    with pytest.raises(ResearchError, match="integrity_failed"):
        referenced_custom_artifacts(r.w, r.rid, referenced)
    assert referenced_custom_artifacts(r.w, r.rid, {"sections": [section("常规正文")]}) == ([], [])


def test_chart_review_cannot_cross_snapshots(custom_records):
    r = custom_records
    c = Charts(r.w)
    c.view_chart(r.rid, r.chart["artifact_id"])
    c.review_custom_chart(r.rid, r.chart["artifact_id"], "已查看", "已核对")
    state, revision = r.w.task(r.rid)
    state["snapshot_id"] = "replacement-snapshot"
    r.w._save(state, revision)
    with pytest.raises(ResearchError, match="not_in_active_snapshot"):
        c.report_chart(r.rid, r.chart["artifact_id"])


def test_standard_chart_still_uses_the_existing_report_path(custom_records):
    r = custom_records
    standard = r.w.artifact(r.rid, "chart", {
        "template": "income_profit", "title": "收入与利润", "data": [{"value": "100"}],
        "image_path": str(r.image), "image_sha256": sha(r.image), "template_version": "research-charts-v1",
    })
    assert Charts(r.w).report_chart(r.rid, standard["artifact_id"])["template"] == "income_profit"
    assert "custom_review" not in Charts(r.w).report_chart(r.rid, standard["artifact_id"])


def test_unverified_exploration_cannot_be_draft_evidence_or_valuation(custom_records):
    r = custom_records
    d = Drafts(r.w)
    eid = r.calc["artifact_id"]
    text = "转换比率为{{explore:" + eid + ".cash_conversion}}。"
    with pytest.raises(ResearchError, match="validation_required"):
        d.save_section(r.rid, r.sid, 2, text, "测试判断", [eid])
    # Execution metadata claiming success is not independent verification.
    r.calc.update(status="completed", success=True)
    with pytest.raises(ResearchError, match="validation_required"):
        referenced_custom_artifacts(r.w, r.rid, {"sections": [section(text, [eid])]})
    r.validations[eid] = {"status": "passed", "independent_recalculation": "fixture", "boundary_cases": ["zero"]}
    saved = d.save_section(r.rid, r.sid, 2, text, "测试判断", [eid])
    assert saved["evidence_refs"] == [eid]
    assert r.w.artifacts(r.rid, "calculation") == []
    with pytest.raises(ResearchError, match="active_valuation_calculation_required"):
        d.save_conclusion(r.rid, r.sid, "中性观察", "测试", ["测试"], ["测试"], ["测试"], eid)
    # A later failed validation removes eligibility when the report is built again.
    del r.validations[eid]
    with pytest.raises(ResearchError, match="validation_required"):
        referenced_custom_artifacts(r.w, r.rid, {"sections": [saved]})


def test_validated_exploration_does_not_cross_snapshots(custom_records):
    r = custom_records
    eid = r.calc["artifact_id"]
    r.validations[eid] = {"status": "passed"}
    draft = {"sections": [section("{{explore:" + eid + ".cash_conversion}}", [eid])]}
    assert len(referenced_custom_artifacts(r.w, r.rid, draft)[1]) == 1
    state, revision = r.w.task(r.rid)
    state["snapshot_id"] = "replacement-snapshot"
    r.w._save(state, revision)
    with pytest.raises(ResearchError, match="not_in_active_snapshot"):
        referenced_custom_artifacts(r.w, r.rid, draft)


@pytest.mark.parametrize("value", [float("nan"), "Infinity", "-Infinity"])
def test_exploratory_scalar_rejects_nonfinite(value):
    with pytest.raises(ResearchError, match="nonfinite"):
        exploration_value({"result": {"x": value}, "output_unit": "ratio"}, "x")


def test_exploratory_values_have_units_and_require_a_numeric_field():
    assert exploration_value({"result": {"x": "0.125"}, "output_unit": "ratio"}, "x") == "12.50%"
    assert exploration_value({"result": {"x": 12.5}, "output_unit": "CNY"}, "x") == "12.5元"
    with pytest.raises(ResearchError, match="unknown_exploration"):
        exploration_value({"result": {"x": 1}}, "missing")
    with pytest.raises(ResearchError, match="numeric_scalar"):
        exploration_value({"result": {"x": [1, 2]}}, "x")


def test_validated_exploration_and_reviewed_chart_survive_report_exports(custom_records, demo_report):
    r = custom_records
    eid, cid = r.calc["artifact_id"], r.chart["artifact_id"]
    r.validations[eid] = {"status": "passed", "independent_recalculation": "fixture", "boundary_cases": ["zero"]}
    c = Charts(r.w)
    c.view_chart(r.rid, cid)
    c.review_custom_chart(r.rid, cid, "曲线及标注可读", "数据与选取期间一致")
    draft = {"sections": [section("现金转换比率{{explore:" + eid + ".cash_conversion}}。{{chart:" + cid + "}}", [eid], 1)] +
                         [section("测试正文。", [eid], n) for n in range(2, 9)],
             "conclusion": {"rating": "中性观察", "summary": "测试观点", "theses": ["测试"],
                            "risks": ["测试"], "invalidation": [], "calculation_id": None}}
    charts, explorations = referenced_custom_artifacts(r.w, r.rid, draft)
    import base64
    charts[0]["image_base64"] = base64.b64encode(r.image.read_bytes()).decode()
    agent = {"snapshot_id": r.sid, "draft": draft, "evidence": [], "calculations": [],
             "charts": charts, "explorations": explorations}
    demo_report.request_metadata.update(lite_pack_id=r.sid, display_metrics=[])
    request = SimpleNamespace(input_metadata={"agent_research": agent})
    report = apply_agent_research(demo_report, request)
    rendered = render_agent_markdown(report)
    assert "85.00%（经验证的探索计算" in rendered
    assert "适用条件与限制：正利润期间" in rendered
    assert "未晋升为标准指标" in rendered
    assert "{{" not in rendered
    assert "data:image/png;base64," in render_agent_html(report)
    assert report.request_metadata["agent_explorations"][0]["validation"]["status"] == "passed"
    saved = report.model_dump(mode="json")["request_metadata"]
    assert saved["agent_charts"][0]["custom_execution"]["code"]
    assert saved["agent_charts"][0]["custom_review"]["approved"]
    assert saved["agent_explorations"][0]["promotion_state"] == "not_promoted"
    assert saved["agent_calculations"] == []


def test_serialized_unverified_result_does_not_bypass_render_gate(demo_report):
    agent = {"snapshot_id": "s", "draft": {"sections": [section("测试", ["exploration_fake"], n) for n in range(1, 9)],
             "conclusion": {"rating": "中性观察", "summary": "测试", "calculation_id": None}},
             "evidence": [], "calculations": [], "charts": [],
             "explorations": [{"artifact_id": "exploration_fake", "mode": "calculation", "snapshot_id": "s",
                               "validation_status": "unverified", "result": {"x": 1}}]}
    demo_report.request_metadata.update(lite_pack_id="s", display_metrics=[])
    with pytest.raises(ResearchError, match="validated_current_exploration_required"):
        apply_agent_research(demo_report, SimpleNamespace(input_metadata={"agent_research": agent}))


def test_agent_excel_keeps_full_prose_global_risk_and_report_images(demo_report, custom_records, tmp_path):
    import base64
    from openpyxl import load_workbook
    from analysis.exports import export_report

    report = deepcopy(demo_report)
    risk = "终端需求恶化时重新评估。"
    for section in report.sections:
        section.summary = f"第{section.number}章完整第一段。\n\n完整第二段：" + "分析依据。" * 110
    report.request_metadata.update(
        agent_body_markdown="![研究图](data:image/png;base64,fixture)", agent_summary="最终研究结论。",
        agent_research={"draft": {"conclusion": {"contract_version": "buy-side-v2", "risk_summary": risk}}},
        agent_charts=[{"title": "研究图", "image_base64": base64.b64encode(custom_records.image.read_bytes()).decode()}],
        agent_calculations=[{"method": "pe_scenarios", "artifact_id": "calculation_fixture", "result": {
            "scenarios": [{"name": "base", "growth": "0.03", "multiple": "20", "eps": "5",
                           "fair_value": "100", "upside": "0.1", "reason": "测试假设"}]}}],
    )
    path = export_report(report, "xlsx", tmp_path)
    workbook = load_workbook(path)
    body = "".join(str(row[1] or "") for row in workbook["八步正文"].values)
    for section in report.sections:
        assert section.summary.replace("\n", "") in body
    assert body.count(risk) == 1
    assert "失效条件" not in str(list(workbook["置顶结论"].values))
    assert "最终研究结论。" in str(list(workbook["置顶结论"].values))
    assert len(workbook["研究图表"]._images) == 1
    assert max(d.height for d in workbook["八步正文"].row_dimensions.values()) < 409
    assert workbook["模型估值结果"]["B2"].value == .03
    assert workbook["模型估值结果"]["B2"].number_format == "0.0%"
