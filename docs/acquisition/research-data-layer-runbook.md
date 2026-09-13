# 八步数据层运行说明（2026-09-13）

本轮运行链路为：范围与身份校验 → 有界采集及原始响应 → 内容寻址原件 → 带定位解析 → 字段解释与标准化 → 事实及确定性指标 → 54 题逐期间覆盖与后续工作。报告、前端改造、全市场批处理继续暂停。

工作树 `D:/估值模型-worktrees/fact-materialization-ultra`，分支 `codex/fact-materialization-ultra`。真实验收和具体缺口见 [交付验证](research-data-layer-verification.md)，删除清单见 [清理审计](data-layer-cleanup-audit.md)。

## 1. 环境与 API 启动

```powershell
Set-Location 'D:/估值模型-worktrees/fact-materialization-ultra'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH=(Resolve-Path src).Path
# 新 Python 环境才需安装；本机已运行验证。
python -m pip install -e '.[sources,dev]'

# 显式绑定本轮独立采集库；不重绑旧生产库。
python -m analysis.cli serve --host 127.0.0.1 --port 8000 `
  --acquisition-db tmp/research-data-layer-live-v1/analysis.db `
  --acquisition-data-root tmp/research-data-layer-live-v1/data
```

`GET http://127.0.0.1:8000/api/structured/registry` 可检查结构化 API。此启动方式已实际用本地 HTTP 验证；启动本身不发起供应商采集。CLI 的 `plan` 与 API 的 `StructuredPlanRequest` 均支持 `report_periods`、`valuation_start`、`industry_profile_id`。未显式绑定的旧默认 Web 启动不能用来验证结构化运行。

## 2. 七家公司已有数据与原件处理

以下源库只读；输出写入当前工作树，不复制重绑 namespace。

```powershell
$sourceRuntime='D:/估值模型-worktrees/business-model-cninfo-direct-empty/var/pilots/cninfo-content-archive-20260905/runtime'
$researchArgs=@('--db',"$sourceRuntime/analysis.db",'--data-root',"$sourceRuntime/data",'--output','tmp/research-data-layer-v1','--as-of','2026-09-13')

# 先复用已有完整原件，再补缺；目录按 hasMore 和总数验证闭合。
python -m analysis.structured.research documents @researchArgs
python -m analysis.structured.research fetch-documents @researchArgs
python -m analysis.structured.research cache @researchArgs

# 普通重复执行复用物化；显式重算检查同合同结果哈希相等。
python -m analysis.structured.research cache @researchArgs --recompute
python scripts/audit_research_outputs.py --output tmp/research-data-layer-v1 --audit-file tmp/output-before.json
python -m analysis.structured.research cache @researchArgs
python scripts/audit_research_outputs.py --output tmp/research-data-layer-v1 --compare tmp/output-before.json --audit-file tmp/output-after.json
```

`--ticker 600519` 可缩小缓存处理和基线文件补采范围。新分析截止日产生新目录请求；已下载原件按 URL 请求键和 SHA256 复用。修订版与旧版各保留原件及解析，不用后下载的文件覆盖旧证据。`cache` 对同公司多个已完成 run 使用同一解释合同合并，冲突留缺；不会把不同 namespace 自动重绑成一个库。

## 3. 依据缺口精确补采

`<公司>/next-work.json` 区分 acquisition、semantic_processing、document_reading、deterministic_calculation 和 research_context。只有 `acquire_allowed=true` 的缺口进入以下工具。已有但尚未解释的字段不会重复抓取。

```powershell
python scripts/run_research_gap.py `
  --gap-file tmp/research-data-layer-v1/600519/next-work.json `
  --stock-list tmp/data-layer-evidence/cninfo-stock-list.json `
  --output tmp/research-data-layer-live-v1 --ticker 600519 `
  --dataset customers_peer --as-of 2026-09-13

# 上一命令只保存具体计划；加 --execute 即执行正式 runtime。
python scripts/run_research_gap.py `
  --gap-file tmp/research-data-layer-v1/600519/next-work.json `
  --stock-list tmp/data-layer-evidence/cninfo-stock-list.json `
  --output tmp/research-data-layer-live-v1 --ticker 600519 `
  --dataset customers_peer --as-of 2026-09-13 --execute

python -m analysis.structured.research cache `
  --db tmp/research-data-layer-live-v1/analysis.db `
  --data-root tmp/research-data-layer-live-v1/data `
  --output tmp/research-data-layer-live-v1/materialized --as-of 2026-09-13
```

必要时加 `--proxy http://127.0.0.1:7897`。正文适配器在直连传输失败后也会尝试此代理。原件下载失败、来源明确空结果、解析格式未适配和语义未确认分别保留原因；HTTP 403 不会被记成空数据。

EM-F 新增量只补未提交的报告期并刷新最新两期。显式 `--report-period` 限定目录展开；支持筛选的报告期接口同时下推日期集合。估值默认请求近 14 日必要报价，默认物化最后有效交易日；历史估值需在 `structured plan` 指定 `--valuation-start YYYY-MM-DD`，不会取得 OHLCV。

## 4. 问题触发的文件和行业表格

`fetch-selected --entries <文件>` 接受已发现的目录条目 JSON 数组。每项需有 `ticker`、`canonical_resource_id`、`title`、`resource_url`、`published_at`、`expected_mime_types`、`question_ids` 和 `trigger_reason`。文件仍通过 D01–D21 选择门，触发参数不能越过独立审计/英文年报停采规则。

本轮已取得的真实条目可直接复跑：

```powershell
python -m analysis.structured.research fetch-selected @researchArgs --entries tmp/data-layer-evidence/selected-quarter-entries.json
python -m analysis.structured.research fetch-selected @researchArgs --entries tmp/data-layer-evidence/selected-industry-entries.json
python -m analysis.structured.research index-documents @researchArgs
```

PDF 保留页与文本块坐标；HTML 保留段落、物理表格行/单元格位置；JSON 保留字段路径；XLSX 保留工作表/单元格/公式；CSV 保留行列。DOC、旧 XLS、XBRL、ZIP 等尚无本轮验证适配；扫描件需既有 MinerU 流程。原文段落与官方统计表观察可消费，但全文下载/关键词命中不直接关闭研究问题。

## 5. 输出与状态

| 位置（相对本工作树） | 内容 |
|---|---|
| `tmp/research-data-layer-v1/<公司>/` | `manifest.json`、直接/维度事实、事件、原始字段缺口、独立来源核验 |
| 同上 `coverage-facts.jsonl`、`computed-facts.jsonl` | 合并后可用事实；确定性指标的输入 ID、公式版本及精确十进制值 |
| 同上 `question-coverage.jsonl`、`gaps.jsonl`、`next-work.json` | 54 题、401 要求逐期间覆盖；区分数据缺失与处理/研究工作 |
| 同上 `normalized-records.jsonl` | 所选业务/治理/资本记录的维度、日期、生命周期、原始字段及消费边界 |
| `tmp/research-data-layer-v1/acquisition/` | 原件、请求账本及历史；损坏的未完成原件保留在 quarantine 后恢复 |
| `tmp/research-data-layer-v1/parsed/` | `research-parser-v1.0.2` 哈希绑定解析；旧版本保留 |
| `live-documents.json`、`selected-documents/` | 分开记录选择、下载、解析、原文整理和消费状态 |
| `document-evidence.jsonl`、`official-table-facts.jsonl` | 带页/表/单元格定位的原文及官方行业观察 |
| `tmp/research-data-layer-live-v1/` | 独立真实采集库、请求捕获、行业与增量样本、精确补采状态 |
| `tmp/data-layer-evidence/output-audit-after.json` | 原件哈希、独立复算与重复输出字节一致性验收 |

`history/` 是旧物化版本证据。`source_text_available` 表示有可追溯原文，`pending` 继续列明单位/期间、方法、判断或来源缺口。K01–K11 的整条研究计算路线尚未全部自动接入，`calculation_status.complete_route_integrated=false` 明示此处理边界；已有单季/TTM、毛利率和本轮财务指标可以独立使用。报告、前端与全市场任务没有因此自动验收。
