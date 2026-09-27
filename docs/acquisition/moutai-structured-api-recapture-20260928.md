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

## 结论

结构化 API 已能在空数据根重新获取贵州茅台当前结构化数据，重复 baseline 可复用既有 snapshot，失败、空响应和物化缺口均可追踪。真实报告正文仍只通过研究任务按需采集；本次 baseline 没有扩大为全量报告归档。真实 acquisition incremental 的安全水位线仍是待验收边界，不能用本次结构化 plan 的幂等结果替代。

