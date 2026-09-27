import { useEffect, useMemo, useState } from "react";

const API = "/api";
const STATUS_CLASS = {
  "双源一致": "verified", "权威单源": "single", "供应商直采": "verified", "待核验": "pending", "估算": "estimated",
  "未披露": "muted", "暂无该数据": "muted", "不适用": "muted",
};

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch { /* no-op */ }
    throw new Error(detail);
  }
  return response.json();
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "暂无该数据";
  if (typeof value !== "number") return String(value);
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function pct(value) {
  return value === null || value === undefined ? "暂无该数据" : `${(value * 100).toFixed(1)}%`;
}

function Icon({ name }) {
  const paths = {
    dashboard: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
    reports: "M6 2h9l5 5v15H6V2Zm8 2v5h4M9 13h8M9 17h8M9 9h2",
    methods: "M4 5h16M4 12h16M4 19h16M8 3v4M16 10v4M11 17v4",
    data: "M4 6c0-2 3.6-3 8-3s8 1 8 3-3.6 3-8 3-8-1-8-3Zm0 0v6c0 2 3.6 3 8 3s8-1 8-3V6m-16 6v6c0 2 3.6 3 8 3s8-1 8-3v-6",
    plus: "M12 5v14M5 12h14",
    refresh: "M20 6v6h-6M4 18v-6h6M6.5 8a7 7 0 0 1 11.2-1.8L20 9M4 15l2.3 2.7A7 7 0 0 0 17.5 16",
    arrow: "m9 18 6-6-6-6",
    shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-10 2 2 4-5",
    export: "M12 3v12m0 0 4-4m-4 4-4-4M5 17v4h14v-4",
  };
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name]} /></svg>;
}

function App() {
  const [view, setView] = useState("dashboard");
  const [reports, setReports] = useState([]);
  const [methods, setMethods] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");

  async function loadAll() {
    setLoading(true); setError("");
    try {
      const [reportData, methodData] = await Promise.all([api("/reports"), api("/methods")]);
      setReports(reportData); setMethods(methodData);
      if (selected) {
        const refreshed = reportData.find((item) => item.report_id === selected.report_id);
        if (refreshed) setSelected(refreshed);
      }
    } catch (err) { setError(err.message); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadAll(); }, []);
  useEffect(() => { if (toast) { const timer = setTimeout(() => setToast(""), 3200); return () => clearTimeout(timer); } }, [toast]);

  function openReport(report) { setSelected(report); setView("report"); }
  const activeReport = selected || reports[0];

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">八</div><div><strong>财报研判</strong><small>A-SHARE LAB</small></div></div>
      <nav>
        <NavButton icon="dashboard" label="研究概览" active={view === "dashboard"} onClick={() => setView("dashboard")} />
        <NavButton icon="reports" label="报告档案" active={view === "reports" || view === "report"} onClick={() => setView("reports")} />
        <NavButton icon="methods" label="方法库" active={view === "methods"} onClick={() => setView("methods")} />
        <NavButton icon="data" label="数据与公告" active={view === "data"} onClick={() => setView("data")} />
      </nav>
      <div className="sidebar-note"><Icon name="shield" /><p>只生成研究评级和估值区间，不生成自动交易指令。</p></div>
    </aside>
    <main className="workspace">
      <header className="topbar">
        <div><p className="eyebrow">LOCAL RESEARCH WORKSPACE</p><h1>{titleFor(view, activeReport)}</h1></div>
        <div className="top-actions"><button className="ghost" onClick={loadAll}><Icon name="refresh" />刷新</button></div>
      </header>
      {error && <div className="error-banner"><strong>无法完成请求</strong><span>{error}</span><button onClick={() => setError("")}>×</button></div>}
      {loading ? <Loading /> : <>
        {view === "dashboard" && <Dashboard reports={reports} methods={methods} onOpen={openReport} />}
        {view === "reports" && <ReportArchive reports={reports} onOpen={openReport} />}
        {view === "report" && activeReport && <ReportView report={activeReport} />}
        {view === "methods" && <MethodLibrary methods={methods} />}
        {view === "data" && <DataDesk notify={setToast} onError={(message) => setError(message)} />}
      </>}
    </main>
    {toast && <div className="toast">{toast}</div>}
  </div>;
}

function NavButton({ icon, label, active, onClick }) {
  return <button className={active ? "active" : ""} onClick={onClick}><Icon name={icon} /><span>{label}</span></button>;
}

function Dashboard({ reports, methods, onOpen }) {
  const latest = reports.slice(0, 4);
  const reviewed = reports.filter((item) => item.status === "已复核").length;
  const conflicts = reports.filter((item) => item.audit?.conflicts?.length).length;
  return <div className="page-stack">
    <section className="intro-panel"><div><p className="eyebrow">EVIDENCE BEFORE NARRATIVE</p><h2>从置顶结论回溯到每一个事实</h2><p>严格八步正文、分级核验、冻结快照与方法版本共同构成可复算的研究档案。</p></div><div className="intro-index">08<span>STEP</span></div></section>
    <section className="metric-grid">
      <Metric label="报告版本" value={reports.length} note={`${reviewed} 份已复核`} />
      <Metric label="活动方法" value={methods.length} note="文档当前为骨架状态" />
      <Metric label="含冲突报告" value={conflicts} note="冲突不进入确定性估值" tone={conflicts ? "warn" : "good"} />
      <Metric label="导出格式" value="4" note="网页 / Markdown / Excel / PDF" />
    </section>
    <section className="panel"><PanelTitle title="最近报告" meta="按创建时间倒序" />
      {latest.length ? <div className="report-list">{latest.map((report) => <ReportRow key={report.report_id} report={report} onClick={() => onOpen(report)} />)}</div> : <Empty title="尚无报告" text="先在数据与公告中完成结构化采集，再由研究工作区生成 ReportVersion。" />}
    </section>
    <EightStepStrip />
  </div>;
}

function Metric({ label, value, note, tone = "" }) { return <article className={`metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>; }

function ReportArchive({ reports, onOpen }) {
  const [query, setQuery] = useState("");
  const filtered = reports.filter((item) => `${item.ticker}${item.company_name}${item.industry}`.toLowerCase().includes(query.toLowerCase()));
  return <section className="panel archive"><PanelTitle title="报告档案" meta={`${filtered.length} 个不可覆盖版本`} /><input className="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索代码、公司或行业" />
    {filtered.length ? <div className="report-list">{filtered.map((report) => <ReportRow key={report.report_id} report={report} onClick={() => onOpen(report)} />)}</div> : <Empty title="没有匹配报告" text="请调整搜索条件。" />}
  </section>;
}

function ReportRow({ report, onClick }) {
  return <button className="report-row" onClick={onClick}><div className="ticker-box">{report.ticker.slice(0, 6)}</div><div className="report-main"><strong>{report.company_name}</strong><span>{report.industry} · v{report.version} · 数据截止 {new Date(report.as_of).toLocaleDateString("zh-CN")}</span></div><div className="report-rating"><StatusBadge value={report.status} /><strong>{report.conclusion.rating}</strong></div><div className="report-value"><span>基准价值</span><strong>{fmt(report.conclusion.fair_value_base)}</strong></div><Icon name="arrow" /></button>;
}

function ReportView({ report }) {
  const [openStep, setOpenStep] = useState(1);
  const [tab, setTab] = useState("report");
  const c = report.conclusion;

  return <div className="page-stack report-page">
    <section className="report-hero"><div><div className="report-kicker"><StatusBadge value={report.status} /><span>v{report.version}</span><span>{report.industry}</span></div><h2>{report.company_name}<small>{report.ticker}</small></h2><p>数据截止 {new Date(report.as_of).toLocaleString("zh-CN")} · 快照 {report.data_snapshot_id}</p></div></section>
    <section className="conclusion-card"><div className="conclusion-title"><div><p className="eyebrow">PINNED CONCLUSION</p><h3>置顶结论</h3></div><div className="rating"><span>研究评级</span><strong>{c.rating}</strong><small>{c.rating_confirmed ? "用户已确认" : "待用户确认"}</small></div></div>
      <div className="kpi-row"><Kpi label="当前价格" value={fmt(c.current_price)} /><Kpi label="合理价值区间" value={`${fmt(c.fair_value_low)} — ${fmt(c.fair_value_high)}`} note={`基准 ${fmt(c.fair_value_base)}`} /><Kpi label="安全边际" value={pct(c.margin_of_safety)} /><Kpi label="证据完整度" value={pct(c.evidence_completeness)} note={`可信度 ${pct(c.evidence_confidence)}`} /></div>
      <div className="thesis-grid"><TextList title="核心逻辑" items={c.core_theses} /><TextList title="主要风险" items={c.major_risks} tone="risk" /><TextList title="失效条件" items={c.invalidation_conditions} /></div>
    </section>
    <div className="tabs"><button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}>八步正文</button><button className={tab === "coverage" ? "active" : ""} onClick={() => setTab("coverage")}>八步数据覆盖</button><button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>审计附录</button><button className={tab === "export" ? "active" : ""} onClick={() => setTab("export")}>导出</button></div>
    {tab === "report" && <section className="steps-panel">{report.sections.map((section) => <Step key={section.number} section={section} open={openStep === section.number} onClick={() => setOpenStep(openStep === section.number ? 0 : section.number)} />)}</section>}
    {tab === "coverage" && <ResearchCoverage report={report} />}
    {tab === "audit" && <Audit report={report} />}
    {tab === "export" && <ExportPanel report={report} />}
  </div>;
}

function Kpi({ label, value, note }) { return <article><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</article>; }
function TextList({ title, items, tone = "" }) { return <div className={tone}><h4>{title}</h4><ul>{(items?.length ? items : ["暂无该数据"]).map((item, i) => <li key={i}>{item}</li>)}</ul></div>; }

function Step({ section, open, onClick }) {
  return <article className={`step ${open ? "open" : ""}`}><button className="step-head" onClick={onClick}><span>0{section.number}</span><div><h3>{section.title}</h3><p>{section.summary}</p></div><Icon name="arrow" /></button>{open && <div className="step-body">
    <div className="method-chips">{section.method_refs.map((item) => <code key={item}>{item}</code>)}</div>
    {section.claims.length > 0 && <div className="claim-list">{section.claims.map((claim) => <div key={claim.claim_id}><span>{claim.claim_kind}</span><p>{claim.text}</p><small>{[...claim.evidence_fact_ids.map((id) => `fact:${id}`), ...claim.evidence_source_ids.map((id) => `source:${id}`)].join(" · ") || "分析判断"}</small></div>)}</div>}
    {section.facts.length > 0 && <details><summary>查看 {section.facts.length} 条结构化事实</summary><ul className="fact-list">{section.facts.map((fact, i) => <li key={i}>{fact}</li>)}</ul></details>}
    {section.tables.map((table, i) => <DataTable key={`${table.name}-${i}`} title={table.name} rows={table.rows} />)}
    {section.warnings.length > 0 && <div className="warning-box"><strong>注意事项</strong>{section.warnings.map((item, i) => <p key={i}>{item}</p>)}</div>}
  </div>}</article>;
}

function DataTable({ title, rows }) {
  const normalized = Array.isArray(rows) ? rows : Object.entries(rows || {}).map(([key, value]) => ({ metric_id: key, value }));
  if (!normalized.length) return <div className="data-block"><h4>{title}</h4><p className="muted">暂无该数据</p></div>;
  const columns = [...new Set(normalized.flatMap((row) => Object.keys(row || {})))].slice(0, 8);
  return <div className="data-block"><h4>{title}</h4><div className="table-wrap"><table><thead><tr>{columns.map((col) => <th key={col}>{col}</th>)}</tr></thead><tbody>{normalized.slice(0, 50).map((row, i) => <tr key={i}>{columns.map((col) => <td key={col}>{typeof row[col] === "object" ? JSON.stringify(row[col], null, 0) : fmt(row[col], 4)}</td>)}</tr>)}</tbody></table></div>{normalized.length > 50 && <small>仅在页面展示前 50 行，完整内容见导出与审计对象。</small>}</div>;
}

function Audit({ report }) {
  const audit = report.audit;
  return <section className="panel audit-panel"><div className="audit-summary"><Metric label="来源" value={audit.sources.length} note="独立上游以 source_id 追踪" /><Metric label="冲突" value={audit.conflicts.length} note="不得静默取平均" tone={audit.conflicts.length ? "warn" : "good"} /><Metric label="缺失" value={audit.missing_items.length} note="明确标注，不由模型补造" /><Metric label="方法" value={audit.method_bundle.methods.length} note={report.method_bundle_id} /></div>
    <DataTable title="来源清单" rows={audit.sources} /><DataTable title="模型运行与输入血缘" rows={audit.model_runs} /><DataTable title="数据质量检查" rows={audit.data_quality_checks} /><DataTable title="双源核验记录" rows={audit.verification_records} />
    <div className="audit-notes"><TextList title="冲突" items={audit.conflicts} tone="risk" /><TextList title="缺失项" items={audit.missing_items} /><TextList title="人工修订与版本变化" items={[...audit.manual_edits, ...audit.version_changes]} /></div>
  </section>;
}

function ResearchCoverage({ report }) {
  const coverage = report.research_coverage || {};
  const questions = coverage.questions || coverage.question_coverage || [];
  const steps = coverage.steps || coverage.step_coverage || [];
  const byStep = new Map(steps.map((item) => [item.step_id, item]));
  const fixedSteps = ["ES01", "ES02", "ES03", "ES04", "ES05", "ES06", "ES07", "ES08"];
  const questionRows = questions.map((item) => ({
    question_id: item.question_id,
    step_id: item.step_id,
    数据状态: item.state || item.status || "pending",
    适用性: item.applicability || "unknown",
    必需项: item.required_requirement_ids || [...(item.ready_requirement_ids || []), ...(item.missing_requirement_ids || [])],
    已得: item.ready_requirement_ids || [],
    待补: item.missing_requirement_ids || [],
    不适用: item.not_applicable_requirement_ids || (item.state === "not_applicable" ? ["问题整体不适用"] : []),
    可选增强: { ready: item.optional_ready_ids || [], missing: item.optional_missing_ids || [] },
    补充路径: item.next_paths || item.supplement_paths || [],
    方法状态: item.method_status || "unknown",
    假设状态: item.assumption_status || "unknown",
    研究状态: item.analysis_status || "not_started",
  }));
  return <section className="panel audit-panel"><PanelTitle title="八步数据覆盖" meta={`覆盖快照 ${report.research_coverage_snapshot_id || "该版本未评估"}`} />
    <p className="muted">数据就绪与证据完整度、方法成熟度、假设确认和人工验收分别展示；缺少覆盖快照时不会由旧 evidence_scores 推定八步就绪。</p>
    <div className="step-strip">{fixedSteps.map((stepId, index) => { const item = byStep.get(stepId); return <div key={stepId}><span>0{index + 1}</span><strong>{stepId} · {item?.state || item?.status || "未评估"}</strong><small>{item ? `可分析 ${(item.ready_question_ids || []).length} · 待补 ${(item.pending_question_ids || []).length} · 不适用 ${(item.not_applicable_question_ids || []).length}` : "无冻结逐题结果"}</small></div>; })}</div>
    <DataTable title="逐题覆盖明细" rows={questionRows} />
    <DataTable title="研究窗口" rows={[coverage.analysis_scope || { status: "未评估" }]} />
  </section>;
}

function ExportPanel({ report }) {
  return <section className="panel export-panel"><PanelTitle title="统一结果对象导出" meta="四种格式不重复计算核心数字" /><div className="export-grid">{[["html", "网页报告", "适合本地阅读与打印"], ["md", "Markdown", "适合归档和二次编辑"], ["xlsx", "Excel模型", "保留假设与公式复核"], ["pdf", "PDF报告", "由最终网页打印生成"]].map(([fmtName, label, desc]) => <a key={fmtName} href={`${API}/reports/${report.report_id}/exports/${fmtName}`}><Icon name="export" /><div><strong>{label}</strong><span>{desc}</span></div></a>)}</div></section>;
}

function MethodLibrary({ methods }) {
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState(null);
  const groups = { STEP: "八步总纲", FIN: "财务子模型", VAL: "估值模型", IND: "行业手册" };
  const filtered = filter === "all" ? methods : methods.filter((item) => item.method_id.startsWith(`${filter}.`));
  return <div className="method-layout"><section className="panel method-list"><PanelTitle title="方法库" meta={`${methods.length} 个活动版本 · 正文待后续填充`} /><div className="filter-row"><button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部</button>{Object.entries(groups).map(([key, label]) => <button key={key} className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>{label}</button>)}</div><div className="method-cards">{filtered.map((method) => <button key={method.method_ref} className={selected?.method_ref === method.method_ref ? "selected" : ""} onClick={() => setSelected(method)}><div><code>{method.method_ref}</code><StatusBadge value="骨架" /></div><strong>{groups[method.method_id.split(".")[0]] || "方法"}</strong><span>{method.doc_path}</span></button>)}</div></section>{selected && <aside className="panel method-detail"><button className="close" onClick={() => setSelected(null)}>×</button><p className="eyebrow">METHOD SPEC</p><h2>{selected.method_ref}</h2><StatusBadge value="内容待补充" /><dl><dt>适用行业</dt><dd>{selected.applies_to.join("、")}</dd><dt>必需输入</dt><dd>{selected.required_inputs.join("、") || "无"}</dd><dt>强制停止</dt><dd>{selected.stop_conditions.join("；") || "无"}</dd><dt>缺失策略</dt><dd>{selected.missing_policy}</dd><dt>实现</dt><dd><code>{selected.implementation_ref}</code></dd><dt>内容哈希</dt><dd className="hash">{selected.content_hash}</dd></dl><p className="skeleton-warning">方法 Markdown 当前只有结构骨架；请在专项研究完成前勿将其视作成熟投研方法。</p></aside>}</div>;
}

function DataDesk({ notify, onError }) {
  const [ticker, setTicker] = useState("");
  const [result, setResult] = useState(null);
  const [sourceDefinitions, setSourceDefinitions] = useState([]);
  const [acquisitionRuns, setAcquisitionRuns] = useState([]);
  const [acquisitionMode, setAcquisitionMode] = useState("baseline");
  const [acquisitionBusy, setAcquisitionBusy] = useState(false);
  const [structuredMode, setStructuredMode] = useState("incremental");
  const [companyScope, setCompanyScope] = useState("company-only");
  const [datasetIds, setDatasetIds] = useState("");
  const [structuredRunIds, setStructuredRunIds] = useState([]);
  const [structuredBusy, setStructuredBusy] = useState(false);
  const [document, setDocument] = useState({ ticker: "", path: "", title: "", source_name: "巨潮/交易所正式文件" });
  useEffect(() => {
    api("/source-definitions?scope=business_model")
      .then(setSourceDefinitions)
      .catch((err) => onError(err.message));
  }, []);

  async function refreshAcquisitionRuns(targetTicker = ticker) {
    if (!targetTicker) return;
    try {
      setAcquisitionRuns(await api(`/acquisition-runs?ticker=${encodeURIComponent(targetTicker)}`));
    } catch (err) { onError(err.message); }
  }

  async function createAcquisitionRun() {
    setAcquisitionBusy(true);
    try {
      const run = await api(`/companies/${ticker}/acquisition-runs`, {
        method: "POST",
        body: JSON.stringify({ mode: acquisitionMode, as_of: new Date().toISOString() }),
      });
      setResult(run);
      await refreshAcquisitionRuns(ticker);
      notify("采集计划已冻结；尚未执行外部请求");
    } catch (err) { onError(err.message); }
    finally { setAcquisitionBusy(false); }
  }

  async function executeAcquisitionRun(runId) {
    setAcquisitionBusy(true);
    try {
      const run = await api(`/acquisition-runs/${runId}/execute`, { method: "POST" });
      setResult(run);
      await refreshAcquisitionRuns(ticker);
      notify("采集执行已结束；请核对 coverage 与材料缺口");
    } catch (err) { onError(err.message); }
    finally { setAcquisitionBusy(false); }
  }

  async function structuredPlan() {
    setStructuredBusy(true);
    try {
      const datasets = datasetIds.split(",").map((item) => item.trim()).filter(Boolean);
      const plan = await api("/structured/plans", {
        method: "POST",
        body: JSON.stringify({ ticker, mode: structuredMode, company_scope: companyScope, datasets, research_profile_id: "eight-step-lite-v1.0.0" }),
      });
      setResult(plan);
      setStructuredRunIds(plan.run_ids || []);
      notify("结构化计划已冻结；请显式执行或恢复运行");
    } catch (err) { onError(err.message); }
    finally { setStructuredBusy(false); }
  }
  async function executeStructuredRun(runId, resume = false) {
    setStructuredBusy(true);
    try {
      const path = `/structured/runs/${runId}/${resume ? "resume" : "execute"}`;
      const run = await api(path, { method: "POST" });
      setResult(run);
      notify(resume ? "结构化运行已恢复" : "结构化运行已执行");
    } catch (err) { onError(err.message); }
    finally { setStructuredBusy(false); }
  }
  async function ingest() { try { const res = await api("/documents", { method: "POST", body: JSON.stringify(document) }); setResult(res); notify("公告已按哈希归档并建立全文索引"); } catch (err) { onError(err.message); } }
  return <div className="data-grid"><section className="panel"><PanelTitle title="结构化字段采集" meta="唯一常规入口 · 计划、执行、恢复均可审计" /><label>股票代码<input value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="例如 600519" /></label><label>运行模式<select value={structuredMode} onChange={(e) => setStructuredMode(e.target.value)}><option value="baseline">baseline · 适用数据基线</option><option value="incremental">incremental · 新期间与重叠窗口</option><option value="due">due · 仅本轮到期任务</option></select></label><label>公司范围<select value={companyScope} onChange={(e) => setCompanyScope(e.target.value)}><option value="company-only">仅目标公司</option><option value="company-with-peers">目标与有界同行</option><option value="peer-set">首批七家公司集合</option></select></label><label>数据集子集（可选）<input value={datasetIds} onChange={(e) => setDatasetIds(e.target.value)} placeholder="例如 F01,B01；留空使用适用字段" /></label><button className="primary" disabled={!ticker || structuredBusy} onClick={structuredPlan}>{structuredBusy ? "处理中…" : "创建结构化计划"}</button>{structuredRunIds.length > 0 && <div className="acquisition-runs"><strong>结构化运行</strong>{structuredRunIds.map((runId) => <div key={runId}><code>{runId}</code><button disabled={structuredBusy} onClick={() => executeStructuredRun(runId)}>执行</button><button disabled={structuredBusy} onClick={() => executeStructuredRun(runId, true)}>恢复</button></div>)}</div>}<p className="muted">计划不会隐式联网；执行后才会创建带来源、期间和快照定位的结构化事实。</p></section><section className="panel acquisition-panel"><PanelTitle title="公告与研究资料" meta="按研究问题按需采集，不做全量报告归档" /><label>股票代码<input value={ticker} onChange={(e) => setTicker(e.target.value)} onBlur={() => refreshAcquisitionRuns()} placeholder="例如 600519" /></label><label>运行模式<select value={acquisitionMode} onChange={(e) => setAcquisitionMode(e.target.value)}><option value="baseline">baseline · 首次完整历史回溯</option><option value="incremental">incremental · 水位线增量更新</option><option value="reconcile">reconcile · 缺口与历史修订对账</option></select></label><div className="source-plan"><strong>版本化来源计划</strong>{sourceDefinitions.length ? sourceDefinitions.map((source) => <div key={`${source.source_definition_id}@${source.version}`}><span>{source.display_name}</span><StatusBadge value={source.policy_status || source.status} /><small>{source.applicability_summary || "适用性在计划阶段确定"}</small></div>) : <p className="muted">正在读取来源注册表；没有来源时不能把运行称为完整。</p>}</div><button className="primary" disabled={!ticker || acquisitionBusy || !sourceDefinitions.length} onClick={createAcquisitionRun}>只规划并冻结 coverage</button>{acquisitionRuns.length > 0 && <div className="acquisition-runs"><strong>最近采集运行</strong>{acquisitionRuns.slice(0, 5).map((run) => <div key={run.run_id}><div><code>{run.run_id}</code><span>{run.mode} · {run.status}</span><small>coverage_accounted={String(Boolean(run.coverage_accounted))} · gaps={run.material_gap_count ?? "待计算"}</small></div><button disabled={acquisitionBusy || ["succeeded", "partial", "failed"].includes(run.status)} onClick={() => executeAcquisitionRun(run.run_id)}>执行/恢复</button></div>)}</div>}</section>
    <section className="panel"><PanelTitle title="正式公告归档" meta="PDF/HTML/TXT/Markdown · SHA-256 · FTS5" />{Object.entries(document).map(([key, value]) => <label key={key}>{({ ticker: "股票代码", path: "本地文件绝对路径", title: "公告标题", source_name: "来源名称" })[key]}<input value={value} onChange={(e) => setDocument({ ...document, [key]: e.target.value })} /></label>)}<button className="primary" disabled={!document.ticker || !document.path || !document.title} onClick={ingest}>归档并索引</button></section>
    {result && <section className="panel data-result"><PanelTitle title="本次结果" meta="所有失败显式降级" /><pre>{JSON.stringify(result, null, 2)}</pre></section>}
  </div>;
}

function PanelTitle({ title, meta }) { return <div className="panel-title"><h2>{title}</h2><span>{meta}</span></div>; }
function StatusBadge({ value }) { return <span className={`status-badge ${STATUS_CLASS[value] || ""}`}>{value}</span>; }
function Empty({ title, text, action, onAction }) { return <div className="empty"><div>∅</div><h3>{title}</h3><p>{text}</p>{action && <button className="primary" onClick={onAction}>{action}</button>}</div>; }
function Loading() { return <div className="loading"><span></span><p>正在读取本地研究档案…</p></div>; }

function EightStepStrip() {
  const names = ["商业模式", "财务报表", "公司治理", "资本行为", "估值分析", "驱动与风险", "行业情况", "情景结论"];
  return <section className="panel"><PanelTitle title="严格八步框架" meta="审计附录不计为第九步" /><div className="step-strip">{names.map((name, i) => <div key={name}><span>0{i + 1}</span><strong>{name}</strong></div>)}</div></section>;
}

function titleFor(view, report) {
  return ({ dashboard: "研究概览", reports: "报告档案", methods: "方法库", data: "数据与公告", report: report ? `${report.company_name} · v${report.version}` : "报告" })[view];
}

export default App;
