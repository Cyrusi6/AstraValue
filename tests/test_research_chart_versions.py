"""A label/template change must not overwrite an image embedded in an older report."""
from pathlib import Path

from test_research_workspace import workspace
from analysis.research.charts import Charts
from analysis.research.workspace import digest, sha


def test_new_sensitivity_template_preserves_old_image_and_plotting_values(workspace):
    state = workspace.prepare_research("贵州茅台", "2025-01-01")
    rid = state["research_id"]
    data = [dict(growth=g, multiple=m, fair_value=str((1 + g) * m * 10))
            for g in (-.1, .1) for m in (15, 20)]
    calc = workspace.artifact(rid, "calculation", {
        "method": "pe_scenarios", "result": {"sensitivity": data}})
    old_ident = "chart_" + digest([state["snapshot_id"], "valuation_sensitivity", data,
                                  "research-charts-v2"])[:24]
    old = workspace.state / "charts" / (old_ident + ".png")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"frozen previous-version image")
    previous_hash = sha(old)
    charts = Charts(workspace)
    current = charts.create_chart(rid, "valuation_sensitivity", calc["artifact_id"])
    assert current["data"] == data
    assert current["template_version"] == "research-charts-v3"
    assert Path(current["image_path"]) != old
    assert sha(old) == previous_hash
    assert charts.view_chart(rid, current["artifact_id"])["sha256"] == current["image_sha256"]
    repeated = charts.create_chart(rid, "valuation_sensitivity", calc["artifact_id"])
    assert repeated["artifact_id"] == current["artifact_id"]
    assert sha(old) == previous_hash
