# 旧文件名与研究输入合同迁移审计（2026-10-02）

本轮检查当前结构化导出、轻量包、研究工作区、计算与图表入口、报告桥接、CLI、脚本和活动运行说明。已修复以下 14 类读取或文件合同问题。原始采集数据、已冻结快照及历史报告不改写；没有恢复旧全量采集编排。

## 修复清单

| 编号 | 问题及实际影响 | 当前处置 |
|---|---|---|
| F01 | 新导出为 `facts.jsonl` / `dimensional-facts.jsonl`，lite 与 bridge 只找 `coverage-facts.jsonl`，事实被当成空集合 | 直接消费正式文件与 manifest 选中版本；缺文件、缺清单、缺选中版本显式报错 |
| F02 | 多个运行在 `runs/<run_id>/`，根目录或精确键读取漏掉运行；空 reconcile 可能遮住 baseline；同行有同类问题 | 主公司及同行统一发现当前根与 `runs/*`，排除 `history/*`；所有文件及运行清单进入输入哈希 |
| F03 | `normalized-records.jsonl` 的旧生产者被删，公司业务、管理层和资本记录全部丢失 | 新增只读、范围受限的记录投影；从已提交且验证成功的页读取，核对快照/namespace/来源/许可/可得时间，仅保留白名单字段；保留数字准入边界及版本血缘 |
| F04 | `events.jsonl` 未注册或读取，正式分红事件及独有来源漏出报告，另会重造观察时间事件 | 冻结并原样传递正式 EventRecord；补齐来源绑定；工作区及材料目录可读取与引用；已有正式事件不再生成重复替代事件 |
| F05 | `question-coverage.jsonl` / 逐题工作项生产者被删，54 题空覆盖却写已完整保留；bridge 也漏嵌套键 | 基于正式选中事实、原始记录、文档证据和版本化注册表重建 401 项要求；包内冻结完整覆盖，旧包支持嵌套读取；数字、原文、待解释、待研究分别记录 |
| F06 | 独立校验脚本仍只读 `coverage-facts.jsonl`，报 `source_fact_missing` | 支持正式事实与维度、当前多运行及选中清单，保留哈希、计数、冲突与派生公式的独立核验 |
| F07 | 资料目录精确索引旧根文件；正式指标可用却显示整个数据集缺失 | 按文件名读取已冻结根/运行文件；按准确 fact_id 反查原数据集；已有目录追加新索引，空文件不遮住有效记录 |
| F08 | 补采脚本强制旧 `next-work` 的 `scope_sha256` 和每项 dataset_id，当前任务混有阅读/计算导致 KeyError | 消费当前 profile/items；只执行有 dataset_id、允许采集的采集任务；其他项记录跳过理由，失败及未完成项保持非零退出状态 |
| F09 | 正式导出复用旧目录后，旧 records/coverage/work 留在旁边，会混入新事实 | 导出声明版本及完整文件哈希；正式目录只读取声明产物；旧文件只在确认为旧格式的目录兼容读取 |
| F10 | 缓存只看数据库、run 与 profile ID，升级后仍复用缺辅助文件的旧输出 | 缓存绑定导出版本、记录投影版本、profile/解释内容哈希、截止日及数据库/WAL；复用前核验全部必需文件 |
| F11 | 手册先执行只写数据库/Parquet 的 materialize，再读取未生成的 JSONL 目录 | 现有 `structured materialize` 增加 `--output-dir`，复用同一个导出器；手册接通实际命令及带截止日的 pack 路径 |
| F12 | `prepare_processing`、`prepare_statements` 固定复制五个文件，再次丢掉新覆盖产物 | 派生候选校验并继承父包全部声明文件的原始 bytes，再替换实际加工结果；候选版本及身份升级，未知附加文件也保留 |
| F13 | 工作区只校验旧五个固定输出，新声明文件可能漏验 | 保留五项基础必需检查，并校验全部声明产物与路径范围；缺失、篡改或越界失败 |
| F14 | 公告缓存相对路径按进程目录解释；默认配置仍选历史缓存 | 相对路径统一按 workspace 根解析；新任务用专用配置明确登记新投影，运行说明说明旧默认路径的用途 |

补充收紧了三个直接影响迁移结果的条件：原始记录不包含公司类型/报告日期目录等前置查询页；构建研究包时排除截止日之后才可得的记录和事件；同一来源行仅按显式且时间有序的 supersedes 选择修订，未解决的记录冲突保留。完整查询为空的凭证进入目录，失败与未完成状态继续保留；没有将 API 空返回改成“公司无事项”。

## 保留的名称与范围

- `coverage-facts.jsonl`：仅作历史冻结投影读取兼容，新生产链不再生成它。
- `normalized-records.jsonl`：保留有意义的供应商原始字段结构，由新的受限投影生产；不承接旧全量报告下载。
- `question-coverage.jsonl`、`next-work.json`：由当前研究包按问题重建，不再依赖已删除的 `structured/research.py`。
- acquisition/MinerU 目录中的 `events.jsonl`：是解析过程事件日志，有独立生产者和目录；不能按同名文件混入结构化 EventRecord。
- `materialization_audit` 的 replay-summary：有独立回放审计合同，继续保留。
- 旧默认配置路径在当前机器确实存在，属于历史样本绑定；没有删除或偷偷替换。`income_quarter`、`cashflow_quarter`、`litigation`、`violation`、`allotment` 的部分研究路由为空属于现有范围配置，本轮保留字段白名单与采集凭证，不调整功能取舍。

## 贵州茅台真实缓存复验

本轮只读回放 2026-10-02 已真实取得的数据，不发起新请求。源库为 `tmp/moutai-api-final-20261002/analysis.db`，baseline 为 `structured-run-d58b1fad57728dae9cdb5fb3`，incremental 为 `structured-run-76bb54846a81e7b14b02980b`。

| 检查项 | 结果 |
|---|---|
| baseline 输出 | 1,514 条事实、273 条维度事实、8 条事件、252 条业务记录 |
| incremental 输出 | 89 条事实、24 条业务记录 |
| 研究必需输入 | 299 ready、2 source_text_available、12 pending，共 313 项 |
| 逐题覆盖 | 54 题、401 项要求、1,897 个期间输入；327 ready、113 source_text_available、1,454 pending、3 not_applicable |
| 研究原始字段目录 | 13 类已取得业务数据集；治理摘要保留 12 条记录 |
| 分红事件 | 8 条通过工作区查询、分页阅读、引用与报告桥接；公告时间、每股换算、来源和 announced_plan 性质保留 |
| 报告输入 | 485 条事实（含别名）、12 条维度事实、27 个来源、8 条事件 |
| 半年累计数据 | 收入 90,703,260,964.48 元、归母净利润 44,516,880,421.86 元；累计口径及 snapshot/row/field 血缘核对一致 |
| 独立公式/值检查 | 595 项通过 |
| 缓存重复性 | 源库全部 6 个运行可导出，第二次全部复用；重复 prepare 复用同一研究快照 |
| 原件保护 | 129 个受保护文件哈希不变，涵盖源库、原件、旧导出与先前快照；新增采集请求为 0 |

12 项 pending 为价格与总市值、7 类定位原文、同行、年报原文与中报原文。该配置的 evidence_roots 仍为空；已下载原件可以在研究任务中按需解析和采用，不能把这些状态直接解释为来源没有资料。54 个研究问题仍需模型实际分析。

新的报告任务恢复配置为：

```text
tmp/moutai-report-astra-medium-20261002/workspace-config-repaired-v3.json
state_root: tmp/moutai-report-astra-medium-20261002/state-repaired-v3
research_id: r_e5e30487e019b5d45967ff1f
snapshot_id: lite-pack-34e74a8933488bdd975e2a47
```

同一 research_id 可存在于不同状态根，调用时必须带上述专用配置。先前空快照、v1/v2 修复样本保持原样，不自动替换已冻结任务。

复现命令：

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
python -X utf8 tmp/moutai-projection-fix-20261002/verify_complete.py
```

证据在 `tmp/moutai-projection-fix-20261002/acceptance-complete.json`。原始数据及生成包保留 Git 忽略。本轮没有生成研究正文或成品报告，独立报告验收仍等待用户通知。合并 main、推送及删除分支/worktree 尚未执行。

## 自动验证结果

- 全量 Python：1,828 passed、14 skipped、2 failed，391.37 秒。新增记录/覆盖/事件/CLI/候选包继承测试均在全量运行中通过，详见 `full-regression.xml` / `full-regression.log`。
- 注册表子进程测试失败原因为 Windows 编码：外层 `python -X utf8` 不会把同样设置自动传给子进程，导致中文 stderr 解码异常。按项目环境设置 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8` 后，该项复测 **1 passed**，见 `encoding-recheck.xml`；未修改注册表代码或测试断言。
- 最终仍未通过的项目为既有知识发布门 `test_all_54_questions_and_recorded_acceptance_are_ready`，缺 `agent_samples` 和 `human_review`，54 个知识问题的基础映射已就绪；本轮没有伪造验收记录或放宽门槛。
- 14 个跳过项涉及显式开启的 PDF 视觉、Docker 沙箱及本地真实缓存验证；本轮真实输入另由上述脚本实查。
- `npm run build`、`openspec validate --all --strict`（2 passed / 0 failed）、`git diff --check` 通过。当前运行时旧文件名引用扫描仅剩清单中说明的历史兼容或独立生产者。
