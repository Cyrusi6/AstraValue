> 历史参考，已于2026-09-14退出活动队列；原任务状态保留，不作当前执行指令。当前任务见 [eight-step-production-pipeline-v1](../../changes/eight-step-production-pipeline-v1/tasks.md)。

## Why

AstraValue 当前第三步只有治理事件展示和方法 skeleton，尚不能回答“在历史时点，当时已经公开的信息支持怎样的治理与管理层画像”，也不能为项目大脑 Codex 提供可追溯的按需证据查询。现在需要建立独立的数据底座，使 Codex 能在严格无未来信息的前提下自主补采、判断并完成报告，同时把业务缺失与技术完整性失败明确分开。

本 change 落实 [治理与管理层数据采集底座实施方案](../../../治理与管理层数据采集底座实施方案.md)，不修改并行的 business-model-acquisition-v1；后者的通用采集控制面在本 change 中仅作为待实现的共享依赖合同，不能被视为当前已有能力。

## What Changes

- 新增治理采集 scope 及 11 个稳定问题 ID，明确上市以来、最近五个完整年度和完整事项生命周期等覆盖窗口。
- 在共享采集控制面上增加 acquisition_scope、question_set_id、question_set_version、query_pack_version 和 source_registry_version 身份，避免不同研究范围误用 checkpoint、manifest 或最新批次。
- 将正式披露及证监会、交易所监管资料设为治理 v1 权威来源；AKShare 和搜索摘要只作发现，法院、工商和中登来源延后。
- 新增原始证据到 GovernanceClaim、类型化治理记录和 GovernanceSnapshot 的不可变血缘，并区分抽取、校验、复核三个状态轴。
- 只有“正式来源、确定性抽取、完整校验”的记录可以自动进入权威画像；全部 LLM 抽取先进入候选层，可选人工数据维护不是报告生成门禁。
- 新增公司内权威人员 ID、别名、任期和显式跨公司 PersonLinkDecision；禁止按姓名自动跨公司合并。
- 新增 state_at、known_at 和 perspective 双时点查询；默认严格模式无未来信息，事后重建必须显式请求。
- 用完整披露作为锚点、公告作为增量重建状态；无锚点、断档、数字不足和冲突返回可查询的不完整状态，不伪造负面事实。
- 新增不可变治理快照、只读画像/事件/coverage/conflict/candidate/lineage/evidence API，并兼容现有 EventRecord 与旧查询。
- 新增紧凑 Codex 输入包、按需证据工具和 CodexSessionManifest，记录实际读取的对象、版本、hash 和引用，不保存隐藏推理。
- 新增受控补采与 fresh-context Codex 子任务；严格模式先做来源和 available_at 校验，未来内容隔离后才允许回流父 Codex。
- 新增 Codex 自主治理报告和八份人类可读 trace；业务缺失、冲突和预算耗尽不阻断报告，hash、血缘、未来泄漏、非法时间和输出 schema 错误才是硬失败。
- 新增 append-only SQLite 迁移、兼容视图、feature flag 和确定性投影；旧快照、旧报告及旧事件不可变。

### Scope

本 change 覆盖正式证据采集后的治理抽取、实体、历史状态、查询、Codex 取证/补采、报告输入输出与审计 trace。治理方法论仍由 [第三步方法文件](../../../docs/methodology/steps/03_governance.md) 管理；本 change 只提供事实与证据底座。

### Non-goals

- 不生成治理总分、治理评级、管理层诚信或能力的权威字段。
- 不把 Codex 判断写回权威事实层。
- 不以 AKShare、新闻、搜索摘要或同源镜像替代正式原文。
- 不接入法院、工商、中登，也不从这些来源缺失推断结论。
- 不按姓名自动合并跨公司人员。
- 不从 no_data、访问失败、未披露或不完整状态推导“没有风险”。
- 不补完治理方法 skeleton，不修改并行的 business-model-acquisition-v1。
- 不把原始公告、数据库、凭据、浏览器资料、生成报告或真实运行证据提交到 Git。

## Capabilities

### New Capabilities

- governance-acquisition-scope：治理问题集、历史覆盖、来源角色、scope 身份、coverage 和共享采集控制面接入。
- governance-evidence-facts：字段级证据、确定性/LLM 抽取隔离、三轴状态、类型化治理事实和版本修订。
- governance-person-identity：公司内人员权威身份、多人多职务任期及显式跨公司链接决定。
- governance-bitemporal-snapshots：state_at/known_at 双时点、严格与事后模式、锚点加增量、缺口冲突及不可变快照。
- governance-evidence-api：治理快照、类型化状态、事件、coverage、候选、冲突、血缘和证据片段的稳定接口。
- codex-governance-workflow：紧凑输入、按需工具、实际读取清单、受控补采、子 Codex 未来信息隔离、自主报告与可读 trace。

### Modified Capabilities

无。当前 openspec/specs 尚无已归档主规格；并行 change 中的通用 acquisition capability 不是本 change 可修改的既有主规格。本 change 通过显式依赖和兼容合同接入，后续合并时不得复制通用 kernel。

## Impact

### Affected layers

- 配置：治理 question set、query pack、来源角色和报告/工具 schema。
- 数据模型：抽取运行、claim、人员与链接、治理类型化记录、gap/conflict、快照、Codex session 和 research task/result。
- 存储：SQLite append-only schema 迁移、content-addressed 证据引用和可选派生读模型。
- 服务：治理抽取、实体解析、双时点重建、快照、受控补采、Codex 工具与 trace renderer。
- API/CLI：共享 acquisition scope 参数以及治理只读查询和显式快照接口。
- 报告/导出：第三步 Codex 自主报告、引用、完整性状态及 Markdown/HTML/Excel/PDF 同源投影。
- 测试：冻结夹具、在线样本和人工黄金样本三个独立验收门。

### Migration and compatibility

- 新表和新记录采用 append-only 迁移，不原地重写既有 EventRecord、DocumentRecord、ReportVersion 或历史 snapshot。
- 现有事件与 lineage API 保持兼容；治理类型化接口为增量扩展。
- 新旧投影并存期间，关闭 governance_management_v1 feature flag 可回退到当前事件级第三步展示。
- 补采或更正只能产生新 manifest、snapshot 和 report；旧 ID 与 hash 不漂移。

### Acceptance boundaries

- 自动化确定性门必须通过，包括双时点、来源独立性、抽取准入、人员身份、锚点增量、未来信息隔离、不可变性和 trace 一致性。
- 真实联网门单独记录 passed、pending 或 failed，只证明执行时指定样本和来源，不外推到全 A 股或全历史。
- 开发期人工黄金门必须验证字段证据和可读投影，但不成为每次 Codex 报告的运行时审批。
- 业务 incomplete、conflicted、no_data、来源受限或补采预算耗尽是软状态；技术 hash、血缘、时间、命名空间或 schema 违规是硬失败。

### Risks

- Point-in-time：迟披露、更正和可变网页可能造成未来信息泄漏，必须以可证明 available_at 和严格子任务回流门控制。
- Source independence：同一上游材料的多个镜像必须共享 independence_group，不能伪装双源。
- Licensing：来源注册必须保存访问与归档政策，不绕过登录、验证码、付费墙或 robots。
- Revision：同 URL 新内容、新更正和乱序公告只追加版本，不重写历史。
- Missing data：coverage 是执行属性，不是治理事实；缺失必须显式传给 Codex。
- Parallel dependency：共享 acquisition kernel 尚可能由另一分支实现，合并时需优先复用其最终合同并解决接口差异，不能复制控制面。
