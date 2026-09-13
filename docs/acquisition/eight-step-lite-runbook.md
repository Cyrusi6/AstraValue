# 八步轻量核心版运行说明（2026-09-13）

当前实现版本为 `eight-step-lite-pack-v1.0.3`，profile 为 `eight-step-lite-v1.0.0`。默认命令只读取既有事实投影和原件索引，输出供模型阅读的轻量研究输入，不生成报告、评级、目标价或交易指令。七家公司实际验收见 [逐公司验收表](eight-step-lite-acceptance.md)。

## 1. 环境与默认离线构建

```powershell
Set-Location 'D:/估值模型-worktrees/fact-materialization-ultra'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH=(Resolve-Path src).Path
```

茅台包显式引用已保存的期后公告目录证据；该命令本身不联网：

```powershell
python -m analysis.structured.research lite `
  --input tmp/research-data-layer-v1 `
  --supplement tmp/research-data-layer-live-v1/materialized `
  --supplement tmp/eight-step-lite-v1/network-audit/2026-09-13 `
  --ticker 600519 --as-of 2026-09-13 `
  --output tmp/eight-step-lite-v1
```

其余六家公司可由同一进程按参数顺序串行构建：

```powershell
python -m analysis.structured.research lite `
  --input tmp/research-data-layer-v1 `
  --supplement tmp/research-data-layer-live-v1/materialized `
  --ticker 000858 --ticker 000568 --ticker 000596 `
  --ticker 002304 --ticker 600809 --ticker 603369 `
  --as-of 2026-09-13 --output tmp/eight-step-lite-v1
```

同一 profile、源投影、同行输入、证据索引、截止日和预算会得到稳定 `pack-id`。再次运行返回 `cache_reused=true`、`performed_network_io=false`；同行 `coverage-facts.jsonl` 的路径和 SHA256 也进入缓存键，同行更新不会错误复用旧包。

## 2. 显式刷新期后公告目录

仅在确需核查最新事项时增加 `--execute`。它刷新当年有界公告目录并登记 D01–D21 类别，不下载未触发正文：

```powershell
python -m analysis.structured.research lite `
  --input tmp/research-data-layer-v1 `
  --supplement tmp/research-data-layer-live-v1/materialized `
  --ticker 600519 --as-of 2026-09-13 `
  --output tmp/eight-step-lite-v1 --execute
```

2026-09-13 已实际执行一次，证据位于 `tmp/eight-step-lite-v1/network-audit/2026-09-13/live-documents.json`：4 个未缓存请求、62 条公告、3 页闭合，`terminal=true`、`body_downloaded=false`。无需更新时不要为验收重复联网。若目录不可达，保留直连、`http://127.0.0.1:7897` 或适用官方备选的实际失败证据，不把目录失败写成“无重大变化”。

## 3. 定向读取原文

从 `evidence-index.jsonl` 或 `core-pack.md` 取得真实 evidence ID，再按页读取：

```powershell
python -m analysis.structured.research evidence `
  --pack 'tmp/eight-step-lite-v1/600519/2026-09-13/<pack-id>' `
  --evidence-id '<evidence-id>' --page 1 --max-tokens 2000
```

返回值包含 `token_count`、`truncated`、`next_page`、定位、语义状态和 `original_hash_verified`。`source_text_available` 只说明原件及上下文可读，不说明对应问题已经形成研究结论。

## 4. 输出与模型阅读顺序

每个当前包位于 `tmp/eight-step-lite-v1/<ticker>/<as-of>/<pack-id>/`：

| 文件 | 用途 |
|---|---|
| `core-pack.md` | 唯一默认模型输入，含六组核心内容、三年/八季度、54 题导航、缺口、冲突和短引用 |
| `core-pack.json` | 精确数值、单位、期间、事实 ID、公式输入、namespace、前期事实和质量状态 |
| `core-coverage.json` | 核心 requirement 状态以及 54 题 `core/conditional/deferred` 路由，不改写全量覆盖分母 |
| `evidence-index.jsonl` | 原件哈希、URL、本地路径、页/表/字段定位及原文哈希 |
| `next-work.json` | 仅激活范围内的 acquisition、parsing/semantic、document reading、calculation 和 research context 工作 |
| `manifest.json` | profile、源投影、同行输入、输出哈希、token 方法、请求统计和缓存身份 |

给模型的约束是：先读 `core-pack.md`；只引用已有事实；缺证据时按 ID 有界读取原文；假设与结论另列；不得自动评级。主包默认上限 15,000 token，本环境没有 `tiktoken`，所以使用 `ceil(UTF-8 bytes / 2)` 的保守估计。必保数字、风险、冲突、核心缺口和引用仍超限时返回 `budget_exceeded`，不会截断后宣称完整。

## 5. 自动验收

```powershell
python scripts/validate_eight_step_lite.py `
  --output-root tmp/eight-step-lite-v1 `
  --as-of 2026-09-13 `
  --output tmp/eight-step-lite-v1/acceptance.json
```

验证器按每家公司 `last-run-audit.json` 选择当前包，独立核对输入/同行/输出/原件哈希、三年八季度、六组、54 题路由、预算、事实值、派生和同比输入链。它不调用打包器实现函数，也不替代人工对业务语义、重大风险和投资判断的验收。

## 6. 明确保留的边界

- 当前只验收七家白酒；银行、保险、券商和未知行业返回“轻量画像未验证”，不能套普通企业公式。
- 七包构建和复跑均为零网络；当前联网只完成茅台期后目录，其余六家公司目录仍待按用途刷新。
- 54 题质量状态仍为 `pending`，因为全量 requirement 的语义、方法和外部研究缺口没有被轻量路由删除。
- 报告、前端和全市场批处理仍暂停；人工业务验收为 `pending`。
