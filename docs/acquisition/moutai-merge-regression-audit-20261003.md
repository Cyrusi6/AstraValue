# 贵州茅台合并回归审计（2026-10-03）

本次审计针对 2026-10-02 合并后的贵州茅台研究报告，区分合并引入的问题和合并前已有的问题。审计基准为合并前提交 `25b7a1f`、本分支合并提交 `601d7cc` 以及当前修复工作树。

## 已确认是合并引入并已修复

### 行情事实被逐题覆盖错误排除

`601d7cc` 新增的研究覆盖准入规则把 `point_in_time` 字段限定为 `period_type=instant`。结构化 API 的正式行情事实使用 `period_type=market_quote`，因此 2026-09-30 的收盘价、PE、PB、PS、总市值均进入 `period_mismatches`，没有进入 `fact_index`。研究覆盖随后把 ES05 的价格、估值和市值要求，以及 ES08 的行情期间要求错误标成 `pending/input_not_acquired`。

修复位于 `src/analysis/structured/research_coverage.py`：`point_in_time` 现在接受 `instant` 和 `market_quote`。新增回归测试验证 `market_cap.CLOSE_PRICE` 的 `market_quote` 事实可以使对应要求变为 `ready`。

使用当前报告的三个 projection roots 和 evidence roots 重新计算（只读、未改写冻结包）得到：ES05 的价格、PE、PB、PS、总市值 5 项要求均恢复为 `ready`；ES08 的 9 个行情期间要求均恢复为 `ready`。仍待处理的 ES05.009 是 `outside_research_profile`，属于研究范围选择，不是采集合并回归。

### 同行来源闭环已补齐

合并后恢复同行投影时，同行指标带有正式 `structured-source-*` 来源，但报告审计来源筛选没有递归保留这些来源；同行指标还缺少 `currency` 和 `scope`，导致 `aligned_peers` 将三家同行全部排除。当前代码已补充同行指标的 `currency/scope`、研究简报的 `peer_comparison`，并把同行集合及其指标来源写入审计附录。定向报告、研究简报和来源闭环测试已通过。

这部分属于合并后暴露并在当前工作树修复的闭环缺陷；原先 `audit.peer_set_refs=[]` 的历史遗漏早于本次合并，不能归因于合并本身。

## 确认是合并前已有，暂不在本轮扩大修复

### 股利待核验冲突

8 条记录对应 8 个不同期间（2020-12-31 至 2025-12-31 的不同方案记录）和不同 `event_id`，均为 `announced_plan`、`verification_status=pending`，当前没有公告原文证据。它们不是一条事件重复 8 次。事件投影和待核验规则在合并前已经存在；`601d7cc` 只是把正式 `events.jsonl` 接入当前报告，所以报告从 2 条变成 8 条。当前代码仅改善冲突展示，增加 `event_id` 和 `period_end`，没有擅自改变核验结论。

### 研究覆盖数量

`305 ready / 11 source_text_available / 23 pending` 是当前轻量研究包（3 年、8 个季度）的状态，不能与历史标准包（5 年、12 个季度）直接比较。54 道研究题仍为 `pending`，这是业务验收和材料缺口，不是合并丢失财务事实。此前物化文件未接入研究快照的旧文件名问题已由前序修复处理。

### 提示词和写作契约版本

报告使用的提示词文件为 v5；代码元数据仍为 `PROMPT_VERSION=buy-side-v4`，写作数据契约为 `contract_version=buy-side-v2`。这三个版本属于不同层级，且代码与文档在合并前已经如此约定。后续应统一提示词元数据和报告契约命名，但本轮不修改历史报告产物，也不把它记录为合并回归。

### DCF 缺失

研究工作区在合并前就只支持 `pe_scenarios`，当前报告因此只有相对 PE（`VAL.RELATIVE`）。通用 DCF 引擎仍在，知识分支的 FCFF 前置计算尚未接入研究工作区和 reporting bridge；贵州茅台当前还缺完整资本开支、合同资产等 FCFF 输入。接入 DCF 需要独立的事实映射、假设、血缘和报告测试，本轮只记录，不把相对 PE 改成未经确认的 DCF。

## 验证

已通过：

```text
python -m pytest tests/test_reporting.py tests/test_research_authoring.py tests/structured/test_research_lite_material_routes.py tests/structured/test_report_coverage_source_closure.py tests/structured/test_research_coverage.py -q
56 passed

python -m pytest tests/test_research_catalog.py tests/test_research_workspace.py tests/structured/test_research_lite.py tests/structured/test_report_coverage_source_closure.py tests/structured/test_research_coverage.py -q
70 passed
```

当前 6.1-sol 报告仍是旧冻结产物，尚未用本次覆盖准入和同行闭环修复重新生成；因此不能把该产物的 `human_acceptance=pending` 或正文中的旧同行描述改写成已验收。重新生成报告后需再次检查同行正文、覆盖计数和股利冲突明细。
