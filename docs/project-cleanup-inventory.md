# 项目精简盘点与处置清单

更新时间：2026-09-28
盘点工作树：`D:\估值模型-worktrees\fact-materialization-ultra`  
当前分支：`codex/fact-materialization-ultra`（清理、结构化增量修复和回采记录已提交）

这份清单把代码事实、已确认的产品决策和后续验收分开记录。表中的“动作”是目标处置，不表示已经完成删除或合并；在验收门通过前，不得用 `git branch -D`、工作树删除或批量删除替代迁移。

## P01-P14 功能处置

| 编号 | 功能与当前证据 | 目标动作 | 完成条件与边界 |
|---|---|---|---|
| P01 | 结构化数据采集与任务运行：`src/analysis/structured/{service,runtime,planner,scheduler,storage,records,protocols,registry,reading,coverage,repair}.py`；`src/analysis/api.py` 的 `/api/structured/*`；`src/analysis/cli.py` 的 `structured` 子命令。 | **保留并作为唯一常规入口**。`/api/structured/*` 和结构化 CLI 承担计划、执行、恢复、记录、阅读任务、覆盖查询。 | 旧全量同步移除后，结构化 API 仍可独立建立、执行、恢复和查询 600519；中断恢复、幂等、来源门禁和持久化测试通过。 |
| P02 | 结构化事实物化：`src/analysis/structured/materialization.py`、`materialization_contracts.py`、`materialization_audit.py`、`materialization_replay.py`，并由 `structured materialize` 调用。 | **保留为唯一 `StructuredFactMaterializer` 路径**；旧的直接财务/行情拼装不再并行维护。 | 物化结果的事实数量、期间、单位、来源和哈希可回溯到结构化记录；旧数据只作为显式迁移/回放输入，不作为生产入口。 |
| P03 | 八步轻量研究包与报告桥接：`src/analysis/structured/research_lite.py`、`reporting_bridge.py`、`report_semantics.py`；`/api/structured/reports`；CLI `structured report`。旧 `structured/research.py` 全量编排已删除。 | **保留**。完整报告链固定为“研究工作区 → reporting bridge → `ReportVersion`”。 | `/api/structured/reports` 只接受已冻结且可校验的研究包；报告生成、导出和审计信息能从包与物化事实复现。 |
| P04 | 研究工作区与资料处理：`src/analysis/research/`（当前 fact worktree 有已修改和未跟踪的资料、计算、图表、补充资料、Python sandbox、报告组装代码），以及 `docs/acquisition/research-workspace-runbook.md`。 | **纳入并作为报告唯一上游**。fact worktree 的 4 组未提交研究内容全部纳入统一主线。 | 资料采集、状态、计算、图表、笔记表和报告组装使用同一研究任务/快照；真实样本和贵州茅台验收文档保留。 |
| P05 | 知识产品：`codex/knowledge-base-v1` 中的 `src/analysis/knowledge/`、`knowledge_release.py`、`config/methods/knowledge/catalog.v1.json` 及知识发布测试/文档；该分支还夹带了已删除的旧 adapters、旧 research 编排和包装入口。当前 `src/analysis/research/knowledge.py` 仍读取旧 `research-cards`。 | **按文件范围选择性迁移知识产品，不整支合并**。同 ID 内容使用 `knowledge-content` 修订，同时保留 KB/core 的严格发布门；旧研究知识入口待用户决定是否迁移后删除。 | 知识服务、内容版本、来源定位、缺口和发布门验收通过；迁移后旧全量采集和旧物化路径仍不存在；未决冲突先标明 candidate，不擅自覆盖用户选择。历史 acceptance 不能仅凭 bundle ID 复用，必须匹配当前内容哈希。 |
| P06 | 治理全链路：`src/analysis/governance/`（当前 main 约 26 个 Python 文件、约 16,695 行），治理测试和 `scripts/validate_governance_*.py`。当前 API/主 CLI 未直接接入。 | **保留并接入统一生产链路**，不再作为孤立测试包。 | 治理采集、抽取、冲突/版本、报告输入和审计状态在结构化运行及研究工作区中有明确入口；接入完成前不得宣称生产可用。 |
| P07 | business-profile 分支独有逻辑：`origin/codex/business-profile-v1` 的 `src/analysis/business_evidence/profile.py`、`config/business_evidence/profile.schema.json`、对应测试和文档。该分支基于旧主线，不能整支合并。 | **只移植独有业务画像逻辑**到统一业务分析入口；不直接 merge 该分支。 | 画像输入复用统一业务证据/来源注册表，输出进入研究工作区和报告桥；移植后删除旧分支特有重复入口。 |
| P08 | 商业模式证据与事实：`src/analysis/business_evidence/{cli,corpus,models,routing,store}.py`、`docs/acquisition/business-evidence-facts.md`、相关 schema。当前主要是独立 CLI。 | **保留并接入统一业务分析入口**；与 P07 的画像逻辑共享证据存储和引用。 | 路由、复核、事实查询和画像消费使用同一 manifest/snapshot；无证据时保留缺口，不生成完整结论。 |
| P09 | 旧 adapters 与 legacy 同步：`src/analysis/adapters/` 已删除；`SyncRequest/SyncResult`、历史同步表和兼容测试仍被旧报告回放使用。 | **生产路径删除；历史模型待用户决定**。结构化 API 是唯一常规数据入口。 | 旧请求明确失败/不再注册；若保留模型，只能作为只读历史兼容，不得创建新的同步批次。 |
| P10 | 旧全量行情和报告抓取：legacy providers（AkShare、Sina、BaoStock、Tushare、official）及按 5 年/12 季度和 market/report scope 批量抓取的旧组合。 | **删除**，不再以全量行情或报告抓取补齐八步研究。按需报告原文采集由研究工作区的资料任务完成。 | 常规运行只请求研究问题需要的结构化字段/期间；报告原文仅在研究任务明确需要时采集，并保留来源、页码/定位和缺口。 |
| P11 | 旧直接报告入口：`src/analysis/api.py` 的 `/api/reports*`、`AnalysisService.create_report` 的直接输入流程、前端报告新建/旧 CLI 输入。`ReportVersion` 模型和导出能力仍被 P03 使用。 | **删除 `/api/reports` 直接输入和旧报告入口**；保留 `ReportVersion`、导出和审计对象作为研究工作区链路的结果模型。 | 只能从研究工作区冻结包经 reporting bridge 生成报告；旧直接输入测试、前端入口、文档和 CLI 同步移除。 |
| P12 | 旧来源注册表历史版本：`config/data_sources/business_model_sources.v1.0–v1.10.json`；当前代码默认 `v1.11`，测试和历史回放仍按版本哈希读取。 | **待用户决定：保留只读历史版本或迁移后删除**。生产默认只使用 `v1.11`。 | 无论取舍，manifest、快照和报告必须保留可追溯的 registry version/hash；旧文件不能作为当前默认来源。 |
| P13 | 已删除的重复启动包装器：`scripts/run_demo.py`、`scripts/smoke_online_sources.py`。前者曾转发到早期 CLI 演示子命令，后者曾转发到现行 `smoke-sources`；两者均无独有功能，`e9f553e` 已在知识/事实分支删除。 | **删除**，现行调用方只使用仍存在的结构化、采集和 smoke CLI，或安装后的 `ashare-analysis` 入口。 | README、前端空状态提示、脚本文档和测试引用一并更新；不删除实际 smoke/structured probe 实现。历史说明不表示这些包装器或演示子命令仍可运行。 |
| P14 | OpenSpec、分支和工作树：当前主线 `main` HEAD `181a4cb`；`codex/knowledge-base-v1` HEAD `027ac38`；fact worktree 已将清理、增量修复和回采记录提交到 `d10c6f9`，仅保留 `tmp_v4.txt`、`v4-contact.png`、`v4-all-contact.png` 三个未跟踪临时文件；knowledge-base worktree 有未提交茅台验收文档。 | **统一 OpenSpec 到 `eight-step-production-pipeline-v1`；整合后本地和远程只保留 `main`**。以 knowledge-base 为候选基线，先纳入 fact worktree 已提交内容和两份茅台文档，再 fast-forward/整理到 main。 | 先保存并审核全部未提交内容，确认两份茅台验收文档都保留；完成测试、真实联网/人工验收和 review 后，才删除其他分支、远程引用和 worktree。 |

## 决策与验收边界

当前工作树还有三份未跟踪的贵州茅台验收材料：`tmp_v4.txt`（含评级、目标价和八步正文）以及 `v4-contact.png`、`v4-all-contact.png`（报告渲染联系图）。它们不是源代码，也未加入 Git；在人工黄金验收完成前保留，最终归档或删除需结合用户阅读结果决定。

已确认的产品决策：

- 结构化 API 是唯一常规数据入口；旧 adapters、legacy 同步、旧同步端点、旧全量行情+报告抓取删除。
- 报告原文只按需采集；报告只保留“研究工作区 → reporting bridge → `ReportVersion`”链路，删除 `/api/reports` 直接输入。
- 完整知识产品纳入；知识同 ID 采用 knowledge-content 修订，同时保留 KB/core 严格发布门。
- 治理全链路接入；business-profile 独有逻辑移植到统一业务分析入口。
- fact worktree 的 4 组未提交研究内容全部纳入；两份贵州茅台验收文档都保留。
- OpenSpec 统一为 `eight-step-production-pipeline-v1`；整合完成后只留 `main`。

当前尚未等同于完成的事项：

1. 旧 adapters、全量编排和直接报告写入口已从当前 worktree 删除；剩余工作是回归验证、研究内容整合和分支收口。
2. 600519 已在空数据根完成真实结构化 baseline、重复计划幂等、物化和研究包/报告回放；真实 acquisition incremental 仍受 `safe_through=null` 门禁，不能写成已完成。
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
