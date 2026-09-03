# A股全行业八步财报分析系统

这是一个本地运行、证据可追溯、计算可复现的个人投研系统。报告固定采用“置顶结论卡 → 严格八步正文 → 审计附录”。展示上共有十个一级区块，但分析正文始终只有八步，附录不算第九步。

项目当前进度、真实同步批次和已知边界统一记录在 [`阶段日志.md`](阶段日志.md)。每次实质开发、重要测试、真实同步或新报告生成后必须追加更新该日志。

方法库采用 Markdown、JSON、Python 三层结构：Markdown 负责解释，JSON 固化规则，Python 执行确定性计算。当前 37 份 Markdown 方法文件都只建立了带版本号的 `content_status: skeleton` 骨架，正文刻意留待后续专项研究，不应被视为已完成的方法论。LLM 不是财务数字的计算源。

跨层软件变更使用 OpenSpec 管理 proposal、行为规格、设计和任务；完整流程及其与现有文档的职责边界见 [`docs/openspec_workflow.md`](docs/openspec_workflow.md)。

## 快速开始

```powershell
python -m pip install -e ".[sources,dev]"
python scripts/validate_method_library.py
python scripts/validate_golden_samples.py
python -m pytest
python scripts/run_demo.py
uvicorn analysis.api:app --reload
```

浏览器访问 `http://127.0.0.1:8000`。前端开发模式见 `frontend/README.md`。

在线同步前可按本机网络情况设置代理：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:HTTPS_PROXY="http://127.0.0.1:7897"
python scripts/smoke_online_sources.py --ticker 600519 --timeout 20
```

在线探测默认只报告 `ok / degraded / unavailable / failed / timeout`，不会阻塞离线测试；需要把任一异常视为失败时加 `--strict`。

Tushare Pro 不在默认同步列表中。需要时安装可选依赖并仅通过环境变量提供令牌：

```powershell
python -m pip install -e ".[tushare]"
$env:TUSHARE_TOKEN="你的令牌"
python scripts/smoke_online_sources.py --ticker 600519 --providers tushare
```

Wind 需要本机 Wind 终端、`WindPy` 与有效商业授权，当前免费默认环境不伪装为可用数据源；其后可按同一适配器接口接入。

启动 API 后，可以先同步真实数据，再让报告自动使用该同步批次：

```powershell
$sync = @{
  providers = @("official", "akshare", "sina", "baostock")
  annual_years = 5
  single_quarters = 12
  download_official_documents = $true
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType "application/json" `
  -Uri "http://127.0.0.1:8000/api/companies/600519/sync" -Body $sync

$report = @{
  ticker = "600519"
  company_name = "贵州茅台"
  industry = "消费"
  use_synced_facts = $true
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType "application/json" `
  -Uri "http://127.0.0.1:8000/api/reports" -Body $report
```

### 全公告与事件同步

公告同步可以独立于财务同步执行；独立公告批次不会替换报告生成时所需的最新财务批次。下面的示例索引近两年全部公告，但只下载并解析分红、回购和增减持三类事件附件：

```powershell
$events = @{
  providers = @("official")
  scopes = @("announcements")
  as_of = "2026-09-03T00:00:00Z"
  announcement_years = 2
  max_announcements = 1000
  max_event_documents = 200
  event_types = @("dividend", "repurchase", "holding_change")
  download_official_documents = $true
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType "application/json" `
  -Uri "http://127.0.0.1:8000/api/companies/600519/sync" -Body $events
```

- `scopes` 可使用 `announcements / governance / capital_actions / risks` 控制公告研究范围；使用 `announcements` 时覆盖所有已识别事件类别。
- `event_types` 为空时不做类别过滤；可选类别以 [`config/event_taxonomy.json`](config/event_taxonomy.json) 为准。
- `max_event_documents = 0` 或 `download_official_documents = $false` 时只建立公告索引，不下载附件。
- 公告、事件和解析版本均按快照追加；同一 PDF 的交易所/巨潮镜像共享上游哈希，不能冒充两个独立来源。

查询与证据链接口：

- `GET /api/companies/{ticker}/announcements`：默认返回每个 canonical 公告的最新已存版本，可按 `event_type`、`as_of` 和 `data_snapshot_id` 过滤。
- `GET /api/companies/{ticker}/events`：默认返回每个 canonical 事件的最新已存版本，可按 `event_type`、`root_event_id`、`as_of` 和 `data_snapshot_id` 过滤。
- `GET /api/announcements/{announcement_record_id}/lineage`：公告、附件和关联事件血缘。
- `GET /api/events/{event_id}/lineage`：事件、正式来源、附件、前序事件及根事项血缘。
- 创建报告时可以传入 `event_sync_result_id` 显式锁定事件批次；未指定且 `use_synced_facts = true` 时，系统会自动选择截止时点内最新的非空事件批次，不会被更晚的公告索引空批次遮蔽。
- 选中的事件批次 ID 会写入报告元数据；重算、重新分析和人工复核均继续携带该批次及事件记录，历史报告不会因后续同步而漂移。
- 事件会按 [`config/event_taxonomy.json`](config/event_taxonomy.json) 的 `report_steps` 路由到第三、第四和第六步。正文只陈列披露事实、状态链与证据定位，不自动判定利好、利空或治理质量。

### 财报附注经营维度同步

经营维度可独立同步。该范围只选择最近指定年数的年度报告，不下载季度报告；解析结果包括分业务、分产品、分地区、分销售渠道的收入/成本/毛利率，以及产销存、产能和前五名客户/供应商汇总。

```powershell
$dimensions = @{
  providers = @("official")
  scopes = @("dimensions")
  as_of = "2026-09-03T00:00:00Z"
  annual_years = 5
  download_official_documents = $true
} | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType "application/json" `
  -Uri "http://127.0.0.1:8000/api/companies/600519/sync" -Body $dimensions
```

- `GET /api/companies/{ticker}/dimensions` 可按 `metric_id`、`dimension_type`、`as_of`、`data_snapshot_id` 和 `verification_status` 精确查询；`limit` 默认 500，范围 1—5000。
- `GET /api/companies/{ticker}/dimensions/review-queue` 只返回状态为“待核验”的经营维度事实，可用 `data_snapshot_id` 锁定批次，并用 `limit` 控制返回量。
- `GET /api/dimensional-facts/{dimensional_fact_id}/lineage` 返回来源、正式文档及页码/表名/行列/字符偏移证据。
- 分产品、地区和渠道合计会与主营业务合计勾稽，披露毛利率会按收入和成本复算；冲突统一降为“待核验”，不取平均。
- 报告未指定批次时，系统分别锁定最新财务批次和最新经营维度批次，并把两个批次 ID 写入报告元数据；第一步直接展示可追溯维度事实。
- 经营维度事实的 `verification_status` 与财务事实使用同一状态枚举；当前自动审核队列只收集“待核验”，不会把“权威单源”误判为冲突。

## 当前能力

- 方法文档、机器规则、代码实现三层绑定与版本哈希；
- 财务口径换算、杜邦、FCF、ROIC/WACC与会计恒等式校验；
- 相对估值、FCFF/FCFE DCF、DDM、剩余收益、SOTP、NAV、Reverse DCF和周期标准化；
- A股行业估值路由及方法停止条件；
- 双源/权威单源/冲突/估算/未披露/暂无/不适用状态；
- 巨潮、上交所、深交所定期报告检索，正式 PDF 自动归档、哈希与结构化解析；
- 巨潮与交易所全公告索引、确定性事件分类、定向 PDF 下载、字段提取和事项状态链；
- 年报分业务/产品/地区/渠道、产销存、产能及客户供应商集中度解析，含表内勾稽、证据定位和报告第一步接入；
- AKShare 东方财富与新浪财经五年年报/十二个单季度三表同步，新浪历史收盘价后备，以及 BaoStock 行情和季度净利润复核；
- 仅在价格、TTM归母净利润、归母净资产和股本均完成双源核验后，确定性复算 PE TTM/PB，再用 BaoStock 比率交叉验证；
- 可选 Tushare Pro 第二行情源（`python -m pip install -e ".[tushare]"` 并通过 `TUSHARE_TOKEN` 环境变量传入凭证）；
- 最新正式重述优先、旧版本保留、关键字段“正式披露 + 独立来源”双重核验；
- 报告版本冻结、重算、重新分析和版本差异；
- Markdown、HTML、Excel、PDF导出；
- PDF、HTML、TXT和Markdown正式披露文件人工归档入口。

Excel 中保留可编辑情景假设、跨表公式、冻结输出、输入血缘、完整“经营维度事实”“公司事件”及检查表。经营维度表固定输出来源、文档、页码、表名、行列和证据片段；公司事件表固定输出事项状态链、金额、股数、比例、来源、文档、页码、证据片段、字符偏移、事件条款、影响指标和快照。Markdown、HTML 与 PDF 对事件使用紧凑摘要视图，完整 33 列血缘仍保留在 Excel、报告 JSON 和 lineage API。PDF 从同一 HTML 结果打印生成，需要本机 Edge 或 Chrome。

## HTML 使用边界

- **报告输出**：已支持 HTML，和 Markdown、Excel、PDF共用同一份版本化报告 JSON，HTML 只负责展示，不重复计算财务数字。
- **公告输入**：本地 `.html/.htm` 可以归档和提取文本；如果交易所提供真实公告正文 HTML 或 XBRL，可进一步接入结构化解析，通常会比 PDF 表格更稳定。
- **内容校验**：JavaScript 验证页、登录页和反爬提示不属于公告正文，不能作为正式披露证据。当前自动抓取以 PDF 为主；上交所附件返回验证页时，会拒绝该内容并回退到巨潮的同份正式 PDF。

## 验收状态

- 方法库文档—配置—代码—测试引用已建立并可自动检查，但方法正文仍是骨架。
- 十类 A 股黄金样本的公司清单和严格验收器已建立；所有样本当前均为 `pending_manual_validation`。
- 正式声称某家公司通过黄金样本验收前，必须人工核对至少 50 个关键事实，并覆盖至少三期完整年报和十二个季度。
- 示例报告使用虚构公司与虚构数据，只用于演示流程和导出一致性。

## 可信边界

免费数据源会限流、改版或缺少一致预期。系统保证的是来源与计算链可审计，不保证任何外部数据绝对无误，也不提供自动交易、仓位建议或收益承诺。报告在用户确认评级与关键预测前保持草稿状态；缺失、冲突或方法不适用时必须显式降级，不能由 LLM 补造。
