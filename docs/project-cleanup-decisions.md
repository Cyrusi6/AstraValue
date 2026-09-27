# 项目精简决策记录

更新时间：2026-09-28
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
12. **来源注册表（待用户确认）**：当前生产默认使用 `business_model_sources.v1.11.json`；`v1.0–v1.10` 是否保留为只读历史证据，待用户选择。无论取舍，历史 manifest、快照和报告必须保留 registry version/hash 定位。
13. **OpenSpec**：统一到 `eight-step-production-pipeline-v1`，不再并行维护另一套生产实施变更。
14. **分支治理**：整合完成、测试和人工验收通过后，本地和远程只保留 `main`；在此之前不得删除承载未提交材料的 worktree 或分支。
15. **低风险重复脚本**：`scripts/run_demo.py`、`scripts/smoke_online_sources.py` 作为重复转发器删除；调用方改用 `analysis.cli`/`ashare-analysis` 入口。

## 本次新增的结构化入口决定

- `/api/structured/*` 是唯一常规数据入口，负责计划、执行、恢复、记录、阅读任务、覆盖和结构化报告包提交。
- `POST /api/structured/reports` 与 CLI `structured report` 只接收已冻结、可校验的研究包；它们调用统一的 reporting bridge 生成 `ReportVersion`。
- `/api/reports` 及其直接输入、重算、重分析和审阅入口从生产产品中删除；报告导出能力保留在研究工作区生成的 `ReportVersion` 上。
- 结构化 API 不默认抓取全量行情或全部年报。字段、期间、同行和原文范围由八步研究问题及研究任务决定。

## 等待用户确认的保留边界

- `POST /api/documents` 是否继续作为受限的按需原文入口。
- `SyncRequest/SyncResult`、同步表和旧回放字段是否作为只读历史兼容保留；生产不得创建新的 legacy 同步批次。
- 当前代码审计确认：`SyncRequest` 已无生产调用；`SyncResult` 的物理表和迁移仍支撑旧数据库恢复，唯一运行时回填点是 `AnalysisService._with_synced_facts`。若选择保留兼容，建议切断新报告的自动回填，只保留只读 getter、迁移和历史报告读取。
- v1.11 注册表仍保存 5 个 `legacy_definitions` 身份，但新 planner/orchestrator 不遍历它们；删除这些字段会改变默认 registry hash 并使已有 snapshot/DB 无法按原身份复核，因此是否只读保留与 Sync 兼容一起决定。
- `AnalysisService.patch_assumptions/recalculate/reanalyze/review` 当前没有公开 API 路由，只被旧测试和内部兼容代码调用；`ReportVersion` 查询、变化查看和导出仍是公开只读能力。是否连这些内部旧版本编辑方法一起删除，随历史兼容取舍处理。
- 知识候选在独立工作树的 `tests/knowledge` 当前仍有 1 项发布门失败：缺少当前工作树可验证的 `KNOWLEDGE_ACCEPTANCE`、agent sample 和 human review；历史 `candidate-54-v2` JSON 还会因 `candidate_content_identity` 哈希不匹配而被拒绝。另有旧 `src/analysis/research/knowledge.py` 与新 `analysis.knowledge` 两套知识入口，迁移新服务后需决定适配或删除旧入口。
- `business_model_sources.v1.0–v1.10` 是否保留为只读历史注册表。
- 知识分支的 56 个方法状态、两组互补问题路径、IFRS3 source ID、来源定位字段和 pilot 时间线如何合并。
- 旧 `src/analysis/research/knowledge.py`（research-cards）是否迁移为新 `analysis.knowledge.KnowledgeService` 的适配层后删除；当前研究工作区的 `Catalog` 和工具注册仍在调用它。

审计给出的默认建议（尚未替用户确认）：保留 56 条方法路径并把双路径标成互补；保留来源级 `locator` 与规则级 `source_refs[].locator/support` 两层定位；用 alias 兼容 IFRS3 重复 source ID；把 `pilot_remaining.md` 移到 acceptance history 并标记 superseded；保留 1/54、pilot、54/54 三段历史时间线；把旧 research-cards 入口迁移到 `KnowledgeService` 适配层后删除。

## 仍需通过的验收

这些是执行门，不是新的产品取舍：

- 600519：结构化 baseline、执行中断后 resume、重复执行幂等、物化、研究包和报告闭环。
- 抽查旧输出与新结构化输出的关键指标、期间、单位、来源和数量；差异必须能解释并记录。
- 研究工作区资料、计算、图表、补充资料、知识读取、治理输入和报告桥接的端到端测试。
- `/api/reports` 删除后，结构化 API、前端其他数据流程和导出仍可用；直接报告测试和文档引用全部清理。
- `/api/documents` 审计确认它当前是受注册来源约束的本地原文按需入口，不执行网络全量下载；但前端“正式公告归档”面板仍缺少 `source_url`，提交后会失败，需在保留该入口时改成来源 URL/注册定义/研究任务绑定表单。
- 当前生效来源注册表、manifest、快照和报告的引用检查；确认删除历史文件不会破坏可复核证据。
- 真实联网状态、人工验收状态和已知缺口写入贵州茅台验收文档；离线测试通过不能替代业务验收。

## 当前状态与边界

候选事实工作树已执行旧链清理，删除旧 adapters、旧全量采集链、旧 `structured/research.py`、旧包装入口和直接报告写入口；structured 已提供基于 finalized parent 与 unsafe coverage 的独立 reconcile 入口（Runtime、Service、CLI、API），贵州茅台真实 reconcile 尝试已记录，但仍留下空响应、失败和待执行窗口，不能据此宣称 incremental 通过。详见 `docs/project-cleanup-history.md` 与 `docs/acquisition/moutai-structured-api-recapture-20260928.md`。这不等于最终 main 已收口：知识分支冲突仍待用户决定，真实茅台 baseline 仍有失败和空响应，全量测试和人工报告验收仍未完成。

因此在实现、验证、人工验收和 review 完成前，不能删除承载材料的 worktree、分支或远程引用。
