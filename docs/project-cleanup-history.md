# 项目精简与结构化 API 迁移历史

更新时间：2026-10-02（补充用户确认的编辑链与治理编排决策）

这份记录保存本轮实施前的用户要求、Codex session 中形成的初始计划，以及当前实现证据，避免在后续分支收口时丢失上下文。它是历史和验收索引，不替代当前 OpenSpec 合同。

## 用户确认的目标

- 梳理 Codex 历史 session、代码、文档、分支和 worktree，建立功能清单。
- 发现重复功能、过时功能或会改变产品语义的取舍时，先列出并询问用户；不能擅自选择。
- 结构化 API 成为唯一常规数据入口，删除旧的一次性全量行情和报告抓取。
- 报告原文只按研究问题按需采集，用于结构化 API 无法表达的公告、事件和重要报告证据。
- 研究主链固定为：

  `结构化 API → baseline / incremental / reconcile → acquisition snapshot → structured facts → StructuredFactMaterializer + research_lite → 研究工作区 → reporting bridge → ReportVersion`

- 实施、真实联网回采、自动测试、人工黄金样本和 Git 分支收口分别验收；最后才只保留本地和远程 `main`。

## Session 中的初始实施计划

1. 删除 `src/analysis/adapters/` 全量同步实现、旧行情/财务/报告下载链和旧同步兼容接口。
2. 删除 `scripts/run_demo.py`、`scripts/smoke_online_sources.py` 等纯包装入口。
3. 保留 `src/analysis/acquisition/` 的注册表、适配器、快照、checkpoint、恢复和按需报告采集；把公告目录、事件筛选和文档解析迁移到这条链，禁止恢复成全量报告下载。
4. 删除旧 `structured/research.py`，只保留 `StructuredFactMaterializer + research_lite`。
5. 删除旧的直接 `/api/reports` 输入；只允许研究工作区冻结包进入 reporting bridge，再生成 `ReportVersion`。
6. 更新 README、CLI、前端和 OpenSpec，移除旧全量采集说明。
7. 用空数据根验证 baseline 可重新获取；相同公司和截止时间重复运行幂等；incremental 只取新增/变更；checkpoint 与 reconcile 可恢复；报告 snapshot 可复用且内容变化产生新版本；失败、空响应、部分成功、来源不可用分别记录。
8. 用贵州茅台 `600519` 完成真实 baseline、重复采集、incremental 和一次按需报告采集，再运行全量 Python 测试、前端构建、OpenSpec strict 和旧入口扫描。

## 从 session 盘点出的功能决策

已确认并已写入 `docs/project-cleanup-decisions.md` 的方向：完整知识产品和研究工作区纳入；知识同 ID 内容采用 `knowledge-content` 修订但保留严格发布门；治理接入主链；business-profile 只移植独有业务画像逻辑；旧 adapters、旧同步、旧历史读取和直接报告写入口删除；唯一物化/研究路径为 `StructuredFactMaterializer + research_lite`；报告主链为研究工作区到 reporting bridge；OpenSpec 统一到 `eight-step-production-pipeline-v1`。

以下知识分支冲突曾需用户决定，现已处理并保留历史记录：

1. 56 个方法按 `published/candidate` 状态整合，保留严格发布门。
2. ES01.Q10 的两条互补路径和 ES02.Q04 的两条互补路径均保留。
3. IFRS3 source ID 使用 canonical source，旧 ID 作为历史 alias。
4. `pilot_remaining.md` 移入 acceptance history 并标注 superseded；1/54、pilot review、54/54 时间线保留。
5. 旧 `research-cards` 迁移到版本绑定的薄适配层，旧运行时正文入口删除。

## 当前证据边界

### 2026-09-28 候选报告材料只读检查

已只读检查当前工作树中暂存的贵州茅台候选材料：`tmp_v4.txt` 为 8 章文本报告，包含评级、目标价、情景假设、37 条证据索引及计算边界；`v4-contact.png` 与 `v4-all-contact.png` 为对应的多页渲染联系图。文本与渲染均可读取，图表和附录页存在。该检查只证明材料可读和渲染完整，不替代来源逐条核验、独立复算或用户人工黄金验收；三份文件已在 v5 验收后归档到 `D:\估值模型-archives\fact-materialization-ultra\moutai-report-qa-20261003\`，不加入提交。

审计知识候选分支时发现：`codex/knowledge-base-v1` 的合并结果包含知识提交，但同时重新带回了旧 `src/analysis/adapters/`、`src/analysis/structured/research.py`、旧包装脚本和部分旧研究入口。因此它只能作为内容来源逐项迁移，不能直接作为最终 `main` 的整合基线；当前候选工作树的旧链删除结果必须优先保留。

进一步审计确认，knowledge 分支的 `structured/runtime.py` 回退了本轮 incremental 水位线和 reconcile 实现，`acquisition/runtime.py` 还通过 `AdapterManager` 重新连接旧 adapters；这两个文件不得从 knowledge 分支覆盖当前候选版本。可迁移范围限于知识服务、知识目录/正文、发布门和经逐项核对的财务计算链。

主工作树 `D:\估值模型` 的未跟踪 `.tmp-knowledge-diff.txt` 和 `openspec/changes/eight-step-knowledge-base-v1/` 已完整归档到 `D:\估值模型-archives\worktree-cleanup-20261003\main-untracked\`；它们未纳入当前唯一生产 OpenSpec，也不会因清理分支而丢失。

- 候选事实分支 `codex/fact-materialization-ultra` 已删除旧 adapters、旧全量采集链、旧 `structured/research.py`、旧包装脚本和直接报告写入路径，提交 `33b0800`。
- 贵州茅台真实结构化 baseline run 为 `structured-run-ed9bcd788fc8ac594c4c01a9`：62 个分区任务，50 success、9 no_data、3 failed；失败和空响应均保留来源证据，没有用 fixture 或缓存冒充成功。
- 该回采物化得到 548 facts、300 dimensional facts、8 events、21 sources；物化哈希为 `2bb49e8dc68d49b25d5dabdabf5ce11fae3e6281ff9865d3816c84aa446bacfb`。
- 相同截止时间重复 baseline 返回 `created=false`、`attempted_job_ids=[]`，说明计划幂等；由于仍有失败/空响应，incremental 安全门禁拒绝本次增量，不能把它写成增量成功。
- 全量 Python 测试已通过（1680 passed、15 skipped）；`compileall`、前端 build、OpenSpec strict 和 `git diff --check` 也已通过。`tests/structured` 覆盖连续财务增量、事件 overlap、失败 coverage 门禁和动态报告期刷新。真实 API 的长期增量窗口仍待用贵州茅台完成，不能用这些离线/Mock 测试替代联网验收。
- 2026-09-28 重新运行候选工作树的全量 pytest 得到 1764 passed、15 skipped、1 failed；唯一失败是 `tests/knowledge/release/test_full_release.py`，其 `candidate_content_identity` 和 54/54 coverage 均通过，但当前版本没有重新绑定的 Agent 样例与人工验收（`agent_samples=false`、`human_review=false`）。该失败是发布门真实拒绝，不应通过伪造记录或静默跳过解决；常规回归和完整发布门需在最终报告中分开列示。
- `reconcile` 的 acquisition 证据存在，但普通 structured run 尚未提供独立 reconcile mode；在实现或补充契约前，不能把 acquisition reconcile 测试当成 structured 链完整验收。
- 知识产品已按文件选择性迁入当前工作树；知识回归为 79 项通过，发布门新增 `candidate_content_identity` 哈希检查。全量 pytest 的唯一失败仍是当前候选缺少重新绑定的 Agent 样例和人工验收，不是代码回归失败；旧 acceptance 哈希不匹配时会被拒绝。
- 当前候选已完成一次显式 CLI 试读：构建 `cleanup-candidate-v1` 后，54/54 coverage 和 `ES02.Q08`（industrial/manufacturing 上下文）读取均成功；`default_published=false`，因此这次试读不等于默认包发布。
- 知识适配层已迁移到 `KnowledgeService`：旧 `cashflow-definition` 只映射到 `knowledge.working_capital`，研究目录保存 bundle/version/hash，未发布时明确返回 `no_default_release`；旧 research-cards 不再是运行时正文来源。
- business-profile 独有逻辑已选择性迁入 `business_evidence/profile.py` 与研究工作区 adapter，输出绑定当前 snapshot artifact，仍由 reporting bridge 生成 `ReportVersion`，没有新增独立报告入口。
- IFRS3 重复来源已 canonicalize，旧 source ID 仅保留 version-scoped historical alias；因此 `knowledge.es04_q05` 的 4 条来源引用与旧 source/case review identity 已失效，当前 candidate 必须重新绑定审阅，未以旧 acceptance 冒充通过。
- 2026-10-02 按需报告验收：只选 `cninfo:1225114741`（贵州茅台 2025 年报），新空 acquisition 根真实 HTTP 200，解析 143 页；同一目录重复执行后运行、attempt、正文 snapshot 和 derived artifact 数量不增加，复用同一 snapshot。详见 `docs/acquisition/moutai-on-demand-report-recapture-20261002.md`。
- 真实 incremental 补充验收：`structured-run-096a85739296a0de6f6cffc2` 在 `income_fields` 安全窗口完成 3 个成功分区、106 条记录；整批仍被失败/空响应安全门禁拒绝。针对性研究/知识/画像回归通过，完整发布门仍因缺少新 Agent 样例和人工验收保持阻塞。
- 用户已确认删除旧内部报告编辑方法（修改假设、重算、重分析、审阅）；历史报告读取和导出保留，后续修订回到研究工作区并经 reporting bridge 生成新 `ReportVersion`。
- 用户已确认删除治理模块独立的报告生成、模型运行器和工具会话编排；保留治理取证、事件重建和快照，后续绑定共享 acquisition manifest、研究工作区和 reporting bridge。
- 用户已确认删除历史治理模型及旧测试/文档；`CodexInputPack`、`CodexToolRead`、`CodexSessionManifest`、研究任务/结果包、快照采用、治理 findings/report 模型已从运行时代码和旧测试移除，退役治理 OpenSpec 目录已删除。保留的治理事实模型只服务取证、事件重建和快照。
- 用户已确认将 `eight-step-knowledge-base-v1` 移入 `openspec/retired/`，保留其历史 proposal/design/spec/tasks；活动实施入口只保留 `eight-step-production-pipeline-v1`。

## 交接约束

- `tmp_v4.txt`、`v4-contact.png`、`v4-all-contact.png` 已完成 v5 验收并归档到 `D:\估值模型-archives\fact-materialization-ultra\moutai-report-qa-20261003\`，不加入提交。
- 未完成用户决策前，不删除知识分支、worktree 或远程分支；分支收口是最后一步。
- 自动测试、真实 API、缓存重放和人工报告阅读分别报告，任何一种通过都不替代其他验收。
- 本轮有效截止日回采证据见 `docs/acquisition/moutai-valid-cutoff-recapture-20260928.md`：baseline 和 `income_fields` incremental 均为真实 HTTP 响应；同参数重复 baseline/incremental 返回既有 run 且不新增 attempts。早期未来 cutoff 和旧 partial 按需报告记录只作历史证据，已标为 superseded，不能用于当前通过结论。

本轮历史治理模型删除、治理接入说明、默认能力缺口回归和 OpenSpec 清单更新已提交到候选分支 `c7ad3a6`。贵州茅台 v4 材料、v5 成品和验收记录已归档；候选 worktree 的 `tmp/`、`var/`、`output/` 也已完整复制到 `D:\估值模型-archives\fact-materialization-ultra\runtime-evidence-20261003\`，清单记录文件数和字节数。

## 2026-10-02 本地 provenance 与九项空响应归因

按用户决定移除所有请求中的 `__retrieved_at`，同时停止把它投影成供应商字段、业务期间或内容版本的一部分。数据集元数据为 1.2.1，上游协议合同保持 1.2.0；旧解释合同哈希未变。

最终空根 `tmp/moutai-api-final-20261002` 的 baseline `structured-run-d58b1fad57728dae9cdb5fb3` 为 53 成功、9 空响应、0 失败；reconcile `structured-run-f2e745256fdc4ea6133d8a44` 真实复查后仍为 9 空响应。已校验 62 个快照哈希、1495 条记录的本地 provenance 与内容版本哈希；重复采集没有新增请求、快照或记录。物化为 548 事实、300 维度、8 事件，重复输出相同。

9 项茅台 API 空响应与同行样本和旧报告正文的区别已写入逐主题来源对照。当前整批 incremental 在联网前被这 9 项 coverage 阻止，已向用户提交是否允许已验证空查询推进增量的选择，尚未修改规则。全量 Python 1729 通过、14 跳过，唯一失败仍为知识发布 Agent/人工门；前端构建与 OpenSpec strict 通过。详见 `docs/acquisition/moutai-structured-api-acceptance-20261002.md`。

## 2026-10-02 九项空响应补齐延期

用户确认这些接口历史上也为空后，决定“现在先不管，作为后期的补充”。本轮茅台 9 项 API 缺口补齐移到后续，原响应、缺口和已有报告证据保留。已同步决策、盘点、验收说明及任务 11.8；代码规则未在本次讨论中修改。建议下一步依次完成其余适用数据集的真实增量、茅台新数据到报告的闭环、剩余知识/治理验收，再合并与清理分支。

## 2026-10-02 增量与恢复落实

延期已落实到公司默认范围，贵州茅台执行其余 22 项。用户最终确认完整空查询允许以后更新，不要求先有历史数据；未完成任务独立恢复，不影响正常项目，实际依赖缺失输入的任务才暂缓。

首次增量完成 3 个任务后退出进程，另一个进程恢复且不重跑已完成任务。旧规则产生的 10 个新空窗口通过正式 reconcile 追加完整性证据；随后两轮真实增量均为 18 有数据、10 完整为空、0 未完成，同参数重复不增加请求或记录。最终运行返回的 542 条观察均为已知业务内容，未出现新增披露。

已修复异常已存页被跳过、有效终页保存后错误请求下一页两个恢复漏洞。保存历史页版本，用新的完成证明确定可消费页；研究工作区跑完独立任务后汇总可用候选快照和未完成清单，部分成果保持 partial。

全量 Python 1745 通过、14 跳过、1 个既有知识发布门失败；OpenSpec strict 通过。证据见 [增量与恢复验收](acquisition/moutai-incremental-recovery-20261002.md)。真实治理 manifest、知识和报告人工验收仍待完成，未合并、推送或删除分支/worktree。

## 2026-10-03 v5 报告验收与发布门延期

贵州茅台 `贵州茅台-6.1sol-20261003-final` 已由 Codex 按用户授权完成代人工成品验收。8 页 PDF 逐页检查、八章结构、同行连接、31 条数字引用、19 个 Excel 工作表、提示词哈希和五种导出格式检查通过；验收记录为 `overall_decision=passed`。研究覆盖、股利核验、DCF、系统草稿状态和模型身份独立凭证按用户决定保留为 deferred。

真实治理 manifest 与知识 Agent/人工发布门属于合并前遗留验收缺口，按用户决定延期，不再阻断本轮代码合并。知识发布测试继续单独保留，默认代码回归门不再运行该真实发布门；延期证据见 `docs/knowledge/acceptance/release-deferred-20261003.json`。

## 2026-10-03 合并收口完成

- 候选 codex/fact-materialization-ultra 已快进合入 main，主线提交为 2d1c4b1；主 worktree 清洁。
- 主线最终检查：默认 Python 回归 1912 passed、14 skipped、0 failed；compileall、前端 npm run build、OpenSpec strict、旧入口运行时扫描和 git diff --check 均通过。知识发布门单独运行仍按预期因 agent_samples、human_review 缺失返回失败，已登记为 deferred，不计入默认回归。
- v4 验收材料、v5 报告、候选 tmp/、var/、output/ 及主/知识 worktree 未提交内容已保存到 D:\估值模型-archives\，并带有 SHA-256 或文件数/字节数清单。
- 清理完成后只保留 main 的本地和远程引用；知识 OpenSpec 草案保留在仓外归档，活动 OpenSpec 只有 eight-step-production-pipeline-v1。
