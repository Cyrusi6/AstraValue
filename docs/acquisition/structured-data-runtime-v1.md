# 结构化数据优先 v1：配置、运行、恢复与回滚

本文说明 `structured-data-first-v1` 分支的软件入口和验收边界。结构化来源的成功字段可以在来源、字段定义、快照、行键、字段路径和质量检查全部绑定后直接消费；这不等于双源一致、正式披露核验、方法完成或人工验收。

## 配置合同

运行时原子加载 `config/structured_data/` 下的七个版本化注册表：

- `datasets.v1.json`：55 个数据集的协议、参数、历史枚举、行键和空结果合同；
- `fields.v1.json`：2,504 个数据集字段位置、性质、定义/单位状态和唯一主备路由；
- `peer_sets.v1.json`：首批七家公司及已选同行集合；
- `schedules.v1.json`：北京时间调度、重叠窗口、重试和来源间隔；
- `reading_rules.v1.json`：R01–R12 阅读触发；
- `research_requirements.v1.json`：54 个八步问题及 401 个需求项；
- `industry_profiles.v1.json`：11 类行业画像及条件/替代规则。

本轮行键与排序契约修正后的 `datasets.v1.json` 为版本 `1.2.0`，
`content_sha256=53c6de27110a4de071b16123ae63ce5ea95f5f5cadc97f511a7bbd1cdab720e5`。
该 hash 必须随运行冻结并出现在计划/运行证据中；它只标识配置版本，不代表生产全历史已执行。

任何版本、hash、引用、允许参数或唯一主路由校验失败都会在来源 I/O 前拒绝。运行创建后保存完整冻结配置；恢复不得换用当前文件覆盖旧运行。

## 安装与离线检查

```powershell
python -m pip install -e ".[sources,dev]"
python scripts/validate_structured_registry.py
python -m pytest tests/structured -q
python -m compileall -q src
```

所有结构化 CLI 都要求显式绑定隔离的 `--db` 和 `--data-root`。仅查询配置、身份、计划或状态不会隐式安装系统计划任务。

## CLI

```powershell
# 零网络生成并持久化 baseline 计划；留空 --dataset 表示全部适用数据集
python -m analysis.cli structured plan 600519 --mode baseline `
  --company-scope company-only `
  --db var/pilots/structured-v1/analysis.db `
  --data-root var/pilots/structured-v1/data --json

# 对已持久化 run 执行一轮，或从未完成范围恢复一轮
python -m analysis.cli structured run RUN_ID `
  --db var/pilots/structured-v1/analysis.db `
  --data-root var/pilots/structured-v1/data --json
python -m analysis.cli structured resume RUN_ID `
  --db var/pilots/structured-v1/analysis.db `
  --data-root var/pilots/structured-v1/data --json

# 查询运行、记录、阅读任务和逐题覆盖
python -m analysis.cli structured status RUN_ID --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json
python -m analysis.cli structured records --run-id RUN_ID --limit 500 --offset 0 --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json
python -m analysis.cli structured reading-tasks --run-id RUN_ID --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json
python -m analysis.cli structured coverage SNAPSHOT_ID --limit 500 --offset 0 --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json

# 身份、行业画像与同行均为本地状态查询
python -m analysis.cli structured resolve 600519 --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json
python -m analysis.cli structured profile 600519 --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json
python -m analysis.cli structured peers 600519 --company-scope company-with-peers --db var/pilots/structured-v1/analysis.db --data-root var/pilots/structured-v1/data --json
```

`baseline` 枚举供应商可得历史，`incremental` 只计划新期间与登记重叠范围，`due` 只执行本轮到期工作并退出。显式 `--dataset` 是受控子集，不代表全部适用数据集覆盖；不传该参数以及 API/前端传空数组都表示全部适用数据集。

### 行键、分页完整性与报告期缺失

每个数据集的 `primary_key_fields` 必须来自真实响应中的非空且唯一字段；任何以 `__` 开头的运行时合成字段都不得作为行键。若排序列在同一公司内不唯一，注册表会追加稳定的决胜列（例如 `END_DATE,HOLDER_NAME`），使 `page_number` 分页形成全序。`complete_pagination` 和 `on_demand_complete_pagination` 数据集在终页使用页清单核对页序、声明页数、声明总数和去重后的行键数；发现重复、漂移或丢行时记录 `partial_success`，覆盖状态为 `partial`，不推进 `safe_through`，但保留已提交页面和失败证据。

EM-F 的报告日期目录只表示供应商声明的可查询期间，不保证明细接口一定返回该期。`report_period` 批次会在响应行的 `REPORT_DATE` 与请求期间集合之间计算差集；缺失期间写入 attempt/page/coverage payload 的 `absent_periods`，并以 `supplier_period_absent` 作为诊断。查询已执行完毕时覆盖仍可为 `complete`（空批次则为 `no_data`），但验收必须显式读取该字段，不能把目录声明当作明细事实。

分页大小由数据集注册表的 `request.page_size` 冻结并进入请求指纹，缺省为 500。当前已验证的例外为 `surveys=50`、`fund_holds=100`；其余数据集不设置覆盖值并继续使用 500，不在运行时根据 9701 响应自适应改写页大小。

## API

绑定 `AcquisitionRuntime` 后，API 使用同一 database/data-root 的结构化服务。主要入口为：

- `GET /api/structured/registry` 与 `/fields`；
- `POST /api/structured/company-resolution`；
- `GET /api/structured/companies/{ticker}/industry-profile` 与 `/peer-candidates`；
- `POST /api/structured/plans`；
- `POST /api/structured/runs/{run_id}/execute` 与 `/resume`；
- `GET /api/structured/runs/{run_id}`、`/records`、`/reading-tasks`、`/research-coverage/{snapshot_id}`。

新策略 `/api/companies/{ticker}/sync` 在持久入队后返回 202；明确 `source_strategy=legacy-v1` 的旧同步保留 200。校验错误、冻结身份冲突、租约/存储忙和完整性错误分别返回 422、409、503 和 500。响应会过滤凭据、原始正文和不必要的本机绝对路径。

## 隔离真实样本

以下命令只写入 Git 忽略目录，不执行七家公司全历史，也不修改生产库：

```powershell
python scripts/smoke_structured_sources.py `
  --db var/structured-live-20260908/sample.db `
  --data-root var/structured-live-20260908/data `
  --output-dir var/structured-live-20260908/evidence `
  --industry-matrix
```

默认直连并验证 TLS；直连不可用时可显式增加 `--proxy http://127.0.0.1:7897`。脚本保存有界响应、SHA-256 和 checkpoint；再次运行复用已终结步骤。`--force-step NAME` 只重跑指定样本，不清空其他证据。

## 恢复与回滚

执行前可对绑定库创建经过完整性检查的备份：

```powershell
python -m analysis.cli acquisition-db backup `
  --db var/pilots/structured-v1/analysis.db `
  --data-root var/pilots/structured-v1/data --json
```

恢复同一运行时使用 `structured resume RUN_ID`。已提交页、原始 snapshot、失败 attempt 和冻结配置继续保留；达到尝试上限的失败不会无限循环。模块迁移 `structured_data_0001` 为追加、原子且幂等，不提供破坏性 down migration。

### 终态失败的定向补采

普通 `resume` 不领取已经达到两次尝试上限的 `failed` job。只有 baseline supervisor 已退出、活动租约为零且操作者明确发出启动消息后，才可使用独立的 repair 入口；它不会删除原 attempt，也不会创建 incremental。先用终态事件中的完整 `reason_code` 和数据集精确生成内容寻址 manifest：

```powershell
$revision = git rev-parse HEAD
python -m analysis.cli structured repair-plan RUN_ID `
  --dataset baostock_balance `
  --reason "EXACT_REASON_CODE_FROM_TERMINAL_EVENT" `
  --revision $revision `
  --output var/pilots/structured-repair/repair.json `
  --db D:\path\to\analysis.db `
  --data-root D:\path\to\data --json

python -m analysis.cli structured repair-status `
  var/pilots/structured-repair/repair.json `
  --revision $revision `
  --db D:\path\to\analysis.db `
  --data-root D:\path\to\data --json

# 每次命令只执行一轮；同一 manifest 对每个 job 最多追加两次 attempt。
python -m analysis.cli structured repair-run `
  var/pilots/structured-repair/repair.json `
  --revision $revision --max-jobs 25 `
  --db D:\path\to\analysis.db `
  --data-root D:\path\to\data --json
```

生产使用监督脚本时，必须显式传入已退出的原 supervisor PID、revision、namespace 和 manifest hash。脚本最多执行两轮，不等待或轮询 baseline，不安装计划任务：

```powershell
python scripts/supervise_structured_repair.py `
  --workspace D:\path\to\frozen-repair-worktree `
  --db D:\path\to\analysis.db --data-root D:\path\to\data `
  --manifest D:\path\to\repair.json `
  --expected-revision GIT_SHA `
  --expected-namespace NAMESPACE_ID `
  --expected-manifest-sha256 MANIFEST_SHA256 `
  --supervisor-pid EXITED_BASELINE_PID `
  --evidence-dir D:\path\to\repair-evidence
```

BaoStock repair 会先做一次匿名登录探针，再为本轮多个 job 复用同一会话；登录失败返回 `source_unavailable` 且不创建 job attempt。已有有效 observation 的共享日历 snapshot 优先离线重放，状态中的 `query_io_count=0` 表示没有再次发出数据查询；登录探针仍单独记录为来源 I/O。

原渠道不可用不授权自动换源。备用渠道只有在逐数据集真实样本确认字段语义、单位、期间、主键、排序/分页、PIT 可得时间、许可与缺失语义等价，并建立新的真实来源身份和版本后，才能通过独立 change/manifest 执行。否则保持 `fallback_unqualified` 或来源缺口，不能把东方财富近似字段写成 BaoStock。

repair 代码本身没有数据库迁移。回滚时停止 repair 进程并切回原 revision；已经追加的 attempt、snapshot、page、record 和 coverage 是合法不可变历史，不删除。自动测试、隔离真实样本、生产 repair 与独立人工抽样分别记录，任何一项都不自动将 `manual_acceptance` 改为通过。

需要暂时回到旧业务入口时，明确传 `source_strategy=legacy-v1` 和旧 providers；这只切换新请求策略，不倒改已有结构化记录。若必须回退隔离数据库文件，应先停止对应 worker、核对 database/data-root/namespace 与备份清单，再使用已验证备份恢复；不要在生产库上删除结构化表模拟回滚。

## 七家公司生产执行与终验方案（尚未运行）

1. 对实际生产库和数据根做只读绑定预检与备份，记录来源策略、数据集、字段、同行、调度、需求和行业画像版本/hash。
2. 以 `600519`、`baseline`、`peer-set` 创建七家公司全部适用数据集计划；人工核对公司、数据集、目录/日期批次、当前快照/按需/不适用/缺口后，再逐个 `run_id` 执行。
3. 只有分区终页、稳定行键、记录投影和覆盖水位均闭合才将对应分区记为完成。局部成功字段可消费，但七家公司全历史保持 `partial`，直到所有适用分区终结或明确登记不可得边界。
4. 基线结束后，在两个不同的供应商更新时点分别创建并执行 `incremental` 计划；分别核对只新增/更新应取分区、成功缓存未重抓、30 日事件重叠和未完成事项刷新。同一 checkpoint 的立即重复调用不能代替两次生产增量。
5. 最终核对 SQLite、DuckDB/Parquet、报告、API、前端和导出引用相同事实与覆盖快照 ID，再进入独立人工签署。软件测试、隔离样本、全历史生产、两次增量和人工验收分别记录。

停止条件包括 namespace 或冻结 hash 不一致、身份冲突、租约失效/被占用、分页总数或终页不闭合、SDK 结果集未耗尽、来源访问限制、原始响应无法定位、质量规则拒绝或重试耗尽。恢复前必须确认相同冻结上下文仍可读取、绑定一致且来源门允许；随后只对同一 `run_id` 执行 `resume`，从未完成范围继续并保留失败历史。

### “等待独立生产执行与人工验收”的实际操作

交付后由独立执行者在批准的生产库/data-root 上完成以下顺序，软件验收通过不能替代这些步骤：

1. 先做只读 preflight 和备份，核对分支提交 SHA、`datasets.v1.json` hash、namespace、数据库路径、data-root 和活动租约；不一致即停止。
2. 创建七家公司 `baseline` 计划，人工先审核公司身份、适用数据集、目录/日期批次和缺口，再按计划返回的 `run_id` 启动执行。不得把 `plan` 或排队的 202 响应当作生产成功。
3. 仅在每个分区的分页终页、稳定行键、记录投影和覆盖水位闭合后记录该分区完成；传输错误要保留为失败并记录原因，不改代码绕过供应商限制。
4. 基线完成后，在两个不同供应商更新时间分别创建并运行两次 `incremental`，核对只抓新增/更新分区、成功缓存复用、事件 30 日重叠和未完成事项刷新。
5. 独立人工验收者依据人工清单逐条抽样，在确认输出版本、事实 ID、期间/单位/来源定位和八步状态后填写签署记录；签署只覆盖明确样本，不自动授权全历史或投资结论发布。

本轮不执行上述生产步骤；当前状态保持 `full_history_executed=false`、`production_data_modified=false` 和 `manual_acceptance=pending`。

## 当前已知缺口

- 2,487 个字段位置中仍有大量 `unclassified` 或定义/单位待确认项；只有确定性准入子集可以进入公式。
- F05 在银行、寿险/财险、券商样本中出现并返回部分行业字段，但同名列非空不自动证明口径或业务适用。银行部分资本/流动性候选列仍为空；财险的寿险类 `NBV_*` 值不得直接当作财险专用指标。
- 行业规模、渠道库存、白酒批价、全面关联交易、单体报表、债券生命周期、严格 PIT 历史版本等仍按 GAP01–GAP12 展示。
- 方法文档仍为 `content_status: skeleton`；数据可分析、方法就绪、假设确认和研究完成分别显示。
- 本分支只完成隔离代表样本。七家公司全历史生产采集、后续两次增量和人工黄金样本签署均未在本轮执行。

人工抽样入口见 [结构化数据优先 v1 人工验收清单](structured-data-manual-acceptance-v1.md)。
