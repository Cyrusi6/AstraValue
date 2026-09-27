# 项目精简与结构化 API 迁移历史

更新时间：2026-09-28

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

仍需用户决定、不得自动合并的知识分支冲突：

1. 是否接受 56 个方法按 `published/candidate` 状态整合。
2. ES01.Q10 是否同时保留 `pricing_power` 和 `competitive_advantage` 两条互补路径。
3. ES02.Q04 是否同时保留 `roic` 和 `roe_dupont` 两条互补路径。
4. IFRS3 两个 source ID 是否合并为 canonical source。
5. 来源元数据多个版本保留哪一套定位字段。
6. `pilot_remaining.md` 删除、移入 history，还是原位保留。
7. 1/54、pilot review、54/54 验收记录如何标记时间线。

## 当前证据边界

审计知识候选分支时发现：`codex/knowledge-base-v1` 的合并结果包含知识提交，但同时重新带回了旧 `src/analysis/adapters/`、`src/analysis/structured/research.py`、旧包装脚本和部分旧研究入口。因此它只能作为内容来源逐项迁移，不能直接作为最终 `main` 的整合基线；当前候选工作树的旧链删除结果必须优先保留。

主工作树 `D:\估值模型` 另有未跟踪的 `openspec/changes/eight-step-knowledge-base-v1/`。在最终只保留 `main` 前必须先保存或明确归档，不能因分支清理而丢失。

- 候选事实分支 `codex/fact-materialization-ultra` 已删除旧 adapters、旧全量采集链、旧 `structured/research.py`、旧包装脚本和直接报告写入路径，提交 `33b0800`。
- 贵州茅台真实结构化 baseline run 为 `structured-run-ed9bcd788fc8ac594c4c01a9`：62 个分区任务，50 success、9 no_data、3 failed；失败和空响应均保留来源证据，没有用 fixture 或缓存冒充成功。
- 该回采物化得到 548 facts、300 dimensional facts、8 events、21 sources；物化哈希为 `2bb49e8dc68d49b25d5dabdabf5ce11fae3e6281ff9865d3816c84aa446bacfb`。
- 相同截止时间重复 baseline 返回 `created=false`、`attempted_job_ids=[]`，说明计划幂等；由于仍有失败/空响应，incremental 安全门禁拒绝本次增量，不能把它写成增量成功。
- 全量 Python 测试已通过（1680 passed、15 skipped）；`compileall`、前端 build、OpenSpec strict 和 `git diff --check` 也已通过。`tests/structured` 覆盖连续财务增量、事件 overlap、失败 coverage 门禁和动态报告期刷新。真实 API 的长期增量窗口仍待用贵州茅台完成，不能用这些离线/Mock 测试替代联网验收。
- `reconcile` 的 acquisition 证据存在，但普通 structured run 尚未提供独立 reconcile mode；在实现或补充契约前，不能把 acquisition reconcile 测试当成 structured 链完整验收。

## 交接约束

- `tmp_v4.txt`、`v4-contact.png`、`v4-all-contact.png` 是当前候选工作树未跟踪临时文件，未经确认不删除、不加入提交。
- 未完成用户决策前，不删除知识分支、worktree 或远程分支；分支收口是最后一步。
- 自动测试、真实 API、缓存重放和人工报告阅读分别报告，任何一种通过都不替代其他验收。
