# 结构化数据与研究工作区运行说明

常规链路固定为：结构化 API/CLI 计划与执行 → `StructuredFactMaterializer` →
`research_lite` 轻量包 → 研究工作区 → reporting bridge → `ReportVersion`。

旧的 `analysis.structured.research` 全量缓存、全量报告下载和按供应商拼装入口已删除。报告原文只由研究任务按问题触发，目录和原文状态分别记录；目录请求成功不代表问题已回答。

## 1. 结构化采集

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
python -m analysis.cli structured plan 600519 `
  --mode baseline --company-scope company-only `
  --as-of 2026-09-27T00:00:00+00:00 `
  --research-profile eight-step-lite-v1.0.0 `
  --db tmp/moutai/analysis.db --data-root tmp/moutai/data --json
```

计划只做身份、字段、期间和来源校验，不产生网络 I/O。使用返回的 `run_id` 执行或恢复：

```powershell
python -m analysis.cli structured run <run_id> `
  --db tmp/moutai/analysis.db --data-root tmp/moutai/data --json
python -m analysis.cli structured resume <run_id> `
  --db tmp/moutai/analysis.db --data-root tmp/moutai/data --json
```

重复执行复用已提交的 snapshot 和 checkpoint；出现部分失败时先查询 `status`，再按运行记录执行 `resume` 或明确的 `reconcile`，不手工修改水位线。

查询完整但为空时，记录“本次已查完但没有数据”，保留旧数据，下一轮仍可联网；首次采集为空也适用。只有超时、失败、漏页等未完成窗口需要重试或恢复。贵州茅台默认采集按已确认延期清单执行 22 项，9 项原始空响应和报告证据保留，显式 `--dataset` 可补采。

增量同时处理正常更新和原窗口补采，失败项不会挡住其他独立任务。恢复会重新核查已保存页，异常页重取，有效终页补写完成状态。运行结束查看 `status.summary`：`updated`、`empty`、`unfinished` 分别为有数据、完整为空、仍未完成的任务，未完成项附原因与下次恢复位置。工作区有未完成项时保持 `partial`，可用数据仍形成候选快照。

## 2. 物化与轻量研究包

```powershell
python -m analysis.cli structured materialize <run_id> `
  --research-profile eight-step-lite-v1.0.0 `
  --as-of 2026-09-27T00:00:00+00:00 --no-persist --summary `
  --output-dir tmp/moutai/materialized/600519/runs/<run_id> `
  --db tmp/moutai/analysis.db --data-root tmp/moutai/data --json

python -m analysis.research lite `
  --input tmp/moutai/materialized `
  --ticker 600519 --as-of 2026-09-27 `
  --output tmp/moutai/packs
```

`core-pack.md` 是模型首包；`core-pack.json`、`manifest.json` 和 `evidence-index.jsonl` 保存事实、期间、单位、来源哈希和缺口。轻量包不把普通日线或未触发原文作为默认研究输入。

`--output-dir` 导出正式 `facts.jsonl`、`dimensional-facts.jsonl`、来源、事件、研究记录及 `manifest.json`，在 `exports` 中返回文件清单和哈希；绝对本地路径可能被 CLI 脱敏，文件位于指定输出目录。上例 `--no-persist` 不写报告事实与时序投影；导出的 JSONL 是本地文件输出，不产生采集请求。去掉该参数会同时持久化报告投影。每个 baseline、incremental 或 reconcile 运行分别导出到自己的 `runs/<run_id>`，再统一构建研究包，避免只导出一次增量而遗漏历史期间。

轻量包直接读取各清单的选中版本及 `runs/*` 当前运行；`history/` 不作当前输入。文件或消费清单缺失时应报错，不能解释成供应商无数据。旧研究包保持冻结，修复后通过新的包版本和独立研究状态恢复；本轮实例见 [茅台衔接修复](moutai-projection-bridge-repair-20261002.md)。

按证据 ID 读取原文：

```powershell
python -m analysis.research evidence `
  --pack tmp/moutai/packs/600519/2026-09-27/<pack-id> `
  --evidence-id <evidence-id> --page 1 --max-tokens 2000
```

## 3. 研究工作区与报告

研究任务先调用 `ResearchWorkspace.prepare_research` 建立冻结快照，再按问题调用 `query_research`、`read_evidence` 和 `request_materials`。补充材料只有在问题、影响和材料类型明确时执行；缓存复用、空响应、失败和未实现能力分别记录。

新回采验收使用独立配置 `tmp/moutai/workspace-config.json`，从 `config/research_workspace.json` 复制后核实以下绑定，再执行工作区入口。默认配置包含历史缓存位置，不能据此证明新回采已接通；所有配置相对路径均相对项目根目录。

| 配置项 | 本次验收绑定 |
| --- | --- |
| `state_root` | `tmp/moutai/workspace-state`，建立新研究状态 |
| `pack_roots` | `["tmp/moutai/packs"]`，不同时登记旧包根目录 |
| `projection_roots` | `["tmp/moutai/materialized"]` |
| `identity_file` | 实际取得并核验的官方证券主数据 JSON 路径 |
| `evidence_roots` | 本次已保存的原文与目录证据路径；未取得则保留缺口 |
| `valuation_caches` | 已核验具备所需历史行情的绑定数据库和数据根；未取得则保留缺口 |

```powershell
python -m analysis.research.cli prepare_research `
  --config tmp/moutai/workspace-config.json `
  --arguments '{"company":"贵州茅台","as_of":"2026-09-27"}'
```

后续工作区命令使用同一个 `--config`，并核对返回研究任务的 pack 路径和输入哈希。已经冻结的旧任务不会因新导出自动切换输入。

`next-work.json` 同时包含采集、计算、阅读和解释任务。`scripts/run_research_gap.py` 只处理当前 profile 下具有 `dataset_id`、`stage=acquisition` 且 `acquire_allowed=true` 的选定数据集任务；无数据集身份的任务记录跳过原因，交由研究工作区 `request_materials` 解析。脚本保留执行失败及未完成窗口，返回非零退出码；跳过任务不表示这些研究缺口已经解决。

报告只能由冻结研究包调用 `POST /api/structured/reports` 或 `structured report` CLI 生成。生成后的 `ReportVersion` 可通过只读报告查询和导出接口读取；旧的 `/api/reports` 写入口、假设修改、重算、重分析和审阅入口不存在。

## 4. 验收边界

- 自动化采集合同测试不替代真实来源回采；真实回采需记录请求、快照、失败和重放证据。
- `600519` 的 baseline、重复采集、incremental、checkpoint 恢复和一次按需报告采集必须在临时数据根完成后，才能把旧链路删除视为生产验收完成。
- 研究包的 `pending`、`source_text_available`、`capability_gap` 与 `not_applicable` 必须保持分开；未知不补零，空响应不写成“无事项”。
