# 数据层清理审计（2026-09-13）

范围：`codex/fact-materialization-ultra`，单 Agent；原始数据、冻结合同、归档和必要历史回放保留。

| 处置 | 对象 | 调用证据与理由 |
|---|---|---|
| 保留 | `structured/{runtime,planner,storage,protocols,records,materialization*,interpretation,repair}.py` | CLI/API/service 使用统一 runtime；repair、分页、namespace 与旧解释回放依赖这些模块。BaoStock 财务指标仍有效，不整体删除 |
| 保留 | `src/analysis/acquisition/adapters/`、`acquisition/legacy.py`、旧注册表历史和仓外规划归档 | 新 acquisition adapters 负责注册表绑定；`acquisition/legacy.py` 只用于历史文档快照 reconcile 和回放，不恢复旧同步入口。 |
| 删除 | `src/analysis/adapters/` 旧生产 adapters | 旧 provider 全量同步已无运行时入口；结构化 API 与 acquisition 注册表承担当前采集。旧适配器测试和兼容同步测试同步删除。 |
| 保留 | `scripts/{archive_cninfo_inventory,parse_announcement_mineru,build_mineru_reading_index,smoke_structured_sources,supervise_structured_repair}.py` | 包含目录归档、解析、真实源取证或恢复逻辑，不是重复入口；其联网范围需要统一治理 |
| 改造 | `structured/runtime.py`、`protocols.py` | 全历史默认和强制 columns=ALL 与新范围冲突；在计划冻结及执行前应用新范围，旧证据不重写 |
| 改造 | `structured/reading.py`、正文选择与解析入口 | 原分类将所有英文材料排除，且没有 D01–D21/期间/问题绑定 |
| 改造 | 物化与 coverage | 旧解释保留；新消费须按研究范围、字段、期间计数，不能用行情数量冲抵财报 |
| 删除 | `scripts/run_demo.py` | 仅转发到早期 CLI 演示子命令；包装脚本和该演示子命令均已删除，虚构报告只保留 `src/analysis/demo.py` 测试夹具，不作为生产入口 |
| 删除 | `scripts/smoke_online_sources.py` | 仅转发现有 `analysis.cli smoke-sources` 参数；README 改为直接 CLI，保留实际持久化探针实现 |

依赖审计：pyproject 中 FastAPI、httpx、DuckDB、openpyxl、PyMuPDF 等仍有有效调用；此次不因删除两个启动包装器移除运行依赖。生成证据目录 tmp/var 不做批量删除，避免误删唯一真实证据。删除前使用 PowerShell 核实绝对路径位于指定工作树；本清单也是实际删除记录。
