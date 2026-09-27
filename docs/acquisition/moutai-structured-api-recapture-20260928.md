# 贵州茅台结构化 API 回采验收记录

更新时间：2026-09-28  
代码工作树：`codex/fact-materialization-ultra`  
验证对象：`600519`（贵州茅台）

## 本次真实回采

本次在空数据根 `tmp/moutai-real-api/data` 和空数据库 `tmp/moutai-real-api/analysis.db` 上执行了结构化 baseline。为避免本机 Python 可编辑安装仍指向主工作树，命令显式设置了 `PYTHONPATH=src`，实际加载文件为候选工作树下的 `src/analysis`。

计划命令：

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -m analysis.cli structured plan 600519 --mode baseline `
  --company-scope company-only --as-of '2026-09-28T00:00:00+08:00' `
  --db 'tmp/moutai-real-api/analysis.db' `
  --data-root 'tmp/moutai-real-api/data' --json
```

计划生成 `structured-run-ed9bcd788fc8ac594c4c01a9`，覆盖 31 个数据集、32 个计划项。实际执行完成 62 个分区任务，结果为：50 个 success、9 个 no_data、3 个 failed、0 个 pending/retryable。失败和空响应均写入运行事件，没有用 fixture 或缓存冒充成功：

- 3 个失败（`company_basic`、`controller`、`repurchase`）均为 `eastmoney_business_failure`，保留失败快照和两次尝试记录；
- 9 个空响应均为 `eastmoney_9201_empty`，状态为 `no_data`；
- 其余任务产生真实结构化快照和记录。

随后执行 `structured materialize --summary`，未联网，得到 548 个 facts、300 个 dimensional facts、8 个 events、21 个 sources；物化哈希为 `2bb49e8dc68d49b25d5dabdabf5ce11fae3e6281ff9865d3816c84aa446bacfb`。物化保留字段定义、单位和标准映射缺口，未将缺失值填零。

## 重复采集

使用相同公司、截止时间、数据根和数据库再次执行 baseline plan，返回同一 run ID，`created=false`，未发生网络请求。随后执行 run，`attempted_job_ids=[]`，说明同一计划不会重复创建快照或重复抓取。当前 baseline 含失败或空响应分区，因此按安全 coverage 门禁执行同截止时间的 `incremental` 会明确拒绝，并列出缺少安全水位线的数据集；只有所有适用分区都是 `complete` 且带 `safe_through` 时，incremental 才会创建新计划，并从最近安全水位线开始。 本次实际命令在 2026-09-29 截止时间返回 validation，列出 `allotment`、`bond_issuance`、`company_basic`、`controller`、`customers_peer`、`goodwill`、`guarantee`、`litigation`、`pledge`、`repurchase`、`seo`、`unlock_peer`，没有产生网络请求。

## 增量边界

结构化计划现在也按 `structured_acquisition_coverage` 的安全水位线执行增量：没有完整 `complete + safe_through` coverage 时明确拒绝，不把失败或空响应当作 checkpoint；有水位线时把历史起点收紧到水位线之后，并按冻结 schedule 对事件/生命周期数据保留有限 overlap。它仍没有把普通 structured run 伪装成 acquisition runtime 的 `source_checkpoints`，而 acquisition runtime 的 baseline/incremental/reconcile 门禁继续独立保留。此前贵州茅台的真实证据已记录在清单：baseline `f0ee2b25-a927-467e-b99e-6b8cffb3eeee`、重复 baseline `7964ad85-48b0-46e9-80d7-160f06348462`、按需报告 `c1076c43-1ac9-4f1b-b16e-6eae016e9d2a`、reconcile `10c7d9f3-5074-40d7-9743-262aeb5bd49d`；reconcile 的 `safe_through=null`，所以不能把它写成已完成的真实 incremental。

在真实回采之外，结构化运行时的可重复边界由集成测试覆盖：连续两轮财务增量沿最新安全水位线推进，已提交期间保持可复用，同时刷新最近两个报告期；事件数据每轮只回读冻结 schedule 指定的 30 天 overlap，并从上一轮水位线继续推进；任一适用数据集存在失败 coverage 时，增量计划在联网前拒绝。动态报告期任务的 dedupe 只在同一 run 内保持幂等，后续 run 可以重新请求同一最近期间，以接收修订后的结构化结果。测试文件为 `tests/structured/test_structured_runtime_integration.py`，本工作树的 `tests/structured` 全套测试通过。

## 新增 structured reconcile 真实验收

在同一真实数据库上执行：

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -m analysis.cli structured plan 600519 --mode reconcile --from-latest `
  --company-scope company-only --as-of '2026-09-28T00:00:00+08:00' `
  --db 'tmp/moutai-real-api/analysis.db' --data-root 'tmp/moutai-real-api/data' --json
```

计划生成 `structured-run-c9beb7959d5b62cc14fd5f8b`，联网前 `performed_network_io=false`，只选 parent 中 12 个不安全窗口。执行及恢复遵守单轮和重试上限：最终 7 个窗口真实 `no_data`、2 个真实 `failed`、2 个仍为 `pending`、1 个 `retryable`；没有把空响应写成成功，也没有修改成功 coverage 或手工推进水位线。`tmp/moutai-real-api/reconcile-last.json` 保存了最后一次状态摘要。

## 结论

结构化 API 已能在空数据根重新获取贵州茅台当前结构化数据，重复 baseline 可复用既有 snapshot，reconcile 能在不联网的计划阶段定位 parent 并只恢复不安全窗口，失败、空响应和物化缺口均可追踪。真实报告正文仍只通过研究任务按需采集；本次 baseline 和 reconcile 没有扩大为全量报告归档。由于真实来源仍有空响应和失败，真实 incremental 的安全水位线仍未满足，不能把本次 reconcile 写成 incremental 通过。

## 真实安全窗口 incremental

为验证增量执行本身，在同一贵州茅台数据库中只选择 baseline 已建立 `complete + safe_through` 的 `income_fields` 数据集；这不是把不完整的整批 baseline 宣称为完整增量。计划阶段未联网并生成 `structured-run-096a85739296a0de6f6cffc2`：

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
python -m analysis.cli structured plan 600519 --mode incremental --dataset income_fields `
  --company-scope company-only --as-of '2026-09-29T00:00:00+08:00' `
  --db 'tmp/moutai-real-api/analysis.db' --data-root 'tmp/moutai-real-api/data' --json
```

首次执行后恢复两轮，最终 3 个 `income_fields` 分区全部 `succeeded`，共提交 106 条真实 API 记录，`failed=0`、`no_data=0`、`pending=0`。执行使用既有安全水位线计算增量起点，未手工修改 checkpoint；同一运行的恢复只处理剩余 pending job。整批适用数据集仍因前述 `no_data`/`failed` coverage 被安全门禁拒绝，必须先 reconcile 成功后才能宣称全量增量完成。

