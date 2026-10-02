# 项目精简决策记录

更新时间：2026-10-02（已纳入报告编辑与治理编排决策）
记录范围：本轮项目精简、分支整合和结构化报告入口决策。

## 已确认决策

1. **整合基线**：以 `codex/knowledge-base-v1` 为候选整合基线；它已包含 fact-materialization、knowledge-content、knowledge-core 和 knowledge-verification 的已提交等价补丁。
2. **知识产品**：纳入完整知识产品。知识同 ID 内容采用 knowledge-content 的修订，但保留 KB/core 的严格发布门、来源定位、版本和缺口检查。
3. **模型职责**：由 Codex 自主完成假设、估值、评级和写作；研究工作区和报告链必须保存输入、来源、计算和审计记录。
4. **治理**：治理模块走全链路接入，不作为孤立包保留。治理数据需进入结构化运行、研究工作区和报告审计边界。
5. **business-profile**：只移植 `business-profile-v1` 的独有业务画像逻辑到统一业务分析入口；不直接合并该旧基线分支。
6. **知识分支整合边界**：`codex/knowledge-base-v1` 当前合并结果重新带回旧 adapters、旧 `structured/research.py` 和包装入口，不能整支直接合并；只迁移知识产品和已核验研究内容，沿用当前候选工作树的删除结果。
7. **fact worktree 未提交内容**：4 组未提交研究内容全部纳入；两份贵州茅台验收文档全部保留。
8. **唯一事实路径**：新 `StructuredFactMaterializer` 与 `research_lite` 是唯一物化/研究包路径，不再并行维护旧财务拼装和旧全量抓取路径。
9. **唯一报告路径**：研究工作区 → reporting bridge → `ReportVersion`。保留 `ReportVersion`、导出和审计能力作为结果模型，但删除 `/api/reports` 直接输入。
10. **legacy 清理**：删除旧 adapters、legacy 同步、旧同步端点和旧直接报告入口。旧全量行情与报告抓取也删除。
11. **报告原文范围**：报告原文只按研究任务按需采集；不再默认进行全量报告抓取。
12. **来源注册表历史兼容**：当前生产默认使用 `business_model_sources.v1.11.json`；`v1.0–v1.10` 保留为只读历史证据，不参与新 planner/orchestrator。历史 manifest、快照和报告继续保留 registry version/hash 定位。
13. **OpenSpec**：统一到 `eight-step-production-pipeline-v1`，不再并行维护另一套生产实施变更。
14. **分支治理**：整合完成、测试和人工验收通过后，本地和远程只保留 `main`；在此之前不得删除承载未提交材料的 worktree 或分支。
15. **低风险重复脚本**：`scripts/run_demo.py`、`scripts/smoke_online_sources.py` 作为重复转发器删除；调用方改用 `analysis.cli`/`ashare-analysis` 入口。
16. **旧内部报告编辑方法**：删除 `AnalysisService.patch_assumptions`、`recalculate`、`reanalyze`、`review` 及仅为其服务的请求模型/派生路径；历史报告读取和导出保留，后续修改统一回到研究工作区。
17. **治理独立编排**：删除治理模块独立的报告生成、模型运行器和工具会话系统；保留治理取证、实体解析、事件重建/reducers、快照和脱敏，并接入研究工作区及现有 reporting bridge。
18. **历史治理模型**：删除 `CodexInputPack`、`CodexToolRead`、`CodexSessionManifest`、研究任务/结果包/隔离项、快照采用对象、治理报告及其 findings/technical validation 模型、旧测试和退役 OpenSpec 文档。治理只保留取证、实体解析、事件重建、快照和研究工作区接口；历史治理报告对象不再提供代码级读取兼容。

## 本次新增的结构化入口决定

- `/api/structured/*` 是唯一常规数据入口，负责计划、执行、恢复、记录、阅读任务、覆盖和结构化报告包提交。
- `POST /api/structured/reports` 与 CLI `structured report` 只接收已冻结、可校验的研究包；它们调用统一的 reporting bridge 生成 `ReportVersion`。
- `/api/reports` 及其直接输入、重算、重分析和审阅入口从生产产品中删除；报告导出能力保留在研究工作区生成的 `ReportVersion` 上。
- 结构化 API 不默认抓取全量行情或全部年报。字段、期间、同行和原文范围由八步研究问题及研究任务决定。

## 本轮继续实施采用的保留边界

用户要求继续按已确认方案实施。前一轮列出的推荐方案现在作为实现基线：

- `POST /api/documents` 保留为受注册来源约束的按需原文入口；前端必须填写 `source_url`，并可填写来源定义及版本，禁止把它做成全量报告下载器。
- `SyncRequest`、`SyncResult` 和同步表只保留历史读取、迁移和回放；旧编辑方法已删除，新结构化报告禁止自动回填 legacy synced facts，不再创建新的 legacy 同步批次。
- `business_model_sources.v1.0–v1.10` 作为只读历史注册表保留，v1.11 是唯一生产默认注册表。
- 旧 `research-cards` 迁移为绑定知识版本的薄适配层后，删除旧 `Knowledge` 实现和重复卡片目录；未发布的 candidate 不得被适配层静默当作 default。
- ES01.Q10 的 `pricing_power` 与 `competitive_advantage`、ES02.Q04 的 `roic` 与 `roe_dupont` 均保留为同题互补路径，不标成 alternatives。
- IFRS3 使用 canonical source ID，旧 ID 只作为历史 bundle/manifest alias；`pilot_remaining.md` 移入 acceptance history 并标记 superseded，保留 1/54、pilot、54/54 时间线。
- 当前知识候选仍需重新绑定 Agent 样例和人工验收；旧 acceptance 不自动授权新版本发布。

## 已确认、仍需执行的验收边界

- 当前代码审计确认：`SyncRequest` 已无生产调用；`SyncResult` 的物理表和迁移仍支撑旧数据库恢复。新报告已切断自动回填，只保留只读 getter、迁移和历史报告读取。
- v1.11 注册表仍保存 5 个 `legacy_definitions` 身份，但新 planner/orchestrator 不遍历它们；这些字段作为只读历史身份保留，不能被新计划使用。
- `AnalysisService.patch_assumptions/recalculate/reanalyze/review` 已按用户决定删除；`ReportVersion` 查询、变化查看和导出仍是公开只读能力。报告修改统一通过研究工作区草稿和 reporting bridge 产生新版本。
- 知识候选当前不能生成可发布 bundle：IFRS3 canonical source 变更使 `knowledge.es04_q05` 的旧 source/case review identity 失效，且仍缺当前版本 Agent sample 与 human review；历史 acceptance 会被 `candidate_content_identity` 检查拒绝。`src/analysis/research/knowledge.py` 已迁移为按 bundle 绑定的 `KnowledgeService` 薄适配层，未发布时返回 `no_default_release`。
- 知识分支的 56 个方法状态、来源定位字段和历史时间线已经按上述互补/canonical/alias 规则作为迁移基线；当前目录已完成 canonical 迁移，但必须重新绑定 `knowledge.es04_q05` 的 source/case review、Agent 样例和人工验收，才能生成并发布新的 candidate/default。

## 仍需通过的验收

这些是执行门，不是新的产品取舍：

- 600519：结构化 baseline、执行中断后 resume、重复执行幂等、物化、研究包和报告闭环。
- 抽查旧输出与新结构化输出的关键指标、期间、单位、来源和数量；差异必须能解释并记录。
- 研究工作区资料、计算、图表、补充资料、知识读取、治理取证/重建/快照和报告桥接的端到端测试；治理独立报告/运行器/会话编排不得重新出现。
- `/api/reports` 删除后，结构化 API、前端其他数据流程和导出仍可用；直接报告测试和文档引用全部清理。
- `/api/documents` 审计确认它当前是受注册来源约束的本地原文按需入口，不执行网络全量下载；但前端“正式公告归档”面板仍缺少 `source_url`，提交后会失败，需在保留该入口时改成来源 URL/注册定义/研究任务绑定表单。
- 当前生效来源注册表、manifest、快照和报告的引用检查；确认删除历史文件不会破坏可复核证据。
- 真实联网状态、人工验收状态和已知缺口写入贵州茅台验收文档；离线测试通过不能替代业务验收。

- 按需报告正文只按研究任务选定范围采集；2026-10-02 已用贵州茅台 2025 年报完成一份真实 HTTP 回采和重复快照复用验收，不能扩大解释为全量报告完成。

## 当前状态与边界

候选事实工作树已执行旧链清理，删除旧 adapters、旧全量采集链、旧 `structured/research.py`、旧包装入口、直接报告写入口和旧内部报告编辑方法；structured 已提供基于 finalized parent 与 unsafe coverage 的独立 reconcile 入口（Runtime、Service、CLI、API），贵州茅台真实 reconcile 尝试已记录，但仍留下空响应、失败和待执行窗口，不能据此宣称 incremental 通过。治理独立编排已删除，共享 manifest → 治理 artifact → 研究工作区 → reporting bridge 已接入；默认配置尚未登记真实治理 manifest，因此真实治理样本仍是缺口。详见 `docs/project-cleanup-history.md` 与 `docs/acquisition/moutai-structured-api-recapture-20260928.md`。这不等于最终 main 已收口：知识目录 canonical 迁移已完成，当前 candidate 仍待 review/Agent/人工验收；真实茅台 baseline 仍有失败和空响应，完整测试和人工报告验收仍未完成。

因此在实现、验证、人工验收和 review 完成前，不能删除承载材料的 worktree、分支或远程引用。
