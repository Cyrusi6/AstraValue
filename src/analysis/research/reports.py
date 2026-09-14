from __future__ import annotations

import base64
import html
import re
from datetime import date
from pathlib import Path

from analysis.models import AssumptionRecord, ClaimRecord, ClaimKind, ReportSection, ResearchRating, ScenarioName, SourceRecord
from analysis.reporting import STEP_TITLES
from .drafts import Drafts
from .workspace import ResearchError, ResearchWorkspace, sha, digest


def bind_valuation_request(request, draft, calculations):
    """Keep assumptions and base valuation in the existing audit and Excel contract."""
    cid = draft["conclusion"]["calculation_id"]
    if not cid:
        return
    calc = next((x for x in calculations if x["artifact_id"] == cid), None)
    if not calc or calc["method"] != "pe_scenarios":
        raise ResearchError("active_valuation_calculation_required")
    labels = {"bear": ScenarioName.BEAR, "base": ScenarioName.BASE, "bull": ScenarioName.BULL}
    source_id = "host-assumption-" + cid
    request.sources.append(SourceRecord(source_id=source_id, name="宿主模型显式估值假设",
        source_type="host-model-assumption",
        document_hash=digest(calc["assumptions"])))
    for row in calc["result"]["scenarios"]:
        for name, unit in (("growth", "ratio"), ("multiple", "times")):
            request.assumptions.append(AssumptionRecord(
                assumption_id=f"{cid}:{row['name']}:{name}", scenario=labels[row["name"]],
                name=name, value=float(row[name]), unit=unit,
                reason=row["reason"] + "；失效条件：" + str(row["invalidation"]),
                source_ids=[source_id], valid_until=date.fromisoformat(row["valid_until"]), confirmed=False))
    base = next(x for x in calc["result"]["scenarios"] if x["name"] == "base")
    request.model_inputs["VAL.RELATIVE"] = {
        "metric_per_share": float(base["eps"]), "target_multiple": float(base["multiple"]),
        "_lineage": {
            "metric_per_share": [f"fact:{calc['input_fact_ids'][key]}" for key in ("earnings", "shares")]
                + [f"assumption:{cid}:base:growth", f"formula:{calc['formula_version']}:forward_eps"],
            "target_multiple": [f"assumption:{cid}:base:multiple"],
        },
    }


def apply_agent_research(report, request):
    """Attach explicit research to the existing ReportVersion before its single storage commit."""
    agent = request.input_metadata.get("agent_research")
    if not agent:
        return report
    draft = agent["draft"]
    if [x["number"] for x in draft["sections"]] != list(range(1,9)) or not draft["conclusion"]:
        raise ResearchError("complete_agent_draft_required")
    if agent["snapshot_id"] != report.request_metadata.get("lite_pack_id"):
        raise ResearchError("agent_report_snapshot_mismatch")
    metrics = {i["fact_ref"]: i for i in report.request_metadata["display_metrics"] if i.get("fact_ref")}
    evidence = {x["evidence_id"]: x for x in agent["evidence"]}
    calculations = {x["artifact_id"]: x for x in agent["calculations"]}
    charts = {x["artifact_id"]: x for x in agent["charts"]}
    allowed = set(metrics) | set(evidence) | set(calculations)
    claims = []
    sections = []
    bodies = []
    cited = []

    def replace(match, images=True):
        kind, ref = match.group(1), match.group(2)
        if kind == "chart":
            if ref not in charts:
                raise ResearchError("unknown_chart_reference:"+ref)
            chart = charts[ref]
            return f"\n\n![{chart['title']}](data:image/png;base64,{chart['image_base64']})\n\n" if images else f"（图：{chart['title']}）"
        if kind == "value":
            if ref not in metrics:
                raise ResearchError("unknown_value_reference:"+ref)
            from analysis.structured.research_lite import _format_number
            item = metrics[ref]
            return _format_number(item["fact"]["value"], item["fact"]["unit"], item["metric_id"])
        if kind == "cite":
            if ref not in allowed:
                raise ResearchError("unknown_citation:"+ref)
            if ref not in cited:
                cited.append(ref)
            return f"[注{cited.index(ref)+1}]"
        if kind == "valuation":
            if ref not in calculations or calculations[ref]["method"] != "pe_scenarios":
                raise ResearchError("unknown_valuation_reference:"+ref)
            rows = calculations[ref]["result"]["scenarios"]
            labels = {"bear":"悲观","base":"基准","bull":"乐观"}
            text = "\n\n| 情景 | 盈利增长假设 | PE假设 | 每股价值 | 相对现价 |\n|---|---:|---:|---:|---:|\n"
            for x in rows:
                text += f"| {labels[x['name']]} | {float(x['growth']):.1%} | {float(x['multiple']):g}倍 | {float(x['fair_value']):.2f}元 | {float(x['upside']):+.1%} |\n"
            return text+"\n"
        raise ResearchError("unknown_report_reference_kind")

    token = re.compile(r"\{\{(\w+):([^{}]+)\}\}")
    for section in draft["sections"]:
        if set(section["evidence_refs"]) - allowed:
            raise ResearchError("unknown_agent_claim_evidence")
        claim = ClaimRecord(ticker=report.ticker, category=list(("business","financial","governance","capital","valuation","risk","industry","scenario"))[section["number"]-1],
            text=section["judgment"], claim_kind=ClaimKind.ANALYST_JUDGEMENT,
            evidence_fact_ids=[metrics[r]["fact"]["fact_id"] for r in section["evidence_refs"] if r in metrics],
            evidence_ids=[r for r in section["evidence_refs"] if r not in metrics],
            counter_evidence=section["counter_evidence"], unknowns=section["unknowns"],
            invalidation_conditions=section["invalidation"], as_of=report.as_of)
        claims.append(claim)
        plain = token.sub(lambda m: replace(m,False),section["markdown"])
        rendered = token.sub(replace,section["markdown"])
        if "{{" in rendered:
            raise ResearchError("malformed_report_reference")
        sections.append(ReportSection(number=section["number"],title=STEP_TITLES[section["number"]-1],summary=plain,claims=[claim]))
        bodies.append(f"## {section['number']}. {STEP_TITLES[section['number']-1]}\n\n{rendered}")
    conclusion = draft["conclusion"]
    report.sections = sections
    report.claims = claims
    report.conclusion.rating = ResearchRating(conclusion["rating"])
    report.conclusion.rating_confirmed = False
    report.conclusion.core_theses = conclusion["theses"]
    report.conclusion.major_risks = conclusion["risks"]
    report.conclusion.invalidation_conditions = conclusion["invalidation"]
    cid = conclusion["calculation_id"]
    if cid:
        if cid not in calculations:
            raise ResearchError("conclusion_calculation_not_in_snapshot")
        result = calculations[cid]["result"]
        values = {x["name"]:float(x["fair_value"]) for x in result["scenarios"]}
        report.conclusion.fair_value_low = values["bear"]
        report.conclusion.fair_value_base = values["base"]
        report.conclusion.fair_value_high = values["bull"]
    report.schema_version = "1.1.0"
    report.request_metadata["agent_body_markdown"] = "\n\n".join(bodies)
    report.request_metadata["agent_summary"] = conclusion["summary"]
    report.request_metadata["agent_citations"] = [{"reference":ref, "detail": metrics.get(ref) or evidence.get(ref) or calculations.get(ref)} for ref in cited]
    report.request_metadata["rating_origin"] = "host_model"
    report.request_metadata["research_status"] = "authored_pending_review"
    report.audit.missing_items = [x for x in report.audit.missing_items if x != "分析对象：尚无带证据的 ClaimRecord"]
    report.request_metadata["agent_calculations"] = list(calculations.values())
    return report


def render_agent_markdown(report):
    meta = report.request_metadata
    c = report.conclusion
    text = f"# {report.company_name}研究报告\n\n研究截止：{report.as_of.date()}　|　研究评级：**{c.rating.value}**\n\n"
    text += meta["agent_summary"] + "\n\n"
    if c.fair_value_base is not None:
        text += f"**基准价值：{c.fair_value_base:.2f}元/股；情景区间：{c.fair_value_low:.2f}—{c.fair_value_high:.2f}元/股。**\n\n"
    if c.current_price is not None:
        text += f"参考价格：{c.current_price:.2f}元；价格记录时点：{c.price_as_of.isoformat() if c.price_as_of else '缺失'}。\n\n"
    text += meta["agent_body_markdown"]
    text += "\n\n## 附录：证据与计算\n\n正文为模型研究判断；历史事实、预测假设和计算结果分别留存。完整财务明细与血缘见同版Excel及JSON。\n\n"
    for i,item in enumerate(meta["agent_citations"],1):
        row = item["detail"]
        if "fact" in row:
            detail = f"{row.get('label',row.get('metric_id'))}，{row.get('period')}，{row.get('period_type')}；事实 {row['fact']['fact_id']}"
        elif "locator" in row:
            detail = f"原文 {row.get('period')}，{row.get('locator')}；{row.get('source_url','')}；原件SHA256 {row.get('original_sha256','')}"
        else:
            detail = f"计算 {row.get('method')}，版本 {row.get('formula_version')}；假设及输入见JSON计算记录。"
        text += f"{i}. {detail}\n\n"
    text += "### 覆盖与未完成项\n\n核心数据、全文研究和报告质量分别验收。完整54题覆盖不等于正文研究完成度；明确资料缺口见各节，人工人读仍待验收。\n"
    return text


def render_agent_html(report):
    import markdown
    # Markdown is agent text, not executable HTML. Images are generated data URIs only.
    body = markdown.markdown(html.escape(render_agent_markdown(report),quote=False),extensions=["tables"])
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>研究报告</title>
<style>@page{size:A4;margin:18mm 18mm 20mm}body{font-family:'Microsoft YaHei',sans-serif;color:#1b2a38;line-height:1.8;max-width:900px;margin:30px auto;font-size:14px}h1{font-size:29px;color:#15384d}h2{font-size:21px;margin-top:32px;break-after:avoid}h3{break-after:avoid}p{orphans:3;widows:3}img{max-width:100%;break-inside:avoid}table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid #dce3e7;padding:8px;text-align:left}th{background:#edf3f5}li{overflow-wrap:anywhere}a{color:#235e79}@media print{body{margin:0;max-width:none}h2{margin-top:22px}img,table{break-inside:avoid}}</style><body>""" + body + "</body></html>"


class Reports:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def build_report(self, research_id: str, formats: list[str] | None = None):
        """Build the existing ReportVersion from agent text. Formats: md, html, xlsx, pdf."""
        from analysis.structured.reporting_bridge import build_report_request
        from analysis.service import AnalysisService
        from analysis.storage import ReportStorage
        from analysis.exports import export_report
        state, path, payload = self.w.pack(research_id)
        draft = Drafts(self.w).get_draft(research_id)
        if draft["missing_sections"] or not draft["conclusion"]:
            return {"status":"analysis_pending","missing_sections":draft["missing_sections"],"missing_conclusion":not bool(draft["conclusion"])}
        formats = formats or ["md","html","xlsx","pdf"]
        if not formats or set(formats)-{"md","html","xlsx","pdf"}:
            raise ResearchError("invalid_export_format")
        charts = []
        for chart in self.w.artifacts(research_id,"chart"):
            image = Path(chart["image_path"])
            if sha(image) != chart["image_sha256"]:
                raise ResearchError("chart_integrity_failed")
            charts.append({**chart,"image_base64":base64.b64encode(image.read_bytes()).decode()})
        request = build_report_request(path)
        request.claims = []
        request.report_notes = "宿主模型自主研究；数据/公式/假设分别记录；人读验收未完成。"
        request.input_metadata["agent_research"] = {"snapshot_id":state["snapshot_id"],"draft":draft,
            "evidence":payload["evidence"],"charts":charts,"calculations":self.w.artifacts(research_id,"calculation")}
        bind_valuation_request(request, draft, request.input_metadata["agent_research"]["calculations"])
        # Use the project's existing report storage, models, timeseries and exports.
        report = AnalysisService(storage=ReportStorage(self.w.state / "reports.sqlite")).create_report(request)
        target = self.w.state / "reports" / report.ticker / report.report_id
        target.mkdir(parents=True,exist_ok=False)
        (target/"report.json").write_text(report.model_dump_json(indent=2),encoding="utf-8")
        outputs = {}
        for fmt in formats:
            output = export_report(report,fmt,target)
            outputs[fmt] = {"path":str(output),"sha256":sha(output)}
        artifact = self.w.artifact(research_id,"report",{"report_id":report.report_id,"report_version":report.version,
            "data_snapshot_id":report.data_snapshot_id,"outputs":outputs,"status":"rendered_pending_review"})
        current, revision = self.w.task(research_id)
        if current["snapshot_id"] != state["snapshot_id"]:
            raise ResearchError("snapshot_changed_during_report_build")
        current["stages"].update(analysis="authored_pending_review", rendering="rendered_pending_review")
        current.update(status="research_pending_review", latest_report_id=report.report_id)
        self.w._save(current, revision)
        return artifact

    def view_report(self, research_id: str, report_id: str, page: int = 1):
        """Render one verified PDF page to PNG; MCP returns the PNG as image content."""
        import fitz
        artifact = next((x for x in self.w.artifacts(research_id,"report") if x["report_id"]==report_id),None)
        if not artifact or "pdf" not in artifact["outputs"]:
            raise ResearchError("pdf_report_not_available")
        file = artifact["outputs"]["pdf"]
        if sha(Path(file["path"]))!=file["sha256"]:
            raise ResearchError("report_integrity_failed")
        with fitz.open(file["path"]) as document:
            if not 1 <= page <= len(document):
                raise ResearchError("report_page_out_of_range")
            image = Path(file["path"]).parent / f"page-{page:03}.png"
            document[page-1].get_pixmap(matrix=fitz.Matrix(1.3,1.3)).save(image)
            return {"path":str(image),"page":page,"total_pages":len(document),"sha256":sha(image)}
