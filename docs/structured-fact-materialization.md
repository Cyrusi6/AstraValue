# 结构化事实物化专项交付

上一轮完成历史规划中的 **1.1、1.2、1.3**。本轮从 `aace728` 继续完成 **1.4–1.7** 的实现与自动化/真实缓存验证，新增结果见下方；规划文件只作为当时的交付背景，现已移至仓外归档。工作树为 `D:/估值模型-worktrees/fact-materialization-ultra`，分支为 `codex/fact-materialization-ultra`，基于 `8ef6735` 审计和修复已有基础。报告、batch、frontend、主工作树及阶段日志均未纳入本次改动。没有启动全市场采集，没有人工验收。

## 前轮基础行为

- 只消费 finalized run 中成功终态 attempt 的已提交 page/record/field；失败或无成功终态的 attempt 留缺口；校验冻结 dataset/source/field 版本、namespace、snapshot SHA256、policy 和 plan/observation 对应关系。合法复用快照通过原有 observation 合同验证。
- 原字段、原值、原始单位与旧字段表存储的单位分别保留；记录 run、job、page、record version、snapshot、row_key、field_path、field value ID、来源和定义版本。未知、缺定义、缺单位、无效值保留原始表证据及可定位缺口。
- `structured-materialization-v2.0.0` 使用固定兼容合同，而非读取今天的 `FIELD_RULES` 来重新解释旧运行。字段登记 1.0.0、1.1.0 分别有 **16、25** 个已有标准映射同时满足冻结登记的 definition/unit confirmed 与 formula_eligible；其中分红字段仅进入方案事件。现有登记共 31 个 formula_eligible 字段，未具备标准映射的字段不会被静默补定义。
- 新运行若冻结完整 `materialization_contract`，其 field_registry 内容哈希必须与 run context 完全一致，且逐项满足定义、单位和规则合同。当前采集运行不会自动注入或升级此合同。未来登记扩展仍是独立的来源/字段治理工作。
- 流量、存量、比率、报价分开处理。累计转单季与 TTM 复用 `records.derive_single_quarter/derive_ttm`，公式版本为 `structured-periods-v1.0.0`；每个派生结果保存输入事实 ID、值、单位和期间。优先已登记同源单季/TTM，已有单季与累计值矛盾时阻止相关选择和派生。缺季度不补零，存量、比率、价格不累加。
- 同源、同期间、同单位、同范围的营业收入与营业成本可复用 `formulas.gross_margin`；版本 `structured-gross-margin-v1.0.0`，零收入分母输出缺口，亏损/负值不截断。该机械计算不代表 `FIN.INCOME` 或 `FIN.NORMALIZATION` 的 skeleton 正文已经验收。
- 所有原始版本留存；仅显式 supersedes 且时间顺序有效的修订可替代对应旧版本。无修订关系的冲突、不同来源的矛盾值、单位冲突不会按哈希或到达顺序任选一个。相同值可稳定去重。口径、期间、复权、分部类型/代码/父级以及持有人身份参与选择。
- 分部可投影成 `DimensionalFactRecord` 并保存父级/层级；不同产品、地区和持有人保持不同身份。实际现有 `segments` 数值登记仍未知，因此旧运行保留缺口；通过完整冻结合成合同验证分部投影，未擅自放开生产登记。现有已确认的 `FREE_HOLDNUM_RATIO` 已通过真实存储链路的合成测试。
- 已确认语义的 `PRETAX_BONUS_RMB` 投影为现金分红方案条款事件，保留每 10 股原值并换算每股。`announced_plan` 不等于实施或支付，`amount/effective_at` 不填，原进度文本独立保留。现有事件模型下使用 `待核验` 表示方案不具备实际财务数值准入，事件本体和条款已完整持久化。其他缺事件定义的数据保留定位缺口。
- 缺时间戳、无时区时间戳不以 now 补齐；实际可得时点为 record available_at、observed_at、snapshot available_at 的最大值。严格历史模式强制显式带时区截止时间并过滤晚到记录。供应商历史发布日期不被当作历史版本证明。日期精度的中国供应商公告采用当地日末，策略写入事件元数据。

## 存储与消费合同

`service.materialize` 使用已有 bound `ReportStorage`，DuckDB 为 SQLite 同名 `.duckdb`，Parquet 在其同目录的 `parquet/materializations/<hash>/`。数值、维度、事件都写入相同绑定；Parquet 按唯一 ID 排序并原子替换。重复 ID 的不同 payload 在 SQLite/DuckDB 均拒绝覆盖。完成清单仅在全部投影成功后写入追加表 `structured_fact_materializations`，部分写入可通过重复物化恢复。

清单包含全部 evidence IDs、selected IDs、逐字段缺口、期间/冲突缺口、截止条件和两个 Parquet 的定位、数量与 SHA256。数值的“程序计算”状态单独表达确定性推导，不伪装成供应商直采或人工核验。

**消费方必须使用清单的 selected IDs。** 原始事实带 `requires_materialization_selection`，通用 metric-only latest 选择器默认拒绝这些记录，防止绕过期间/修订/冲突检查。可用 `ReportStorage.get_materialization(hash)` 读取清单，再将其 selected IDs 传入 `is_fact_consumable(..., materialization_selected_ids=frozenset(...))`。原始 FactRecord 不因不同截止时点的选择而被覆写。

lineage 入口：

- `GET /api/facts/{id}/lineage`：原始字段定位或递归公式输入。
- `GET /api/dimensional-facts/{id}/lineage`、`GET /api/events/{id}/lineage`：返回精确 committed record/field/page/job/snapshot。
- `GET /api/structured/materializations/{hash}`：固定清单、选择集合与缺口。

SQLite 使用同一只读事务内的分批流式读取（默认 256 条记录），字段查询按 `min(400, SQLite variable limit - 1)` 分块。兼容批量 list API 仍保持全局排序和 limit；测试覆盖 64 个参数上限、2,200 个 ID，以及跨 1,025 条记录的流式读取。内存为投影结果和缺口的规模，**不是常数内存**；不会一次加载全部原始字段 payload。CLI `--summary` 避免再次序列化完整对象。

## 自动化与缓存证据（2026-09-12）

- `python -m pytest tests/structured -o addopts= -q --basetemp=tmp/pytest/structured-committed`：**273 passed in 40.76s**，含 57 项物化专项测试。
- `python -m pytest tests/test_storage.py tests/test_research_data_contracts.py tests/test_formulas.py tests/test_api.py tests/test_acquisition_storage_namespace.py -o addopts= -q --basetemp=tmp/pytest/related`：**36 passed in 8.39s**。
- `python -m compileall -q src/analysis tests/structured`：通过。
- 方法库校验：`METHOD_LIBRARY_OK`；结构化登记校验：`STRUCTURED_REGISTRY_OK`。
- 黄金清单只通过结构校验：10 项中 0 validated、10 pending，`ready_for_acceptance=false`；不是人工验收通过。
- 历史规划校验和 `git diff --check` 结果见本文件末尾交付检查。

合成绑定验收通过 3 个数值事实、2 个不同持有人维度、1 个分红方案事件；SQLite/DuckDB/Parquet 内容、稳定 SHA、精确 lineage、重复调用零新增采集记录全部验证。它使用 HTTP MockTransport，不是当前联网样本。

保留定位：

- 沙箱：`D:/估值模型-worktrees/fact-materialization-ultra/tmp/pytest/structured-committed/test_bound_numeric_dimension_e0`
- run：`structured-run-1dc8a259d7cc39dffeb13699`
- 物化哈希：`3edb561a0abd5e9b9cdbfb9fbe7b562e8bd6e669b10712d49d96110c9723eda4`
- 源测试输出：`tmp/structured-tests.txt`。

**真实缓存只读回放：**

- 数据库：`D:/估值模型/tmp/structured-baostock-full-20260909/rehearsal.db`。
- 数据根：同目录 `data`；先验证路径 identity hash、namespace 和 marker nonce，未复制/重绑/移动数据库。
- run：`structured-run-59762e592da0b46251f13423`；namespace：`f1cb1cfa-6ea5-4091-ada6-380482097f55`。
- 已提交字段 **114,048**；实际可准入数值、维度、事件均为 **0**，原冻结登记 1.0.0 未提供可准入 BaoStock 映射。缺口为定义未确认 2,574、标准映射缺失 32,247、单位定义未确认 79,196、未登记字段 31。这不是采集无数据，也不是数据为零。
- 核验引用原始快照 **521** 个，回放前后字节 SHA 均保持原值；数据库、WAL、marker 哈希未变。
- 两次普通回放耗时 2.702 / 2.708 秒，结果哈希均为 `2c114e73656eaa0865cc52b1dfbc697c5d7695a4b2da949b5134151acbaf1ac5`。
- 开启 tracemalloc 后 Python 分配峰值 93.50 MiB；含完整逐字段缺口结果，不包括解释器全部 RSS，带 profiling 的耗时不作为普通吞吐指标。
- 完整本地证据：`tmp/materialization-replay.json`、`tmp/materialization-profiled.json`。未向原缓存写入任何事实或投影。

## 复现

以下命令均在本工作树的 PowerShell 执行。显式设置 PYTHONPATH，确保调用本分支源码。

```powershell
Set-Location 'D:/估值模型-worktrees/fact-materialization-ultra'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = (Resolve-Path src).Path
New-Item -ItemType Directory -Force tmp | Out-Null
$env:TEMP = (Resolve-Path tmp).Path
$env:TMP = $env:TEMP
python -m pytest tests/structured -o addopts= -q --basetemp=tmp/pytest/reproduce
python -m compileall -q src/analysis tests/structured
历史规划校验（已归档）
```

原缓存只读回放使用独立 reader，避免 CLI service 的 runtime bootstrap 对原库进行常规注册写入：

```powershell
python -m analysis.structured.materialization_replay structured-run-59762e592da0b46251f13423 --db 'D:/估值模型/tmp/structured-baostock-full-20260909/rehearsal.db' --data-root 'D:/估值模型/tmp/structured-baostock-full-20260909/data'
```

在保留的合法合成沙箱上重复写入投影：

```powershell
python -m analysis.cli structured materialize structured-run-1dc8a259d7cc39dffeb13699 --db 'D:/估值模型-worktrees/fact-materialization-ultra/tmp/pytest/structured-committed/test_bound_numeric_dimension_e0/bound.db' --data-root 'D:/估值模型-worktrees/fact-materialization-ultra/tmp/pytest/structured-committed/test_bound_numeric_dimension_e0/data' --summary --json
```

需要严格截止时点时增加 `--as-of 2026-09-01T00:00:00+00:00 --strict-historical`；只计算不写投影时增加 `--no-persist`。这里的 service bootstrap 仍只针对独立合成沙箱，不用于原缓存。

## 本次文件清单

- `src/analysis/structured/materialization.py`：准入、标准化、选择、期间与公式派生、维度和事件。
- `src/analysis/structured/materialization_contracts.py`：固定历史登记兼容合同及拒绝原因。
- `src/analysis/structured/materialization_replay.py`：原缓存绑定验证、只读回放和快照 SHA 验证。
- `src/analysis/structured/storage.py`：参数数量有界的批量/流式读取。
- `src/analysis/structured/service.py`：本任务 materialize 的同库投影和清单落库。
- `src/analysis/structured/consumption.py`：物化清单的消费选择门。
- `src/analysis/models.py`：增加需要公式和输入 lineage 的程序计算状态。
- `src/analysis/storage.py`：物化清单和字段级 lineage 读取。
- `src/analysis/timeseries.py`：不可覆盖校验与确定排序的物化 Parquet。
- `src/analysis/cli.py`：materialize 的 summary 开关。
- `src/analysis/api.py`：只增加物化清单 lineage 查询。
- `tests/structured/test_materialization.py`、`tests/structured/test_materialization_projection.py`。
- 历史规划任务文件：仅 1.x（已随规划材料移出主仓库）。
- `docs/structured-fact-materialization.md`：本交付记录。

## 残留边界

旧默认合同中 BaoStock 缓存数值仍不准入；显式新解释的非零结果见下文。分部数值的生产登记、其他未定义事件类型仍有可追溯缺口；没有通过修改冻结合同掩盖它们。新数据或修订追加新的物化版本，旧事实和清单保持不变。报告/批处理/前端继续暂停，方法正文 skeleton、真实当前联网和人工黄金验收均未完成。

## 交付检查

- 历史规划严格校验：当时返回 valid；规划文件现已归档。
- `git diff --check`：通过。
- 前轮规划记录为 3/12（1.1–1.3）；当时新增 1.4–1.7，本轮不修改历史勾选。

## 本轮后补解释交付：1.4–1.7

已通过正常显式合同入口得到 **100,189 条选中可消费事实**（81,965 个非状态数值、18,224 个状态编码），覆盖全部实际有数值的 BaoStock 数据集。61 个字段规则具有官方定义/单位/定位证据；60 个实际非空。原字段表和原定义版本没有重写。完整逐字段说明见 [BaoStock 解释依据](acquisition/baostock-interpretation-v1.md)，机器验收记录见 [验证 JSON](acquisition/baostock-interpretation-validation.v1.json)。

| dataset | 原记录数 | 事实数 |
|---|---:|---:|
| baostock_adjust | 31 | 93 |
| baostock_balance | 78 | 468 |
| baostock_basic | 1 | 2 |
| baostock_calendar | 0 | 0 |
| baostock_cash_flow | 78 | 468 |
| baostock_daily | 6074 | 97110 |
| baostock_dupont | 78 | 624 |
| baostock_growth | 78 | 390 |
| baostock_operation | 78 | 448 |
| baostock_profit | 78 | 586 |

新解释为 `baostock-interpretation-v1.0.0`，生效时间 `2026-09-12T06:20:00+00:00`；严格历史在此时间前拒绝 100,399 个映射候选，得到 0 事实。新解释后每条 `available_at` 至少晚于定义生效时间，并同时保留原观察/快照可得时间和 pubDate；没有把当前解释当成旧运行当年已知。

- 两次真实物化 hash：`2f40f713dfdb7a085782dd0d0a84afe54a066ffe6b45dc6097328e3042f905f0`；耗时 12.861 / 12.586 秒；`repeat_hash_equal=true`。
- 组合物化合同 hash：`6dc10e774f265b969b58df6986d007ceb70e41cf904b7c34c6ba1b9c2dbee491`；新增解释自身 hash：`9521639a413e60c670eb00a892343e7825eaf8f4fb91f93244d0a0f9289e598d`。
- 旧默认结果仍为 0，hash 保持 `2c114e73656eaa0865cc52b1dfbc697c5d7695a4b2da949b5134151acbaf1ac5`，四项缺口计数完全相同。
- 独立 audit 不调用 materializer 或映射换算函数：100,189 条逐一核对原快照 bytes SHA、row_ordinal、row_key、field_path、原字段版本、run/job/page/成功 attempt、原值、输出单位和独立 Decimal 倍率、报告日期、可得时间；521 个快照通过，映射非空原值与选中输出计数完全对应。
- 原库 SHA：`72e339d2ad926351ed304401f77ea32cc8d412d9d717d1ed1793bf0d3f9b3956`；WAL/marker 和 521 引用快照不变，attempt 1,009 → 1,009（新增 0）。
- 全量相关验证：**356 passed in 54.36s**（structured 320 + 受影响回归 36，含新增解释 47）；独立 audit 补充 attempt/单位核验后，绑定 CLI/audit 集成复测 1 passed in 1.65s；compileall、历史规划校验、git diff --check 通过。测试输出 `tmp/baostock-evidence/regression.txt`。

本轮通过 `service.materialize(..., interpretation_contract=...)` 冻结完整后补合同并投影到合法同绑定存储；该链路的合成测试覆盖 SQLite manifest、lineage、DuckDB/Parquet 及重复调用。本次真实缓存采用只读计算导出，**没有持久化到原 ReportStorage**：source namespace 为 `f1cb1cfa-6ea5-4091-ada6-380482097f55`；projection namespace 为 null，表示普通文件而非伪造的新绑定。完整事实文件约 418.1 MiB；内存随事实/缺口输出规模增长，原记录读取仍为 256 条分块，并非恒定内存。

本地完整定位：`D:/估值模型-worktrees/fact-materialization-ultra/tmp/baostock-materialization-v1/`，含 `facts.jsonl`、`manifest.json`、`coverage.json`（61 个指标包含 0 数值项）、`sources.jsonl`、`field-gaps.jsonl`、`samples.json`、`independent-audit.json`、`replay-summary.json`。这些大输出不提交；提交证据摘要和可复现入口。

```powershell
Set-Location 'D:/估值模型-worktrees/fact-materialization-ultra'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = (Resolve-Path src).Path
python -m analysis.structured.materialization_replay structured-run-59762e592da0b46251f13423 --db 'D:/估值模型/tmp/structured-baostock-full-20260909/rehearsal.db' --data-root 'D:/估值模型/tmp/structured-baostock-full-20260909/data' --interpretation-contract baostock-interpretation-v1.0.0 --output-dir tmp/baostock-materialization-v1
python -m analysis.structured.materialization_audit --db 'D:/估值模型/tmp/structured-baostock-full-20260909/rehearsal.db' --data-root 'D:/估值模型/tmp/structured-baostock-full-20260909/data' --output-dir tmp/baostock-materialization-v1
```

删除 `--interpretation-contract` 复现旧合同结果；增加 `--as-of 2026-09-12T06:19:59+00:00 --strict-historical` 复现定义生效前过滤。只读 reader 才能用于原缓存；普通 `analysis.cli structured materialize ... --interpretation-contract ...` 用于允许 runtime bootstrap 的合法绑定存储。

剩余缺口：210 个空值；1,444 个 `reported_period` 数值只确认报告期末、未确认累计/单季窗口，因此不派生单季/TTM；日历无缓存记录且未新增解释；13,618 个身份/日期/文本字段不作为数值；31 个旧技术字段继续 unknown。利润金额的单位为元，股本为股，均已纳入；未把 ratio 当金额。报告/batch/frontend、主工作树、其他任务日志均未修改。公开文档当前联网核实、历史真实缓存回放、人工验收三个状态分别保留，人工验收仍由主 agent/用户独立完成。

本轮提交文件为 `interpretation.py`、解释合同配置及三份源证据/验证文档、`materialization.py`、`materialization_replay.py`、`materialization_audit.py`、`records.py`、`service.py` 的 materialize 参数、`cli.py` 的 materialize 参数、新增专项测试及本交付记录；不提交规划材料或本地原始/大输出文件。

## 主 Agent 收尾复核（2026-09-13）

实施提交为 `9bc8b677128979d58aceaf7a9ed218aa1f63aacd`。主 Agent 独立复跑 `tests/structured/test_baostock_interpretation.py`，结果为 **47 passed in 4.45s**；复跑上文 `materialization_audit` 命令，核验 **100,189 条事实、521 个快照**，`all_mapped_numeric_inputs_accounted_for=true`，物化哈希与交付记录一致。前述 356 项全量相关测试为实施 Agent 的运行记录，本次主 Agent 未重复全量测试。

依据代码交付、专项复测与真实缓存审计，主 Agent 将历史规划中的 **1.4–1.7** 标记完成；事实物化任务为 **7/7**，整个历史变更为 **7/16**。210 个空值、1,444 个期间窗口未确认事实及空日历缓存的边界保持不变。报告、批处理、前端和全局交付任务当时继续暂停；当前行情采集和人工黄金验收未完成。
