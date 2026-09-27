from __future__ import annotations

import base64
import html
import re
from datetime import date, datetime, time, timezone
from pathlib import Path

from analysis.models import AssumptionRecord, ClaimRecord, ClaimKind, ReportSection, ResearchRating, ScenarioName, SourceRecord
from analysis.reporting import STEP_TITLES
from .drafts import Drafts
from .workspace import ResearchError, ResearchWorkspace, sha, digest

REPORT_TOKEN = re.compile(r"\{\{(\w+):([^{}]+)\}\}")


def exploration_value(exploration: dict, field: str):
    """Render a validated exploratory scalar without promoting it into the standard fact namespace."""
    from decimal import Decimal, InvalidOperation
    value = exploration.get("result", {})
    for part in field.split("."):
        if not part or not isinstance(value, dict) or part not in value:
            raise ResearchError("unknown_exploration_value_reference:" + field)
        value = value[part]
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ResearchError("exploration_value_must_be_numeric_scalar")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResearchError("exploration_value_must_be_numeric_scalar") from exc
    if not number.is_finite():
        raise ResearchError("nonfinite_exploration_value")
    unit = exploration.get("output_unit", "")
    if isinstance(unit, dict):
        unit = unit.get(field, "")
    if unit == "ratio":
        return format_ratio(number)
    rendered = format(number, ".12g")
    unit = {"CNY": "元", "shares": "股", "times": "倍", "percent": "%"}.get(unit, unit)
    return rendered + (str(unit) if unit else "")


def referenced_custom_artifacts(workspace, research_id, draft):
    """Load only referenced custom work; an unrelated abandoned experiment never blocks a report."""
    chart_ids, exploration_ids = set(), set()
    for section in draft["sections"]:
        exploration_ids.update(ref for ref in section["evidence_refs"] if ref.startswith("exploration_"))
        for kind, ref in REPORT_TOKEN.findall(section["markdown"]):
            if kind == "chart":
                chart_ids.add(ref)
            elif kind == "explore":
                exploration_ids.add(ref.split(".", 1)[0])
            elif kind == "cite" and ref.startswith("exploration_"):
                exploration_ids.add(ref)
    from .charts import Charts
    charts = [Charts(workspace).report_chart(research_id, key) for key in sorted(chart_ids)]
    explorations = []
    if exploration_ids:
        from .custom_python import get_validated_exploration
        from .custom_archive import archive_exploration
        explorations = [archive_exploration(workspace, research_id, get_validated_exploration(workspace, research_id, key))
                        for key in sorted(exploration_ids)]
    return charts, explorations


def format_ratio(value):
    from decimal import Decimal
    percent=value*100
    return f"{percent:.6g}%" if 0<abs(percent)<Decimal('0.01') else f"{percent:.2f}%"


def bind_valuation_request(request, draft, calculations, storage=None):
    """Keep assumptions and base valuation in the existing audit and Excel contract."""
    cid = draft["conclusion"]["calculation_id"]
    if not cid:
        return
    calc = next((x for x in calculations if x["artifact_id"] == cid), None)
    if not calc or calc["method"] != "pe_scenarios":
        raise ResearchError("active_valuation_calculation_required")
    labels = {"bear": ScenarioName.BEAR, "base": ScenarioName.BASE, "bull": ScenarioName.BULL}
    source_id = "host-assumption-" + cid
    source = SourceRecord(source_id=source_id, name="宿主模型显式估值假设",
        source_type="host-model-assumption",
        document_hash=digest(calc["assumptions"]))
    if storage is not None:
        from analysis.storage import StorageError
        try:
            previous = storage.get_source(source_id)
        except StorageError:
            previous = None
        if previous is not None:
            if previous.model_dump(exclude={"retrieved_at"}) != source.model_dump(exclude={"retrieved_at"}):
                raise ResearchError("assumption_source_conflict")
            source = previous
    request.sources.append(source)
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
    if draft.get("conclusion", {}).get("contract_version") == "buy-side-v2":
        from .authoring import check_prose
        risk = draft["conclusion"].get("risk_summary", "")
        if not risk.strip() or len(re.sub(r"\s", "", risk)) > 300:
            raise ResearchError("risk_summary_requires_1_to_300_nonspace_characters")
        for section in draft["sections"]:
            if section.get("contract_version") != "buy-side-v2":
                raise ResearchError("mixed_writing_contract")
            check_prose(section["markdown"], section["number"])
    if [x["number"] for x in draft["sections"]] != list(range(1,9)) or not draft["conclusion"]:
        raise ResearchError("complete_agent_draft_required")
    if agent["snapshot_id"] != report.request_metadata.get("lite_pack_id"):
        raise ResearchError("agent_report_snapshot_mismatch")
    metrics = {i["fact_ref"]: i for i in report.request_metadata["display_metrics"] if i.get("fact_ref")}
    evidence = {x["evidence_id"]: x for x in agent["evidence"]}
    calculations = {x["artifact_id"]: x for x in agent["calculations"]}
    charts = {x["artifact_id"]: x for x in agent["charts"]}
    explorations = {x["artifact_id"]: x for x in agent.get("explorations", [])}
    for item in explorations.values():
        if (item.get("mode") != "calculation" or item.get("validation_status") != "validated"
                or not item.get("validation_id") or item.get("snapshot_id") != agent["snapshot_id"]):
            raise ResearchError("validated_current_exploration_required")
    allowed = set(metrics) | set(evidence) | set(calculations) | set(explorations)
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
        if kind == "explore":
            if "." not in ref:
                raise ResearchError("exploration_value_field_required")
            eid, field = ref.split(".", 1)
            if eid not in explorations:
                raise ResearchError("unknown_exploration_reference:" + eid)
            result = exploration_value(explorations[eid], field)
            if eid not in cited:
                cited.append(eid)
            return f"{result}（经验证的探索计算，见注{cited.index(eid)+1}）"
        if kind == "value":
            if "." in ref:
                cid, field = ref.rsplit(".", 1)
                calc = calculations.get(cid)
                if not calc or field != "value" or calc.get("result", {}).get("unit") != "ratio":
                    raise ResearchError("unknown_calculation_value_reference:"+ref)
                from decimal import Decimal
                value = Decimal(calc["result"][field])
                if not value.is_finite():
                    raise ResearchError("nonfinite_calculation_value")
                if cid not in cited:
                    cited.append(cid)
                return format_ratio(value)
            if ref not in metrics:
                raise ResearchError("unknown_value_reference:"+ref)
            from analysis.structured.research_lite import _format_number
            item = metrics[ref]
            unit = item["fact"]["unit"]
            if unit in {"shares", "CNY_per_share"}:
                from decimal import Decimal
                number = Decimal(str(item["fact"]["value"]))
                return f"{number:,.0f}股" if unit == "shares" else f"{number:.2f}元/股"
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

    token = REPORT_TOKEN
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
        if section["number"] == 6 and draft["conclusion"].get("contract_version") == "buy-side-v2":
            bodies[-1] += "\n\n### 下行风险与评级失效触发条件\n\n" + draft["conclusion"]["risk_summary"]
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
    # An exploratory evidence reference still needs its method and limitations in the appendix,
    # even when a chapter describes the finding without interpolating a numeric field.
    cited.extend(ref for ref in explorations if ref not in cited)
    report.request_metadata["agent_citations"] = [{"reference":ref, "detail": metrics.get(ref) or evidence.get(ref) or calculations.get(ref) or explorations.get(ref)} for ref in cited]
    report.request_metadata["rating_origin"] = "host_model"
    report.request_metadata["research_status"] = "authored_pending_review"
    report.request_metadata["research_progress"] = {"authored_sections":len(sections), "required_sections":8,
        "linked_claims":len(claims), "full_question_data_status":report.research_coverage.get("overall_status"),
        "human_acceptance":"pending"}
    report.conclusion.evidence_completeness = len(claims)/len(sections)
    report.request_metadata["evidence_completeness_scope"] = "已撰写判断的有效引用覆盖；不是54题数据完整度"
    if cid:
        actual_date = calculations[cid]["result"]["price_fact_period"]
        report.conclusion.price_as_of = datetime.combine(date.fromisoformat(actual_date),time.min,tzinfo=timezone.utc)
        report.request_metadata["price_time_precision"] = "date_only"
    report.audit.missing_items = [x for x in report.audit.missing_items if x != "分析对象：尚无带证据的 ClaimRecord"]
    report.request_metadata["agent_calculations"] = list(calculations.values())
    report.request_metadata["agent_explorations"] = list(explorations.values())
    report.request_metadata["agent_charts"] = list(charts.values())
    return report


def render_agent_markdown(report):
    meta = report.request_metadata
    c = report.conclusion
    text = f"# {report.company_name}研究报告\n\n研究截止：{report.as_of.date()}　|　研究评级：**{c.rating.value}**\n\n"
    text += meta["agent_summary"] + "\n\n"
    if c.fair_value_base is not None:
        text += f"**基准价值：{c.fair_value_base:.2f}元/股；情景区间：{c.fair_value_low:.2f}—{c.fair_value_high:.2f}元/股。**\n\n"
    if c.current_price is not None:
        text += f"参考价格：{c.current_price:.2f}元；行情日期：{c.price_as_of.date().isoformat() if c.price_as_of else '缺失'}。\n\n"
    text += meta["agent_body_markdown"]
    text += "\n\n## 附录：证据与计算\n\n完整财务明细、计算假设和来源定位见同版Excel及JSON。\n\n"
    for i,item in enumerate(meta["agent_citations"],1):
        row = item["detail"]
        if row.get("mode") == "calculation" and row.get("validation_status") == "validated":
            units = row.get('output_unit', '')
            unit_labels = {'CNY': '人民币元', 'ratio': '比例（正文以百分比表示）', 'shares': '股', 'times': '倍'}
            if isinstance(units, dict):
                units = '、'.join(dict.fromkeys(unit_labels.get(unit, unit) for unit in units.values()))
            else:
                units = unit_labels.get(units, units)
            detail = (f"经验证的探索计算：{row.get('definition', row.get('purpose', '自定义方法')).rstrip('。；;')}；"
                      f"适用条件与限制：{row.get('applicability', '').rstrip('。；;')}；"
                      f"结果单位：{units}；结果期间：{row.get('output_period', '')}。"
                      "代码、只读输入、独立复算、边界用例及验证记录保存在同版JSON；未晋升为标准指标。")
        elif "fact" in row:
            detail = f"{row.get('label',row.get('metric_id'))}，实际数据期 {row['fact'].get('period_end') or row.get('period')}；标准值与来源见同版事实索引。"
        elif "locator" in row:
            locator=row.get('locator','')
            if locator.startswith('pages:'): locator='第'+locator[6:].replace(',','、')+'页'
            elif locator.startswith('page:'): locator='第'+locator[5:]+'页'
            elif locator.startswith('json:'): locator='结构化记录，精确定位见JSON'
            title=row.get('title') or '公司披露原文'
            for key,label in {"channel_inventory":"渠道库存","platform_transactions":"线上交易","product_mix":"产品结构"}.items():
                title=title.replace(key,label)
            period = str(row.get('period') or '')
            location = '，'.join(part for part in (period, locator) if part)
            detail = f"[{title}]({row.get('source_url','')})" + (f"，{location}" if location else '') + '。'
        else:
            labels = {'financial_summary':'历史增速与合并口径杜邦计算','pe_scenarios':'盈利情景、每股价值与敏感性计算',
                      'inventory_composition':'存货构成计算','inventory_allowance_ratio':'存货减值准备率计算',
                      'price_change':'同一产品销售合同价累计调整幅度','payout_ratio':'年度现金分红率计算',
                      'dividend_yield':'历史分红按参考价格折算的股息率','forecast_dividend_yield':'未来十二个月股息率情景计算'}
            label=labels.get(row.get('method'),row.get('method') or row.get('title') or '补充证据')
            detail = f"{label}；公式、输入、假设及版本见同版计算记录。"
        text += f"{i}. {detail}\n\n"
    for chart in meta.get("agent_charts", []):
        if chart.get("template") == "custom_python":
            text += (f"定制图《{chart['title']}》：当前快照数据，经模型查看图片并核对绘图数据；"
                     "代码、绘图底表、来源与检查记录见同版JSON。\n\n")
            for source in chart.get("validated_exploration_sources", []):
                if any(item["reference"] == source.get("artifact_id") for item in meta["agent_citations"]):
                    continue
                text += (f"该图使用经验证的探索计算：{source.get('definition', '')}；"
                         f"适用条件与限制：{source.get('applicability', '')}。\n\n")
    return text


def render_agent_html(report):
    import markdown
    # Markdown is agent text, not executable HTML. Images are generated data URIs only.
    body = markdown.markdown(html.escape(render_agent_markdown(report),quote=False),extensions=["tables"])
    body = re.sub(r"([。；])((?:\[注\d+\])+)",r'<span style="white-space:nowrap">\1\2</span>',body)
    return """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>研究报告</title>
<style>@page{size:A4;margin:18mm 18mm 20mm}body{font-family:'Microsoft YaHei',sans-serif;color:#1b2a38;line-height:1.8;max-width:900px;margin:30px auto;font-size:14px}h1{font-size:29px;color:#15384d}h2{font-size:21px;margin-top:32px;break-after:avoid}h3{break-after:avoid}p{orphans:3;widows:3;break-inside:avoid}img{max-width:100%;break-inside:avoid}table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid #dce3e7;padding:8px;text-align:left}th{background:#edf3f5}li{overflow-wrap:anywhere}a{color:#235e79}@media print{body{margin:0;max-width:none}h2{margin-top:22px}img,table{break-inside:avoid}}</style><body>""" + body + "</body></html>"


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
        charts, explorations = referenced_custom_artifacts(self.w, research_id, draft)
        rendered_charts = []
        for chart in charts:
            image = Path(chart["image_path"])
            if sha(image) != chart["image_sha256"]:
                raise ResearchError("chart_integrity_failed")
            rendered_charts.append({**chart,"image_base64":base64.b64encode(image.read_bytes()).decode()})
        request = build_report_request(path)
        request.claims = []
        request.report_notes = "宿主模型自主研究；数据/公式/假设分别记录；人读验收未完成。"
        evidence = payload["evidence"] + payload.get("supplemental_evidence", [])
        evidence += [dict(x, evidence_id=x["artifact_id"])
                     for x in self.w.artifacts(research_id, "evidence_read")]
        evidence += [dict(x, evidence_id=x["artifact_id"])
                     for x in self.w.business_profiles(research_id)]
        request.input_metadata["agent_research"] = {"snapshot_id": state["snapshot_id"], "draft": draft,
            "evidence": evidence,
            "charts":rendered_charts,"calculations":self.w.artifacts(research_id,"calculation"),
            "explorations":explorations}
        request.input_metadata["processing_audit"] = payload.get("processing_audit")
        request.input_metadata["organized_disclosures"] = payload.get("organized_disclosures", [])
        request.input_metadata["processing_attachments"] = payload.get("processing_attachments", [])
        storage = ReportStorage(self.w.state / "reports.sqlite")
        bind_valuation_request(request, draft, request.input_metadata["agent_research"]["calculations"], storage)
        # Use the project's existing report storage, models, timeseries and exports.
        report = AnalysisService(storage=storage).create_report(request)
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
