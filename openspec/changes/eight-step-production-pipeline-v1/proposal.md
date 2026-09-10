## Why

现有结构化采集已经能够冻结来源、分页响应和原始字段，但结构化记录尚未形成一条可直接驱动八步报告的标准事实与确定性计算流水线。现在补齐物化、标准化、覆盖评估和报告入口，才能让采集层的真实结果在本地可复算地进入八步分析，而不是停留在采集数据库中。

## What Changes

- 新增从已完成结构化运行读取记录与字段、构建带来源定位的 `FactRecord`、维度事实和事件输入的物化层。
- 新增年度、累计、单季、TTM、存量和比率的显式整理，以及财务恒等式、缺失和异常的可审计检查。
- 新增按八步问题与行业画像聚合的数据覆盖输出，明确 `ready`、`pending` 和 `not_applicable`，不把未知填成零或不适用。
- 新增从结构化运行生成报告草稿及 Markdown/Excel/PDF/HTML 导出的 CLI/API 入口；报告数字统一来自同一冻结输入。
- 新增全市场证券目录导入和有界批量规划，使同一流水线可扩展到全 A 股；未取得的数据保留缺口状态。
- 新增脱离网络的夹具测试、真实缓存回放测试、CLI 契约和一致性验证。

## Capabilities

### New Capabilities

- `structured-fact-materialization`: 将结构化记录物化为可消费的标准事实、维度和事件，并保留完整字段级血缘。
- `eight-step-analysis-pipeline`: 按八步需求生成确定性计算、覆盖状态和版本化报告草稿。
- `ashare-universe-batch-processing`: 管理全 A 股证券目录、批量计划、失败恢复和逐证券结果汇总。

### Modified Capabilities

- `structured-acquisition-repair`: 增加已完成结构化运行可被事实物化和报告消费的契约；不改变既有采集状态、来源门禁和快照不变量。

## Impact

- 影响 `src/analysis/structured`、`src/analysis/formulas.py`、`src/analysis/reporting.py`、`src/analysis/service.py`、CLI/API 和前端数据展示。
- 复用现有 SQLite 结构化存储、共享快照、`FactRecord`/`ReportVersion` 和导出器，不创建第二套原始数据存储。
- 需要新增版本化配置和报告元数据；不会提交凭据、原始响应、数据库或生成报告。
- 严格历史模式只使用 `available_at` 不晚于截止时点的记录；当前研究允许供应商历史序列，但报告必须标注 point-in-time 限制。
- 自动化测试、真实联网/缓存回放和人工黄金验收分别记录，任何一类通过都不代表另外两类完成。

## Non-goals

- 不把现有 `content_status: skeleton` 方法文档升级为成熟投研方法。
- 不自动生成用户确认前的投资评级、预测假设或交易指令。
- 不承诺一次网络运行就完成全 A 股全部历史；每个证券、数据集和期间都必须有实际状态与缺口原因。
