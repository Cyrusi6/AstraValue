# A股全行业八步财报分析系统

这是一个本地运行、证据可追溯、计算可复现的个人投研系统。报告固定采用“置顶结论卡 → 严格八步正文 → 审计附录”。展示上共有十个一级区块，但分析正文始终只有八步，附录不算第九步。

项目当前进度、真实同步批次和已知边界统一记录在 [`阶段日志.md`](阶段日志.md)。每次实质开发、重要测试、真实同步或新报告生成后必须追加更新该日志。

方法库采用 Markdown、JSON、Python 三层结构：Markdown 负责解释，JSON 固化规则，Python 执行确定性计算。当前 37 份 Markdown 方法文件都只建立了带版本号的 `content_status: skeleton` 骨架，正文刻意留待后续专项研究，不应被视为已完成的方法论。LLM 不是财务数字的计算源。

跨层软件变更使用 OpenSpec 管理 proposal、行为规格、设计和任务；完整流程及其与现有文档的职责边界见 [`docs/openspec_workflow.md`](docs/openspec_workflow.md)。

## 当前入口（2026-09-14）

产品目标是：框架准备紧凑资料，Codex／Claude Code 自主研究、提出估值假设并给出评级，代码负责核算、绘图和组装，模型检查最终研报。知识库按需读取。

- [四层职责与验收](计划.md)：产品决策。
- [当前任务](openspec/changes/eight-step-production-pipeline-v1/tasks.md)：唯一实施清单。
- [轻量包运行说明](docs/acquisition/eight-step-lite-runbook.md)：已实现的低层命令。
- [茅台报告验收](docs/acquisition/moutai-golden-report-acceptance.md)：数据获认可，当前报告人读未通过。
- [OpenSpec工作流与历史角色](docs/openspec_workflow.md)：避免重复计划。

新增需求在原有底座上补充：MCP研究入口、正式公式计算、常规/定制图表、独立验证探索和Markdown引用组装，见[统一需求与验收](计划.md#统一交互与验收约定2026-09-14-讨论定稿)。高层公司任务准备、按需知识接口及模型研究到人读报告尚待实施；以下历史兼容入口存在不代表新流程已交付，也不构成全量采集的默认启动指令。

## 公司业务与商业模式资料采集（v1）

已归档扫描公告的文字与表格解析使用 MinerU 精准解析 API v4（`vlm`），本地密钥读取 `.env.local` 的 `MINERU_API`。新入口为 `scripts/parse_announcement_mineru.py`，支持远端任务恢复及带 PDF 页码的不可变结果；见 [解析配置与用法](docs/acquisition/mineru-precision-parser.md)。原本地 OCR 执行流程已移除，历史结果继续保留。

这部分只建立可审计的资料采集底座；业务问题仍以 [`docs/methodology/steps/01_business_model.md`](docs/methodology/steps/01_business_model.md) 的 `content_status: skeleton` 文件为边界。本功能不抽取业务结论，不调用 Codex，不判断护城河、定价权、战略可信度或投资价值。

所有会写入或联网的采集命令都必须显式提供匹配的 `--db` 和 `--data-root`。首次运行使用 `baseline` 完成从证据锚点开始的历史计划；有安全 checkpoint 后使用 `incremental`，它按来源水位线和固定 overlap window 检查新增及变化；缺口、迟到修订、完整性问题或不兼容注册表变化使用 `reconcile`。调用方不能用时间或问题筛选缩小 production baseline 后仍声称完整。

明确的来源挑战（包括 `x-tengine-error: denied by bot`）会停止当前 run 内该来源版本的后续请求，逐计划记录 `policy_skipped: source_access_halted`；接管同一 run 后仍保持停止。bootstrap 不可用时，下游逐项记录零 I/O 的 `dependency_unavailable`，并在终态 `causal_groups` 汇总共同原因和受影响数量。两者都保留 barrier，运行可自行以有缺口的 partial 状态终结。

API 与 CLI 共用 reconcile 选择器，只接受 finalized 父运行，优先定位最早未解决 barrier，再考虑无 barrier 的未解决 coverage、隔离快照和最早已完成时间片。输出包含精确页/游标、来源、查询、父时间片和固定 overlap 范围；计划只包含该目标及所需前置查询。`--from-latest-run` 会列出被排除的较新未终结 run；任何适用且 enabled 的主采来源缺少安全 checkpoint 时，incremental 在创建 run 和联网前拒绝。

当前默认注册表为 `business_model_sources.v1.9.json`：巨潮 `1.8.0` 主采，上交所 `1.4.0` 按需补缺。默认完整计划对上交所生成 `on_demand_supplement` 静态覆盖，只有已终结运行的实际缺口才通过 `acquire supplement` 显式启动；当前上交所协议只支持定期报告补缺。补缺保持来源身份，不自动清除巨潮屏障。巨潮公告 schema 3 兼容有证明的 null 空结果与历史 HTML，首发参数为 `category_sf_szsh`；材料类型以标题和已归档正文共同分类。详见 [`cninfo-history-archive-policy-2026-09-05.md`](docs/acquisition/cninfo-history-archive-policy-2026-09-05.md)。旧 v1.0–v1.8 合同保留用于历史重放。直连、TLS 验证、来源并发 1、最小间隔 5 秒、无默认自动重试和既有个人本地研究范围继续适用。贵州茅台 IR 仍为 `pending_policy/disabled`。自动化、真实联网和人工黄金验收分别记录。

公开大附件从 `business_model_sources.v1.8.json`（巨潮 `1.7.0`）起支持，当前默认 1.9 沿用：128 MiB 响应上限、600 秒 attempt 预算，保留 30 秒 socket timeout 和全部既有访问边界。旧配置和旧计划保留，终态失败必须通过独立冻结计划补抓。下载每次读取前后和返回前校验总预算，超时不得提交正文成功。

兼容 `/api/companies/{ticker}/sync` 不接受把 `business_model` 与旧财务 scope 混在同一次请求中：两类工作必须分别发起。这样旧 adapter 不会在 business-model 来源审核失败时绕过注册表门禁，结构化 attempts 也不会与 legacy 自由文本结果混成同一权威摘要。

```powershell
# 创建计划并执行；加 --plan-only 可只冻结 run/coverage，不联网
python -m analysis.cli acquire start 600519 --mode baseline `
  --db var/pilots/business-model-acquisition-v1/analysis.db `
  --data-root var/pilots/business-model-acquisition-v1/data --json

# 后续增量
python -m analysis.cli acquire start 600519 --mode incremental `
  --db var/pilots/business-model-acquisition-v1/analysis.db `
  --data-root var/pilots/business-model-acquisition-v1/data --json

# 从同一 ticker/scope 的确定性最近运行修复缺口
python -m analysis.cli acquire start 600519 --mode reconcile --from-latest-run `
  --db var/pilots/business-model-acquisition-v1/analysis.db `
  --data-root var/pilots/business-model-acquisition-v1/data --json

# 注册表驱动的最小探针；它持久化 smoke run，但不推进 production checkpoint
python -m analysis.cli smoke-sources --ticker 300750 --source szse.disclosures `
  --db var/pilots/business-model-acquisition-v1/analysis.db `
  --data-root var/pilots/business-model-acquisition-v1/data --json
```

`no_data` 只表示一次 discovery 查询成功完成 schema 校验、总数闭合和分页终止证明且结果为空。网络失败、受限、登录、付费、限流、超时、解析失败或部分完成绝不能转换成 `no_data`，也不能自动生成“未披露”或“暂无该数据”。注册表外域名只能形成隔离的 source candidate；人工批准并发布新注册表版本前不得请求正文或进入正式证据。

CLI 退出码固定如下：

- `0`：运行成功并可作为默认消费批次；
- `2`：参数、注册表或 namespace 校验错误；
- `3`：运行已终结，但存在材料缺口或不能默认消费；
- `4`：integrity/internal 失败；
- `5`：可重试控制面冲突；JSON subtype 为 `active_lease` 或 `storage_busy`。也就是说，exit code 5 不是来源无数据或运行失败结论。

升级已有数据库前可显式创建只读备份：

```powershell
python -m analysis.cli acquisition-db backup `
  --db var/pilots/business-model-acquisition-v1/analysis.db `
  --data-root var/pilots/business-model-acquisition-v1/data --json
```

独立 backup 命令只做只读 preflight 和验证后的备份输出；它不会创建 binding intent、namespace、migration、runtime 或 acquisition run，也不会触发迁移。原始响应、PDF、数据库、备份、派生文本、证据清单文件和试点输出都位于 Git 忽略的数据根中，不应提交到仓库。

## 结构化数据优先（v1）

新同步以东方财富和 BaoStock 的版本化字段路由为默认策略，主源字段有效时直接消费，只有实际缺口才进入已登记备用。运行、恢复、API/CLI、隔离真实样本、回滚和已知缺口见 [结构化数据优先 v1 运行说明](docs/acquisition/structured-data-runtime-v1.md)；人工签署保持独立，使用 [人工验收清单](docs/acquisition/structured-data-manual-acceptance-v1.md)。

## 快速开始

```powershell
python -m pip install -e ".[sources,dev]"
python scripts/validate_method_library.py
python scripts/validate_golden_samples.py
python -m pytest
python -m analysis.cli demo
uvicorn analysis.api:app --reload
```

浏览器访问 `http://127.0.0.1:8000`。前端开发模式见 `frontend/README.md`。

在线同步前可按本机网络情况设置代理：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7897"
$env:HTTPS_PROXY="http://127.0.0.1:7897"
python -m analysis.cli smoke-sources --ticker 600519 `
  --db var/pilots/business-model-acquisition-v1/analysis.db `
  --data-root var/pilots/business-model-acquisition-v1/data
```

该脚本委托给注册表驱动的 `smoke-sources` 命令，创建带 lease 的独立 smoke run，并报告规范 attempt 状态；它不会推进 production checkpoint。需要把材料缺口反映到退出码时加 `--strict`。

Tushare Pro 不在默认同步列表中。需要时安装可选依赖并仅通过环境变量提供令牌：

```powershell
python -m pip install -e ".[tushare]"
$env:TUSHARE_TOKEN="你的令牌"
# Tushare 仍只通过既有 financial/market sync 显式 opt-in；
# 不属于 business_model v1，也不会由 smoke-sources 枚举。
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

## 已有模块与历史兼容能力（不等于端到端验收）

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
- 当前黄金验收按当前任务第6节执行，数据、真实模型分析、图表与人工阅读分开验证；旧固定50事实/十二季度门槛不作为当前轻量报告标准。
- 示例报告使用虚构公司与虚构数据，只用于演示流程和导出一致性。

## 可信边界

免费数据源会限流、改版或缺少一致预期。系统保证的是来源与计算链可审计，不保证任何外部数据绝对无误，也不提供自动交易、仓位建议或收益承诺。模型可自主提出估值假设并给出研究评级，明确依据与敏感性；缺失、冲突或方法不适用时必须显式降级，不能由 LLM 补造。


## 八步数据层运行交付

2026-09-13 的范围约束、原件解析、财务物化、精确补采与逐题覆盖已接入。启动命令和真实缓存/联网复现见 [数据层运行说明](docs/acquisition/research-data-layer-runbook.md)；实际数量、独立复算、删除清单入口及剩余来源/语义/处理缺口见 [交付验证](docs/acquisition/research-data-layer-verification.md)。报告按当前任务推进，前端与全市场暂缓，人工验收单独记录。
