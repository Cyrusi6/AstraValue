# 贵州茅台结构化 API 真实链验收记录（2026-10-02）

## 当前结论

修复 `__retrieved_at` 后，在新的空数据根重新完成 31 个数据集的真实 baseline：**53 succeeded、9 no_data、0 failed、0 pending、0 retryable**。旧的 `company_basic`、`controller`、`repurchase` 三个 `9501` 请求错误均已消失，分别取得 1、2、2 条记录。

同参数重复计划和执行没有新增请求、快照或记录。新数据经 `StructuredFactMaterializer` 生成 **548 条事实、300 条维度事实、8 个事件、21 个来源**，重复物化的结果和数量不变。

9 个空响应已逐项查明到来源覆盖层面：东方财富当前主题表没有贵州茅台的记录。旧取证中“有数据”的部分来自同行样本，茅台自身的补充证据来自年报/中报正文。未发现这 9 个接口过去曾返回茅台结构化记录的证据。供应商未收录的内部原因无法由当前响应确定。

整批 incremental 仍在创建运行及联网前被 9 项空 coverage 阻止。`reconcile` 已真实复查这 9 项，仍为空。用户随后决定将其补齐列为后续工作；下一步先明确其余适用数据集的验收范围并完成真实增量，本记录中的全范围增量仍未通过。

## 验收对象与证据

- 工作树：`D:/估值模型-worktrees/fact-materialization-ultra`
- 字段修复代码提交：`4c203d5`；来源对照文档提交：`d1ef534`、`71ca462`、`275f509`
- 数据库：`tmp/moutai-api-final-20261002/analysis.db`
- 数据根：`tmp/moutai-api-final-20261002/data`（创建前不存在；未从旧根复制数据）
- 公司：`600519.SH`
- baseline 截止时间：`2026-10-02T00:00:00+08:00`
- 数据集注册表：`1.2.1`；上游来源：`structured-eastmoney@1.2.0`
- 机器记录：同目录 `acceptance.json`、`baseline-rounds.json`、`reconcile-rounds.json`、`provenance-audit.json`

| 操作 | 运行 ID | 结果 |
|---|---|---|
| 完整 baseline | `structured-run-d58b1fad57728dae9cdb5fb3` | 62 个任务：53 成功、9 空响应、0 失败，全部终结 |
| 同参数重复 baseline | 同上 | `created=false`、`attempted_job_ids=[]` |
| reconcile | `structured-run-f2e745256fdc4ea6133d8a44` | 仅重查 9 个空响应任务，仍为 9 no_data，无待执行或重试任务 |
| 完整 incremental | 未创建运行 | 在联网前拒绝，错误列明 9 个缺少安全 coverage 的数据集 |
| 重复物化 | baseline 同一运行 | 物化哈希、事实、维度、事件数量全部不变，无采集请求 |

baseline 和 reconcile 的 `finalized.result=succeeded` 表示执行任务已完成且没有失败；这 9 个主题的事实覆盖仍为空。`default_consume_eligible=false` 由 `_finalize_if_terminal` 固定写入，原因是 `human_acceptance_independent`，不能用它判断是否由空响应造成。完整增量的实际阻塞来自 `_incremental_starts` 对 `complete + safe_through` 的要求。

## 本地 provenance 边界

`__retrieved_at` 是本地记录的采集时间，不属于供应商字段。本轮完成：

1. 从全范围和 lite profile 的请求/消费字段中移除它；`request_fields` 在最终生成 `columns`、`fields`、`sty` 时也排除它。
2. 从 5 个数据集的 `date_fields` 中移除它；尚未登记业务日期的条目保持空日期集合，不能用采集日期充当业务期间。生成器同步停止补入合成日期。
3. 运行时仍保留 `raw_row.__retrieved_at`、`observed_at` 等本地 provenance，但仅用原始响应行计算内容版本哈希、业务期间和字段投影。
4. 数据集元数据升级为 `1.2.1`，新增可选的 `source_definition_version` 明确保留未变的上游请求合同 `1.2.0`；旧注册表缺少该项时继续使用自身版本。已核对 Eastmoney/BaoStock 来源哈希仍匹配已有解释合同，旧合同和旧快照未改写。

真实核对结果：

| 检查 | 实测 |
|---|---|
| 请求中包含 `__retrieved_at` | 0 |
| 投影成业务字段的 `__retrieved_at` | 0 |
| 保存本地采集时间的记录 | 1,495 / 1,495 |
| 原始响应快照文件哈希 | 62 / 62 匹配 |
| 以原始响应行独立重算内容版本哈希 | 1,495 / 1,495 匹配 |
| 三个修复数据集误用采集时间为业务期间 | 0 |

重复 baseline 前后均为：71 个 attempt（62 baseline + 9 reconcile）、62 个 snapshot、1,495 条记录、39,332 个上游字段。reconcile 对相同查询的空响应复用了已有 snapshot。重复物化前后均为 548 条事实、300 条维度事实、8 个事件，物化哈希为 `fdc2e60f31bccdf1fd80ba965e1c3173405be7e18df150aff697e7a4e2fc441b`。

## 9 个空响应为什么以前看起来“取到了”

逐主题接口、历史样本和报告证据见 [来源对照](moutai-no-data-source-comparison-20261002.md)。本次检查单公司过滤、可用的过滤字段变体、单公司 `in (...)` 与七家公司分页结果；9 个接口的茅台请求均返回：

```json
{"version":null,"result":null,"success":false,"message":"返回数据为空","code":9201}
```

HTTP 均为 200。协议解析器将已登记的 `9201` 转为零行终止页；原始响应的 `result` 是 null，并没有上游 `count=0` 字段。保存的规范化 page 为 `row_count=0`、`total_count=0`、`terminal=true`。原始响应 SHA-256：`ea6528a2204b61a7ee34683a73ca00f8afdc86aadf3f765d4dae39bbfb19dbe7`。

七家公司请求中，9 个主题依次取得 686、39、60、5、4、3、46、25、24 条记录；翻完全部页后均没有 600519。当前接口可返回同行记录，因此不能把茅台查空归因为这些接口整体失效。旧字段样本没有证明茅台在这些表中有行。

| 茅台主题 | 旧资料实际能提供什么 | 当前结论 |
|---|---|---|
| 客户/供应商 `customers_peer` | 2021–2025 年报的前五名金额和集中度 | 有报告披露，API 无茅台行；汇总不能替代逐户明细 |
| 担保 `guarantee` | 2021–2026H1 共 10 期的担保栏目 | 可引用对应期“无/不适用”披露，不能推断历史上从无担保 |
| 诉讼 `litigation` | 同 10 期“无重大诉讼、仲裁”披露 | 结论限重大事项和已核对报告期，普通诉讼仍未知 |
| 增发 `seo`、配股 `allotment` | 2021–2025 证券发行栏目“不适用” | 仅为对应期证据 |
| 债券发行 `bond_issuance` | 2021–2026H1 债券栏目“不适用” | 公司发行与公司持有的债券分别判断 |
| 商誉 `goodwill` | 三表商誉字段/年报行为空，尚无登记的专题核查 | 保持空白或未知，不补 0；必要时查附注 |
| 质押 `pledge` | 同 10 期前十大股东表的质押、标记或冻结列 | 可读取报告期股东状态，不等于逐笔质押事件表 |
| 解禁 `unlock_peer` | 同 10 期限售股份变动“不适用” | 区分报告期披露、计划解禁和实际解除限售 |

已有报告核查路径为 `output/research/贵州茅台-目录资料补齐核查/目录接入与核查结果.json`；实时接口对照为 `output/research/贵州茅台-缺失API核查/20261002T-real-probe/comparison.json`。它们只作为已有真实证据使用，未写入本轮空数据根冒充 API 回采。新 acquisition 对原文的取得/解析不自动等于这些主题已经完成语义整理。

## 自动检查与后续范围

- 定向回归：72 项通过；补充来源合同兼容回归 78 项通过；补充版本绑定断言 1 项通过。
- 全量 Python：**1,729 passed、14 skipped、1 failed**。唯一失败为 `tests/knowledge/release/test_full_release.py::test_all_54_questions_and_recorded_acceptance_are_ready`，当前候选缺 Agent 样例和人工 review。这是已有发布门，未删除或放宽。日志：`tmp/provenance-final-regression-20261002.log` 和同名 XML。
- 前端构建、OpenSpec strict（2 项）通过；旧 `src/analysis/adapters/`、旧 `structured/research.py` 及旧包装入口均不存在。

后续决定：用户将本轮茅台 9 项 API 空响应的补齐延期。原始 `no_data`、快照及已取得的报告证据继续保留；下一步验收其余适用数据集，并列明未验收的后补项。当前增量代码和 required/conditional 配置尚未调整，本文不据此改写已记录运行的状态或水位线。

报告人工阅读、知识发布门、真实治理 manifest 和分支收口仍按原清单验收，本次字段修复不关闭这些门。

## 历史记录与复现

旧根 `tmp/moutai-real-api` 的 baseline `ed9bcd788fc8ac594c4c01a9`、reconcile `c9beb7959d5b62cc14fd5f8b`、incremental `f0ed0ddff7cae39bf9dda5e3` 保存修复前的 `9501` 响应，继续保留。先前用于定位的 lite/fullscope/provenance-only 临时根属于过程证据；最终代码验收以本文 `moutai-api-final-20261002` 为准。

在工作树根执行以下命令可读取最终状态；同 cutoff 的 baseline plan/run 会复用已完成运行。若做独立回采，换成新的空 `--db`/`--data-root` 配对路径，再按返回的 run ID 多轮执行 `structured run` 到终态。

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
$env:PYTHONUTF8='1'
python -m analysis.cli structured status structured-run-d58b1fad57728dae9cdb5fb3 `
  --db tmp/moutai-api-final-20261002/analysis.db `
  --data-root tmp/moutai-api-final-20261002/data --json
python -m analysis.cli structured plan 600519 --mode baseline `
  --as-of 2026-10-02T00:00:00+08:00 `
  --db tmp/moutai-api-final-20261002/analysis.db `
  --data-root tmp/moutai-api-final-20261002/data --json
```
