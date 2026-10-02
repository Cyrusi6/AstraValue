# 贵州茅台有效截止日 API 回采与重复增量验收

2026-09-28 08:30—08:40（Asia/Shanghai），在此前不存在的 `tmp/moutai-recapture-valid-cutoff/analysis.db` 与空 `data/` 上，以 `600519`、`company-only`、`income_fields` 执行真实 API 回采。baseline 截止于 `2026-09-27T00:00:00+08:00`，incremental 截止于 `2026-09-28T00:00:00+08:00`，均早于实际执行时间。当前仅完成这个数据集的回采验收，不能外推为全部数据集或八步报告已验收。

原 `tmp/moutai-real-api` 中截止于 2026-09-29 的增量证据保留不改写；它的请求确实执行过，但不能证明当时尚未来临的覆盖上界已经安全取得。本次新空库证据替代其“有效截止日增量已通过”的用途，没有手工修改任何历史 coverage 或 checkpoint。

## 实际结果

| 阶段 | 运行与结果 | 实际提交记录 |
| --- | --- | --- |
| baseline | `structured-run-ffcbe26eaae0a4751f4f30fe`；8 succeeded，其他状态均为 0 | 公司类型 1 条、报告期目录 103 条、利润表明细 26 条，明细报告期为 2020Q1—2026H1 |
| 同截止日重复 baseline | 同 run ID，`created=false`；执行返回 `attempted_job_ids=[]` | 记录、字段、快照和 attempts 数量及内容摘要均未变 |
| incremental | `structured-run-24a6a1abee752640782da61b`；3 succeeded，其他状态均为 0 | 公司类型 1 条、报告期目录 103 条、利润表明细 2 条，明细仅 2026-03-31 与 2026-06-30 |
| 修复后重复 incremental | 同 run ID，`created=false`；执行返回 `attempted_job_ids=[]` | 记录、字段、快照和 attempts 数量及内容摘要均未变 |
| baseline 重复物化 | 277 个 facts、6 个 sources，两次输出完全相同 | `facts=277`、`structured_fact_materializations=1`，没有新增网络请求 |

baseline 共 8 次真实 HTTP 200 响应，incremental 共 3 次真实 HTTP 200 响应。运行采用正常网络传输，未注入 fixture、MockTransport 或旧数据根；增量重新请求目录后因内容相同复用原目录快照。整个新库最终有 11 个 attempts、11 个 discovery observations、9 个原始快照、236 条结构化记录和 6,748 个原始字段记录。9 个快照对应的本地原件 SHA-256 全部校验一致。

236 条结构化记录是两个运行各自的审计记录之和（130 + 106），不能说成 236 个财务事实；其中利润表明细是 baseline 26 条、增量刷新 2 条，其余是公司类型与目录。此次增量未发现新增报告期，也未证明供应商实际修订了内容；它验证了规定的最近两期刷新和历史期间不重复请求。目录 API 本身仍返回 103 个可用报告期，财务明细请求参数只含需要获取的期间。

增量目录窗口为 `2026-09-26T00:00:00Z`—`2026-09-27T16:00:00.000001Z`，由既有安全水位线按当前日粒度规则生成；刷新明细请求的 `dates` 为 `2026-06-30,2026-03-31`。baseline 和 incremental 的目录 coverage 均为 `complete`，`safe_through` 分别为 `2026-09-26T16:00:00.000001Z` 和 `2026-09-27T16:00:00.000001Z`。

物化哈希为 `8ba6ef19f800d9f935956d4945d1e03e5e2edd37a0b3e2459ea9e9d4ff6f2b95`。这次使用普通实时回采物化（`strict_historical=false`），保留实际 9 月 28 日获取时间；它不是“在历史截止日当时已经拥有这些原件”的回测证明。字段定义、单位与标准映射缺口仍如实输出，未填零。

## 真实验收发现并修复的缺陷

首次重复生成同截止日 incremental 计划返回退出码 4：`plan partially overlaps an existing structured run`。失败原文保存在 `incremental-repeat-plan.json`，未被修复后的结果覆盖。原因是重复规划先读取已经推进的水位线，导致相同请求重新计算出不同窗口，随后与自身已有任务部分冲突。

`StructuredDataRuntime.plan` 现在先核对同 run ID 的完整冻结上下文及 acquisition run 身份，完全相同才返回原运行；之后才为新请求计算增量窗口。模式、显式报告期、公司范围等有差异时不会误用旧运行；发生既有任务重叠时明确拒绝。旧快照、记录、水位线和运行身份均不重写。

回归覆盖完成后同截止日重复规划、较新运行推进水位线后再次重复旧请求、重复执行不产生新 page/record，以及不同模式/期间/范围不能混用。`python -m pytest tests/structured -q` 全部通过，当前集合为 367 项；`git diff --check` 通过。修复后对上述真实运行再次执行相同 plan/run 也通过，证据为 `incremental-repeat-plan-fixed.json` 和 `incremental-repeat-run-fixed.json`。

## 复现命令与证据

工作目录为候选工作树 `D:\估值模型-worktrees\fact-materialization-ultra`。每次独立 CLI 调用都显式使用此工作树的 `src`，避免本机 editable install 指向主工作树：

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
python -m analysis.cli structured plan 600519 --mode baseline --dataset income_fields --company-scope company-only --as-of '2026-09-27T00:00:00+08:00' --db tmp/moutai-recapture-valid-cutoff/analysis.db --data-root tmp/moutai-recapture-valid-cutoff/data --json
python -m analysis.cli structured run structured-run-ffcbe26eaae0a4751f4f30fe --db tmp/moutai-recapture-valid-cutoff/analysis.db --data-root tmp/moutai-recapture-valid-cutoff/data --json
```

baseline 首轮后通过 `structured resume` 正常恢复 7 轮，全部为每轮 1 个尚未执行的任务、每个任务 1 次 attempt；没有跳过重试门或修改 checkpoint。再次执行上述 baseline plan/run 验证幂等。

```powershell
python -m analysis.cli structured plan 600519 --mode incremental --dataset income_fields --company-scope company-only --as-of '2026-09-28T00:00:00+08:00' --db tmp/moutai-recapture-valid-cutoff/analysis.db --data-root tmp/moutai-recapture-valid-cutoff/data --json
python -m analysis.cli structured run structured-run-24a6a1abee752640782da61b --db tmp/moutai-recapture-valid-cutoff/analysis.db --data-root tmp/moutai-recapture-valid-cutoff/data --json
```

incremental 首轮后通过 `structured resume` 正常恢复 2 轮，再重复 plan/run。每次实际命令、起止时间、退出码和 stderr 保存在同目录的 `*.meta.json`，完整 CLI 输出保存在同名 `*.json`；`capture_cli.py` 只是调用当前公开 CLI 并保存输出，不改采集结果。

- `baseline-before-repeat.json` / `baseline-after-repeat.json`：baseline 重复前后只读数据库审计。
- `incremental-before-repeat.json` / `incremental-after-fixed-repeat.json`：增量重复前后审计，含 attempts、HTTP observations、报告期、coverage、快照请求参数与原件哈希。
- `baseline-materialize.json` / `baseline-materialize-repeat.json`，`materialize-before-repeat.json` / `materialize-after-repeat.json`：重复物化及落库计数。
- `acceptance-comparison.json`：上述幂等对比结果。
- `old-runs-readonly-audit.json` / `old-reconcile-current-summary.json`：以下旧运行的只读数据库核查。

这些数据库、供应商响应及运行 JSON 属于本地验收证据，不纳入 Git。

## 旧运行重新核对与剩余验收

只读核查 `tmp/moutai-real-api/analysis.db`，structured reconcile `structured-run-c9beb7959d5b62cc14fd5f8b` 在本次检查时为 **8 no_data、3 failed、1 pending**，未 finalized。唯一 pending 是 `unlock_peer`；`company_basic`、`controller`、`repurchase` 均已两次 `parse_failed / eastmoney_business_failure`；8 个 no_data 均为 `eastmoney_9201_empty`，其 `safe_through=null`。旧 `reconcile-last.json` 中的“7 no_data、2 failed、2 pending、1 retryable”是更早状态，不能作为当前最终结果。本次未重新执行或修改该旧运行。

只读核查 `tmp/moutai-cleanup/analysis.db`，此前标记为按需报告的 `c1076c43-1ac9-4f1b-b16e-6eae016e9d2a` 实际 finalized 为 **partial**：目录发现成功，但第一份上交所原文请求遇到 `upstream_bot_challenge`，另外 3 份因同源停止门被跳过，4 个 resource observations 均没有正文 snapshot。它不能作为“按需报告原文采集成功”的证据，也不能验证同一报告重复请求复用正文快照。需要使用确切的研究需求与单份报告，在可访问的已注册来源完成真实按需正文验收；本次没有绕过来源访问限制或扩展为全量下载。

仍未由本项验收覆盖：全部结构化数据集的安全增量、旧 reconcile 剩余 pending/失败/空响应、成功的单份按需原文与重复复用、真实供应商内容变化的新版本、以及使用完整八步输入生成并由用户验收报告。建议单独明确未来 `as_of` 的合同：真实执行拒绝未来 cutoff，历史异常证据保留；不要倒改旧水位线来补验收。
