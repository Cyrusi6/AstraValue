# 结构化数据与研究工作区运行说明

常规链路固定为：结构化 API/CLI 计划与执行 → `StructuredFactMaterializer` →
`research_lite` 轻量包 → 研究工作区 → reporting bridge → `ReportVersion`。

旧的 `analysis.structured.research` 全量缓存、全量报告下载和按供应商拼装入口已删除。报告原文只由研究任务按问题触发，目录和原文状态分别记录；目录请求成功不代表问题已回答。

## 1. 结构化采集

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
python -m analysis.cli structured plan 600519 `
  --mode baseline --company-scope company-only `
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

## 2. 物化与轻量研究包

```powershell
python -m analysis.cli structured materialize <run_id> `
  --research-profile eight-step-lite-v1.0.0 `
  --db tmp/moutai/analysis.db --data-root tmp/moutai/data --json

python -m analysis.research lite `
  --input tmp/moutai/materialized `
  --ticker 600519 --as-of 2026-09-27 `
  --output tmp/moutai/packs
```

`core-pack.md` 是模型首包；`core-pack.json`、`manifest.json` 和 `evidence-index.jsonl` 保存事实、期间、单位、来源哈希和缺口。轻量包不把普通日线或未触发原文作为默认研究输入。

按证据 ID 读取原文：

```powershell
python -m analysis.research evidence `
  --pack tmp/moutai/packs/600519/<pack-id> `
  --evidence-id <evidence-id> --page 1 --max-tokens 2000
```

## 3. 研究工作区与报告

研究任务先调用 `ResearchWorkspace.prepare_research` 建立冻结快照，再按问题调用 `query_research`、`read_evidence` 和 `request_materials`。补充材料只有在问题、影响和材料类型明确时执行；缓存复用、空响应、失败和未实现能力分别记录。

报告只能由冻结研究包调用 `POST /api/structured/reports` 或 `structured report` CLI 生成。生成后的 `ReportVersion` 可通过只读报告查询和导出接口读取；旧的 `/api/reports` 写入口、假设修改、重算、重分析和审阅入口不存在。

## 4. 验收边界

- 自动化采集合同测试不替代真实来源回采；真实回采需记录请求、快照、失败和重放证据。
- `600519` 的 baseline、重复采集、incremental、checkpoint 恢复和一次按需报告采集必须在临时数据根完成后，才能把旧链路删除视为生产验收完成。
- 研究包的 `pending`、`source_text_available`、`capability_gap` 与 `not_applicable` 必须保持分开；未知不补零，空响应不写成“无事项”。
