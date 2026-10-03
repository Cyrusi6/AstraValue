# 项目精简决策记录

更新时间：2026-10-03（已纳入采集延期、完整空查询、失败隔离、报告验收、材料归档和主线 CI 收口证据）
记录范围：本轮项目精简、分支整合和结构化报告入口决策。

## 已确认决策

1. **整合基线**：以 `codex/knowledge-base-v1` 为候选整合基线；它已包含 fact-materialization、knowledge-content、knowledge-core 和 knowledge-verification 的已提交等价补丁。
2. **知识产品**：纳入完整知识产品。知识同 ID 内容采用 knowledge-content 的修订，但保留 KB/core 的严格发布门、来源定位、版本和缺口检查。
3. **模型职责**：由 Codex 自主完成假设、估值、评级和写作；研究工作区和报告链必须保存输入、来源、计算和审计记录。
4. **治理**：治理模块走全链路接入，不作为孤立包保留。治理数据需进入结构化运行、研究工作区和报告审计边界。
5. **business-profile**：只移植 `business-profile-v1` 的独有业务画像逻辑到统一业务分析入口；不直接合并该旧基线分支。
6. **知识分支整合边界**：`codex/knowledge-base-v1` 当前合并结果重新带回旧 adapters、旧 `structured/research.py` 和包装入口，不能整支直接合并；只迁移知识产品和已核验研究内容，沿用当前候选工作树的删除结果。
7. **fact worktree 未提交内容**：4 组未提交研究内容全部纳入；贵州茅台 v4 材料、v5 成品和验收记录已归档，候选 `tmp/`、`var/`、`output/` 运行证据已在仓外完整保存；主 worktree 与其他 worktree 的未提交内容也有独立归档。
8. **唯一事实路径**：新 `StructuredFactMaterializer` 与 `research_lite` 是唯一物化/研究包路径，不再并行维护旧财务拼装和旧全量抓取路径。
9. **唯一报告路径**：研究工作区 → reporting bridge → `ReportVersion`。保留 `ReportVersion`、导出和审计能力作为结果模型，但删除 `/api/reports` 直接输入。
10. **legacy 清理**：删除旧 adapters、legacy 同步、旧同步端点和旧直接报告入口。旧全量行情与报告抓取也删除。
11. **报告原文范围**：报告原文只按研究任务按需采集；不再默认进行全量报告抓取。
12. **来源注册表历史兼容**：当前生产默认使用 `business_model_sources.v1.11.json`；`v1.0–v1.10` 保留为只读历史证据，不参与新 planner/orchestrator。历史 manifest、快照和报告继续保留 registry version/hash 定位。
13. **OpenSpec**：停止使用该工具链。相关 skill、配置和规划文件已于 2026-10-03 移至 `D:\估值模型-archives\openspec-20261003`；主仓库当前以代码、测试、运行手册和验收记录为准。
14. **分支治理**：整合完成、测试和人工验收通过后，本地和远程只保留 `main`；在此之前不得删除承载未提交材料的 worktree 或分支。
15. **低风险重复脚本**：`scripts/run_demo.py`、`scripts/smoke_online_sources.py` 作为重复转发器删除；调用方改用 `analysis.cli`/`ashare-analysis` 入口。
16. **旧内部报告编辑方法**：删除 `AnalysisService.patch_assumptions`、`recalculate`、`reanalyze`、`review` 及仅为其服务的请求模型/派生路径；历史报告读取和导出保留，后续修改统一回到研究工作区。
17. **治理独立编排**：删除治理模块独立的报告生成、模型运行器和工具会话系统；保留治理取证、实体解析、事件重建/reducers、快照和脱敏，并接入研究工作区及现有 reporting bridge。
18. **历史治理模型**：删除 `CodexInputPack`、`CodexToolRead`、`CodexSessionManifest`、研究任务/结果包/隔离项、快照采用对象、治理报告及其 findings/technical validation 模型、旧测试和历史规划文档。治理只保留取证、实体解析、事件重建、快照和研究工作区接口；历史治理报告对象不再提供代码级读取兼容。

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
- `/api/documents` 审计确认它当前是受注册来源约束的本地原文按需入口，不执行网络全量下载；前端“正式公告归档”面板已补齐 `source_url`、来源定义和版本字段，并通过前端构建。
- 当前生效来源注册表、manifest、快照和报告的引用检查；确认删除历史文件不会破坏可复核证据。
- 真实联网状态、人工验收状态和已知缺口写入贵州茅台验收文档；离线测试通过不能替代业务验收。

- 按需报告正文只按研究任务选定范围采集；2026-10-02 已用贵州茅台 2025 年报完成一份真实 HTTP 回采和重复快照复用验收，不能扩大解释为全量报告完成。

## 2026-10-03 报告验收与遗留门延期

- 贵州茅台 `贵州茅台-6.1sol-20261003-final` 已完成代人工成品验收：八章、数字/引用、同行连接、PDF、Excel 和提示词版本检查通过，验收记录的 `overall_decision` 为 `passed`。
- 真实治理 manifest、知识 Agent 样例/人工发布门属于合并前遗留验收缺口，按用户决定延期；它们保留为独立 deferred acceptance，不阻断本轮代码合并。
- 研究覆盖、股利事件核验、DCF、`ReportVersion` 草稿状态和模型身份凭证同样按用户决定延期；报告系统字段保持原值，不由人工验收记录伪造修改。
- 知识发布测试从默认代码回归门分离，继续作为可单独运行的真实发布门；延期状态必须在验收记录中保留。

## 当前状态与边界

候选事实工作树已删除旧 adapters、旧全量采集链、旧 `structured/research.py`、旧包装入口、直接报告写入口和旧内部报告编辑方法。贵州茅台空根 baseline 为 53 成功、9 空响应、0 失败；9 项按用户决定延期后，本轮其余 22 项已完成真实增量，正常空查询不再阻止更新。未完成项目按原窗口恢复，独立任务继续执行。详见 [本轮增量与恢复验收](acquisition/moutai-incremental-recovery-20261002.md)。治理共享 workspace 已接入，真实治理 manifest 和知识发布门按用户决定延期；贵州茅台 v5 报告成品人工验收已通过。候选内容已经合入 `main`，其他本地和远程分支、候选 worktree 已清理。

## 2026-10-03 主线合并与 CI 收口

- 计划合并提交为 `42c31c0`；随后补齐干净 CI 环境所需的 `requests`、`beautifulsoup4`、`lxml` 和研究测试依赖，相关提交为 `9684f74`、`5f2179b`、`0a9dd4b`。
- GitHub Actions `verify` 已通过：采集契约测试、治理注册表与测试、默认 Python 回归和前端构建均通过。证据：[run 37111950599](https://github.com/Cyrusi6/AstraValue/actions/runs/37111950599)。
- 本地 `main` 与 `origin/main` 已对齐，工作树干净；延期的治理资料、知识发布门、研究覆盖、股利核验、DCF、草稿状态和模型身份凭证仍保持 deferred，不因 CI 通过而改变。

## 2026-10-02 采集时间与空响应

用户确认 `__retrieved_at` 移出请求字段，只保留本地 provenance。已同步请求范围、日期元数据、字段投影和内容版本哈希；数据集元数据版本与未变的来源协议版本分开冻结。真实验收见 [API 验收记录](acquisition/moutai-structured-api-acceptance-20261002.md)。

9 项空响应已与旧同行样本和报告正文逐项核对。用户随后决定“现在先不管，作为后期的补充”：本轮贵州茅台的 `customers_peer`、`guarantee`、`litigation`、`seo`、`allotment`、`bond_issuance`、`goodwill`、`pledge`、`unlock_peer` 补齐工作延期，保留原始 `no_data` 和已有报告证据。下一步按其余适用数据集完成真实增量和报告验收，记录本轮范围与后补项；不将这项样本范围调整扩展为所有公司、所有空响应均可放行。

延期范围已落实到公司默认采集配置，原始水位记录不改写。知识发布、真实治理资料和报告人工验收的既有要求未改变。

## 2026-10-02 完整空查询与失败隔离

用户最终确认：`no_data` 本身不能禁止以后更新，不再要求“已有历史数据”。查询完整且明确为空，记录“本次已查完但没有数据”；超时、失败、漏页保留未完成位置，供下次重试或恢复。

已完成项目正常增量，未完成项目本轮尝试原窗口补采，再次失败也不影响其他独立任务。真正依赖缺失数据的任务暂缓，说明缺少什么。所有可运行任务结束后，统一列出有数据、完整为空、仍未完成及下次恢复位置；未完成项不得记成完成。

恢复前核查已保存页：异常页重新请求；有效终页保存后、完成状态写入前发生中断时补写完成证明，不额外请求一页。历史快照和原始页保留，研究工作区可使用成功恢复后验证通过的数据；部分成果保持 partial。
