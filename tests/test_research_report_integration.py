"""Real-cache integration; test prose never constitutes host research or golden acceptance."""
import json
import os
from pathlib import Path

import pytest

from analysis.research.calculations import Calculations
from analysis.research.drafts import Drafts
from analysis.research.reports import Reports
from analysis.research.workspace import ResearchError, ResearchWorkspace, sha


@pytest.mark.skipif(os.environ.get("ASTRAVALUE_REAL_CACHE_TEST") != "1", reason="explicit local real-cache opt-in")
def test_real_frozen_pack_to_existing_report_and_exports(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config/research_workspace.json").read_text(encoding="utf-8"))
    config["state_root"] = str(tmp_path / "state")
    w = ResearchWorkspace(root, config)
    prepared = w.prepare_research("600519", "2026-09-13")
    rid, sid = prepared["research_id"], prepared["snapshot_id"]
    _, path, pack = w.pack(rid)
    before = sha(path / "manifest.json")
    calc = Calculations(w).calculate(rid, "pe_scenarios", {"earnings":"F199", "shares":"F211", "price":"F291"}, {
        "scenarios": {"reason":"仅用于接口复算测试", "value":[dict(name=n,growth=g,multiple=m,reason="测试假设，非研究结论",valid_until="2026-12-31",invalidation=["测试变化"])
            for n,g,m in [("bear",-.1,15),("base",0,20),("bull",.1,25)]]},
        "sensitivity_growth":{"value":[-.1,0,.1],"reason":"测试"},
        "sensitivity_multiples":{"value":[15,20,25],"reason":"测试"}})
    drafts = Drafts(w)
    for n in range(1,9):
        drafts.save_section(rid,sid,n,"接口测试正文，非公司研究。数值：{{value:F199}}。{{cite:F199}}",
                            "接口测试判断，不供投资分析",["F199"],["测试条件变化"])
    drafts.save_conclusion(rid,sid,"中性观察","仅用于验证同版导出，不是黄金候选",["测试"],["测试"],["测试"],calc["artifact_id"])
    report = Reports(w).build_report(rid, ["md","html","xlsx"])
    assert report["status"] == "rendered_pending_review"
    folder = Path(report["outputs"]["md"]["path"]).parent
    saved = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert len(saved["claims"]) == 8
    assert len(saved["assumptions"]) == 6
    assert saved["audit"]["model_runs"][0]["status"] == "成功"
    assert len(saved["audit"]["model_runs"]) == 1
    assert saved["model_inputs"]["VAL.RELATIVE"]["_lineage"]["target_multiple"]
    assert "{{" not in Path(report["outputs"]["md"]["path"]).read_text(encoding="utf-8")
    assert sha(path / "manifest.json") == before
    assert w.get_task(rid)["stages"]["rendering"] == "rendered_pending_review"
    drafts.save_section(rid,sid,1,"未知引用 {{cite:missing}}", "测试", ["F199"], ["测试"])
    with pytest.raises(ResearchError, match="unknown_citation"):
        Reports(w).build_report(rid, ["md"])
