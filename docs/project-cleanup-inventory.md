# 项目精简盘点与处置清单

更新时间：2026-10-02（延期范围、增量和空查询规则已落实，失败隔离与恢复已接入）
盘点工作树：`D:\估值模型-worktrees\fact-materialization-ultra`  
当前分支：`codex/fact-materialization-ultra`（清理、结构化增量修复和回采记录已提交）

这份清单把代码事实、已确认的产品决策和后续验收分开记录。表中的“动作”是目标处置，不表示已经完成删除或合并；在验收门通过前，不得用 `git branch -D`、工作树删除或批量删除替代迁移。

## P01-P14 功能处置

| 编号 | 功能与当前证据 | 目标动作 | 完成条件与边界 |
|---|---|---|---|
| P01 | 结构化数据采集与任务运行：`src/analysis/structured/{service,runtime,planner,scheduler,storage,records,protocols,registry,reading,coverage,repair}.py`；`src/analysis/api.py` 的 `/api/structured/*`；`src/analysis/cli.py` 的 `structured` 子命令。 | **保留并作为唯一常规入口**。`/api/structured/*` 和结构化 CLI 承担计划、执行、恢复、记录、阅读任务、覆盖查询。 | 旧全量同步移除后，结构化 API 仍可独立建立、执行、恢复和查询 600519；中断恢复、幂等、来源门禁和持久化测试通过。 |
| P02 | 结构化事实物化：`src/analysis/structured/materialization.py`、`materialization_contracts.py`、`materialization_audit.py`、`materialization_replay.py`，并由 `structured materialize` 调用。 | **保留为唯一 `StructuredFactMaterializer` 路径**；旧的直接财务/行情拼装不再并行维护。 | 物化结果的事实数量、期间、单位、来源和哈希可回溯到结构化记录；旧数据只作为显式迁移/回放输入，不作为生产入口。 |
| P03 | 八步轻量研究包与报告桥接：`src/analysis/structured/research_lite.py`、`reporting_bridge.py`、`report_semantics.py`；`/api/structured/reports`；CLI `structured report`。旧 `structured/research.py` 全量编排已删除。 | **保留**。完整报告链固定为“研究工作区 → reporting bridge → `ReportVersion`”。 | `/api/structured/reports` 只接受已冻结且可校验的研究包；报告生成、导出和审计信息能从包与物化事实复现。 |
| P04 | 研究工作区与资料处理：`src/analysis/research/`，包括资料、计算、图表、补充资料、Python sandbox、报告组装代码；运行说明见 `docs/acquisition/research-workspace-runbook.md`。早期未提交的 4 组研究代码已保存到 fact 候选分支。 | **纳入并作为报告唯一上游**。 | 资料采集、状态、计算、图表、笔记表和报告组装使用同一研究任务/快照；真实样本和贵州茅台验收文档保留。 |
| P05 | 知识产品：`src/analysis/knowledge/`、`knowledge_release.py`、`config/methods/knowledge/catalog.v1.json` 及知识发布测试/文档已按文件选择性迁入；旧 `research-cards` 目录只作历史材料。 | **保留版本化 KnowledgeService 和严格发布门；研究入口改为按 bundle 绑定的薄适配层。** 不整支合并 knowledge-base 分支。 | 适配层、canonical source alias、分页读取和无默认发布的 fail-closed 行为已测试；IFRS3 变更使旧 review identity 失效，必须重新绑定 Agent/人工验收后才能发布 default。 |
| P06 | 治理能力：`src/analysis/governance/` 中的取证、实体解析、事件重建/reducers、冲突处理、快照和脱敏；旧独立报告生成、模型运行器、工具会话、findings/report 模型和隔离 OpenSpec 已删除。 | **保留治理取证、事件重建和快照，删除独立编排及历史治理对象**；治理能力已接入研究工作区及 reporting bridge，禁止恢复第二套报告发布器或模型运行器。 | 共享 acquisition manifest、冻结原件和研究任务配置；治理 artifact 通过现有 workspace → reporting bridge → `ReportVersion` 进入报告。默认配置未登记真实治理 manifest，真实治理业务验收仍待补；旧治理报告对象不再提供代码级读取兼容。 |
| P07 | `business-profile-v1` 独有画像计算已迁入 `src/analysis/business_evidence/profile.py`、schema、测试和方法说明；`src/analysis/research/business_profile.py` 负责绑定研究 snapshot。 | **保留并通过研究工作区调用**；不直接 merge 旧基线分支，也不保留独立 profile report CLI。 | 画像复用 FactStore/FrozenCorpus，引用校验和截止日/公司校验通过；artifact 可被报告桥显式引用，仍需真实公司画像样例和人工报告验收。 |
| P08 | 商业模式证据与事实：`src/analysis/business_evidence/{cli,corpus,models,routing,store}.py`、`docs/acquisition/business-evidence-facts.md`、相关 schema。当前主要是独立 CLI。 | **保留并接入统一业务分析入口**；与 P07 的画像逻辑共享证据存储和引用。 | 路由、复核、事实查询和画像消费使用同一 manifest/snapshot；无证据时保留缺口，不生成完整结论。 |
| P09 | 旧 adapters 与 legacy 同步：`src/analysis/adapters/` 已删除；`SyncRequest/SyncResult`、历史同步表和兼容测试仍被旧报告回放使用。旧内部报告编辑方法已确认删除。 | **生产路径删除；历史同步模型只读保留**。结构化 API 是唯一常规数据入口；历史报告读取和导出保留。 | 旧请求明确失败/不再注册；不得创建新的 legacy 同步批次。历史报告只能读取/导出，修改假设、重算、重分析和审阅统一回到研究工作区。 |
| P10 | 旧全量行情和报告抓取：legacy providers（AkShare、Sina、BaoStock、Tushare、official）及按 5 年/12 季度和 market/report scope 批量抓取的旧组合。 | **删除**，不再以全量行情或报告抓取补齐八步研究。按需报告原文采集由研究工作区的资料任务完成。 | 常规运行只请求研究问题需要的结构化字段/期间；报告原文仅在研究任务明确需要时采集，并保留来源、页码/定位和缺口。 |
| P11 | 旧直接报告入口：`src/analysis/api.py` 的 `/api/reports*`、旧 `AnalysisService.create_report` 直接输入流程、前端报告新建/旧 CLI 输入。`ReportVersion` 模型和导出能力仍被 P03 使用。 | **删除 `/api/reports` 直接输入和旧报告编辑/编排入口**；保留 `ReportVersion`、历史读取、导出和审计对象作为研究工作区链路的结果模型。 | 新报告只能从研究工作区冻结包经 reporting bridge 生成；旧编辑方法已删除，旧直接输入测试、前端入口、文档和 CLI 引用同步清理。 |
| P12 | 旧来源注册表历史版本：`config/data_sources/business_model_sources.v1.0–v1.10.json`；当前代码默认 `v1.11`，测试和历史回放仍按版本哈希读取。 | **保留为只读历史证据**；生产默认只使用 `v1.11`。 | 新 planner/orchestrator 不遍历旧版本；manifest、快照和报告保留 registry version/hash 定位。 |
| P13 | 已删除的重复启动包装器：`scripts/run_demo.py`、`scripts/smoke_online_sources.py`。前者曾转发到早期 CLI 演示子命令，后者曾转发到现行 `smoke-sources`；两者均无独有功能，`e9f553e` 已在知识/事实分支删除。 | **删除**，现行调用方只使用仍存在的结构化、采集和 smoke CLI，或安装后的 `ashare-analysis` 入口。 | README、前端空状态提示、脚本文档和测试引用一并更新；不删除实际 smoke/structured probe 实现。历史说明不表示这些包装器或演示子命令仍可运行。 |
| P14 | OpenSpec、分支和工作树：2026-10-02 核对 `main` 为 `181a4cb`、`codex/knowledge-base-v1` 为 `027ac38`；fact 候选已包含清理、增量修复、知识候选、治理接入，最新采集时间修复代码为 `4c203d5`。三份未跟踪报告材料继续保留；其他 worktree 的未提交内容须在合并前核对。 | **统一 OpenSpec 到 `eight-step-production-pipeline-v1`；整合后本地和远程只保留 `main`**。以当前 fact worktree 为代码基线，按用户决定选择性处理知识冲突，再合并到 main。 | 先保存并审核全部未提交内容，确认两份茅台验收文档都保留；完成测试、真实联网/人工验收和 review 后，才删除其他分支、远程引用和 worktree。 |

- 2026-10-02 已完成一份明确报告的真实按需正文验收：贵州茅台 2025 年报在新空根 HTTP 200、143 页解析成功，重复执行复用同一正文 snapshot；没有扩大为全量报告归档。
当前增量收口补充：旧重复入口已清理，最新空根 baseline 为 53 成功、9 空响应、0 失败。9 项延期已落实到贵州茅台默认范围，本轮其余 22 项真实增量及同参数重复采集已完成；完整空查询允许以后更新，失败项独立补采。治理真实 manifest、知识候选 review/Agent/人工验收及报告人工验收仍待完成；在此之前保留候选材料、worktree 和分支。具体运行见 [增量与恢复验收](acquisition/moutai-incremental-recovery-20261002.md)。

## 决策与验收边界

当前工作树还有三份未跟踪的贵州茅台验收材料：`tmp_v4.txt`（含评级、目标价和八步正文）以及 `v4-contact.png`、`v4-all-contact.png`（报告渲染联系图）。它们不是源代码，也未加入 Git；在人工黄金验收完成前保留，最终归档或删除需结合用户阅读结果决定。

已确认的产品决策：

- 结构化 API 是唯一常规数据入口；旧 adapters、legacy 同步、旧同步端点、旧全量行情+报告抓取删除。
- 报告原文只按需采集；报告只保留“研究工作区 → reporting bridge → `ReportVersion`”链路，删除 `/api/reports` 直接输入。
- 完整知识产品纳入；知识同 ID 采用 knowledge-content 修订，同时保留 KB/core 严格发布门。
- 治理全链路接入；business-profile 独有逻辑移植到统一业务分析入口。
- 治理取证、事件重建和快照保留；治理独立报告生成、模型运行器和工具会话编排删除，后续只通过研究工作区进入 reporting bridge。
- 旧内部报告编辑方法删除；历史报告读取和导出保留，修改统一回到研究工作区。
- fact worktree 的 4 组未提交研究内容全部纳入；两份贵州茅台验收文档都保留。
- OpenSpec 统一为 `eight-step-production-pipeline-v1`；整合完成后只留 `main`。
- 贵州茅台 9 项 API 空响应的补齐延期；现阶段保留缺口与已有报告证据，先验收其余适用数据集及报告主链。

当前尚未等同于完成的事项：

1. 旧重复链已删除，治理共享 workspace 已接入。真实治理资料、知识与报告的人工验收及分支收口仍未完成。
2. 600519 本轮 22 项范围的真实增量、重复运行和中断接续已验证；9 项原始空响应和已有报告证据保留为后补项。真实接口本次未披露新增业务行，新增入库分支另由受控上游变化测试验证。
3. 未提交研究代码、图片、配置和文档需要逐组审阅，确认不是临时文件后才能纳入；`tmp/`、`var/` 中的真实证据不得批量清理。
4. 分支删除属于最后一步；未提交内容未保存、测试或人工验收未完成时不得执行。

## 证据索引

- 主线：`main` / `181a4cb`。
- 候选整合分支：`codex/knowledge-base-v1` / `027ac38`，包含 `fact-materialization-ultra` 及三个 knowledge 分支的已提交等价补丁。
- 当前事实工作树：`codex/fact-materialization-ultra`，清理与结构化增量修复已提交；仍有三个未跟踪临时文件待确认。
- 结构化运行说明：`docs/acquisition/structured-data-runtime-v1.md`。
- 研究工作区运行手册：`docs/acquisition/research-workspace-runbook.md`。
- 贵州茅台验收：`docs/acquisition/moutai-golden-report-acceptance.md`。
- 结构化 API 真实回采：`docs/acquisition/moutai-structured-api-recapture-20260928.md`。
- 最新字段修复与 9 项空响应归因：[真实 API 验收](acquisition/moutai-structured-api-acceptance-20261002.md)、[逐主题来源对照](acquisition/moutai-no-data-source-comparison-20261002.md)。
- 本轮范围、空查询、失败隔离与恢复：[增量与恢复验收](acquisition/moutai-incremental-recovery-20261002.md)。
